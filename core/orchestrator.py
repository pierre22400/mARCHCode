# core/orchestrator.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

from core.types import PatchBlock
from core.decision_router import verify_and_route, Decision, Action, Reasoner
from core.self_dev_policy import SelfDevPolicy
from core.git_diffstats import DiffStatsData  # pour typer diff_stats
from core.archiver import (
    archive_patch_before,
    archive_patch_after,
    archive_patch_post_commit,
    archive_decision,
    append_console_log,
)
from core.error_policy import ErrorCategory, map_error_to_next_action  # <-- ajout

# ------------------------------------------------------------
# Orchestrator — Exécution locale de la phase 3 (MVP mARCHCode)
# ------------------------------------------------------------
# Rôle du fichier :
#   Offrir un point d’entrée unique pour :
#     1) Vérifier un PatchBlock via les deux checkers
#     2) Décider APPLY / RETRY / ROLLBACK
#     3) Exécuter l’action choisie via des adaptateurs (FS, LLM, logs)
#
# Entrée :
#   - PatchBlock (produit par agent_code_writer)
#   - Adapters (callbacks) pour appliquer/committer, régénérer ou rollback
#   - (Optionnel) Reasoner basse conso pour mapper/normaliser les “raisons”
#
# Sorties :
#   - (PatchBlock annoté, Decision)
#
# Contrats :
#   - Ne modifie jamais la logique des checkers ; délègue au router la décision.
#   - Les adaptateurs sont injectés (inversion de dépendance) : ce module
#     n’écrit pas directement sur disque, ne parle pas à Git, ni au LLM.
#
# Schéma :
#     PatchBlock → verify_and_route → Decision
#         └── Action.APPLY    → adapters.apply_and_commit
#         └── Action.RETRY    → adapters.regenerate_with_acw
#         └── Action.ROLLBACK → adapters.rollback_and_log
#
# Exemple d’intégration (pseudo-runner Typer) :
#   adapters = DefaultConsoleAdapters()
#   pb, decision = run_patch_local(pb, adapters)
#   print(decision.summary)
# ------------------------------------------------------------


# ------------------------- Interfaces -------------------------

class ApplyAndCommit(Protocol):
    def __call__(self, pb: PatchBlock, decision: Decision) -> None: ...


class RegenerateWithACW(Protocol):
    def __call__(
        self,
        pb: PatchBlock,
        decision: Decision,
        reasoner: Optional[Reasoner] = None
    ) -> None: ...


class RollbackAndLog(Protocol):
    def __call__(self, pb: PatchBlock, decision: Decision) -> None: ...


@dataclass
class OrchestrationAdapters:
    """
    Conteneur d’adaptateurs d’E/S.
    Fournit des callbacks concrets côté application (FS, Git, ACW, journaux...).
    """
    apply_and_commit: ApplyAndCommit
    regenerate_with_acw: RegenerateWithACW
    rollback_and_log: RollbackAndLog


def run_patch_local(
    pb: PatchBlock,
    adapters: OrchestrationAdapters,
    reasoner: Optional[Reasoner] = None,
    *,
    policy: Optional[SelfDevPolicy] = None,
    diff_stats: Optional[DiffStatsData] = None,
    branch_name: Optional[str] = None,
    partial_ok_count_so_far: int = 0,
    archive_dir: Optional[str] = ".arch_runs/_last",   # archivage YAML par défaut
) -> Tuple[PatchBlock, Decision]:
    """
    Pipeline local MVP :
      1) Archive d’entrée (optionnelle)
      2) Vérifie et route le PatchBlock (checkers → Decision)
      3) Archive décision + patch annoté
      4) Applique une gate de politique self-dev avant APPLY (optionnelle)
      5) Dispatch APPLY / RETRY / ROLLBACK via adaptateurs injectés
      6) Archive post-commit si commit_sha injecté par l’adaptateur Git
      7) Retourne (pb annoté, décision)
    """
    # (A) Archive d’entrée
    if archive_dir:
        archive_patch_before(pb, run_dir=archive_dir)
        append_console_log("[arch] received PatchBlock", run_dir=archive_dir)

    # 1) Vérification + décision
    pb, decision = verify_and_route(pb)

    # (B) Archive décision + patch annoté par les checkers
    if archive_dir:
        archive_decision(decision, run_dir=archive_dir)
        archive_patch_after(pb, run_dir=archive_dir)

    # 2) Gate policy AVANT APPLY
    if decision.action == Action.APPLY and policy and diff_stats is not None:
        ok, violations = policy.evaluate_patch(
            pb,
            diff_stats,
            branch_name=branch_name,
            partial_ok_count_so_far=partial_ok_count_so_far,
        )
        if not ok:
            action = map_error_to_next_action(ErrorCategory.POLICY_VIOLATION, policy_mode=policy.mode)
            if action == "rollback":
                adapters.rollback_and_log(pb, decision)
                if archive_dir:
                    append_console_log(
                        "[policy] BLOCKED: " + " | ".join(violations),
                        run_dir=archive_dir,
                    )
                return pb, decision
            elif action == "retry":
                adapters.regenerate_with_acw(pb, decision, reasoner=reasoner)
                if archive_dir:
                    append_console_log(
                        "[policy] RETRY: " + " | ".join(violations),
                        run_dir=archive_dir,
                    )
                return pb, decision

        elif not ok and policy.mode == "warn":
            if archive_dir:
                append_console_log(
                    "[policy] WARN: " + " | ".join(violations),
                    run_dir=archive_dir,
                )

    # 3) Dispatch APPLY/RETRY/ROLLBACK (inchangé)
    if decision.action == Action.APPLY:
        adapters.apply_and_commit(pb, decision)
        # 6) Archive post-commit si l’adaptateur a injecté meta.commit_sha
        meta = getattr(pb, "meta", None)
        commit_sha = getattr(meta, "commit_sha", None) if meta else None
        if archive_dir and commit_sha:
            archive_patch_post_commit(pb, run_dir=archive_dir)
    elif decision.action == Action.RETRY:
        adapters.regenerate_with_acw(pb, decision, reasoner=reasoner)
    else:
        adapters.rollback_and_log(pb, decision)

    return pb, decision


# ---------- Adaptateurs par défaut (console/no-op) ----------

def _print_header(title: str) -> None:
    bar = "─" * max(12, len(title))
    print(f"\n{title}\n{bar}")


def _fmt_meta(pb: PatchBlock) -> str:
    m = pb.meta
    return (
        f"file={getattr(m, 'file', '') or '∅'}, "
        f"module={getattr(m, 'module', '') or '∅'}, "
        f"plan_line_id={getattr(m, 'plan_line_id', '') or '∅'}"
    )


def default_apply_and_commit(pb: PatchBlock, decision: Decision) -> None:
    _print_header("APPLY → intégration (simulation)")
    print(decision.summary)
    print(f"meta: {_fmt_meta(pb)}")
    # Ici, brancher : write_to_fs(pb), git_commit(...)


def default_regenerate_with_acw(
    pb: PatchBlock,
    decision: Decision,
    reasoner: Optional[Reasoner] = None
) -> None:
    _print_header("RETRY → régénération ciblée (simulation)")
    print(decision.summary)
    print(f"meta: {_fmt_meta(pb)}")
    if decision.reasons:
        print("reasons:", "; ".join(decision.reasons))
    # Ici, brancher : agent_code_writer.regenerate(pb, hints=decision.reasons, reasoner=reasoner)


def default_rollback_and_log(pb: PatchBlock, decision: Decision) -> None:
    _print_header("ROLLBACK → exclusion du patch (simulation)")
    print(decision.summary)
    print(f"meta: {_fmt_meta(pb)}")
    # Ici, brancher : memory_manager.log_rollback(pb), rollback_bundle.append(...)


@dataclass
class DefaultConsoleAdapters(OrchestrationAdapters):
    """
    Adaptateurs de démonstration :
      - Affichent les actions au lieu d’exécuter FS/Git/LLM.
      - Utile pour les tests de bout en bout du MVP.
    """
    def __init__(self) -> None:
        super().__init__(
            apply_and_commit=default_apply_and_commit,
            regenerate_with_acw=default_regenerate_with_acw,
            rollback_and_log=default_rollback_and_log,
        )
