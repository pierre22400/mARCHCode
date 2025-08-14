# adapters/git_console_adapters.py
from __future__ import annotations

"""
Git Console Adapters — pont simple Orchestrator ↔ Git (console)
==============================================================

Rôle
----
Fournir des callbacks compatibles `OrchestrationAdapters` qui appliquent un
`PatchBlock` via Git (commit/push) ou effectuent un rollback local du worktree.
Pensé pour les démos locales/console.

Entrées
-------
- `PatchBlock` validé (balises meta présentes).
- Décision de l'orchestrateur (APPLY/RETRY/ROLLBACK).

Effets
------
- APPLY  → commit Git via `apply_and_commit_git` (affiche le SHA).
- RETRY  → log console des raisons (hook pour régénération ciblée ACW).
- ROLLBACK → rétablit le fichier cible dans le worktree via `rollback_file_changes`.
"""

from core.orchestrator import OrchestrationAdapters
from core.decision_router import Decision, Action, Reasoner
from core.types import PatchBlock
from core.git_adapters import apply_and_commit_git, rollback_file_changes, GitApplyOptions


class GitAdapters(OrchestrationAdapters):
    """
    Adaptateurs Git orientés console.

    Attributes:
        _opts: Options d’application/commit Git (racine, branche, push).
    """

    def __init__(self, *, repo_root: str = ".", branch_name: str = "archcode-self/demo", push: bool = False):
        """
        Initialise l’adaptateur console avec les options Git.

        Args:
            repo_root: Chemin racine du dépôt Git.
            branch_name: Nom de la branche cible.
            push: Si True, effectue aussi un `git push` après le commit.
        """
        self._opts = GitApplyOptions(repo_root=repo_root, branch_name=branch_name, push=push)
        super().__init__(
            apply_and_commit=self._apply_and_commit,
            regenerate_with_acw=self._retry,
            rollback_and_log=self._rollback,
        )

    def _apply_and_commit(self, pb: PatchBlock, decision: Decision) -> None:
        """
        Applique le patch et crée un commit Git (optionnellement push).

        Args:
            pb: PatchBlock à appliquer.
            decision: Décision issue du router (attendu: Action.APPLY).
        """
        sha = apply_and_commit_git(pb, options=self._opts)
        print(f"[GIT] committed {sha} on {self._opts.branch_name}")

    def _retry(self, pb: PatchBlock, decision: Decision, reasoner: Reasoner | None = None) -> None:
        """
        Signale une demande de régénération (placeholder console).

        Args:
            pb: PatchBlock concerné.
            decision: Décision (attendu: Action.RETRY).
            reasoner: Optionnel — permettrait d’enrichir/normaliser les raisons.
        """
        # Ici tu brancheras ton ACW régénération ciblée
        print("[RETRY] raisons:", "; ".join(decision.reasons or []))

    def _rollback(self, pb: PatchBlock, decision: Decision) -> None:
        """
        Rétablit le fichier cible dans le worktree (rollback non versionné).

        Args:
            pb: PatchBlock cible (utilise pb.meta.file).
            decision: Décision (attendu: Action.ROLLBACK).
        """
        if pb.meta.file:
            rollback_file_changes([pb.meta.file], repo_root=self._opts.repo_root)
        print("[ROLLBACK] changes reverted (worktree)")
