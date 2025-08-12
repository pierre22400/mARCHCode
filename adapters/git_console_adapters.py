# adapters/git_console_adapters.py
from __future__ import annotations

from core.orchestrator import OrchestrationAdapters
from core.decision_router import Decision, Action, Reasoner
from core.types import PatchBlock
from core.git_adapters import apply_and_commit_git, rollback_file_changes, GitApplyOptions

class GitAdapters(OrchestrationAdapters):
    def __init__(self, *, repo_root: str = ".", branch_name: str = "archcode-self/demo", push: bool = False):
        self._opts = GitApplyOptions(repo_root=repo_root, branch_name=branch_name, push=push)
        super().__init__(
            apply_and_commit=self._apply_and_commit,
            regenerate_with_acw=self._retry,
            rollback_and_log=self._rollback,
        )

    def _apply_and_commit(self, pb: PatchBlock, decision: Decision) -> None:
        sha = apply_and_commit_git(pb, options=self._opts)
        print(f"[GIT] committed {sha} on {self._opts.branch_name}")

    def _retry(self, pb: PatchBlock, decision: Decision, reasoner: Reasoner | None = None) -> None:
        # Ici tu brancheras ton ACW régénération ciblée
        print("[RETRY] raisons:", "; ".join(decision.reasons or []))

    def _rollback(self, pb: PatchBlock, decision: Decision) -> None:
        if pb.meta.file:
            rollback_file_changes([pb.meta.file], repo_root=self._opts.repo_root)
        print("[ROLLBACK] changes reverted (worktree)")

