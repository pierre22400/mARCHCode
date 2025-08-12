# agents/agent_code_writer.py
from __future__ import annotations

"""
agent_code_writer (ACW) — mARCHCode / Phase 3 (MVP aligné ACWP)
================================================================
Rôle
----
Consommer une `writer_task` produite par ACWP et émettre un `PatchBlock`
balisé (`#{begin_meta: ...}` ... `#{end_meta}`) prêt pour les checkers.

Nouveautés (sécurité douce & idempotence orientée FS)
-----------------------------------------------------
- Support des marqueurs optionnels (writer_task.markers.begin/end).
- Hints d’idempotence pour l’adaptateur FS :
  • insertion des lignes de marqueurs autour du code si fournis
  • calcul d’un `payload_hash` (SHA-256 court) inséré dans la meta inline
  • traces dans `history` : fs_intent=markers|fullfile + payload_hash=...

L’adaptateur FS décidera au moment d’écrire :
  - insert/replace entre marqueurs si le bloc existe et diffère
  - skip si le bloc existe déjà et que les contenus sont identiques
  - full file write si aucun marqueur n’est fourni

Entrée (writer_task : Dict) — champs clés (ACWP)
------------------------------------------------
- task_id: str
- plan_line_id: str
- file: str (.py)
- role: str
- op: "create" | "modify"
- target_symbol: str
- signature: str
- path: Optional[str]
- allow_create: bool
- markers: Dict[str,str]   # {"begin": "...", "end": "..."}  (optionnel)
- depends_on: List[str]
- acceptance: List[str]
- constraints: Dict[str, Any]
- plan_line_ref: Optional[str]
- intent_fingerprint: Optional[str]
- writer_prompt: str
- writer_prompt_yaml: str
- execution_context: Dict[str,Any]
- bus_message_id: Optional[str]

Sortie
------
PatchBlock(code=..., meta=MetaBlock(...))
- ACW ne fixe PAS global_status / next_action (ModuleChecker s’en charge).
"""

from typing import Any, Dict, List, Optional
from textwrap import indent
import hashlib

from core.types import PatchBlock, MetaBlock, now_iso

_BEGIN = "#"+"{begin_meta:"
_END   = "#{end_meta}"


# -------------------- utilitaires internes --------------------

def _infer_module(file_path: str) -> str:
    return file_path.split("/")[0] if "/" in file_path else "module"

def _normalize_signature(sig: str) -> str:
    """
    S’assure d’une ligne 'def ...:' sans erreur syntaxique.
    Si ce n’est pas un def valide, on le commente.
    """
    s = (sig or "").strip()
    if s.startswith("def ") and s.endswith(":"):
        return s
    if s.startswith("def ") and not s.endswith(":"):
        return s + ":"
    return f"# Signature non exécutable fournie par le plan: {s}"

def _choose_docstring_style(constraints: Dict[str, Any]) -> str:
    style = str(constraints.get("docstring", "") or "").lower()
    if style in ("google", "numpy", "rst"):
        return style
    return "google"  # défaut lisible

def _render_docstring(role: str, acceptance: List[str], style: str, path: Optional[str]) -> str:
    checklist = "\n".join([f"- {a}" for a in acceptance]) if acceptance else "- (aucun)"
    base_header = f'Rôle: {role or "unknown"}\nAcceptance (rappel):\n{checklist}'
    path_line = f"\nRoute/API: {path}" if path else ""
    note = "\nNOTE: Implémentation minimale générée automatiquement.\n      Compléter la logique lors des itérations suivantes."
    body = f"{base_header}{path_line}{note}"
    if style == "numpy":
        return f'"""{role or "unknown"}\n\nNotes\n-----\n{body}\n"""'
    if style == "rst":
        indented_body = body.replace("\n", "\n   ")
        return f'"""{role or "unknown"}\n\n.. note::\n   {indented_body}\n"""'
    # google (défaut)
    return f'"""mARCHCode/ACW\n{body}\n"""'

def _body_from_role(role: str, acceptance: List[str], constraints: Dict[str, Any], path: Optional[str]) -> str:
    style = _choose_docstring_style(constraints)
    doc = _render_docstring((role or "").lower(), acceptance, style, path)
    role_low = (role or "").lower()
    if role_low == "dto":
        # MVP: DTO simple → dict ; on évite la dataclass ici.
        return f"{doc}\n    # TODO: compléter la structure DTO\n    return {{}}"
    # Autres rôles : on force un NotImplementedError (exécutable et clair)
    return f"{doc}\n    # TODO: implémenter la logique métier\n    raise NotImplementedError('À implémenter par itération suivante')"

def _prelude_from_constraints(constraints: Dict[str, Any]) -> List[str]:
    """
    Génère des imports/astuces préliminaires sûrs (sans side-effects) selon constraints.
    """
    prelude: List[str] = []
    typing_mode = str(constraints.get("typing", "")).lower()
    if typing_mode in ("strict", "on"):
        prelude.append("from __future__ import annotations")
    # Hints lisibles (sans impact) — la mise en forme réelle sera gérée par CI/pre-commit
    imports_order = str(constraints.get("imports_order", "")).lower()
    if imports_order == "isort":
        prelude.append("# isort: on")
    return prelude

def _validate_writer_task(task: Dict[str, Any]) -> None:
    required = ["plan_line_id", "file", "role", "signature"]
    for k in required:
        if not task.get(k):
            raise ValueError(f"writer_task invalide: champ obligatoire manquant '{k}'")
    if not str(task.get("file")).endswith(".py"):
        raise ValueError("writer_task invalide: 'file' doit cibler un .py")

def _hash_payload(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

def _render_meta_inline(meta: Dict[str, Any]) -> str:
    """
    Rend le dict meta *en ligne* après begin_meta pour les checkers.
    Tri des clés pour stabilité du diff.
    """
    items = []
    for k in sorted(meta.keys()):
        v = meta[k]
        if isinstance(v, bool):
            items.append(f"{k}: {str(v).lower()}")
        else:
            items.append(f"{k}: {v}")
    return f"{_BEGIN} {{ " + ", ".join(items) + " }}"


# ------------------- génération du bloc de code -------------------

def _generate_code_block(task: Dict[str, Any]) -> tuple[str, str, str]:
    """
    Retourne (code_block, payload_hash, fs_intent)
    - code_block : texte final avec #{begin_meta}/#{end_meta}
    - payload_hash : hash des lignes de code utiles (sans les balises & marqueurs)
    - fs_intent : 'markers' si markers fournis, sinon 'fullfile'
    """
    file = task.get("file") or "unknown.py"
    role = task.get("role") or "unknown"
    plan_line_id = task.get("plan_line_id") or "UNKNOWN"
    signature = _normalize_signature(task.get("signature") or "")
    acceptance: List[str] = list(task.get("acceptance") or [])
    constraints: Dict[str, Any] = dict(task.get("constraints") or {})
    path: Optional[str] = task.get("path")
    module = _infer_module(file)

    # Préambule (typing strict, hints d’outillage)
    prelude_lines = _prelude_from_constraints(constraints)

    # Corps de fonction conforme rôle/acceptance/constraints
    body = _body_from_role(role, acceptance, constraints, path)

    # Assemble la charge utile Python (sans balises, sans marqueurs)
    func_lines: List[str] = []
    func_lines.append(signature if signature.startswith("def ") else "# " + signature)
    if signature.startswith("def "):
        func_lines.append(indent(body, "    "))

    payload_py = "\n".join([*prelude_lines, "" if prelude_lines else "", *func_lines]).strip("\n")
    payload_hash = _hash_payload(payload_py)

    # Métadonnées pour les balises (inclut bus_message_id si fourni via ACWP)
    meta_inline = {
        "file": file,
        "module": module,
        "role": role,
        "plan_line_id": plan_line_id,
        "timestamp": now_iso(),
        "status_agent_file_checker": "pending",
        "status_agent_module_checker": "pending",
        "content_hash": payload_hash,  # aide l’idempotence côté FS
    }
    bus_msg = task.get("bus_message_id")
    if bus_msg:
        meta_inline["bus_message_id"] = bus_msg

    # Marqueurs optionnels (writer_task.markers.begin/end)
    markers = task.get("markers") or {}
    m_begin = markers.get("begin")
    m_end = markers.get("end")
    fs_intent = "markers" if (m_begin and m_end) else "fullfile"

    # Bloc final
    lines: List[str] = []
    lines.append(_render_meta_inline(meta_inline))
    if fs_intent == "markers":
        lines.append(str(m_begin))
        lines.append(payload_py)
        lines.append(str(m_end))
    else:
        lines.append(payload_py)
    lines.append(_END)
    return "\n".join(lines), payload_hash, fs_intent


# --------------------------- API publique ---------------------------

def write_code(writer_task: Dict[str, Any]) -> PatchBlock:
    """
    Transforme une writer_task (ACWP) en PatchBlock prêt pour la vérification.
    Ne fixe NI global_status NI next_action (réservés à ModuleChecker).

    Hints d’idempotence pour l’adaptateur FS :
      - meta inline contient `content_hash`
      - history contient `fs_intent=...` et `payload_hash=...`
    """
    if not isinstance(writer_task, dict):
        raise TypeError("writer_task doit être un dict (fourni par ACWP).")
    _validate_writer_task(writer_task)

    # Génère le code balisé (+ hash & intent)
    code_block, payload_hash, fs_intent = _generate_code_block(writer_task)

    # Construit MetaBlock — aligné ACWP
    meta = MetaBlock(
        file=writer_task.get("file"),
        module=_infer_module(writer_task.get("file") or "unknown.py"),
        role=writer_task.get("role"),
        plan_line_id=writer_task.get("plan_line_id"),
        timestamp=now_iso(),
        status_agent_file_checker="pending",
        status_agent_module_checker="pending",
        bus_message_id=writer_task.get("bus_message_id"),
    )

    # PatchBlock sans décision globale (fixée par ModuleChecker plus tard)
    pb = PatchBlock(
        code=code_block,
        meta=meta,
        global_status=None,
        next_action=None,
        source_agent="agent_code_writer",
    )

    # Historique lisible (et hints FS)
    if writer_task.get("task_id"):
        pb.append_history(f"ACW: from task_id={writer_task['task_id']}")
    if writer_task.get("intent_fingerprint"):
        pb.append_history(f"intent_fp={writer_task['intent_fingerprint']}")
    pb.append_history(f"fs_intent={fs_intent}")
    pb.append_history(f"payload_hash={payload_hash}")
    if writer_task.get("writer_prompt"):
        pb.append_history("writer_prompt: present")
    if writer_task.get("writer_prompt_yaml"):
        pb.append_history("writer_prompt_yaml: present")

    return pb

def run_acw(pl, writer_prompt: str) -> PatchBlock:
    """
    mARCHCode — ACW runner (MVP)
    Transforme une PlanLine (objet) + un prompt ACWP en writer_task (dict)
    conforme à write_code(), puis retourne le PatchBlock généré.

    Champs minimaux requis par _validate_writer_task:
      - plan_line_id, file, role, signature

    Les autres champs sont passés tels quels si présents (tolérance MVP).
    """
    # Sécurisation minimale des champs obligatoires
    writer_task: Dict[str, Any] = {
        "task_id": f"TASK-{pl.plan_line_id}",
        "plan_line_id": pl.plan_line_id,
        "file": pl.file,
        "role": pl.role,
        "op": pl.op,
        "target_symbol": pl.target_symbol,
        "signature": pl.signature,
        "acceptance": list(pl.acceptance or []),
        "constraints": dict(pl.constraints or {}),
        "allow_create": bool(getattr(pl, "allow_create", True)),
        # Contexte & traçabilité (best-effort MVP)
        "writer_prompt": writer_prompt,
        "writer_prompt_yaml": writer_prompt,
        "execution_context": {},
        "bus_message_id": "BUS-UNKNOWN",
    }

    # Champs optionnels si présents sur la PlanLine
    if getattr(pl, "markers", None):
        writer_task["markers"] = dict(pl.markers or {})
    if getattr(pl, "path", None):
        writer_task["path"] = pl.path
    if getattr(pl, "depends_on", None):
        writer_task["depends_on"] = list(pl.depends_on or [])
    if getattr(pl, "plan_line_ref", None):
        writer_task["plan_line_ref"] = pl.plan_line_ref
    if getattr(pl, "intent_fingerprint", None):
        writer_task["intent_fingerprint"] = pl.intent_fingerprint

    return write_code(writer_task)
