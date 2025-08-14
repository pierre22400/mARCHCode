# agents/agent_code_writer.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from textwrap import indent
import hashlib
import re

from core.types import PatchBlock, MetaBlock, now_iso
from core.context_loader import load_context_snapshot  # injection du contexte global

"""
agent_code_writer (ACW) — mARCHCode / Phase 3 (MVP aligned ACWP)
================================================================

Bannière pédagogique
--------------------
But du module
    Transformer une `writer_task` (préparée par ACWP) en `PatchBlock` balisé
    (`#{begin_meta: ...}` ... `#{end_meta}`) prêt pour vérifications locales et
    modulaires, de façon déterministe et idempotente.

Points-clés (sécurité douce & idempotence)
    - Marqueurs optionnels : `writer_task.markers.begin/end` (auto-générés si absents).
    - `content_hash` (SHA-256 tronqué) pour stabiliser les diffs et guider l’adaptateur FS.
    - Pas d’effets de bord ni d’appels réseau.

Contrats & limites (MVP)
    - ACW ne fixe NI `pb.global_status` NI `pb.next_action` (ModuleChecker décide).
    - Le contexte global (si dispo) peut être injecté dans `writer_task` : `archcode_context` / `archcode_context_text`.
    - Le rôle opérationnel est porté par `writer_task.role`. Si ACWP ne l’a pas fourni,
      ACW tente une inférence prudente (ex. heuristique DTO).

Glossaire — Enluminure (DTO)
    - DTO = « cartouche d’enluminure » (artefact neutre de données, pas un agent).
    - Version initiale exécutable : peut retourner `{}` pour ne pas casser le pipeline.
    - Évolution : structure & validations ajoutées itérativement sans bloquer la chaîne.
"""

_BEGIN = "#" + "{begin_meta:"
_END = "#{end_meta}"


# -------------------- utilitaires internes --------------------

def _infer_module(file_path: str) -> str:
    """Retourne le « module » déduit du chemin (premier segment avant '/'), sinon 'module'."""
    return file_path.split("/")[0] if "/" in file_path else "module"


def _normalize_signature(sig: str) -> str:
    """Normalise une signature en s’assurant d’avoir `def ...:` ; sinon commente la ligne fournie."""
    s = (sig or "").strip()
    if s.startswith("def ") and s.endswith(":"):
        return s
    if s.startswith("def ") and not s.endswith(":"):
        return s + ":"
    return f"# Signature non exécutable fournie par le plan: {s}"


def _choose_docstring_style(constraints: Dict[str, Any]) -> str:
    """Choisit un style de docstring parmi 'google' (défaut), 'numpy' ou 'rst'."""
    style = str(constraints.get("docstring", "") or "").lower()
    if style in ("google", "numpy", "rst"):
        return style
    return "google"  # défaut lisible


def _render_docstring(role: str, acceptance: List[str], style: str, path: Optional[str], constraints: Optional[Dict[str, Any]] = None) -> str:
    """
    Construit la docstring de fonction selon le style choisi, le rôle et la checklist d’acceptation.
    Ajoute une explication spécifique quand role == 'dto' (cartouche d’enluminure, artefact neutre).
    """
    checklist = "\n".join([f"- {a}" for a in acceptance]) if acceptance else "- (aucun)"
    constraints_snippet = ""
    if constraints:
        # extrait court et stable (évite d’inonder la docstring)
        keys = [k for k in ("typing", "imports_order", "docstring") if k in constraints]
        if keys:
            kv = ", ".join(f"{k}={constraints[k]}" for k in keys)
            constraints_snippet = f"\nContraintes (extraits): {kv}"
    path_line = f"\nRoute/API: {path}" if path else ""
    base_header = f'Rôle: {role or "unknown"}\nAcceptance (rappel):\n{checklist}{constraints_snippet}{path_line}'
    note = "\nNOTE: Implémentation minimale générée automatiquement.\n      Compléter la logique lors des itérations suivantes."

    # Bloc explicatif spécifique DTO (enluminure)
    extra = ""
    if (role or "").lower() == "dto":
        extra = (
            "\n\nEnluminure (DTO)\n"
            "-----------------\n"
            "- Artefact neutre (« cartouche d’enluminure ») pour transporter des données entre agents.\n"
            "- Initialisation MVP : peut retourner {} pour rester exécutable et ne rien casser.\n"
            "- Évolution : la structure et les validations seront ajoutées lors d’itérations ultérieures."
        )

    body = f"{base_header}{note}{extra}"

    if style == "numpy":
        return f'"""{role or "unknown"}\n\nNotes\n-----\n{body}\n"""'
    if style == "rst":
        indented_body = body.replace("\n", "\n   ")
        return f'"""{role or "unknown"}\n\n.. note::\n   {indented_body}\n"""'
    # google (défaut)
    return f'"""mARCHCode/ACW\n{body}\n"""'


def _body_from_role(role: str, acceptance: List[str], constraints: Dict[str, Any], path: Optional[str]) -> str:
    """Retourne un corps de fonction minimal cohérent avec le rôle (DTO vs autres → NotImplementedError)."""
    style = _choose_docstring_style(constraints)
    doc = _render_docstring((role or "").lower(), acceptance, style, path, constraints)
    role_low = (role or "").lower()
    if role_low == "dto":
        # MVP: DTO simple → dict ; on évite la dataclass ici.
        return f"{doc}\n    # TODO: compléter la structure DTO (cartouche d’enluminure neutre)\n    return {{}}"
    # Autres rôles : on force un NotImplementedError (exécutable et clair)
    return f"{doc}\n    # TODO: implémenter la logique métier\n    raise NotImplementedError('À implémenter par itération suivante')"


def _prelude_from_constraints(constraints: Dict[str, Any]) -> List[str]:
    """Génère des imports/astuces préliminaires sûrs (sans side-effects) selon les contraintes."""
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
    """Vérifie la présence des champs requis et que 'file' cible bien un .py."""
    required = ["plan_line_id", "file", "role", "signature"]
    for k in required:
        if not task.get(k):
            raise ValueError(f"writer_task invalide: champ obligatoire manquant '{k}'")
    if not str(task.get("file")).endswith(".py"):
        raise ValueError("writer_task invalide: 'file' doit cibler un .py")


def _hash_payload(s: str) -> str:
    """Retourne un SHA-256 tronqué (12 hex) pour la charge utile Python."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _render_meta_inline(meta: Dict[str, Any]) -> str:
    """Rend le dict `meta` en ligne juste après #{begin_meta: ...} avec tri des clés (diff stable)."""
    items = []
    for k in sorted(meta.keys()):
        v = meta[k]
        if isinstance(v, bool):
            items.append(f"{k}: {str(v).lower()}")
        else:
            # représentation simple, évite les nouvelles lignes dans le inline meta
            items.append(f"{k}: {v}")
    return f"{_BEGIN} {{ " + ", ".join(items) + " }}"


# ------------------- inférence de rôle (robuste PlanLine) -------------------

def _get_signature_from_pl(pl: Any) -> str:
    """Extrait une signature depuis un objet PlanLine tolérant : `signature` puis `function_signature` si disponible."""
    sig = getattr(pl, "signature", None) or getattr(pl, "function_signature", None) or ""
    return str(sig or "")


def _looks_like_dict_return(signature: str) -> bool:
    """Heuristique simple : détecte un retour `dict`/`Dict[...]` dans une annotation de type Python."""
    s = signature.replace(" ", "").lower()
    return "->dict" in s or "->typing.dict" in s or "->dict[" in s or "->dict[" in s


def _infer_role_from_pl(pl: Any) -> str:
    """
    Infère un rôle minimal et sûr à partir des champs existants d'une PlanLine.
    - 'dto' si la signature indique un `dict` (ou si outputs/output_constraints suggèrent 'data-only').
    - sinon 'function' (génération squelette + NotImplementedError).
    """
    # 1) signature
    sig = _get_signature_from_pl(pl)
    if _looks_like_dict_return(sig):
        return "dto"

    # 2) outputs / output_constraints
    outputs = getattr(pl, "outputs", None) or []
    try:
        for out in outputs:
            t = str((out or {}).get("type", "")).lower()
            if "dict" in t or "mapping" in t:
                return "dto"
    except Exception:
        pass

    out_constraints = getattr(pl, "output_constraints", None) or []
    try:
        for oc in out_constraints:
            if isinstance(oc, str) and ("dict" in oc.lower() or "data-only" in oc.lower()):
                return "dto"
    except Exception:
        pass

    # 3) objective_label / implementation_hint
    for key in ("objective_label", "implementation_hint"):
        val = str(getattr(pl, key, "") or "").lower()
        if any(kw in val for kw in ("dto", "conteneur de données", "data transfer object", "structure de données")):
            return "dto"

    # défaut conservateur
    return "function"


# ------------------- génération du bloc de code -------------------

def _generate_code_block(task: Dict[str, Any]) -> tuple[str, str, str, Dict[str, str]]:
    """
    Construit le bloc de code final.

    Returns:
        code_block: texte complet avec #{begin_meta}/#{end_meta}
        payload_hash: hash de la charge utile Python (hors balises/markers)
        fs_intent: 'markers' si begin/end disponibles (fournis ou auto), sinon 'fullfile'
        markers_used: dict {'begin':..., 'end':..., 'auto_generated': 'true'|absent}
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
    meta_inline: Dict[str, Any] = {
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
    markers = dict(task.get("markers") or {})
    m_begin = _sanitize_marker(markers.get("begin") or "")
    m_end = _sanitize_marker(markers.get("end") or "")

    markers_used: Dict[str, str] = {}

    # Si aucun marqueur fourni, auto-génération possible (safe par défaut)
    auto_generate = bool(task.get("markers_auto", True))
    if not m_begin or not m_end:
        if auto_generate:
            m_begin, m_end = _default_markers(plan_line_id, file)
            markers_used["auto_generated"] = "true"
        else:
            m_begin = ""
            m_end = ""

    if m_begin and m_end:
        fs_intent = "markers"
        markers_used["begin"] = m_begin
        markers_used["end"] = m_end
    else:
        fs_intent = "fullfile"
        markers_used = {}

    # Ajoute informations markers dans la meta inline (utile pour adaptateurs/checkers)
    if markers_used:
        meta_inline["markers_auto"] = markers_used.get("auto_generated") == "true"
        if "begin" in markers_used:
            meta_inline["marker_begin"] = markers_used["begin"]
            meta_inline["marker_end"] = markers_used["end"]

    # Bloc final
    lines: List[str] = []
    lines.append(_render_meta_inline(meta_inline))
    if fs_intent == "markers":
        lines.append(m_begin)
        lines.append(payload_py)
        lines.append(m_end)
    else:
        lines.append(payload_py)
    lines.append(_END)
    return "\n".join(lines), payload_hash, fs_intent, markers_used


# --------------------------- API publique ---------------------------

def write_code(writer_task: Dict[str, Any]) -> PatchBlock:
    """
    Transforme une writer_task (ACWP) en PatchBlock prêt pour la vérification.

    Notes:
        - Ne fixe NI `global_status` NI `next_action` (réservés à ModuleChecker).
        - Idempotence: meta.inline contient `content_hash`; history trace `fs_intent` et `payload_hash`.
        - Marqueurs (fournis ou auto) exposés dans la meta inline pour l’adaptateur FS.
    """
    if not isinstance(writer_task, dict):
        raise TypeError("writer_task doit être un dict (fourni par ACWP).")
    _validate_writer_task(writer_task)

    # Génère le code balisé (+ hash & intent)
    code_block, payload_hash, fs_intent, markers_used = _generate_code_block(writer_task)

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

    # trace context presence (dict and/or text)
    if writer_task.get("archcode_context"):
        pb.append_history("archcode_context: present")
    if writer_task.get("archcode_context_text"):
        pb.append_history("archcode_context_text: present")

    # trace markers usage
    if markers_used:
        if markers_used.get("auto_generated"):
            pb.append_history("markers:auto_generated=true")
        if markers_used.get("begin"):
            pb.append_history(f"marker_begin={markers_used.get('begin')}")
            pb.append_history(f"marker_end={markers_used.get('end')}")

    if writer_task.get("writer_prompt"):
        pb.append_history("writer_prompt: present")
    if writer_task.get("writer_prompt_yaml"):
        pb.append_history("writer_prompt_yaml: present")

    return pb


def run_acw(pl, writer_prompt: str) -> PatchBlock:
    """
    ACW runner (MVP) — transforme une PlanLine + prompt ACWP en writer_task, puis en PatchBlock.

    Champs minimaux requis par _validate_writer_task:
        - plan_line_id, file, role, signature

    Remarques:
        - Si `pl` ne porte pas explicitement de `role`, ACW infère un rôle conservateur
          (ex. `dto` si retour dict détecté, sinon `function`) pour produire une tâche valide.
        - Tous les autres champs sont passés tels quels si présents (tolérance MVP).
    """
    # Tentative d'injection du contexte global (best-effort)
    try:
        arch_context = load_context_snapshot()
    except Exception:
        arch_context = {}

    # Version textuelle compacte du contexte (best-effort, import local optionnel)
    try:
        from core.context_formatter import normalize_context_for_prompt  # type: ignore
        arch_context_text = normalize_context_for_prompt(arch_context)
    except Exception:
        arch_context_text = ""

    # Inférence prudente du rôle si absent
    pl_role = getattr(pl, "role", None) or _infer_role_from_pl(pl)

    # Signature robuste (signature ou function_signature)
    pl_signature = _get_signature_from_pl(pl)

    # Sécurisation minimale des champs obligatoires
    writer_task: Dict[str, Any] = {
        "task_id": f"TASK-{pl.plan_line_id}",
        "plan_line_id": pl.plan_line_id,
        "file": pl.file,
        "role": pl_role,
        "op": getattr(pl, "op", None),
        "target_symbol": getattr(pl, "target_symbol", None),
        "signature": pl_signature,
        "acceptance": list(getattr(pl, "acceptance", []) or []),
        "constraints": dict(getattr(pl, "constraints", {}) or {}),
        "allow_create": bool(getattr(pl, "allow_create", True)),
        # Contexte & traçabilité (best-effort MVP)
        "writer_prompt": writer_prompt,
        "writer_prompt_yaml": writer_prompt,
        "execution_context": {},
        "bus_message_id": "BUS-UNKNOWN",
        "archcode_context": arch_context,                 # dict raw
        "archcode_context_text": arch_context_text,       # texte synthétisé (peut être "")
        "writer_prompt_with_context": (writer_prompt + "\n\nCONTEXT:\n" + arch_context_text) if arch_context_text else writer_prompt,
        # Par défaut on génère des marqueurs idempotents sauf si explicitement désactivé
        "markers_auto": True,
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
