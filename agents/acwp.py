# agents/ACWP.py
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
import hashlib
import uuid

from core.types import PlanLine


"""
agent_code_writer_planner (ACWP) — mARCHCode / Phase 3
======================================================

Rôle du module
--------------
Transformer des PlanLine "atomiques" en tâches prêtes pour ACW.
Chaque tâche (writer_task) contient :
  - Métadonnées essentielles (plan_line_id, file, role, signature, etc.)
  - Un prompt texte compact (writer_prompt)
  - Un prompt YAML déterministe (writer_prompt_yaml) pour rétro-compatibilité

Entrées / Sorties
-----------------
Entrées :
  - PlanLine (définie dans core/types.py)
  - (Option) execution_context : dict global léger (branché tel quel)
  - (Option) bus/user story pour enrichir le YAML (compat V1)
Sorties :
  - build_writer_task(pl, ...) → Dict[str, Any]
  - plan_to_writer_tasks([...], ...) → List[Dict[str, Any]]
  - build_prompt(pl, ...) → str (YAML seul, compat héritée)

Contrats respectés
------------------
- Aucune génération de code ici.
- ACW consomme writer_task → produit un PatchBlock.
"""


# ------------------------------ Utils ------------------------------

def _infer_module(file_path: str) -> str:
    """Déduit un nom de module simple à partir du chemin (ex: 'user/controller.py' -> 'user')."""
    return file_path.split("/")[0] if "/" in file_path else "module"


def _format_constraints(constraints: Dict[str, Any] | None) -> List[str]:
    """Transforme `constraints` en lignes YAML de la forme `'  - key: value'`."""
    if not constraints:
        return []
    out: List[str] = []
    for k, v in constraints.items():
        out.append(f"  - {k}: {v}")
    return out


def _indent_block(text: str, indent: int = 2) -> str:
    """Indente chaque ligne de `text` avec `indent` espaces (préserve les lignes vides)."""
    pad = " " * indent
    return "\n".join(pad + line if line else pad for line in text.splitlines())


def _digest_intent(plan_line: PlanLine) -> str:
    """Calcule une empreinte stable (SHA-256 tronqué) de l'intention pour idempotence/cache côté ACW."""
    basis = f"{plan_line.plan_line_id}|{plan_line.signature}|{plan_line.target_symbol}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def _validate_plan_line(pl: PlanLine) -> None:
    """Valide les champs minimaux d'une PlanLine (garde-fous alignés avec le tiddler Autopilot)."""
    if not pl.plan_line_id or not pl.plan_line_id.strip():
        raise ValueError("PlanLine invalide: plan_line_id manquant.")
    if not pl.file or not pl.file.endswith(".py"):
        raise ValueError(f"{pl.plan_line_id}: 'file' doit cibler un .py")
    if pl.op not in ("create", "modify"):
        raise ValueError(f"{pl.plan_line_id}: op doit être 'create' ou 'modify'")
    if not pl.role:
        raise ValueError(f"{pl.plan_line_id}: role manquant")
    if not pl.target_symbol:
        raise ValueError(f"{pl.plan_line_id}: target_symbol manquant")
    if not pl.signature:
        raise ValueError(f"{pl.plan_line_id}: signature manquante")
    if not pl.acceptance or len(pl.acceptance) == 0:
        raise ValueError(f"{pl.plan_line_id}: au moins un critère 'acceptance' requis")


# ------------------------- Prompts (texte & YAML) -------------------------

def _build_writer_prompt_text(pl: PlanLine) -> str:
    """
    Construit un prompt TEXTE compact, auto-contenu, pour guider ACW (sans RAG).
    Met l’accent sur la signature, le rôle, les contraintes et les critères d’acceptation.
    """
    lines: List[str] = []
    lines.append("Tu es un copiste de code (ACW) pour ARCHCode, niveau PlanLine.")
    lines.append("Respecte STRICTEMENT la signature, le rôle, et les contraintes.")
    lines.append("")
    lines.append(f"PlanLine: {pl.plan_line_id}")
    if pl.plan_line_ref:
        lines.append(f"Alias: {pl.plan_line_ref}")
    lines.append(f"Fichier cible: {pl.file}")
    if pl.path:
        lines.append(f"Path/API (si handler): {pl.path}")
    lines.append(f"Opération: {pl.op}")
    lines.append(f"Rôle: {pl.role}")
    lines.append(f"Cible (target_symbol): {pl.target_symbol}")
    lines.append(f"Signature attendue: {pl.signature}")
    if pl.depends_on:
        lines.append(f"Dépend de: {', '.join(pl.depends_on)}")
    if pl.description:
        lines.append(f"Description: {pl.description}")
    lines.append("")
    lines.append("Contraintes:")
    if pl.constraints:
        for k, v in pl.constraints.items():
            lines.append(f"  - {k}: {v}")
    else:
        lines.append("  - style: pep8")
        lines.append("  - typing: strict")
    lines.append("")
    lines.append("Critères d’acceptation (asserts):")
    for a in pl.acceptance:
        lines.append(f"  - {a}")
    lines.append("")
    lines.append("Production attendue (MVP) :")
    lines.append("  - Génère uniquement le bloc de code entouré des balises meta :")
    lines.append("    #{begin_meta: ...} ... #{end_meta}")
    lines.append("  - Remplis les champs meta: file, module (si connu), role, plan_line_id, timestamp.")
    lines.append("  - Ne modifie pas d’autres parties du fichier.")
    lines.append("  - Code Python idiomatique, lisible, 4 espaces, pas de bare except.")
    return "\n".join(lines)


def build_prompt(
    pl: PlanLine,
    *,
    bus_message_id: Optional[str] = None,
    user_story_id: Optional[str] = None,
    user_story: Optional[str] = None,
    loop_iteration: Optional[int] = None,
    task_id: Optional[str] = None,
) -> str:
    """
    Construit un PROMPT YAML déterministe (compat V1) pour ACW.

    - Concatène acceptance + constraints.
    - Force expected_format avec balises #{begin_meta}/#{end_meta}.
    - Fournit meta (file/module/plan_line_id/loop_iteration).
    """
    _validate_plan_line(pl)

    _task_id = task_id or f"TASK-{uuid.uuid4().hex[:8]}"
    _bus_id = bus_message_id or "BUS-UNKNOWN"
    _us_id = user_story_id or "US-UNKNOWN"
    _user_story = user_story or "Contexte utilisateur non fourni (MVP)."
    _loop = loop_iteration if loop_iteration is not None else 1

    exec_line = f"{pl.plan_line_id}: Implémenter {pl.signature} dans {pl.file} (role={pl.role})"
    if pl.description:
        exec_line += f" — {pl.description}"

    lines: List[str] = []
    lines.append(f"task_id: {_task_id}")
    lines.append(f"bus_message_id: {_bus_id}")
    lines.append(f"user_story_id: {_us_id}")
    lines.append("")
    lines.append("user_story: |")
    lines.append(_indent_block(_user_story, 2))
    lines.append("")
    lines.append("execution_plan_line: |")
    lines.append(_indent_block(exec_line, 2))
    lines.append("")
    lines.append("constraints:")
    acc = list(pl.acceptance or [])
    if acc:
        for item in acc:
            lines.append(f"  - {item}")
    c_lines = _format_constraints(pl.constraints or {})
    if c_lines:
        lines.extend(c_lines)
    if not acc and not c_lines:
        lines.append("  - Aucune contrainte fournie (MVP).")
    lines.append("")
    lines.append("meta:")
    lines.append(f"  file: {pl.file}")
    lines.append(f"  module: {_infer_module(pl.file)}")
    lines.append(f"  role: {pl.role}")
    lines.append(f"  plan_line_id: {pl.plan_line_id}")
    lines.append(f"  loop_iteration: {_loop}")
    lines.append("")
    lines.append("expected_format: |")
    lines.append(
        _indent_block(
            "Le code généré doit être entouré des balises suivantes:\n"
            "#{begin_meta: {"
            " file: <file>, module: <module>, role: <role>, plan_line_id: <plan_line_id>, "
            "bus_message_id: <bus_message_id>, status_agent_file_checker: pending, "
            "status_agent_module_checker: pending }}\n"
            "# (insérer ici UNIQUEMENT le code Python demandé)\n"
            "#{end_meta}",
            2,
        )
    )
    return "\n".join(lines)


# ---------------------------- API principale ----------------------------

def build_writer_task(
    pl: PlanLine,
    execution_context: Optional[Dict[str, Any]] = None,
    *,
    bus_message_id: Optional[str] = None,
    user_story_id: Optional[str] = None,
    user_story: Optional[str] = None,
    loop_iteration: Optional[int] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Construit une tâche auto-contenue pour ACW à partir d'une PlanLine.

    Retour:
      - dict 'writer_task' prêt à consommer par ACW, avec:
        * writer_prompt        (texte compact)
        * writer_prompt_yaml   (YAML déterministe, compat V1)
    """
    _validate_plan_line(pl)

    # Empreinte d’intention (cache/idempotence côté ACW)
    intent_fp = pl.intent_fingerprint or _digest_intent(pl)
    task_id_final = task_id or f"TASK-{uuid.uuid4().hex[:8]}"

    # Deux formats de prompt (texte + YAML)
    writer_prompt = _build_writer_prompt_text(pl)
    writer_prompt_yaml = build_prompt(
        pl,
        bus_message_id=bus_message_id,
        user_story_id=user_story_id,
        user_story=user_story,
        loop_iteration=loop_iteration,
        task_id=task_id_final,
    )

    ctx = dict(execution_context or {})

    task: Dict[str, Any] = {
        "task_id": task_id_final,
        "plan_line_id": pl.plan_line_id,
        "file": pl.file,
        "role": pl.role,
        "op": pl.op,
        "target_symbol": pl.target_symbol,
        "signature": pl.signature,
        "path": pl.path,
        "allow_create": pl.allow_create,
        "markers": pl.markers or {},
        "depends_on": list(pl.depends_on or []),
        "acceptance": list(pl.acceptance or []),
        "constraints": dict(pl.constraints or {}),
        "plan_line_ref": pl.plan_line_ref,
        "intent_fingerprint": intent_fp,
        "writer_prompt": writer_prompt,
        "writer_prompt_yaml": writer_prompt_yaml,
        "execution_context": ctx,
    }
    return task


def plan_to_writer_tasks(
    plan_lines: Iterable[PlanLine],
    execution_context: Optional[Dict[str, Any]] = None,
    *,
    bus_message_id: Optional[str] = None,
    user_story_id: Optional[str] = None,
    user_story: Optional[str] = None,
    loop_iteration: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Transforme une séquence de `PlanLine` en liste ordonnée de `writer_tasks`.

    Propage les paramètres communs (bus_message_id, user_story_id, user_story, loop_iteration).
    """
    tasks: List[Dict[str, Any]] = []
    for pl in plan_lines:
        tasks.append(
            build_writer_task(
                pl,
                execution_context=execution_context,
                bus_message_id=bus_message_id,
                user_story_id=user_story_id,
                user_story=user_story,
                loop_iteration=loop_iteration,
            )
        )
    return tasks
