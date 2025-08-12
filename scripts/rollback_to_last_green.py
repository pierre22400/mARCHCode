#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#rollback_to_last_green.py


from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml  # PyYAML

"""
===============================================================================
mARCHCode — rollback_to_last_green.py
-------------------------------------------------------------------------------
Rôle
    Automatiser la procédure "Retour au dernier green" décrite dans la
    gouvernance Git (tiddler + docs/ROLLBACK.md), en exécutant :
      1) Détection du dernier tag green-<YYYYMMDD>-<shortsha>
      2) Vérification/lecture des artefacts
      3) Restauration du code + artefacts
      4) Re-taggage optionnel

Comportement
    - Stratégies :
        * merge (par défaut) : git merge --no-ff <target_sha>
        * reset              : git reset --hard <target_sha>
    - Lecture des métadonnées YAML si disponible :
        .archcode/archive/metadata_<shortsha>.yaml
      (non bloquant si absent)
    - Archive requise :
        .archcode/archive/patch_post_commit_<sha>.tar.gz

Pré-requis
    - Exécuté à la racine du repo Git.
    - Un ou plusieurs tags "green-*" existants et poussés.
    - L’archive correspondant au commit taggé est présente localement.

Usage
    python -m scripts.rollback_to_last_green
    # ou
    python scripts/rollback_to_last_green.py
    # options :
    python scripts/rollback_to_last_green.py --strategy reset --dry-run

Options
    --strategy {merge,reset}  : stratégie de retour (def: merge)
    --dry-run                 : afficher les actions sans exécuter
    --no-clean-check          : ignorer l’état "working tree clean" (dangereux)

Sorties
    - Codes d’erreur explicites en cas de manque d’archive / tag inexistants.
    - Journal clair des actions entreprises.

Traçabilité
    - Aligné avec docs/BRANCHING.md, docs/COMMITS.md, docs/ROLLBACK.md
    - Relit metadata_<shortsha>.yaml si disponible (non bloquant)
===============================================================================
"""


# ---------- Helpers Git ----------

def run(cmd: List[str], dry: bool = False) -> str:
    """Exécute une commande shell et renvoie stdout.strip(). En mode dry-run, affiche seulement."""
    if dry:
        print(f"[DRY] $ {' '.join(cmd)}")
        return ""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Commande échouée: {' '.join(cmd)}\n{proc.stderr}")
    return proc.stdout.strip()


def git_root() -> Path:
    out = run(["git", "rev-parse", "--show-toplevel"])
    return Path(out)


def git_working_tree_clean() -> bool:
    out = run(["git", "status", "--porcelain"])
    return out == ""


def list_green_tags() -> List[str]:
    # Tri par date de création descendante ; on garde la plus récente en tête.
    out = run(["git", "tag", "-l", "green-*", "--sort=-creatordate"])
    tags = [line.strip() for line in out.splitlines() if line.strip()]
    return tags


def tag_to_sha(tag: str) -> str:
    return run(["git", "rev-list", "-n", "1", tag])


def short_sha(full_sha: str) -> str:
    return run(["git", "rev-parse", "--short", full_sha])


def checkout(ref: str, dry: bool = False) -> None:
    run(["git", "checkout", ref], dry=dry)


def merge_noff(target_sha: str, message: str, dry: bool = False) -> None:
    run(["git", "merge", "--no-ff", target_sha, "-m", message], dry=dry)


def push_current_branch(dry: bool = False) -> None:
    run(["git", "push", "origin", "HEAD"], dry=dry)


def push_with_lease(dry: bool = False) -> None:
    run(["git", "push", "--force-with-lease", "origin", "HEAD"], dry=dry)


def reset_hard(ref: str, dry: bool = False) -> None:
    run(["git", "reset", "--hard", ref], dry=dry)


# ---------- Données & lecture metadata ----------

@dataclass
class GreenTarget:
    tag: str
    sha: str
    shortsha: str
    archive_path: Path
    metadata_path: Path


def find_last_green_target(repo_root: Path) -> GreenTarget:
    tags = list_green_tags()
    if not tags:
        raise FileNotFoundError("Aucun tag green-* trouvé.")
    tag = tags[0]
    sha = tag_to_sha(tag)
    ssha = short_sha(sha)
    archive_path = repo_root / ".archcode" / "archive" / f"patch_post_commit_{sha}.tar.gz"
    metadata_path = repo_root / ".archcode" / "archive" / f"metadata_{ssha}.yaml"
    return GreenTarget(tag=tag, sha=sha, shortsha=ssha, archive_path=archive_path, metadata_path=metadata_path)


def read_metadata(meta_path: Path) -> Optional[dict]:
    if not meta_path.exists():
        return None
    try:
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


# ---------- Décompression simple tar.gz ----------

def extract_archive(archive: Path, dest: Path, dry: bool = False) -> None:
    if dry:
        print(f"[DRY] extract {archive} -> {dest}")
        return
    import tarfile
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(path=dest)


# ---------- Logique principale ----------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Rollback vers le dernier état green.")
    parser.add_argument("--strategy", choices=["merge", "reset"], default="merge", help="Stratégie de retour (def: merge)")
    parser.add_argument("--dry-run", action="store_true", help="Afficher les actions sans exécuter")
    parser.add_argument("--no-clean-check", action="store_true", help="Ne pas vérifier l'état 'working tree clean'")
    args = parser.parse_args(argv)

    try:
        root = git_root()
        print(f"[INFO] Repo : {root}")

        if not args.no_clean_check and not git_working_tree_clean():
            print("[ERREUR] Le working tree n'est pas propre. Commit/stash avant rollback, ou utilise --no-clean-check.", file=sys.stderr)
            return 2

        target = find_last_green_target(root)
        print(f"[INFO] Dernier green : {target.tag} -> {target.sha} ({target.shortsha})")
        print(f"[INFO] Archive attendue : {target.archive_path}")
        print(f"[INFO] Metadata attendue : {target.metadata_path}")

        if not target.archive_path.exists():
            print(f"[ERREUR] Archive manquante : {target.archive_path}", file=sys.stderr)
            return 3

        meta = read_metadata(target.metadata_path)
        if meta:
            print(f"[OK] Metadata lue : branch={meta.get('branch')} author={meta.get('author')} created_utc={meta.get('created_utc')}")
        else:
            print("[WARN] Metadata YAML absente ou illisible (continuer quand même).")

        # 1) Se positionner sur le commit ciblé (HEAD détachée)
        print("[STEP] Checkout du commit cible…")
        checkout(target.sha, dry=args.dry_run)

        # 2) Restaurer les artefacts depuis l'archive tar.gz
        print("[STEP] Restauration des artefacts depuis l'archive…")
        extract_archive(target.archive_path, dest=root, dry=args.dry_run)

        # 3) Revenir sur main et appliquer la stratégie
        print("[STEP] Application de la stratégie sur main…")
        checkout("main", dry=args.dry_run)
        if args.strategy == "merge":
            merge_noff(target.sha, message=f"rollback: to {target.tag}", dry=args.dry_run)
            push_current_branch(dry=args.dry_run)
        else:
            reset_hard(target.sha, dry=args.dry_run)
            push_with_lease(dry=args.dry_run)

        print("[OK] Rollback terminé.")
        print("Rappel : si nécessaire, re-crée un tag green après CI verte.")
        return 0

    except Exception as e:
        print(f"[ERREUR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

