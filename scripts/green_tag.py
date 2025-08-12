#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import List

import yaml  # PyYAML

__doc__ = """
===============================================================================
mARCHCode — green_tag.py (v2, YAML)
-------------------------------------------------------------------------------
Objectif
    Automatiser la procédure "green" décrite dans la gouvernance Git (voir tiddler) :
    1) Construire une archive post-commit reproductible :
         .archcode/archive/patch_post_commit_<sha>.tar.gz
    2) Créer et pousser un tag : green-<YYYYMMDD>-<shortsha>

Contexte
    - Python 3.11.6 (Windows OK).
    - A exécuter à la racine du repo Git (mARCHCode).
    - N’implémente PAS les tests : on suppose que la CI/l’utilisateur a validé
      que le build/tests sont OK avant d’appeler ce script.

Effets
    - Crée les répertoires si absents : .archcode/archive/
    - Emballe un set minimal d’artefacts reproductibles :
        * manifest(s) YAML (s’ils existent)
        * fichiers de verrouillage (requirements*.txt, poetry.lock, uv.lock…)
        * journaux de tests (si présents)
        * metadata.yaml (sha, date, auteur, branche)
    - Crée le tag, puis le pousse vers origin.

Sécurité / Garde-fous
    - Refuse d’écraser une archive existante pour le même SHA.
    - Refuse de taguer si le tag green-<date>-<shortsha> existe déjà.
    - Emet des messages explicites (exit codes != 0 en cas d’erreur).

Usage
    python -m scripts.green_tag
    # ou
    python scripts/green_tag.py

Licences & Traçabilité
    - Conçu pour s’aligner strictement avec docs/BRANCHING.md et ROLLBACK.md.
===============================================================================
"""


def run(cmd: List[str]) -> str:
    """Exécute une commande et renvoie stdout.strip(), lève en cas d’échec."""
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Commande échouée: {' '.join(cmd)}\n{proc.stderr}")
    return proc.stdout.strip()


def git_root() -> Path:
    """Retourne la racine du repo Git courant."""
    out = run(["git", "rev-parse", "--show-toplevel"])
    return Path(out)


def git_sha_short() -> str:
    """Retourne le short SHA de HEAD."""
    return run(["git", "rev-parse", "--short", "HEAD"])


def git_sha() -> str:
    """Retourne le SHA complet de HEAD."""
    return run(["git", "rev-parse", "HEAD"])


def git_branch() -> str:
    """Retourne la branche courante (ou HEAD détachée)."""
    try:
        return run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    except Exception:
        return "DETACHED_HEAD"


def git_author() -> str:
    """Retourne le nom de l’auteur du dernier commit."""
    return run(["git", "log", "-1", "--pretty=format:%an"])


def ensure_dirs(p: Path) -> None:
    """Crée les répertoires parents si besoin."""
    p.parent.mkdir(parents=True, exist_ok=True)


def collect_artifacts(root: Path) -> List[Path]:
    """
    Sélection minimale d’artefacts reproductibles.
    Étends cette liste au besoin (wheel, build, reports…).
    """
    candidates = [
        "execution_plan.yaml",
        "plan_validated.yaml",
        "rollback_bundle.yaml",
        "requirements.txt",
        "requirements-dev.txt",
        "poetry.lock",
        "uv.lock",
        "pytest-report.xml",
        "test-results.xml",
        ".pytest_cache/lastfailed",
    ]
    found: List[Path] = []
    for rel in candidates:
        p = root / rel
        if p.exists():
            found.append(p)
    return found


def create_metadata(root: Path, sha: str, shortsha: str, archive_path: Path) -> Path:
    """
    Crée un fichier metadata.yaml à embarquer dans l’archive.

    Format YAML produit :
        sha: "<commit_sha>"
        shortsha: "<short_sha>"
        branch: "<branch_name>"
        author: "<author_name>"
        created_utc: "YYYY-MM-DDTHH:MM:SSZ"
        archive: "<chemin/vers/archive.tar.gz>"
        policy_ref:
          branching: "docs/BRANCHING.md"
          commits: "docs/COMMITS.md"
          rollback: "docs/ROLLBACK.md"
    """
    meta = {
        "sha": sha,
        "shortsha": shortsha,
        "branch": git_branch(),
        "author": git_author(),
        "created_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "archive": str(archive_path.as_posix()),
        "policy_ref": {
            "branching": "docs/BRANCHING.md",
            "commits": "docs/COMMITS.md",
            "rollback": "docs/ROLLBACK.md",
        },
    }
    meta_path = root / ".archcode" / "archive" / f"metadata_{shortsha}.yaml"
    ensure_dirs(meta_path)
    meta_path.write_text(
        yaml.safe_dump(meta, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return meta_path


def create_archive(root: Path, sha: str, shortsha: str) -> Path:
    """Construit .archcode/archive/patch_post_commit_<sha>.tar.gz avec les artefacts."""
    archive_dir = root / ".archcode" / "archive"
    ensure_dirs(archive_dir)
    archive_path = archive_dir / f"patch_post_commit_{sha}.tar.gz"
    if archive_path.exists():
        raise FileExistsError(f"Archive déjà présente: {archive_path}")

    artifacts = collect_artifacts(root)
    # Ajoute metadata.yaml
    meta_path = create_metadata(root, sha, shortsha, archive_path)
    artifacts.append(meta_path)

    # Création de l’archive tar.gz
    with tarfile.open(archive_path, "w:gz") as tar:
        for p in artifacts:
            arcname = p.relative_to(root)
            tar.add(p, arcname=str(arcname))
    return archive_path


def tag_and_push(shortsha: str) -> str:
    """Crée et pousse le tag green-<YYYYMMDD>-<shortsha>."""
    date_str = datetime.utcnow().strftime("%Y%m%d")
    tag = f"green-{date_str}-{shortsha}"
    # Vérifie si le tag existe déjà
    existing = run(["git", "tag", "-l", tag])
    if existing == tag:
        raise FileExistsError(f"Le tag existe déjà: {tag}")
    # Crée et pousse
    run(["git", "tag", "-a", tag, "-m", f"green build {date_str} ({shortsha})"])
    run(["git", "push", "origin", tag])
    return tag


def main() -> int:
    try:
        root = git_root()
        sha = git_sha()
        shortsha = git_sha_short()

        # 1) Archive
        archive_path = create_archive(root, sha, shortsha)
        print(f"[OK] Archive créée: {archive_path}")

        # 2) Tag & push
        tag = tag_and_push(shortsha)
        print(f"[OK] Tag créé et poussé: {tag}")

        print("\nRésumé:")
        print(f"  SHA       : {sha}")
        print(f"  ShortSHA  : {shortsha}")
        print(f"  Archive   : {archive_path}")
        print(f"  Tag       : {tag}")
        print("\nConforme à docs/ROLLBACK.md (état green: CI OK + archive + tag).")
        return 0
    except Exception as e:
        print(f"[ERREUR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
