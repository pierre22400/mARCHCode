# runner/run_plan.py
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.yaml_io import load_execution_plan
from core.fs_apply import apply_patchblock_to_file as apply_patch
from core.types import PlanLine
from agents.acwp import build_prompt
from agents.acw import run_acw
from agents.agent_file_checker import check_file
from agents.agent_module_checker import check_module
from core.git_diffstats import ensure_branch, stage_and_commit

# Injection SHA best-effort : présent chez toi dans adapters/git_adapter
try:
    from adapters.git_adapter import inject_commit_sha_into_meta  # type: ignore
except Exception:

    def inject_commit_sha_into_meta(pb, sha: Optional[str]) -> None:
        """
        Injecte le SHA de commit dans `pb.meta.commit_sha` si possible (fallback no-op).

        Args:
            pb: PatchBlock (ou objet duck-typed) dont l'attribut `meta` peut contenir `commit_sha`.
            sha: SHA de commit à injecter (ou None pour ne rien faire).

        Returns:
            None. Effet de bord tolérant sur `pb.meta` si l'attribut existe.
        """
        return

from core.archiver import (
    archive_execution_plan,
    archive_patch_before,
    archive_patch_after,
    archive_patch_post_commit,
    append_console_log,
    archive_run_info,
)

"""
===============================================================================
mARCHCode — runner/run_plan.py (MVP Phase 3)
-------------------------------------------------------------------------------
Rôle
- Exécuter un execution_plan YAML : pour chaque PlanLine → ACWP → ACW → checkers
  → apply (FS) → commit Git → archivage (.arch_runs/…).

Entrées
- ep_path : chemin du fichier execution_plan.yaml
- repo_root : racine du dépôt cible (écriture FS + Git)

Sorties & effets
- Fichiers écrits dans repo_root selon pb.meta.file
- Commits Git (si repo initialisé) + SHA injecté dans pb.meta (best-effort)
- Archives lisibles dans .arch_runs/<timestamp>/
  - plan.yaml, patch_before.yaml, patch_after.yaml, post_commit.yaml, console.log

Notes
- Pas de “init_run_dir” dans core.archiver : on crée le dossier nous-mêmes (os.makedirs).
- apply_patch : alias de core.fs_apply.apply_patchblock_to_file (signature locale).
- Robustesse : best-effort sur Git (le run continue même si Git est absent).
===============================================================================
"""


def run_plan(ep_path: str, repo_root: str, *, archive_dir: Optional[str] = None) -> None:
    """
    Exécute un plan d’exécution mARCHCode de bout en bout (MVP local).

    Pipeline:
        1) Archive les métadonnées de run et le plan source.
        2) Charge l'ExecutionPlan typé.
        3) (Best-effort) prépare la branche Git de travail.
        4) Pour chaque PlanLine:
            - ACWP → prompt
            - ACW → PatchBlock
            - Checkers fichier & module
            - (si OK) apply FS + commit (best-effort) + archivage post-commit

    Args:
        ep_path: Chemin vers le fichier `execution_plan.yaml`.
        repo_root: Racine du dépôt cible (chemins d'écriture/commit relatifs).
        archive_dir: Dossier des artefacts du run (créé si absent). Si None,
            un dossier `.arch_runs/<timestamp>` sera créé.

    Returns:
        None. Les effets se matérialisent sur le FS, Git (si dispo) et dans `archive_dir`.

    Raises:
        FileNotFoundError: si `ep_path` est introuvable (via chargeur).
        ValueError: si la structure du plan est invalide (via chargeur).
        Toute autre exception est capturée localement lors des étapes best-effort
        (ex. Git non initialisé) pour permettre la poursuite du run.
    """
    # --- Prépare le répertoire d’archives du run ---
    if not archive_dir:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_dir = f".arch_runs/{ts}"
    os.makedirs(archive_dir, exist_ok=True)
    archive_run_info(archive_dir, started_at=datetime.now().isoformat(timespec="seconds"))
    append_console_log(f"[arch] start run, archive_dir={archive_dir}", run_dir=archive_dir)

    # Archive le plan source (copie brute)
    try:
        ep_text = Path(ep_path).read_text(encoding="utf-8")
    except Exception as e:
        ep_text = f"# [warn] impossible de lire {ep_path}: {e}"
    archive_execution_plan(ep_text, run_dir=archive_dir)

    # Charge le plan typé
    ep = load_execution_plan(ep_path)
    print(f"[ExecutionPlan] → {ep.execution_plan_id}")
    append_console_log(f"[plan] id={ep.execution_plan_id}", run_dir=archive_dir)

    # Branche de travail (si Git est initialisé)
    try:
        # NOTE: impl réelle dans core.git_diffstats ; appel conservé (best-effort).
        ensure_branch(repo_root=repo_root)  # type: ignore[call-arg]
        print("• Branche de travail prête (archcode-self/… ou équivalent)")
        append_console_log("[git] ensure_branch ok", run_dir=archive_dir)
    except Exception as e:
        print("• Git indisponible — on continue sans commit (MVP)")
        append_console_log(f"[git] ensure_branch skipped: {e}", run_dir=archive_dir)

    # Contexte d’exécution : écrire depuis repo_root
    prev_cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        for mod in ep.modules:
            module_name = mod.get("module", "unknown")
            plan_lines = mod.get("plan_lines", [])
            print(f"→ Module: {module_name} ({len(plan_lines)} plan_lines)")
            append_console_log(f"[module] {module_name} ({len(plan_lines)})", run_dir=archive_dir)

            for pl_data in plan_lines:
                pl = PlanLine(**pl_data)
                print(f"  • PlanLine {pl.plan_line_id} ({pl.file})")
                append_console_log(f"[plan_line] {pl.plan_line_id} file={pl.file}", run_dir=archive_dir)

                # ACWP → prompt
                prompt = build_prompt(pl)

                # ACW : PatchBlock
                pb = run_acw(pl, prompt)
                archive_patch_before(pb, run_dir=archive_dir)

                # Checkers
                pb = check_file(pb)
                pb = check_module(pb)
                archive_patch_after(pb, run_dir=archive_dir)

                if pb.global_status == "ok":
                    # Applique le patch sur FS (signature locale : pb → (path, count))
                    try:
                        apply_patch(pb)
                        append_console_log("[apply] file written", run_dir=archive_dir)
                    except Exception as e:
                        print(f"    → APPLY FAILED: {e}")
                        append_console_log(f"[apply] failed: {e}", run_dir=archive_dir)
                        break

                    # Commit Git (best-effort)
                    message = f"feat(mARCH): {pl.plan_line_id} {pl.role} {pl.target_symbol} (status={pb.global_status})"
                    try:
                        sha = stage_and_commit([pb.meta.file], message, repo_root=repo_root)  # type: ignore[arg-type]
                        inject_commit_sha_into_meta(pb, sha)
                        archive_patch_post_commit(pb, run_dir=archive_dir)
                        short = (sha or "")[:7] if sha else "∅"
                        print(f"    → OK: fichier écrit & commit {short}")
                        append_console_log(f"[git] commit {sha}", run_dir=archive_dir)
                    except Exception as e:
                        print(f"    → OK: fichier écrit (commit non effectué: {e})")
                        append_console_log(f"[git] commit skipped: {e}", run_dir=archive_dir)
                else:
                    reason = pb.error_trace or "module checker"
                    print(f"    → REJECTED: {reason}")
                    append_console_log(f"[reject] {pl.plan_line_id}: {reason}", run_dir=archive_dir)
                    break
    finally:
        os.chdir(prev_cwd)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exécute un execution_plan YAML (Phase 3 mARCHCode)")
    ap.add_argument("--ep", required=True, help="Chemin vers execution_plan.yaml")
    ap.add_argument("--repo", default=".", help="Racine du repo (où écrire/committer)")
    ap.add_argument(
        "--archive-dir",
        default=None,
        help="Dossier d’archive des artefacts (défaut: .arch_runs/<timestamp>)"
    )
    args = ap.parse_args()
    run_plan(args.ep, args.repo, archive_dir=args.archive_dir)
