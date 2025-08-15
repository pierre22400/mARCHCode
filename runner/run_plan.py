# runner/run_plan.py
from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
import sys
import importlib

# Forcer la racine du repo en tête du PYTHONPATH (évite le conflit avec un paquet "agents" tiers)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


"""
===============================================================================
mARCHCode — runner/run_plan.py (Fusion 'bridge local' + 'runner complet')
-------------------------------------------------------------------------------
Rôle
- Lire un execution_plan (Phase 2) au format "lines" (transformer) **ou**
  un plan typé "modules/plan_lines" (format historique).
- Construire des PlanLine minimales conformes aux attentes d'ACWP.
- Deux modes :
    • --dry-run : ACWP → ACW → écrit les patchs dans .archcode/patches (pas d'effets FS)
    • mode normal : ACWP → ACW → checkers → apply FS → commit Git → archives

Entrées
- --ep         : chemin du execution_plan.yaml
- --repo       : racine du dépôt cible (pour apply/commit)
- --archive-dir: dossier d’archives (si absent → .arch_runs/<timestamp>)
- --dry-run    : désactive checkers/apply/commit/archives, n’émet que les patchs
- --patch-dir  : dossier de sortie des patchs (défaut .archcode/patches)

Notes
- Tente d’importer les utilitaires (archiver, git, fs_apply). S’il manque quelque chose,
  le runner continue en best-effort (ou échoue proprement selon le mode).
===============================================================================
"""

# -----------------------------------------------------------------------------#
# Utils import
# -----------------------------------------------------------------------------#

def _import_first(candidates: List[str]):
    """Importe le premier module disponible dans `candidates`."""
    last_err: Optional[BaseException] = None
    for name in candidates:
        try:
            return importlib.import_module(name)
        except BaseException as e:
            last_err = e
            continue
    raise ModuleNotFoundError(f"Impossible d'importer l'un de {candidates}: {last_err}")

# -----------------------------------------------------------------------------#
# Utils YAML / FS
# -----------------------------------------------------------------------------#

def _read_yaml(path: Path) -> Dict[str, Any]:
    """Charge un YAML en dict ({} si vide)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def _ensure_dir(p: Path) -> None:
    """Crée le répertoire p si nécessaire (parents inclus)."""
    p.mkdir(parents=True, exist_ok=True)


def _posix(s: str) -> str:
    """Normalise un chemin en séparateurs '/' (utile pour les metas/patchs)."""
    return s.replace("\\", "/")


# -----------------------------------------------------------------------------#
# Shims & heuristiques PlanLine
# -----------------------------------------------------------------------------#

@dataclass
class SimplePlanLine:
    """
    Duck-typed PlanLine minimale pour ACWP._validate_plan_line().
    Champs requis :
      - plan_line_id, file (.py), op ∈ {'create','modify'}, role, target_symbol, signature, acceptance[]
    """
    plan_line_id: str
    file: str
    op: str
    role: str
    target_symbol: str
    signature: str
    acceptance: List[str]

    # champs optionnels
    path: Optional[str] = None
    description: Optional[str] = None
    depends_on: Optional[List[str]] = None
    constraints: Optional[Dict[str, Any]] = None
    allow_create: bool = True
    markers: Optional[Dict[str, str]] = None
    plan_line_ref: Optional[str] = None
    intent_fingerprint: Optional[str] = None


def _safe_ident(s: str, default: str = "func") -> str:
    """Convertit une chaîne en identifiant Python simple (a-z, 0-9, _)"""
    base = re.sub(r"[^\w]", "_", (s or "").strip())
    base = re.sub(r"_+", "_", base).strip("_")
    return base or default


def _role_from_hint(role_hint: Optional[str], file_kind: Optional[str]) -> str:
    """Infère un rôle conservateur : 'dto' si demandé, sinon 'function'."""
    rh = (role_hint or "").lower().strip()
    if rh == "dto":
        return "dto"
    return "function"


def _derive_sig_and_symbol(role: str, filename: str) -> tuple[str, str]:
    """
    Déduit un symbole et une signature exécutable minimale depuis le nom de fichier.
    - DTO → def make_<stem>_dto() -> dict:
    - sinon → def <stem>() -> None:
    """
    stem = Path(filename).stem
    ident = _safe_ident(stem, "func")
    if role == "dto":
        symbol = f"make_{ident}_dto"
        sig = f"def {symbol}() -> dict:"
        return sig, symbol
    symbol = ident
    sig = f"def {symbol}() -> None:"
    return sig, symbol


def _ensure_py_target(path: str) -> str:
    """Force une cible .py (si dossier → __init__.py ; si sans suffixe → .py)."""
    if path.endswith(".py"):
        return _posix(path)
    p = Path(path)
    if path.endswith("/") or p.suffix == "":
        return _posix(str(p / "__init__.py"))
    return _posix(str(p.with_suffix(".py")))


# -----------------------------------------------------------------------------#
# Charge/normalise les PlanLines depuis un execution_plan
# -----------------------------------------------------------------------------#

def _from_ep_lines(ep_root: Dict[str, Any]) -> tuple[List[SimplePlanLine], Dict[str, Any]]:
    """
    Lecture du format Phase 2 (scripts.execution_plan_transformer) :
    {
      execution_plan: {
        bus_message_id, loop_iteration, lines: [ { plan_line_id, file_target, role_hint, ... }, ... ]
      }
    }
    """
    ep = ep_root.get("execution_plan") or ep_root
    bus_message_id = ep.get("bus_message_id")
    loop_iteration = ep.get("loop_iteration")
    raw_lines = ep.get("lines") or []
    plan_lines: List[SimplePlanLine] = []

    for ln in raw_lines:
        file_target = str(ln.get("file_target") or "").strip()
        if not file_target:
            continue
        file_posix = _ensure_py_target(file_target)
        role = _role_from_hint(ln.get("role_hint"), ln.get("file_kind"))
        sig, symbol = _derive_sig_and_symbol(role, Path(file_posix).name)

        responsibilities = ln.get("responsibilities") or []
        acceptance = [str(x) for x in responsibilities if str(x).strip()]
        if not acceptance:
            acceptance = [f"fonction {symbol} existe", "fichier Python valide (imports ok)"]

        pl = SimplePlanLine(
            plan_line_id=str(ln.get("plan_line_id") or ""),
            file=file_posix,
            op=("create" if str(ln.get("action") or "create").lower().startswith("create") else "modify"),
            role=role,
            target_symbol=symbol,
            signature=sig,
            acceptance=acceptance,
            path=None,
            description=None,
            depends_on=list(ln.get("depends_on") or []),
            constraints={},
            allow_create=True,
            markers=None,
            plan_line_ref=str(ln.get("plan_line_id") or None),
            intent_fingerprint=None,
        )
        if pl.plan_line_id and pl.file.endswith(".py"):
            plan_lines.append(pl)

    return plan_lines, {"bus_message_id": bus_message_id, "loop_iteration": loop_iteration}


def _from_module_plan(ep_root: Dict[str, Any]) -> tuple[List[SimplePlanLine], Dict[str, Any]]:
    """
    Lecture d’un format historique :
    {
      execution_plan_id, modules: [
        { module: "auth", plan_lines: [ {plan_line_id, file, role, signature, ...}, ... ] }
      ]
    }
    On convertit vers SimplePlanLine en comblant les trous (signature/rôle si manquants).
    """
    bus_message_id = ep_root.get("bus_message_id")
    loop_iteration = ep_root.get("loop_iteration")
    modules = ep_root.get("modules") or []
    plan_lines: List[SimplePlanLine] = []

    for mod in modules:
        for ln in mod.get("plan_lines") or []:
            file_path = _ensure_py_target(str(ln.get("file") or "").strip())
            role = (ln.get("role") or "function").lower()
            if role not in ("dto", "function"):
                role = "function"
            sig = str(ln.get("signature") or "")
            symbol = str(ln.get("target_symbol") or "")
            if not sig or not symbol:
                sig, symbol = _derive_sig_and_symbol(role, Path(file_path).name)

            acc = ln.get("acceptance") or [f"fonction {symbol} existe"]
            pl = SimplePlanLine(
                plan_line_id=str(ln.get("plan_line_id") or ""),
                file=file_path,
                op=("modify" if ln.get("op") == "modify" else "create"),
                role=role,
                target_symbol=symbol,
                signature=sig,
                acceptance=[str(a) for a in acc],
                path=ln.get("path"),
                description=ln.get("description"),
                depends_on=list(ln.get("depends_on") or []),
                constraints=dict(ln.get("constraints") or {}),
                allow_create=bool(ln.get("allow_create", True)),
                markers=dict(ln.get("markers") or {}) or None,
                plan_line_ref=ln.get("plan_line_ref"),
                intent_fingerprint=ln.get("intent_fingerprint"),
            )
            if pl.plan_line_id and pl.file.endswith(".py"):
                plan_lines.append(pl)

    return plan_lines, {"bus_message_id": bus_message_id, "loop_iteration": loop_iteration}


def _load_plan_lines(ep_path: Path) -> tuple[List[SimplePlanLine], Dict[str, Any]]:
    """
    Charge un execution_plan et renvoie (plan_lines, meta).
    Supporte les deux formats (Phase 2 “lines” et format “modules/plan_lines”).
    """
    root = _read_yaml(ep_path)
    # Heuristique : présence d'une clé 'execution_plan' avec 'lines' → format Phase 2
    ep = root.get("execution_plan") or root
    if isinstance(ep.get("lines"), list):
        return _from_ep_lines(root)
    # Sinon : format historique (modules/plan_lines)
    return _from_module_plan(ep)


# -----------------------------------------------------------------------------#
# Archiver / Git / FS (best-effort)
# -----------------------------------------------------------------------------#

# archiver (best-effort)
try:
    from core.archiver import (  # type: ignore
        archive_execution_plan,
        archive_patch_before,
        archive_patch_after,
        archive_patch_post_commit,
        append_console_log,
        archive_run_info,
    )
except Exception:
    def archive_execution_plan(text: str, run_dir: str) -> None: ...
    def archive_patch_before(pb, run_dir: str) -> None: ...
    def archive_patch_after(pb, run_dir: str) -> None: ...
    def archive_patch_post_commit(pb, run_dir: str) -> None: ...
    def append_console_log(msg: str, run_dir: str) -> None: ...
    def archive_run_info(run_dir: str, **kwargs) -> None: ...

# git adapter (best-effort)
try:
    from core.git_diffstats import ensure_branch, stage_and_commit  # type: ignore
except Exception:
    def ensure_branch(*, repo_root: str) -> None: ...
    def stage_and_commit(paths: List[str], message: str, *, repo_root: str) -> Optional[str]:
        return None

try:
    from adapters.git_adapter import inject_commit_sha_into_meta  # type: ignore
except Exception:
    def inject_commit_sha_into_meta(pb, sha: Optional[str]) -> None: ...


# fs apply (obligatoire si mode apply)
try:
    from core.fs_apply import apply_patchblock_to_file as apply_patch  # type: ignore
except Exception:
    apply_patch = None  # type: ignore


# -----------------------------------------------------------------------------#
# Pipeline d'exécution
# -----------------------------------------------------------------------------#

def run_plan(
    ep_path: str,
    repo_root: str,
    *,
    archive_dir: Optional[str] = None,
    dry_run: bool = False,
    patch_dir: Optional[str] = None,
) -> None:
    """
    Exécute un execution_plan YAML.

    Modes:
      - dry_run=True  → ACWP → ACW, écrit les patchs dans patch_dir, pas d'effets FS.
      - dry_run=False → ACWP → ACW → checkers → apply FS → commit Git → archives.

    Args:
        ep_path: Chemin du execution_plan.yaml.
        repo_root: Racine du repo (pour apply/commit).
        archive_dir: Dossier d’archive (créé si absent). Ignoré si dry_run.
        dry_run: Active le mode “bridge local” sans side-effects.
        patch_dir: Dossier de sortie des patchs (défaut: .archcode/patches).
    """
    ep_p = Path(ep_path)
    plan_lines, meta = _load_plan_lines(ep_p)
    if not plan_lines:
        raise ValueError("Aucune PlanLine valide n’a été trouvée dans le plan.")

    # Imports agents (ACWP/ACW) avec alias -> fallback
    ACWP = _import_first(["agents.acwp", "agents.agent_code_writer_planner"])
    ACW  = _import_first(["agents.acw",  "agents.agent_code_writer"])

    # Checkers si pas dry-run
    if not dry_run:
        mod_file_checker = _import_first(["agents.agent_file_checker"])
        mod_module_checker = _import_first(["agents.agent_module_checker"])
        check_file = getattr(mod_file_checker, "check_file")
        check_module = getattr(mod_module_checker, "check_module")

    # Prépare archiver si mode apply
    run_dir = None
    if not dry_run:
        if not archive_dir:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive_dir = f".arch_runs/{ts}"
        run_dir = archive_dir
        os.makedirs(run_dir, exist_ok=True)
        archive_run_info(run_dir, started_at=datetime.now().isoformat(timespec="seconds"))
        try:
            ep_text = Path(ep_path).read_text(encoding="utf-8")
        except Exception as e:
            ep_text = f"# [warn] lecture échouée {ep_path}: {e}"
        archive_execution_plan(ep_text, run_dir=run_dir)

    # Patch dir (toujours, pour inspection)
    patch_dir_p = Path(patch_dir or ".archcode/patches")
    _ensure_dir(patch_dir_p)

    # Branche Git (best-effort) si mode apply
    if not dry_run:
        try:
            ensure_branch(repo_root=repo_root)  # type: ignore[call-arg]
            if run_dir:
                append_console_log("[git] ensure_branch ok", run_dir=run_dir)
            print("• Branche de travail prête (best-effort)")
        except Exception as e:
            if run_dir:
                append_console_log(f"[git] ensure_branch skipped: {e}", run_dir=run_dir)
            print("• Git indisponible — on continue sans commit")

    # Exécution
    prev_cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        # writer tasks depuis ACWP
        tasks = ACWP.plan_to_writer_tasks(
            plan_lines,
            execution_context=None,
            bus_message_id=meta.get("bus_message_id"),
            user_story_id=None,
            user_story=None,
            loop_iteration=meta.get("loop_iteration"),
        )

        produced = 0
        for wt in tasks:
            # ACW
            pb = ACW.write_code(wt)

            # Toujours sauver le patch (y compris dry-run)
            patch_path = patch_dir_p / f"{wt['plan_line_id']}.patch.txt"
            patch_path.write_text(pb.code, encoding="utf-8")
            produced += 1
            print(f"[patch] {patch_path}")

            if dry_run:
                # Pas de checkers, pas d’apply
                continue

            # Archive avant checks
            if run_dir:
                archive_patch_before(pb, run_dir=run_dir)

            # Checkers
            pb = check_file(pb)
            pb = check_module(pb)

            # Archive après checks
            if run_dir:
                archive_patch_after(pb, run_dir=run_dir)

            if pb.global_status == "ok":
                # Apply (FS)
                if apply_patch is None:
                    raise RuntimeError("apply_patch indisponible (core.fs_apply manquant).")
                try:
                    apply_patch(pb)  # type: ignore[misc]
                except Exception as e:
                    print(f"    → APPLY FAILED: {e}")
                    if run_dir:
                        append_console_log(f"[apply] failed: {e}", run_dir=run_dir)
                    break

                # Commit (best-effort)
                msg = f"feat(mARCH): {wt['plan_line_id']} {wt.get('role')} {wt.get('target_symbol')}"
                try:
                    sha = stage_and_commit([pb.meta.file], msg, repo_root=repo_root)  # type: ignore[arg-type]
                    inject_commit_sha_into_meta(pb, sha)
                    if run_dir:
                        archive_patch_post_commit(pb, run_dir=run_dir)
                    short = (sha or "")[:7] if sha else "∅"
                    print(f"    → OK: fichier écrit & commit {short}")
                except Exception as e:
                    print(f"    → OK: fichier écrit (commit non effectué: {e})")
                    if run_dir:
                        append_console_log(f"[git] commit skipped: {e}", run_dir=run_dir)
            else:
                reason = getattr(pb, "error_trace", None) or "module checker"
                print(f"    → REJECTED: {reason}")
                if run_dir:
                    append_console_log(f"[reject] {wt['plan_line_id']}: {reason}", run_dir=run_dir)
                break

        if dry_run:
            print(f"[DONE] dry-run : {produced} patch(s) écrit(s) dans {patch_dir_p}")
        else:
            print(f"[DONE] run complet : {produced} patch(s) traités")
    finally:
        os.chdir(prev_cwd)


# -----------------------------------------------------------------------------#
# CLI
# -----------------------------------------------------------------------------#

def _build_parser() -> argparse.ArgumentParser:
    """Construit le parseur CLI (compatible ancien runner + options dry-run)."""
    ap = argparse.ArgumentParser(description="Exécute un execution_plan (ACWP → ACW → checkers → apply)")
    ap.add_argument("--ep", required=True, help="Chemin vers execution_plan.yaml")
    ap.add_argument("--repo", default=".", help="Racine du repo (où écrire/committer)")
    ap.add_argument("--archive-dir", default=None, help="Dossier d’archives (ignoré en --dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="N’émettre que les patchs (pas de checkers/apply/git)")
    ap.add_argument("--patch-dir", default=".archcode/patches", help="Dossier de sortie des patchs")
    return ap


def main(argv: Optional[List[str]] = None) -> None:
    """Point d’entrée CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    run_plan(args.ep, args.repo, archive_dir=args.archive_dir, dry_run=bool(args.dry_run), patch_dir=args.patch_dir)


if __name__ == "__main__":
    main()
