# agents/agent_file_checker.py
from __future__ import annotations

from typing import Tuple, List
from core.types import PatchBlock

# ------------------------------------------------------------
# Agent FileChecker (LLM-based, format KV minimal)
# ------------------------------------------------------------
# Sortie attendue du LLM :
#   STATUS: ok|rejected
#   REASONS: raison1 | raison2           # court
#   STRATEGY: targeted_regeneration|refactor|skip|escalate_to_user|defer_to_next_iteration|reroute_to_module_checker
#   REMEDIATION: action courte et concrète
#   COMMENT: justification brève (optionnel)
#
# NB : FileChecker n'écrit PAS global_status / next_action.
# ------------------------------------------------------------

_BEGIN_MARK = "#" + "{begin_meta:"
_END_MARK = "#{end_meta}"

_ALLOWED_STRATEGIES = {
    "targeted_regeneration",
    "refactor",
    "skip",
    "escalate_to_user",
    "defer_to_next_iteration",
    "reroute_to_module_checker",
}

def _build_filecheck_prompt(pb: PatchBlock) -> str:
    role = pb.meta.role or "unknown"
    file = pb.meta.file or "unknown.py"

    lines: List[str] = []
    lines.append("Tu es un agent de lint Python (niveau FICHIER). Réponds STRICTEMENT en KV sans autre texte :")
    lines.append("STATUS: ok|rejected")
    lines.append("REASONS: raison1 | raison2")
    lines.append("STRATEGY: targeted_regeneration|refactor|skip|escalate_to_user|defer_to_next_iteration|reroute_to_module_checker")
    lines.append("REMEDIATION: action courte et concrète")
    lines.append("COMMENT: justification brève")
    lines.append("")
    lines.append("Checklist :")
    lines.append("1) Balises meta présentes ('#{begin_meta: ...}' et '#{end_meta}').")
    lines.append("2) Cible .py.")
    lines.append("3) Signature principale plausible (si identifiable).")
    lines.append("4) Style PEP8 approximatif (indentation 4 espaces, lisibilité).")
    lines.append("5) Pas d'import/appel évidemment dangereux (exec/eval non justifiés, suppression de fichiers...).")
    lines.append("6) Cohérence de rôle locale (meta.role) vs contenu.")
    lines.append("→ Si le problème dépasse le fichier, STRATEGY doit être 'reroute_to_module_checker'.")
    lines.append("")
    lines.append(f"Contexte: file={file}, role={role}")
    lines.append("Patch à analyser (entre triples backticks) :")
    lines.append("```python")
    lines.append(pb.code)
    lines.append("```")
    lines.append("")
    lines.append("Réponds UNIQUEMENT avec :")
    lines.append("STATUS: ok|rejected")
    lines.append("REASONS: ...")
    lines.append("STRATEGY: ...")
    lines.append("REMEDIATION: ...")
    lines.append("COMMENT: ...")
    return "\n".join(lines)

def _call_llm(prompt: str) -> str:
    """
    Crochet LLM à brancher plus tard.
    MVP offline : si balises et '.py' présents → ok ; sinon rejected.
    Cas doute module → reroute_to_module_checker.
    """
    meta_ok = (_BEGIN_MARK in prompt) and (_END_MARK in prompt)
    file_is_py = ".py" in prompt
    if meta_ok and file_is_py:
        return (
            "STATUS: ok\n"
            "REASONS: meta ok | cible .py\n"
            "STRATEGY: targeted_regeneration\n"
            "REMEDIATION: aucune\n"
            "COMMENT: patch acceptable au niveau fichier"
        )
    # problème au-delà du fichier ? (offline: on ne sait pas, on oriente module)
    if meta_ok and not file_is_py:
        return (
            "STATUS: rejected\n"
            "REASONS: extension non .py\n"
            "STRATEGY: reroute_to_module_checker\n"
            "REMEDIATION: renommer le fichier en .py ou clarifier la cible\n"
            "COMMENT: incohérence de cible détectée"
        )
    return (
        "STATUS: rejected\n"
        "REASONS: meta manquantes\n"
        "STRATEGY: targeted_regeneration\n"
        "REMEDIATION: ajouter #{begin_meta: ...} et #{end_meta}\n"
        "COMMENT: appliquer le contrat de balisage ARCHCode"
    )

def _parse_kv_response(text: str) -> Tuple[str, List[str], str, str, str]:
    """
    Parse KV → (status, reasons[], strategy, remediation, comment).
    Applique des défauts sûrs.
    """
    status = "rejected"
    reasons: List[str] = []
    strategy = "targeted_regeneration"
    remediation = ""
    comment = ""

    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, val = raw.split(":", 1)
        k = key.strip().upper()
        v = val.strip()
        if k == "STATUS":
            sv = v.lower()
            if sv in ("ok", "rejected"):
                status = sv
        elif k == "REASONS":
            reasons = [r.strip() for r in v.split("|") if r.strip()]
        elif k == "STRATEGY":
            sv = v.strip()
            if sv in _ALLOWED_STRATEGIES:
                strategy = sv
        elif k == "REMEDIATION":
            remediation = v
        elif k == "COMMENT":
            comment = v

    return status, reasons, strategy, remediation, comment

def check_file(pb: PatchBlock) -> PatchBlock:
    """
    FileChecker :
      - construit le prompt KV,
      - appelle le LLM,
      - annote le PatchBlock (meta.status/comment),
      - ne fixe PAS global_status / next_action (réservés à ModuleChecker).
    """
    prompt = _build_filecheck_prompt(pb)
    raw = _call_llm(prompt)
    status, reasons, strategy, remediation, comment = _parse_kv_response(raw)

    # Prépare un commentaire lisible et actionnable
    parts: List[str] = []
    if reasons:
        parts.append("; ".join(reasons))
    if remediation:
        parts.append(f"REMEDIATION: {remediation}")
    if strategy:
        parts.append(f"STRATEGY: {strategy}")
    if comment:
        parts.append(comment)
    note = " | ".join(parts) if parts else ""

    if status == "ok":
        pb.meta.status_agent_file_checker = "ok"
        if note:
            pb.meta.comment_agent_file_checker = note
        return pb

    # Rejet local (mais on laisse la décision globale à ModuleChecker)
    pb.meta.status_agent_file_checker = "rejected"
    pb.meta.comment_agent_file_checker = note or "STRATEGY: targeted_regeneration"
    pb.error_trace = "; ".join(reasons) if reasons else "rejeté par FileChecker"
    return pb
