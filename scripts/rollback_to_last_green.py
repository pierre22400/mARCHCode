#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# rollback_to_last_green.py

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
    """Exécute une commande shell et renvoie `stdout.strip()`.

    Args:
        cmd: Liste des tokens de la commande à exécuter.
        dry: Si True, n’exécute pas et affiche seulement la commande.

    Returns:
        La sortie standard (sans espaces de fin) lorsque `dry` est False, sinon chaîne vide.

    Raises:
        RuntimeError: Si la commande retourne un code non nul.
    """
    if dry:
        print(f"[DRY] $ {' '.join(cmd)}")
        return ""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Commande échouée: {' '.join(cmd)}\n{proc.stderr}")
    return proc.stdout.strip()


def git_root() -> Path:
    """Retourne la racine du dépôt Git courant.

    Returns:
        Chemin `Path` de la racine du dépôt.
    """
    out = run(["git", "rev-parse", "--show-toplevel"])
    return Path(out)


def git_working_tree_clean() -> bool:
    """Indique si l’arbre de travail est propre (sans modifications locales).

    Returns:
        True si `git status --porcelain` est vide, sinon False.
    """
    out = run(["git", "status", "--porcelain"])
    return out == ""


def list_green_tags() -> List[str]:
    """Liste les tags `green-*` triés de la plus récente à la plus ancienne.

    Returns:
        Liste des noms de tags `green-*` (chaînes non vides).
    """
    out = run(["git", "tag", "-l", "green-*", "--sort=-creatordate"])
    tags = [line.strip() for line in out.splitlines() if line.strip()]
    return tags


def tag_to_sha(tag: str) -> str:
    """Résout le SHA complet associé à un tag.

    Args:
        tag: Nom du tag à résoudre.

    Returns:
        SHA (hex) du commit pointé par `tag`.
    """
    return run(["git", "rev-list", "-n", "1", tag])


def short_sha(full_sha: str) -> str:
    """Retourne le SHA abrégé correspondant à un SHA complet.

    Args:
        full_sha: SHA hexadécimal complet.

    Returns:
        SHA abrégé (généralement 7 caractères).
    """
    return run(["git", "rev-parse", "--short", full_sha])


def checkout(ref: str, dry: bool = False) -> None:
    """Effectue un `git checkout` vers une référence donnée.

    Args:
        ref: Référence Git (commit, tag, branche).
        dry: Mode simulation (aucune exécution si True).
    """
    run(["git", "checkout", ref], dry=dry)


def merge_noff(target_sha: str, message: str, dry: bool = False) -> None:
    """Réalise un merge non fast-forward vers `target_sha`.

    Args:
        target_sha: SHA du commit cible à fusionner.
        message: Message de merge.
        dry: Mode simulation (aucune exécution si True).
    """
    run(["git", "merge", "--no-ff", target_sha, "-m", message], dry=dry)


def push_current_branch(dry: bool = False) -> None:
    """Pousse la branche courante sur `origin`.

    Args:
        dry: Mode simulation (aucune exécution si True).
    """
    run(["git", "push", "origin", "HEAD"], dry=dry)


def push_with_lease(dry: bool = False) -> None:
    """Force-push avec `--force-with-lease` sur la branche courante.

    Args:
        dry: Mode simulation (aucune exécution si True).
    """
    run(["git", "push", "--force-with-lease", "origin", "HEAD"], dry=dry)


def reset_hard(ref: str, dry: bool = False) -> None:
    """Fait un `git reset --hard` vers la référence donnée.

    Args:
        ref: Référence Git (commit/branche) vers laquelle revenir.
        dry: Mode simulation (aucune exécution si True).
    """
    run(["git", "reset", "--hard", ref], dry=dry)


# ---------- Données & lecture metadata ----------

@dataclass
class GreenTarget:
    """Représente la cible *green* la plus récente.

    Attributes:
        tag: Nom du tag `green-<date>-<shortsha>`.
        sha: SHA complet du commit taggé.
        shortsha: SHA abrégé du commit taggé.
        archive_path: Chemin vers l’archive `.tar.gz` post-commit.
        metadata_path: Chemin vers le fichier `metadata_<shortsha>.yaml`.
    """
    tag: str
    sha: str
    shortsha: str
    archive_path: Path
    metadata_path: Path


def find_last_green_target(repo_root: Path) -> GreenTarget:
    """Trouve la dernière cible *green* et ses artefacts associés.

    Args:
        repo_root: Racine du dépôt.

    Returns:
        Une instance `GreenTarget` décrivant tag, SHAs et chemins d’artefacts.

    Raises:
        FileNotFoundError: Si aucun tag `green-*` n’est trouvé.
    """
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
    """Charge un fichier YAML de métadonnées *green* (tolérant).

    Args:
        meta_path: Chemin du fichier `metadata_<shortsha>.yaml`.

    Returns:
        Un dict de métadonnées ou None si absent/invalide.
    """
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
    """Extrait une archive `tar.gz` vers un répertoire destination.

    Args:
        archive: Chemin de l’archive `.tar.gz`.
        dest: Répertoire de destination.
        dry: Mode simulation (aucune extraction si True).
    """
    if dry:
        print(f"[DRY] extract {archive} -> {dest}")
        return
    import tarfile
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(path=dest)


# ---------- Logique principale ----------

def main(argv: Optional[List[str]] = None) -> int:
    """Point d’entrée CLI : rollback vers le dernier état *green*.

    Args:
        argv: Liste d’arguments (pour tests). Si None, utilise `sys.argv[1:]`.

    Returns:
        Code de retour POSIX (0 = succès, >0 = erreur).
    """
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
