#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Démonstration EXTERNE — plan manuel → ACWP → ACW → checkers → orchestrateur
----------------------------------------------------------------------------

Usage:
  python demos/demo_external.py --ep execution_plan.yaml --repo . [--branch archcode-self/demo] [--push]

Comportement:
  - Lit un execution_plan.yaml déjà rédigé à la main
  - Pour chaque PlanLine: ACWP (writer_task) → ACW (PatchBlock) → checkers (dans l'orchestrateur)
  - Si APPLY et si Git est disponible: crée/checkout la branche, écrit le fichier, commit, push (optionnel)
  - Archive les artefacts du run sous .arch_runs/<timestamp> (patch avant/après, décision, post-commit)
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from core.types import PlanLine, PatchBlock
from core.orchestrator import (
    OrchestrationAdapters,
    run_patch_local,
)
from core.self_dev_policy import SelfDevPolicy
from core.git_diffstats import compute_diffstats_for_paths, DiffStatsData
from core.archiver import init_run_dir, archive_execution_plan_text, append_console_log
from agents.agent_code_writer_planner import plan_to_writer_tasks
from agents.agent_code_writer import write_code

# --- adaptateurs Git (écriture + commit) ---
from adapters.git_adapter import (
    GitApplyOptions,
    apply_and_commit_git,
    rollback_file_changes,
)


# Orchestrateur → adaptateurs concrets (Git)
def _apply_and_commit_git(pb: PatchBlock, decision) -> None:
    """Applique le patch sur le repo, crée/assure la branche, commit et (optionnellement) push."""
    repo_root = _APPLY_CTX["repo_root"]
    branch = _APPLY_CTX["branch"]
    push = _APPLY_CTX["push"]
    options = GitApplyOptions(repo_root=repo_root, branch_name=branch, push=push)
    sha = apply_and_commit_git(pb, options=options)
    print(f"[git] commit {sha[:7]} on {branch}")


def _regenerate_with_acw(pb: PatchBlock, decision, reasoner=None) -> None:
    """Journalise une demande de régénération ciblée (démo console)."""
    print("[retry] targeted regeneration requested")
    if decision.reasons:
        print("  reasons:", "; ".join(decision.reasons))


def _rollback_and_log(pb: PatchBlock, decision) -> None:
    """Annule les changements du fichier cible dans la worktree et logge l’exclusion du patch."""
    repo_root = _APPLY_CTX["repo_root"]
    target = getattr(pb.meta, "file", None)
    if target:
        try:
            rollback_file_changes([target], repo_root=repo_root)
        except Exception as e:
            print(f"[rollback] non-bloquant: {e}")
    print("[rollback] patch excluded")


_APPLY_CTX: Dict[str, Any] = {}


def main() -> None:
    """Point d’entrée CLI de la démo externe (parse args, boucle plan→writer→checkers→orchestrateur)."""
    ap = argparse.ArgumentParser(description="Demo EXTERNE: plan → ACWP → ACW → checkers → orchestrateur")
    ap.add_argument("--ep", required=True, help="Chemin vers execution_plan.yaml (manuel)")
    ap.add_argument("--repo", default=".", help="Racine du repo (où écrire/committer)")
    ap.add_argument("--branch", default=None, help="Nom de branche clone (ex: archcode-self/demo)")
    ap.add_argument("--push", action="store_true", help="Push après commit")
    ap.add_argument("--policy", default="self_dev_policy.yaml", help="Chemin de la policy (optionnel)")
    args = ap.parse_args()

    repo_root = str(Path(args.repo).resolve())
    branch = args.branch or f"archcode-self/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    _APPLY_CTX.update(repo_root=repo_root, branch=branch, push=bool(args.push))

    # Archive Dir
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = f".arch_runs/external-{ts}"
    init_run_dir(run_dir)

    # Charge le plan (texte pour archive + parse via ton loader)
    ep_text = Path(args.ep).read_text(encoding="utf-8")
    archive_execution_plan_text(ep_text, run_dir=run_dir)
    append_console_log(f"[demo-external] repo={repo_root} branch={branch}", run_dir=run_dir)

    # Parse YAML → objets PlanLine (via ton chargeur/DTO si tu préfères)
    import yaml
    ep = yaml.safe_load(ep_text)
    modules = ep.get("modules", [])

    # Policy (optionnelle)
    policy: Optional[SelfDevPolicy] = None
    try:
        policy = SelfDevPolicy.load_from_file(args.policy)
    except Exception:
        # Silencieux en démo : pas de policy stricte si le chargement échoue
        pass

    # Adapters orchestrateur
    adapters = OrchestrationAdapters(
        apply_and_commit=_apply_and_commit_git,
        regenerate_with_acw=_regenerate_with_acw,
        rollback_and_log=_rollback_and_log,
    )

    # Boucle sur modules/plan_lines
    partial_ok_count = 0
    for mod in modules:
        plan_lines = mod.get("plan_lines", [])
        # ACWP: transforme toutes les PlanLine du module en writer_tasks
        tasks = plan_to_writer_tasks([PlanLine(**pl) for pl in plan_lines])

        for t in tasks:
            # ACW: produit PatchBlock
            pb = write_code(t)

            # diffstats pour gate policy (facultatif: avant apply on recalcule par fichier)
            target_file = t.get("file")
            try:
                diff_stats: DiffStatsData = compute_diffstats_for_paths([target_file], repo_root=repo_root)  # type: ignore
            except Exception:
                diff_stats = None  # type: ignore

            # Orchestrateur: vérif+route+action (+ archivage interne si demandé)
            pb, decision = run_patch_local(
                pb,
                adapters,
                policy=policy,
                diff_stats=diff_stats,
                branch_name=branch,
                partial_ok_count_so_far=partial_ok_count,
                archive_dir=run_dir,
            )

            if (pb.global_status or "") == "partial_ok":
                partial_ok_count += 1

    print("[demo-external] terminé.")


if __name__ == "__main__":
    main()
