# cli/main.py
from __future__ import annotations

import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import typer

from core.yaml_io import load_execution_plan
from core.types import PlanLine
from agents.acwp import build_prompt
from agents.acw import run_acw
from agents.agent_file_checker import check_file
from agents.agent_module_checker import check_module

# Runner (écritures + git + archivage)
from runner.run_plan import run_plan

"""
============================================================
ARCHCode CLI — mARCHCode (Phase 3 MVP)
============================================================

Description
-----------
Interface en ligne de commande pour piloter mARCHCode
pendant la Phase 3 (exécution d’un execution_plan existant).

Commandes (pipeline)
--------------------
  - validate-ep : vérifie la validité structurelle et fonctionnelle
    d’un fichier execution_plan.yaml (invariants mARCHCode).
  - dry-run     : exécute ACWP → ACW → checkers sans écrire sur le
    filesystem ni interagir avec Git, pour tester le pipeline.
  - run         : exécute le pipeline complet avec écritures, commits
    Git et archivage, selon l’execution_plan fourni.

Commandes (gouvernance Git)
---------------------------
  - tag-green        : fabrique l’archive post-commit et crée/pousse
    un tag green-<YYYYMMDD>-<shortsha> (cf. docs/BRANCHING.md & ROLLBACK.md).
  - rollback-green   : revient au dernier état green (merge par défaut,
    reset possible). Restaure aussi les artefacts de l’archive.

Philosophie
-----------
Centraliser les opérations clés dans un CLI unique (Typer),
réduire la friction d’usage, et préparer l’extension progressive
(LLM réels, CI/CD, PR automatiques, etc.).

Entrées attendues
-----------------
- Fichier execution_plan.yaml conforme au contrat mARCHCode.
- Repo Git initialisé (pour la commande run avec commits).

Sorties
-------
- Codes de sortie non-nuls en cas d’erreurs.
- Journaux/artefacts dans le dossier d’archives choisi (ou défaut).

Limites MVP
-----------
- Dépendances inter-PlanLines non résolues automatiquement.
- Écriture “full file” côté adaptateur Git (pas d’insertion par marqueurs).
- Pas de rollback automatique sur run complet (runner gère l’heureux chemin).

Sécurité / Garde-fous (Git)
---------------------------
- 'tag-green' suppose build/tests OK (CI verte).
- 'rollback-green' refuse d’opérer si l’arbre de travail n’est pas clean,
  sauf option --no-clean-check (exposée par le script appelé).


============================================================
"""

app = typer.Typer(add_completion=False, help="ARCHCode CLI — mARCHCode (Phase 3 MVP)")

# --- helpers -------------------------------------------------

_ALLOWED_OP = {"create", "modify"}
_ALLOWED_ROLE = {
    "route_handler",
    "service",
    "repo",
    "dto",
    "test",
    "data_accessor",
    "interface",
}


def _validate_plan_line_dict(pl: Dict) -> List[str]:
    """Validation déterministe des invariants mARCHCode (exécutée dans `validate-ep`)."""
    errs: List[str] = []
    file = str(pl.get("file", ""))
    op = str(pl.get("op", ""))
    role = str(pl.get("role", ""))
    target_symbol = str(pl.get("target_symbol", ""))
    signature = str(pl.get("signature", ""))
    acceptance = pl.get("acceptance", [])
    constraints = pl.get("constraints", {})

    if not file or not file.endswith(".py"):
        errs.append("file doit cibler un .py")
    if op not in _ALLOWED_OP:
        errs.append(f"op invalide (attendus: {sorted(_ALLOWED_OP)})")
    if role not in _ALLOWED_ROLE:
        errs.append(f"role invalide (attendus: {sorted(_ALLOWED_ROLE)})")
    if not target_symbol:
        errs.append("target_symbol requis")
    if not signature or not signature.startswith("def "):
        errs.append("signature requise et doit commencer par 'def '")
    if not isinstance(acceptance, list) or len(acceptance) < 1:
        errs.append("acceptance doit contenir au moins 1 assertion")
    if not isinstance(constraints, dict):
        errs.append("constraints doit être un mapping (dict)")

    return errs


def _summary_counts(ep) -> Tuple[int, int]:
    modules = len(ep.modules or [])
    pl_count = sum(len(m.get("plan_lines", [])) for m in ep.modules or [])
    return modules, pl_count


def _repo_root_from_cli_file() -> Path:
    """
    Déduit la racine du repo depuis ce fichier (…/cli/main.py → repo_root).
    Utile pour localiser robustement les scripts/ même sans package Python.
    """
    return Path(__file__).resolve().parents[1]


def _run_script_via_python(script_relpath: str, args: List[str]) -> int:
    """
    Exécute un script Python en sous-processus :
      sys.executable <repo_root>/<script_relpath> <args…>
    Renvoie le code de sortie.
    """
    repo_root = _repo_root_from_cli_file()
    script_path = (repo_root / script_relpath).resolve()
    if not script_path.exists():
        typer.secho(f"[ERREUR] Script introuvable: {script_path}", fg=typer.colors.RED)
        return 2
    cmd = [sys.executable, str(script_path), *args]
    proc = subprocess.run(cmd)
    return proc.returncode


# --- commands (pipeline) ------------------------------------

@app.command("validate-ep")
def cmd_validate_ep(
    ep: Path = typer.Option(..., "--ep", help="Chemin du execution_plan.yaml"),
) -> None:
    """
    Valide un execution_plan.yaml (invariants mARCHCode).
    Code retour ≠ 0 si erreurs.
    """
    try:
        ep_obj = load_execution_plan(ep)
    except Exception as e:
        typer.secho(f"[validate-ep] YAML invalide: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    mods, total = _summary_counts(ep_obj)
    errors: List[str] = []
    for mod in (ep_obj.modules or []):
        module_name = mod.get("module", "unknown")
        for pl in mod.get("plan_lines", []):
            pl_id = pl.get("plan_line_id", "EP-UNKNOWN")
            errs = _validate_plan_line_dict(pl)
            if errs:
                prefix = f"[{module_name}:{pl_id}] "
                errors.extend(prefix + e for e in errs)
            try:
                PlanLine(**pl)
            except Exception as e:
                errors.append(f"[{module_name}:{pl_id}] dataclass PlanLine: {e}")

    if errors:
        typer.secho("❌ execution_plan invalide :", fg=typer.colors.RED)
        for e in errors:
            typer.echo("  - " + e)
        raise typer.Exit(code=1)

    typer.secho(
        f"✅ execution_plan OK — modules={mods}, plan_lines={total}",
        fg=typer.colors.GREEN,
    )


@app.command("dry-run")
def cmd_dry_run(
    ep: Path = typer.Option(..., "--ep", help="Chemin du execution_plan.yaml"),
) -> None:
    """
    Exécute ACWP → ACW → checkers sans écrire sur le FS ni faire de commit Git.
    Affiche un petit rapport et retourne code ≠ 0 si une PlanLine échoue.
    """
    try:
        ep_obj = load_execution_plan(ep)
    except Exception as e:
        typer.secho(f"[dry-run] YAML invalide: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    ok_count = 0
    rej_count = 0

    for mod in (ep_obj.modules or []):
        module_name = mod.get("module", "unknown")
        plan_lines = mod.get("plan_lines", [])
        typer.secho(f"→ Module {module_name} ({len(plan_lines)} plan_lines)", fg=typer.colors.BLUE)

        for pld in plan_lines:
            pl = PlanLine(**pld)
            typer.echo(f"  • {pl.plan_line_id} → {pl.file}")
            prompt = build_prompt(pl)
            pb = run_acw(pl, prompt)
            pb = check_file(pb)
            pb = check_module(pb)
            status = pb.global_status or "pending"
            if status == "ok":
                ok_count += 1
                typer.secho("    ✓ OK", fg=typer.colors.GREEN)
            else:
                rej_count += 1
                reason = pb.error_trace or "module checker"
                typer.secho(f"    ✗ REJECTED: {reason}", fg=typer.colors.RED)

    typer.echo(f"\nRésumé: ok={ok_count}, rejected={rej_count}")
    raise typer.Exit(code=0 if rej_count == 0 else 1)


@app.command("run")
def cmd_run(
    ep: Path = typer.Option(..., "--ep", help="Chemin du execution_plan.yaml"),
    repo: Path = typer.Option(".", "--repo", help="Racine du repo cible"),
    archive_dir: str | None = typer.Option(
        None,
        "--archive-dir",
        help="Dossier d'archives (.arch_runs/<timestamp> par défaut si omis)",
    ),
) -> None:
    """
    Exécute le pipeline complet (écritures + commit si Git présent).
    Wrap léger autour de runner.run_plan.
    """
    if archive_dir is None:
        archive_dir = f".arch_runs/{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        run_plan(str(ep), str(repo), archive_dir=archive_dir)  # type: ignore[arg-type]
    except TypeError:
        run_plan(str(ep), str(repo))  # type: ignore[arg-type]
    except Exception as e:
        typer.secho(f"[run] échec: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


# --- commands (gouvernance Git) ------------------------------

@app.command("tag-green")
def cmd_tag_green() -> None:
    """
    Crée l’archive post-commit et pousse un tag green-<YYYYMMDD>-<shortsha>.
    S’aligne sur docs/BRANCHING.md et docs/ROLLBACK.md.
    """
    code = _run_script_via_python("scripts/green_tag.py", args=[])
    raise typer.Exit(code=code)


@app.command("rollback-green")
def cmd_rollback_green(
    strategy: str = typer.Option(
        "merge",
        "--strategy",
        "-s",
        help="Stratégie de retour : 'merge' (par défaut) ou 'reset'.",
        case_sensitive=False,
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Simulation : affiche les actions sans exécuter."
    ),
    no_clean_check: bool = typer.Option(
        False,
        "--no-clean-check",
        help="Ignorer la vérification 'working tree clean' (dangereux).",
    ),
) -> None:
    """
    Revient au dernier commit 'green' (merge par défaut, ou reset).
    Lit metadata_<shortsha>.yaml si dispo, vérifie l’archive, restaure les artefacts.
    """
    argv: List[str] = []
    if strategy.lower() not in {"merge", "reset"}:
        typer.secho("[ERREUR] --strategy doit être 'merge' ou 'reset'.", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    argv += ["--strategy", strategy.lower()]
    if dry_run:
        argv.append("--dry-run")
    if no_clean_check:
        argv.append("--no-clean-check")

    code = _run_script_via_python("scripts/rollback_to_last_green.py", args=argv)
    raise typer.Exit(code=code)


# --- entry point --------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
