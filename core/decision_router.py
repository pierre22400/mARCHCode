# core/decision_router.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Callable, Iterable
import re

from core.types import PatchBlock
from core.verification_pipeline import run_local_checkers
from core.error_policy import map_error_to_next_action  # intégration progressive error_policy

"""
Decision Router — Orchestration locale après vérifications (MVP + error_policy ready)
====================================================================================

Rôle du module
--------------
Centraliser la décision opérationnelle à partir d’un PatchBlock déjà passé par :
  1) agent_file_checker  (niveau fichier)
  2) agent_module_checker (niveau module, décision finale)

Nouveauté (préparation harmonisation) :
--------------------------------------
- Si `pb.error_category` est renseigné par un checker, on peut dériver la décision via
  `map_error_to_next_action()` (politique centralisée) au lieu de la logique manuelle.
- Par défaut (MVP), on conserve un fallback déterministe sur (global_status, next_action).

Entrées / Sorties
-----------------
Entrée :
  - PatchBlock `pb` (code + meta + champs globaux éventuels)
Sorties :
  - Decision(action, global_status, next_action, reasons, summary, ...)

Contrats respectés
------------------
- FileChecker n’écrit PAS `pb.global_status` / `pb.next_action`.
- ModuleChecker fixe :
    * `pb.global_status` ∈ {"ok","partial_ok","rejected"}
    * `pb.next_action`  ∈ {"accept","retry","rollback"}
- La politique d’erreurs centralisée (error_policy) est optionnelle et rétro-compatible.

Schéma de décision (fallback manuel, inchangé)
----------------------------------------------
if global_status == ok and next_action == accept → APPLY
elif next_action == rollback or global_status == rejected → ROLLBACK
else → RETRY (couvre partial_ok|retry et cas ambigus)

Notes d’implémentation
----------------------
- `policy_mode` n’est pas connu ici ; on utilise la valeur par défaut de error_policy ("enforce").
- Le champ `pb.error_category` est optionnel dans le MVP et pourra être rempli plus tard par les checkers.
"""


class Action(str, Enum):
    """Action d'orchestration choisie après vérifications."""
    APPLY = "apply"        # intégrer le patch dans le FS + commit
    RETRY = "retry"        # renvoyer vers agent_code_writer (régénération ciblée)
    ROLLBACK = "rollback"  # ignorer/retirer ce patch et journaliser


@dataclass
class Decision:
    """Décision finale issue du router après passage des checkers."""
    action: Action
    global_status: str                   # ok | partial_ok | rejected
    next_action: str                     # accept | retry | rollback (provenant du ModuleChecker)
    reasons: List[str]                   # raisons extraites (si disponibles)
    summary: str                         # condensé lisible pour logs/console
    file_comment: Optional[str] = None   # comment_agent_file_checker
    module_comment: Optional[str] = None # comment_agent_module_checker


# Type alias pour un extracteur/normaliseur de raisons (ex. LLM léger)
Reasoner = Callable[[str], List[str]]


def _heuristic_reason_split(chunks: Iterable[str]) -> List[str]:
    """
    Normalise une liste de fragments textuels en raisons courtes et dédupliquées.

    Règles :
      - découpe sur | ; \n et puces « • »
      - strip des tirets/espaces et ponctuation courante
      - filtre les doublons et tronque les fragments trop longs
    """
    raw: List[str] = []
    for blob in chunks:
        if not blob:
            continue
        # Unifier séparateurs (" | ", ";", puces, retours ligne)
        parts = re.split(r"[|;\n•]+", blob)
        for part in parts:
            p = part.strip(" \t-—:•")
            if 0 < len(p) <= 180:
                raw.append(p)

    # Déduplication en conservant l'ordre
    seen, out = set(), []
    for r in raw:
        if r not in seen:
            out.append(r)
            seen.add(r)
    return out


def _collect_reasons(
    pb: "PatchBlock",
    reasoner: Optional[Reasoner] = None
) -> List[str]:
    """
    Agrège et normalise les raisons issues des commentaires file/module checkers.

    Si `reasoner` est fourni, il est appelé avec le texte fusionné (file+module),
    puis ses résultats sont repassés dans l’heuristique pour garantir la forme.
    """
    fc = (pb.meta.comment_agent_file_checker or "").strip()
    mc = (pb.meta.comment_agent_module_checker or "").strip()

    # Chaîne consolidée passée au reasoner (ou à l’heuristique)
    fused = "\n".join([s for s in (fc, mc) if s])

    if not fused:
        return []

    # Si un reasoner est injecté, on l’utilise (ex.: LLM léger ou mapping catégoriel)
    if callable(reasoner):
        try:
            reasons = reasoner(fused)
            # filet de sécurité : normaliser/filtrer quand même
            return _heuristic_reason_split(reasons)
        except Exception:
            # fallback silencieux vers l’heuristique si le reasoner échoue
            pass

    # Heuristique par défaut
    return _heuristic_reason_split([fused])


def route_after_checks(pb: PatchBlock) -> Decision:
    """
    Mappe (global_status, next_action, error_category) → Action.

    Priorité :
      1) Si pb.error_category est défini → utiliser error_policy.map_error_to_next_action()
      2) Sinon → logique manuelle (fallback MVP) :
           - ok + accept  → APPLY
           - rejected OU rollback → ROLLBACK
           - sinon → RETRY
    """
    gs = (pb.global_status or "").lower()
    na = (pb.next_action or "").lower()

    # 1) Chemin harmonisé par error_policy si disponible
    error_category = getattr(pb, "error_category", None)
    if error_category:
        mapped = map_error_to_next_action(error_category, policy_mode="enforce")
        if mapped == "rollback":
            action = Action.ROLLBACK
        elif mapped == "retry":
            action = Action.RETRY
        elif mapped == "apply":
            action = Action.APPLY
        else:
            action = Action.RETRY  # défaut conservateur
    else:
        # 2) Fallback historique (MVP)
        if gs == "ok" and na == "accept":
            action = Action.APPLY
        elif na == "rollback" or gs == "rejected":
            action = Action.ROLLBACK
        else:
            # couvre partial_ok|retry et tout cas ambigu → on préfère RETRY (régénération ciblée)
            action = Action.RETRY

    reasons = _collect_reasons(pb)

    summary_bits: List[str] = [
        f"global_status={gs or '∅'}",
        f"next_action={na or '∅'}",
        f"decision={action.value}",
    ]
    if getattr(pb.meta, "status_agent_file_checker", None):
        summary_bits.append(f"file_checker={pb.meta.status_agent_file_checker}")
    if getattr(pb.meta, "status_agent_module_checker", None):
        summary_bits.append(f"module_checker={pb.meta.status_agent_module_checker}")
    if error_category:
        summary_bits.append(f"error_category={str(error_category)}")

    summary = " | ".join(summary_bits)

    return Decision(
        action=action,
        global_status=gs or "",
        next_action=na or "",
        reasons=reasons,
        summary=summary,
        file_comment=pb.meta.comment_agent_file_checker or None,
        module_comment=pb.meta.comment_agent_module_checker or None,
    )


def verify_and_route(pb: PatchBlock) -> tuple[PatchBlock, Decision]:
    """
    Pipeline minimal d’orchestration locale :
      1) Exécute les deux checkers (fichier + module) sur `pb`
      2) Dérive la décision (APPLY / RETRY / ROLLBACK) via `route_after_checks`

    Returns:
        tuple[PatchBlock, Decision]: Le PatchBlock annoté + la décision finale.
    """
    pb = run_local_checkers(pb)
    decision = route_after_checks(pb)
    return pb, decision
