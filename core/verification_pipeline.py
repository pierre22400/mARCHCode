# core/verification_pipeline.py

from __future__ import annotations

from typing import Optional
from core.types import PatchBlock
from agents.agent_file_checker import check_file
from agents.agent_module_checker import check_module

def run_local_checkers(pb: PatchBlock) -> PatchBlock:
    """
    Chaînage local de validation d'un PatchBlock :
      1) agent_file_checker (niveau FICHIER) — n'affecte PAS pb.global_status / pb.next_action
      2) agent_module_checker (niveau MODULE) — fixe pb.global_status et pb.next_action

    Contrats respectés :
      - FileChecker écrit uniquement dans pb.meta.status_agent_file_checker,
        pb.meta.comment_agent_file_checker et, en cas de rejet, pb.error_trace.
      - ModuleChecker décide du statut global et de la suite (accept|retry|rollback),
        et annote pb.meta.comment_agent_module_checker / pb.meta.status_agent_module_checker.

    Retourne le PatchBlock annoté, prêt pour l'étape suivante (apply/commit ou régénération).
    """
    if not isinstance(pb, PatchBlock):
        raise TypeError("run_local_checkers attend un PatchBlock valide")

    # 1) Vérification locale de fichier
    pb = check_file(pb)

    # 2) Décision finale au niveau module (toujours appelée, même si AFC a rejeté)
    pb = check_module(pb)

    return pb
