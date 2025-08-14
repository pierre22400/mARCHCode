#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Démonstration SELF-DEV — ARCHCode modifie *son propre repo* (zone non critique)
-------------------------------------------------------------------------------

Usage:
  python demos/demo_selfdev.py --repo . [--branch archcode-self/selfdev] [--push]

Comportement:
  - Forge une PlanLine *minuscule* (création d'une fonction dans arch_sandbox/selfdev_notes.py)
  - ACWP (writer_task) → ACW (PatchBlock) → checkers (via orchestrateur)
  - Commit sur branche clone archcode-self/<...>, push optionnel
  - Archive sous .arch_runs/selfdev-<timestamp>
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from core.types import PlanLine, PatchBlock
from core.orchestrator import OrchestrationAdapters, run_patch_local
from core.self_dev_policy import SelfDevPolicy
from core.git_diffstats import compute_diffstats_for_paths, DiffStatsData
from core.archiver import init_run_dir, append_console_log
from agents.agent_code_writer_planner import build_writer_task
from agents.agent_code_writer import write_code

from adapters.git_adapter import (
    GitApplyOptions,
    apply_and_commit_git,
    rollback_file_changes,
)

# Adaptateurs orchestrateur (Git)
_APPLY_CTX: Dict[str, Any] = {}


def _apply_and_commit_git(pb: PatchBlock, decision) -> None:
    """Applique le patch via Git: assure la branche, écrit le fichier, commit et (optionnellement) push."""
    repo_root = _APPLY_CTX["repo_root"]
    branch = _APPLY_CTX["branch"]
    push = _APPLY_CTX["push"]
    options = GitApplyOptions(repo_root=repo_root, branch_name=branch, push=push)
    sha = apply_and_commit_git(pb, options=options)
    print(f"[git] commit {sha[:7]} on {branch}")


def _regenerate_with_acw(pb: PatchBlock, decision, reasoner=None) -> None:
    """Journalise une demande de régénération ciblée (mode démo, sans déclencher ACW)."""
    print("[retry] (self-dev) targeted regeneration requested")
    if decision.reasons:
        print("  reasons:", "; ".join(decision.reasons))


def _rollback_and_log(pb: PatchBlock, decision) -> None:
    """Annule les changements non commités pour le fichier cible et trace l’exclusion du patch."""
    repo_root = _APPLY_CTX["repo_root"]
    target = getattr(pb.meta, "file", None)
    if target:
        try:
            rollback_file_changes([target], repo_root=repo_root)
        except Exception as e:
            print(f"[rollback] non-bloquant: {e}")
    print("[rollback] patch excluded")


def main() -> None:
    """Point d’entrée CLI: prépare le contexte, génère un petit patch sandbox et lance l’orchestrateur."""
    ap = argparse.ArgumentParser(description="Demo SELF-DEV: ARCHCode modifie un fichier sandbox de son propre repo")
    ap.add_argument("--repo", default=".", help="Racine du repo")
    ap.add_argument("--branch", default=None, help="Branche clone (ex: archcode-self/selfdev)")
    ap.add_argument("--push", action="store_true", help="Push après commit")
    ap.add_argument("--policy", default="self_dev_policy.yaml", help="Policy (optionnelle)")
    args = ap.parse_args()

    repo_root = str(Path(args.repo).resolve())
    branch = args.branch or f"archcode-self/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    _APPLY_CTX.update(repo_root=repo_root, branch=branch, push=bool(args.push))

    # Archive dir
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = f".arch_runs/selfdev-{ts}"
    init_run_dir(run_dir)
    append_console_log(f"[demo-selfdev] repo={repo_root} branch={branch}", run_dir=run_dir)

    # PlanLine minuscule (non critique) → zone sandbox
    pl = PlanLine(
        plan_line_id="PL-SD-0001",
        file="arch_sandbox/selfdev_notes.py",
        op="create",
        role="service",
        target_symbol="hello_arch",
        signature="def hello_arch(name: str) -> str:",
        description="Tiny helper pour démos self-dev",
        acceptance=[
            "Retourne une chaîne contenant name",
            "Aucune I/O externe",
        ],
        constraints={
            "style": "pep8",
            "typing": "strict",
            "docstring": "google",
            "error_handling": "no bare except",
        },
        allow_create=True,
    )

    # ACWP → writer_task
    task = build_writer_task(pl)

    # ACW → PatchBlock
    pb = write_code(task)

    # diffstats (avant APPLY)
    try:
        diff_stats: DiffStatsData = compute_diffstats_for_paths([pl.file], repo_root=repo_root)  # type: ignore
    except Exception:
        diff_stats = None  # type: ignore

    # Policy (optionnelle)
    policy: Optional[SelfDevPolicy] = None
    try:
        policy = SelfDevPolicy.load_from_file(args.policy)
    except Exception:
        # Silencieux en démo si la policy est absente/illisible
        pass

    # Orchestrateur
    adapters = OrchestrationAdapters(
        apply_and_commit=_apply_and_commit_git,
        regenerate_with_acw=_regenerate_with_acw,
        rollback_and_log=_rollback_and_log,
    )

    pb, decision = run_patch_local(
        pb,
        adapters,
        policy=policy,
        diff_stats=diff_stats,
        branch_name=branch,
        partial_ok_count_so_far=0,
        archive_dir=run_dir,
    )

    print("[demo-selfdev] terminé.")


if __name__ == "__main__":
    main()
