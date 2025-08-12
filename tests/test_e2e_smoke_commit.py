# tests/test_e2e_smoke_commit.py
from __future__ import annotations

"""
mARCHCode — E2E Smoke (commit Git réel)
======================================

Ce test crée un repo Git temporaire, fabrique un PatchBlock minimal (avec balises),
passe par run_patch_local avec des adaptateurs "réels" (écriture + commit),
et vérifie qu'un commit SHA existe après l'exécution.

Exécution :
  pytest -s tests/test_e2e_smoke_commit.py
"""

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

from core.orchestrator import (
    run_patch_local,
    OrchestrationAdapters,
    Decision,
    Action,
    Reasoner,
)
from core.types import PatchBlock, MetaBlock  # respecte exactement les noms/casse
from core.decision_router import Decision as DRDecision  # typage assert


# ------------------------------- utils git -------------------------------

def _run_git(args, cwd: Path) -> Tuple[int, str, str]:
    p = subprocess.Popen(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = p.communicate()
    return p.returncode, out.strip(), err.strip()


def _init_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="arch_e2e_"))
    rc, _, err = _run_git(["init", "."], cwd=repo)
    if rc != 0:
        raise RuntimeError(f"git init failed: {err}")
    # config minimale pour commit
    _run_git(["config", "user.name", "arch-e2e"], cwd=repo)
    _run_git(["config", "user.email", "arch-e2e@example.com"], cwd=repo)
    # commit initial vide (facilite diff)
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _run_git(["add", ".gitkeep"], cwd=repo)
    _run_git(["commit", "-m", "init"], cwd=repo)
    return repo


# --------------- adaptateurs réels (écriture + commit git) ---------------

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
class RealGitAdapters(OrchestrationAdapters):
    repo_root: Path

    def __init__(self, repo_root: Path) -> None:
        def _apply_and_commit(pb: PatchBlock, decision: Decision) -> None:
            # Écrit le patch complet tel quel (MVP full-write) puis commit.
            rel = pb.meta.file
            if not rel:
                raise ValueError("meta.file requis")
            target = self.repo_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(pb.code, encoding="utf-8")
            rc, _, err = _run_git(["add", rel], cwd=self.repo_root)
            if rc != 0:
                raise RuntimeError(f"git add failed: {err}")
            rc, sha, err = _run_git(["commit", "-m", f"e2e: {pb.meta.plan_line_id or 'PL-?'}"], cwd=self.repo_root)
            if rc != 0:
                raise RuntimeError(f"git commit failed: {err}")
            # on place le sha dans pb.meta.commit_sha si disponible
            if hasattr(pb.meta, "commit_sha"):
                setattr(pb.meta, "commit_sha", _last_commit_sha(self.repo_root))

        def _retry(pb: PatchBlock, decision: Decision, reasoner: Optional[Reasoner] = None) -> None:
            # Pour un smoke, on n'implémente pas la régénération → no-op
            pass

        def _rollback(pb: PatchBlock, decision: Decision) -> None:
            # Pour un smoke, simple no-op + écriture d’un marqueur
            (self.repo_root / ".rollback_marker").write_text("rollback", encoding="utf-8")

        super().__init__(
            apply_and_commit=_apply_and_commit,
            regenerate_with_acw=_retry,
            rollback_and_log=_rollback,
        )
        self.repo_root = repo_root


def _last_commit_sha(repo_root: Path) -> str:
    rc, out, err = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if rc != 0:
        raise RuntimeError(f"rev-parse failed: {err}")
    return out.strip()


# ------------------------------ fabrique PB ------------------------------

def _make_minimal_pb() -> PatchBlock:
    """
    Construit un PatchBlock avec balises begin_meta/end_meta et une petite fonction.
    FileChecker/ModuleChecker MVP acceptent la présence des balises + 'def '.
    """
    meta_inline = (
        "#{begin_meta: { file: demo/hello.py, module: demo, role: utility, "
        "plan_line_id: PL-0001, status_agent_file_checker: pending, "
        "status_agent_module_checker: pending }}\n"
    )
    body = (
        "def hello(name: str) -> str:\n"
        "    \"\"\"mARCHCode/ACW\n"
        "    Rôle: utility\n"
        "    Acceptance (rappel):\n"
        "    - retourne une salutation\n"
        "    \"\"\"\n"
        "    return f\"Hello {name}!\"\n"
    )
    code = f"{meta_inline}{body}#{'{'}end_meta{'}'}\n"

    meta = MetaBlock(
        file="demo/hello.py",
        module="demo",
        role="utility",
        plan_line_id="PL-0001",
    )
    pb = PatchBlock(
        code=code,
        meta=meta,
        global_status=None,
        next_action=None,
        source_agent="e2e_test",
    )
    return pb


# --------------------------------- test ----------------------------------

def test_e2e_smoke_commit():
    repo = _init_repo()
    adapters = RealGitAdapters(repo_root=repo)
    pb = _make_minimal_pb()

    # Run end-to-end (Phase 3)
    pb_out, decision = run_patch_local(
        pb,
        adapters,
        policy=None,
        diff_stats=None,
        branch_name=None,
        archive_dir=None,
    )

    # Sanity assertions
    assert isinstance(decision, DRDecision)
    assert decision.action in (Action.APPLY, Action.RETRY, Action.ROLLBACK)

    # On attend APPLY dans ce smoke (balises + def)
    assert decision.action == Action.APPLY, f"action={decision.action}, summary={decision.summary}"

    # Le fichier doit exister et être committé
    target = repo / pb.meta.file
    assert target.exists(), f"file not found: {target}"
    sha = _last_commit_sha(repo)
    assert len(sha) >= 7, "commit SHA manquant ou invalide"

