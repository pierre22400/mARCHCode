
# adapters/git_adapter.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from core.types import PatchBlock
from core.git_diffstats import (
    DiffStatsData,
    compute_diffstats_for_paths,
    ensure_branch,
    optional_push,
    stage_and_commit,
)

"""
============================================================
Git Adapter — mARCHCode (MVP) : apply, commit & safe rollback
============================================================

But du module
-------------
Adapter minimal pour :
  1) écrire le code d’un `PatchBlock` dans le FS du repo (mode full-write MVP),
  2) calculer des diffstats ciblés (chemin du patch),
  3) créer/checkout une branche clone `archcode-self/...`,
  4) capturer le `previous_sha` (HEAD avant commit) pour historique/rollback,
  5) stage + commit avec un message normalisé (roadmap),
  6) archiver le patch post-commit pour rollback “green” futur,
  7) pousser la branche (optionnel),
  8) réinjecter le `commit_sha` dans `pb.meta.commit_sha`.

Quand l’utiliser ?
------------------
- Lors de l’étape APPLY, après passage FileChecker/ModuleChecker et gate policy OK.
- En self-dev comme en démo externe, tant que l’écriture « full file » est acceptable.
- Pour sécuriser les commits en permettant un retour rapide au dernier état validé
  (“green”) grâce à `safe_rollback_to_last_green()`.

Entrées attendues
-----------------
- `PatchBlock` avec :
    - `pb.meta.file` (chemin relatif, *.py)
    - `pb.meta.module`, `pb.meta.role`, `pb.meta.plan_line_id` (pour le commit msg)
    - `pb.code` contenant les balises `#{begin_meta: ...}` / `#{end_meta}`
- `GitApplyOptions` pour repo_root / branch / push / dry_run.

Sorties
-------
- SHA du commit (str) si succès.
- `"DRY-RUN"` si `dry_run=True`.
- `pb.meta.commit_sha` mis à jour uniquement en mode non dry-run.
- `previous_sha` archivé dans `pb.history` avant commit.
- Archive `patch_post_commit_<commit_sha>.tar.gz` dans `.archcode/archive/`.

Contrats & limites MVP
----------------------
- Écriture *complète* du fichier (pas d’insertion partielle).
- Pas de merge/diff 3-way : on écrase la cible avec `pb.code`.
- La policy *n’est pas* appliquée ici : gate dans l’orchestrateur avant appel.
- En cas d’absence de `pb.meta.file`, une `ValueError` est levée.
- La détection de “green” se limite au dernier patch post-commit archivé.
"""


def _extract_constraints_summary(pb: PatchBlock) -> str:
    """
    Petite heuristique: essaie d'extraire 2–3 contraintes visibles depuis meta.commentaires.
    (MVP: on scanne les commentaires des checkers pour des tokens connus)
    """
    meta_text = " ".join([
        pb.meta.comment_agent_file_checker or "",
        pb.meta.comment_agent_module_checker or "",
    ]).lower()
    tokens = []
    for key in ("pep8", "typing=strict", "isort", "google", "no bare except", "structlog"):
        if key in meta_text:
            tokens.append(key)
    return ", ".join(tokens) if tokens else "n/a"


def build_commit_message(
    pb: PatchBlock,
    diff: Optional[DiffStatsData] = None,
    extra_notes: str = ""
) -> str:
    """
    Construit un message de commit conforme au modèle de la roadmap.
    Normalisations:
      - role → lowercase
      - module inclus s'il est disponible
    """
    pl = pb.meta.plan_line_id or "PL-UNKNOWN"
    role_low = (pb.meta.role or "role?").lower()
    mod = pb.meta.module or "module?"
    first_line = f"feat(mARCH): {pl} {role_low} {mod}"

    status_file = (pb.meta.status_agent_file_checker or "∅").lower()
    status_mod = (pb.meta.status_agent_module_checker or "∅").lower()
    status_line = (
        f"status: global_status={pb.global_status or '∅'}; "
        f"file_checker={status_file}; module_checker={status_mod}"
    )

    constraints_line = f"constraints: {_extract_constraints_summary(pb)}"

    br = f"{(diff.files_changed if diff else 0)} file(s)"
    notes = extra_notes.strip() if extra_notes else f"blast_radius={br}"

    lines = [
        first_line,
        f"patch_id: {pb.patch_id}",
        f"plan_line_id: {pl}",
        status_line,
        constraints_line,
        f"notes: {notes}",
        "commit_source: ACW→checkers (self-dev)",
    ]
    return "\n".join(lines)


@dataclass
class GitApplyOptions:
    repo_root: str = "."
    branch_name: str = "archcode-self/preview"
    push: bool = False
    dry_run: bool = False  # ← nouveau : permet une démo simulée sans Git


def write_patch_to_fs(pb: PatchBlock, *, repo_root: str) -> str:
    """
    MVP simple: écrit *tout le contenu du patch* dans le fichier cible (création si absent).
    On choisit volontairement la simplicité (pas d'insertion partielle).
    Retourne le chemin absolu écrit.
    """
    rel = pb.meta.file
    if not rel:
        raise ValueError("PatchBlock.meta.file est requis pour écrire le patch.")
    full = Path(repo_root).joinpath(rel)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(pb.code, encoding="utf-8")
    return str(full)

@@
 def apply_and_commit_git(pb: PatchBlock, *, options: GitApplyOptions) -> str:
     """
     1) (option) Checkout/Crée la branche (skip si dry_run)
     2) Écrit le fichier (MVP: full-write)
     3) Calcule diffstats ciblés
     4) (option) Commit (skip si dry_run)
     5) (option) Push

     Retour:
       - commit SHA (str) si non dry-run,
       - "DRY-RUN" si dry_run=True.
     """
     if not pb.meta.file:
         raise ValueError("PatchBlock.meta.file est requis pour commit Git.")
+
+    from core.git_diffstats import _run_git  # réutilisation interne
+
+    # Capture HEAD actuel avant toute modification
+    rc, out, err = _run_git(["rev-parse", "HEAD"], cwd=options.repo_root)
+    if rc == 0:
+        previous_sha = out.strip()
+        pb.append_history(f"git:previous_sha={previous_sha}")
+    else:
+        previous_sha = None
+        pb.append_history("git:previous_sha=UNKNOWN")
 
     # 1) branche (si non dry-run)
     if not options.dry_run:
         ensure_branch(options.branch_name, repo_root=options.repo_root)
@@
     # 4) commit / 5) push (si non dry-run)
     if not options.dry_run:
         sha = stage_and_commit([pb.meta.file], message, repo_root=options.repo_root)  # type: ignore[arg-type]
+        pb.append_history(f"git:commit_sha={sha}")
+        # Archive post-commit si on a un sha précédent
+        if previous_sha:
+            _archive_patch_post_commit(pb, previous_sha, sha, repo_root=options.repo_root)
         if options.push:
             optional_push(options.branch_name, repo_root=options.repo_root)
         pb.meta.commit_sha = sha
         return sha
@@
 def rollback_file_changes(paths: Sequence[str], *, repo_root: str) -> None:
     """
     Rejette les changements non commités sur les chemins donnés.
     """
     from core.git_diffstats import _run_git  # reuse interne
     if not paths:
         return
     rc, _, err = _run_git(["checkout", "--", *paths], cwd=repo_root)
     if rc != 0:
         print(f"[git rollback] {err}")
+
+
+def safe_rollback_to_last_green(*, repo_root: str) -> None:
+    """
+    Reviens au dernier commit "green" connu.
+    Hypothèse MVP :
+      - un commit est "green" si un patch_post_commit archivé est présent
+        (par ex. dans .archcode/archive/patch_post_commit_<sha>.tar.gz)
+      - on utilise le SHA cible de cette archive pour faire un checkout forcé.
+    """
+    from core.git_diffstats import _run_git
+    archive_dir = Path(repo_root) / ".archcode" / "archive"
+    if not archive_dir.exists():
+        print("[git rollback] Aucun archive_dir trouvé, rollback impossible.")
+        return
+    # On récupère la dernière archive par date
+    archives = sorted(archive_dir.glob("patch_post_commit_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
+    if not archives:
+        print("[git rollback] Aucune archive patch_post_commit trouvée.")
+        return
+    # SHA attendu dans le nom : patch_post_commit_<sha>.tar.gz
+    last_sha = archives[0].stem.replace("patch_post_commit_", "")
+    rc, _, err = _run_git(["checkout", last_sha], cwd=repo_root)
+    if rc == 0:
+        print(f"[git rollback] Retour au dernier commit green: {last_sha}")
+    else:
+        print(f"[git rollback] Échec rollback vers {last_sha}: {err}")
+
+
+# --- Helpers internes ---
+
+def _archive_patch_post_commit(pb: PatchBlock, prev_sha: str, new_sha: str, *, repo_root: str) -> None:
+    """
+    Archive post-commit minimaliste pour rollback futur.
+    (MVP) On crée un tar.gz du diff entre prev_sha et new_sha.
+    """
+    import tarfile
+    archive_dir = Path(repo_root) / ".archcode" / "archive"
+    archive_dir.mkdir(parents=True, exist_ok=True)
+    archive_path = archive_dir / f"patch_post_commit_{new_sha}.tar.gz"
+    # pour MVP on n'archive que le fichier modifié par pb
+    full_file_path = Path(repo_root) / pb.meta.file
+    with tarfile.open(archive_path, "w:gz") as tar:
+        if full_file_path.exists():
+            tar.add(full_file_path, arcname=pb.meta.file)
+    pb.append_history(f"git:archive_patch_post_commit={archive_path}")


def inject_commit_sha_into_meta(pb: PatchBlock, commit_sha: Optional[str]) -> None:
    """
    Rétro-compatibilité runner :
    - Some versions de run_plan injectent le SHA de commit dans pb.meta.
    - Ici on fait un best-effort tolérant (no-op si attributs absents).
    Effets :
      • pb.meta.commit_sha = <sha> (si possible)
      • append_history("git_commit=<sha>") (si disponible)
    """
    if not commit_sha:
        return
    # pb.meta.commit_sha (tolérant)
    try:
        meta = getattr(pb, "meta", None)
        if meta is not None and not getattr(meta, "commit_sha", None):
            setattr(meta, "commit_sha", commit_sha)
    except Exception:
        pass
    # trace dans l’history (tolérant)
    try:
        if hasattr(pb, "append_history"):
            pb.append_history(f"git_commit={commit_sha}")
    except Exception:
        pass


