# core/git_diffstats.py
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


"""
git_diffstats — Fusion v1+v2 (métriques riches + helpers Git)
=============================================================

Rôle du module
--------------
Fournir des métriques détaillées sur les différences Git ainsi que des
utilitaires de gestion de branches et de commits, destinés à l’orchestrateur
et aux adaptateurs.

Entrées / Sorties
-----------------
Fonctions principales :
  - compute_diff_stats(repo_root, against_ref="HEAD", include_staged=True, paths=None)
      → retourne un objet DiffStatsData :
          * files_changed
          * loc_added
          * loc_deleted
          * patch_size_bytes
          * has_binary
          * paths
          * by_file
  - compute_diffstats_for_paths(paths, repo_root=None)
      → raccourci pratique (worktree vs HEAD, non-staged)
  - ensure_branch(repo_root, branch_name, create_if_missing=True)
      → assure la présence/positionnement sur une branche
  - stage_and_commit(repo_root, message, paths=None)
      → ajoute et commit les fichiers spécifiés
  - optional_push(repo_root, remote="origin", branch=None)
      → push optionnel selon configuration

Contrats respectés
------------------
- Les métriques doivent être exactes, même en présence de fichiers binaires.
- Les helpers Git ne doivent pas altérer l’état du dépôt hors opérations explicites.
"""

@dataclass
class FileStat:
    """Statistiques élémentaires d’un fichier modifié (lignes ajoutées/supprimées)."""
    file: str
    added: int
    deleted: int


@dataclass
class DiffStatsData:
    """Agrégat de métriques de diff Git (taille patch, nb fichiers, binaire, détail par fichier)."""
    files_changed: int
    loc_added: int
    loc_deleted: int
    patch_size_bytes: int
    paths: List[str] = field(default_factory=list)
    has_binary: bool = False
    by_file: List[FileStat] = field(default_factory=list)


def _run_git(args: List[str], cwd: str | None = None) -> Tuple[int, str, str]:
    """
    Exécute `git <args>` et retourne (rc, stdout, stderr) en texte.
    """
    p = subprocess.Popen(
        ["git"] + args,
        cwd=cwd or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = p.communicate()
    return p.returncode, (out or "").strip(), (err or "").strip()


def ensure_branch(branch_name: str, repo_root: str | None = None) -> None:
    """
    Crée/checkout une branche si nécessaire. Idempotent.
    """
    rc, _, _ = _run_git(["rev-parse", "--verify", branch_name], cwd=repo_root)
    if rc != 0:
        rc2, _, err2 = _run_git(["checkout", "-b", branch_name], cwd=repo_root)
        if rc2 != 0:
            raise RuntimeError(f"git checkout -b {branch_name}: {err2}")
    else:
        rc2, _, err2 = _run_git(["checkout", branch_name], cwd=repo_root)
        if rc2 != 0:
            raise RuntimeError(f"git checkout {branch_name}: {err2}")


def stage_and_commit(paths: List[str], message: str, repo_root: str | None = None) -> str:
    """
    `git add <paths>` + `git commit -m <message>`, puis retourne le SHA.
    """
    rc, _, err = _run_git(["add"] + paths, cwd=repo_root)
    if rc != 0:
        raise RuntimeError(f"git add: {err}")
    rc, _, err = _run_git(["commit", "-m", message], cwd=repo_root)
    if rc != 0:
        raise RuntimeError(f"git commit: {err}")
    rc, sha, err = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if rc != 0:
        raise RuntimeError(f"git rev-parse HEAD: {err}")
    return sha


def optional_push(branch_name: str, repo_root: str | None = None) -> None:
    """
    Push non bloquant (idéal démos). Log en cas d’échec, ne jette pas.
    """
    rc, _, err = _run_git(["push", "-u", "origin", branch_name], cwd=repo_root)
    if rc != 0:
        print(f"[git push] non bloquant: {err}")


def compute_diff_stats(
    repo_root: str,
    *,
    against_ref: str = "HEAD",
    include_staged: bool = True,
    paths: Optional[List[str]] = None,
) -> DiffStatsData:
    """
    Calcule les stats d’un diff contre `against_ref` (HEAD par défaut).

    - include_staged=True : compare index+worktree vs HEAD (ou ref)
      -> `git diff --staged` pour refléter ce qui partira au commit
    - include_staged=False : compare worktree non-stagé vs HEAD (ou ref)
      -> `git diff` standard

    - paths (optionnel) : limite le diff à une liste de chemins
    - Métriques retournées :
        files_changed, loc_added, loc_deleted, patch_size_bytes,
        has_binary, paths (liste), by_file (détail)
    """
    # 1) Construire commandes diff (numstat + patch)
    if include_staged:
        numstat_cmd = ["diff", "--staged", "--numstat"]
        patch_cmd = ["diff", "--staged", "--patch", "--unified=0"]
    else:
        numstat_cmd = ["diff", "--numstat"]
        patch_cmd = ["diff", "--patch", "--unified=0"]

    # Plage/ref (HEAD par défaut) – git accepte `--staged` sans ref explicite,
    # mais on garde la compat. de v1 qui permettait d’inclure une ref.
    if against_ref:
        numstat_cmd.append(against_ref)
        patch_cmd.append(against_ref)

    if paths:
        numstat_cmd += ["--", *paths]
        patch_cmd += ["--", *paths]

    # 2) numstat
    rc, numstat_out, err = _run_git(numstat_cmd, cwd=repo_root)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(numstat_cmd)}: {err}")

    loc_added = 0
    loc_deleted = 0
    paths_changed: List[str] = []
    has_binary = False
    by_file: List[FileStat] = []

    for line in numstat_out.splitlines():
        # format: "<adds>\t<dels>\t<path>"
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, p = parts[0], parts[1], parts[2]
        paths_changed.append(p)
        if a == "-" or d == "-":
            has_binary = True
            by_file.append(FileStat(file=p, added=0, deleted=0))
            continue
        try:
            add_i = int(a)
            del_i = int(d)
        except ValueError:
            # Git met parfois '-' si binaire ; on marque comme binaire par prudence
            has_binary = True
            add_i = del_i = 0
        loc_added += add_i
        loc_deleted += del_i
        by_file.append(FileStat(file=p, added=add_i, deleted=del_i))

    files_changed = len(paths_changed)

    # 3) patch size
    rc, patch_out, err = _run_git(patch_cmd, cwd=repo_root)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(patch_cmd)}: {err}")
    patch_size_bytes = len(patch_out.encode("utf-8", errors="replace"))

    return DiffStatsData(
        files_changed=files_changed,
        loc_added=loc_added,
        loc_deleted=loc_deleted,
        patch_size_bytes=patch_size_bytes,
        paths=paths_changed,
        has_binary=has_binary,
        by_file=by_file,
    )


def compute_diffstats_for_paths(paths: List[str], repo_root: str | None = None) -> DiffStatsData:
    """
    Raccourci pratique : calcule les stats *worktree vs HEAD*,
    limitées aux chemins fournis (non-staged).
    """
    root = repo_root or "."
    return compute_diff_stats(
        root,
        against_ref="HEAD",
        include_staged=False,
        paths=paths or [],
    )
