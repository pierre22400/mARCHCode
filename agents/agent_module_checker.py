# agents/agent_module_checker.py
from __future__ import annotations

from typing import Tuple, List, Dict, Optional
from core.types import PatchBlock
"""
Agent ModuleChecker — V2 (mARCHCode / Phase 3)
==============================================

Rôle du module
--------------
Prendre un PatchBlock (déjà passé par FileChecker) et émettre la décision GLOBALE
de module pour ce patch :
  - pb.global_status ∈ {"ok", "partial_ok", "rejected"}
  - pb.next_action ∈ {"accept", "retry", "rollback"}
Annoter également pb.meta.comment_agent_module_checker avec reasons / strategy / comment.

Entrées / Sorties
-----------------
Entrées :
  - PatchBlock issu du FileChecker
  - Métadonnées internes au PatchBlock
Sorties :
  - Mise à jour de pb.global_status et pb.next_action
  - Renseignement de pb.meta.comment_agent_module_checker
  - (Option) module_reassessment_request.yaml si MODULE_REASSESSMENT = yes

Stratégie V2
------------
1) Heuristique offline (sans LLM) — sûre et déterministe :
     - s’appuie sur status_agent_file_checker
     - détecte la présence de `def ` (bloc plausible) et le rôle
     - produit (status, next_action, reasons, strategy, comment, reassess?, reco)
2) Mode LLM (futur) :
     - _build_modulecheck_prompt → _call_llm → parsing KV
     - aujourd’hui, on privilégie l’heuristique (fallback LLM conservé)

Contrats respectés
------------------
- Pare-feu plan (review_execution_plan) inchangé : renvoie PLAN_OK, etc. (MVP simple)
- Artefact complémentaire : module_reassessment_request.yaml injecté en clair dans le commentaire si MODULE_REASSESSMENT = yes
"""


_ALLOWED_STATUS = {"ok", "partial_ok", "rejected"}
_ALLOWED_NEXT   = {"accept", "retry", "rollback"}
_ALLOWED_STRAT  = {
    "targeted_regeneration",
    "refactor",
    "skip",
    "escalate_to_user",
    "defer_to_next_iteration",
    "reroute_to_module_checker",
}
_ALLOWED_REASSESS = {"scinder", "redéfinir", "réordonner", "ignorer"}

_BEGIN_MARK = "#" + "{begin_meta:"
_END_MARK   = "#{end_meta}"


# ---------------------------- HEURISTIQUE V2 ----------------------------

def _looks_like_function(code: str) -> bool:
    return "def " in (code or "")

def _role_is_plausible(role: Optional[str], code: str) -> bool:
    r = (role or "").lower()
    if not r:
        return True  # pas de rôle → pas bloquant au niveau module
    # On peut raffiner plus tard (repo/service/route_handler), MVP: tolérant
    return True

def _offline_module_decision(pb: PatchBlock) -> Tuple[str, str, List[str], str, str, bool, str]:
    """
    Décision locale sans LLM (V2) :
      → (status, next_action, reasons[], strategy, comment, reassess_flag, reassess_reco)
    Règles :
      - Si FileChecker a rejeté → rejected+retry (raison = error_trace/comment fichier)
      - Sinon :
          * si code contient 'def ' ET rôle plausible → ok+accept
          * si code ne contient pas 'def ' → partial_ok+retry (besoin d’implémentation)
    """
    reasons: List[str] = []
    comment = ""
    strategy = "targeted_regeneration"
    reassess_flag = False
    reassess_reco = "ignorer"

    fc_status = (getattr(pb.meta, "status_agent_file_checker", "") or "").lower()
    code = pb.code or ""
    role = pb.meta.role

    if fc_status == "rejected":
        status = "rejected"
        next_action = "retry"
        if pb.error_trace:
            reasons.append(pb.error_trace)
        if pb.meta.comment_agent_file_checker:
            reasons.append(pb.meta.comment_agent_file_checker)
        comment = "FileChecker a bloqué le patch ; corriger les problèmes locaux."
        return status, next_action, _dedupe_short(reasons), strategy, comment, reassess_flag, reassess_reco

    # FileChecker ok → on regarde la plausibilité fonctionnelle
    has_def = _looks_like_function(code)
    role_ok = _role_is_plausible(role, code)

    if has_def and role_ok:
        status = "ok"
        next_action = "accept"
        reasons.append("Structure fonctionnelle plausible (def détecté)")
        if _BEGIN_MARK not in code or _END_MARK not in code:
            # Très peu probable après ACW, mais on garde l’assertion
            status = "partial_ok"
            next_action = "retry"
            reasons.append("Balises meta incomplètes (begin/end) — à régénérer")
            comment = "Baliser correctement le patch selon le contrat ARCHCode."
        else:
            comment = "Patch cohérent au niveau module. OK pour intégration."
        return status, next_action, _dedupe_short(reasons), strategy, comment, reassess_flag, reassess_reco

    # Pas de def → squelette insuffisant : on demande une régénération ciblée
    status = "partial_ok"
    next_action = "retry"
    reasons.append("Bloc sans 'def' détecté — implémentation incomplète")
    if not role_ok:
        reassess_flag = True
        reassess_reco = "redéfinir"
        reasons.append("Rôle incohérent/indéfini pour ce module")
    comment = "Régénérer le bloc selon la signature attendue et les critères d’acceptation."
    return status, next_action, _dedupe_short(reasons), strategy, comment, reassess_flag, reassess_reco


def _dedupe_short(chunks: List[str]) -> List[str]:
    raw = []
    for c in chunks:
        if not c:
            continue
        # Sépare grossièrement sur séparateurs fréquents
        for part in [p.strip(" -—;:") for p in c.split("|")]:
            if 0 < len(part) <= 200:
                raw.append(part)
    seen, out = set(), []
    for r in raw:
        if r not in seen:
            out.append(r)
            seen.add(r)
    return out


# ------------------------------- LLM MODE -------------------------------

def _build_modulecheck_prompt(pb: PatchBlock) -> str:
    role = pb.meta.role or "unknown"
    file = pb.meta.file or "unknown.py"
    plan_line_id = pb.meta.plan_line_id or "UNKNOWN"

    lines: List[str] = []
    lines.append("Tu es un agent d'audit DE MODULE (niveau global). Réponds STRICTEMENT en KV, sans autre texte :")
    lines.append("STATUS: ok|partial_ok|rejected")
    lines.append("NEXT_ACTION: accept|retry|rollback")
    lines.append("REASONS: raison1 | raison2")
    lines.append("STRATEGY: targeted_regeneration|refactor|skip|escalate_to_user|defer_to_next_iteration|reroute_to_module_checker")
    lines.append("COMMENT: justification brève, exploitable par un LLM")
    lines.append("MODULE_REASSESSMENT: yes|no")
    lines.append("REASSESS_RECOMMENDATION: scinder|redéfinir|réordonner|ignorer")
    lines.append("")
    lines.append("Checklist (module) :")
    lines.append("1) Signature/contrat attendus respectés (fonction principale, types visibles).")
    lines.append("2) Cohérence de rôle avec meta.role (route_handler/service/dto/test/etc.).")
    lines.append("3) Dépendances internes/externes plausibles (pas d'appels impossibles).")
    lines.append("4) Unicité fonctionnelle (pas de doublon évident).")
    lines.append("5) Style/structure globalement raisonnables.")
    lines.append("6) Si le module semble mal découpé (trop vaste/flou), activer MODULE_REASSESSMENT=yes avec REASSESS_RECOMMENDATION.")
    lines.append("")
    lines.append(f"Contexte: file={file}, role={role}, plan_line_id={plan_line_id}")
    lines.append("Patch à analyser (entre triples backticks) :")
    lines.append("```python")
    lines.append(pb.code)
    lines.append("```")
    lines.append("")
    lines.append("RÉPONDS UNIQUEMENT avec :")
    lines.append("STATUS: ...")
    lines.append("NEXT_ACTION: ...")
    lines.append("REASONS: ...")
    lines.append("STRATEGY: ...")
    lines.append("COMMENT: ...")
    lines.append("MODULE_REASSESSMENT: yes|no")
    lines.append("REASSESS_RECOMMENDATION: scinder|redéfinir|réordonner|ignorer")
    return "\n".join(lines)

def _build_plan_review_prompt(ep_text: str) -> str:
    lines: List[str] = []
    lines.append("Tu es un validateur FORMEL d'execution_plan (pré-génération). Réponds STRICTEMENT en KV :")
    lines.append("PLAN_OK: yes|no")
    lines.append("PLAN_REASONS: raison1 | raison2")
    lines.append("PLAN_ACTION: fix_plan|proceed")
    lines.append("AFFECTED_IDS: EP-xxxx | EP-yyyy          # plan_line_id touchés (si connus)")
    lines.append("")
    lines.append("Checklist :")
    lines.append("1) Cohérence structurelle des modules et des plan_lines.")
    lines.append("2) Alignement strict avec plan_validated (si info présente).")
    lines.append("3) Conformité formelle des pseudo-codes.")
    lines.append("4) Chaque plan_line_id est présent et traçable.")
    lines.append("")
    lines.append("Voici le contenu du execution_plan.yaml à auditer :")
    lines.append("```yaml")
    lines.append(ep_text)
    lines.append("```")
    lines.append("")
    lines.append("RÉPONDS UNIQUEMENT avec :")
    lines.append("PLAN_OK: yes|no")
    lines.append("PLAN_REASONS: ...")
    lines.append("PLAN_ACTION: fix_plan|proceed")
    lines.append("AFFECTED_IDS: ...")
    return "\n".join(lines)

def _call_llm(prompt: str) -> str:
    """
    Crochet LLM (futur). MVP offline :
      - Si prompt plan → valide si 'modules:' et 'plan_lines:' présents.
      - Si prompt patch → 'def ' présent ⇒ partial_ok/retry, sinon rejected/retry.
    """
    looks_like_py = "```python" in prompt and "def " in prompt
    if "PLAN_OK:" in prompt:
        ok = "modules:" in prompt and "plan_lines:" in prompt
        if ok:
            return (
                "PLAN_OK: yes\n"
                "PLAN_REASONS: \n"
                "PLAN_ACTION: proceed\n"
                "AFFECTED_IDS: \n"
            )
        return (
            "PLAN_OK: no\n"
            "PLAN_REASONS: modules ou plan_lines manquants\n"
            "PLAN_ACTION: fix_plan\n"
            "AFFECTED_IDS: \n"
        )
    if looks_like_py:
        return (
            "STATUS: partial_ok\n"
            "NEXT_ACTION: retry\n"
            "REASONS: cohérence plausible | tests manquants\n"
            "STRATEGY: targeted_regeneration\n"
            "COMMENT: préciser la signature et ajouter tests unitaires\n"
            "MODULE_REASSESSMENT: no\n"
            "REASSESS_RECOMMENDATION: ignorer\n"
        )
    return (
        "STATUS: rejected\n"
        "NEXT_ACTION: retry\n"
        "REASONS: bloc non fonctionnel au niveau module\n"
        "STRATEGY: targeted_regeneration\n"
        "COMMENT: régénérer la fonction principale selon la signature prévue\n"
        "MODULE_REASSESSMENT: yes\n"
        "REASSESS_RECOMMENDATION: redéfinir\n"
    )

def _parse_kv(text: str) -> dict:
    out = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        k, v = raw.split(":", 1)
        out[k.strip().upper()] = v.strip()
    return out

def _normalize_patch_decision(kv: dict) -> Tuple[str, str, List[str], str, str, bool, str]:
    status = kv.get("STATUS", "rejected").lower()
    if status not in _ALLOWED_STATUS:
        status = "rejected"

    next_action = kv.get("NEXT_ACTION", "retry").lower()
    if next_action not in _ALLOWED_NEXT:
        next_action = "retry"

    reasons = [r.strip() for r in kv.get("REASONS", "").split("|") if r.strip()]

    strategy = kv.get("STRATEGY", "targeted_regeneration").strip()
    if strategy not in _ALLOWED_STRAT:
        strategy = "targeted_regeneration"

    comment = kv.get("COMMENT", "")

    reassess_flag = kv.get("MODULE_REASSESSMENT", "no").lower() == "yes"
    reassess_reco = kv.get("REASSESS_RECOMMENDATION", "ignorer").strip()
    if reassess_reco not in _ALLOWED_REASSESS:
        reassess_reco = "ignorer"

    return status, next_action, reasons, strategy, comment, reassess_flag, reassess_reco

def _build_module_reassessment_yaml(module_id: str, reasons: List[str], recommendation: str, spec_feedback: str = "") -> str:
    reasons_lines = "\n".join([f"  - {r}" for r in reasons]) if reasons else ""
    spec = f"\nspec_feedback: |\n  {spec_feedback}\n" if spec_feedback else "\n"
    return (
        "module_reassessment_request:\n"
        f"  module_id: {module_id}\n"
        "  anomalies:\n"
        f"{reasons_lines}\n"
        f"  recommendation: {recommendation}\n"
        f"{spec}"
    )

# --------------------------- API publique ---------------------------

def check_module(pb: PatchBlock, *, use_llm: bool = False) -> PatchBlock:
    """
    Décision finale au niveau module pour CE patch.
    - Par défaut : heuristique V2 (déterministe)
    - Option use_llm=True : bascule vers le mode LLM KV (MVP offline)
    Effets :
      * pb.global_status, pb.next_action mis à jour
      * pb.meta.status_agent_module_checker et .comment_agent_module_checker renseignés
    """
    if use_llm:
        prompt = _build_modulecheck_prompt(pb)
        raw = _call_llm(prompt)
        kv = _parse_kv(raw)
        status, next_action, reasons, strategy, comment, reassess, reco = _normalize_patch_decision(kv)
    else:
        status, next_action, reasons, strategy, comment, reassess, reco = _offline_module_decision(pb)

    # 1) Statut global & action
    pb.global_status = status
    pb.next_action = next_action

    # 2) Annotation lisible consolidée
    parts: List[str] = []
    if reasons:
        parts.append("; ".join(reasons))
    parts.append(f"STRATEGY: {strategy}")
    if comment:
        parts.append(comment)

    # 3) Artefact de réévaluation de module si nécessaire
    if reassess:
        module_id = pb.meta.module or "unknown_module"
        reassess_yaml = _build_module_reassessment_yaml(
            module_id,
            reasons,
            reco,
            spec_feedback=f"plan_line_id={pb.meta.plan_line_id or 'UNKNOWN'}"
        )
        parts.append("--- module_reassessment_request.yaml ---")
        parts.append(reassess_yaml.strip())

    note = " | ".join(p for p in parts if p)
    pb.meta.status_agent_module_checker = "ok" if status in ("ok", "partial_ok") else "rejected"
    pb.meta.comment_agent_module_checker = note or "analysis unavailable"

    return pb


def review_execution_plan(ep_text: str) -> dict:
    """
    Pare-feu réflexif AVANT génération :
      - Retourne un dict {PLAN_OK, PLAN_REASONS, PLAN_ACTION, AFFECTED_IDS} (strings).
      - À appeler par le planner/runner avant agent_code_writer (ACW).
    MVP offline : exige 'modules:' et 'plan_lines:'.
    """
    prompt = _build_plan_review_prompt(ep_text)
    raw = _call_llm(prompt)
    kv = _parse_kv(raw)
    out = {
        "PLAN_OK": kv.get("PLAN_OK", "no").lower(),
        "PLAN_REASONS": kv.get("PLAN_REASONS", ""),
        "PLAN_ACTION": kv.get("PLAN_ACTION", "fix_plan").lower(),
        "AFFECTED_IDS": kv.get("AFFECTED_IDS", ""),
    }
    return out
