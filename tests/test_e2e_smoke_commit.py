from __future__ import annotations

"""
mARCHCode — E2E Smoke (commit Git réel)
======================================

Ce test crée un repo Git temporaire, fabrique un PatchBlock minimal (avec balises),
passe par run_patch_local avec des adaptateurs "réels" (écriture + commit),
et vérifie qu'un commit SHA existe après l'exécution.

Exécution :
  pytest -s tests/test_e2e_smoke_commit.py
"""

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

from core.orchestrator import (
    run_patch_local,
    OrchestrationAdapters,
    Decision,
    Action,
    Reasoner,
)
from core.types import PatchBlock, MetaBlock  # respecte exactement les noms/casse
from core.decision_router import Decision as DRDecision  # typage assert


# ------------------------------- utils git -------------------------------

def _run_git(args, cwd: Path) -> Tuple[int, str, str]:
    """Exécute une commande Git dans un répertoire donné.

    Args:
        args: Séquence d’arguments pour `git` (ex: ["status", "--porcelain"]).
        cwd: Répertoire de travail où exécuter la commande.

    Returns:
        Tuple `(rc, stdout, stderr)` :
            - rc: code retour processus
            - stdout: sortie standard nettoyée
            - stderr: sortie erreur nettoyée
    """
    p = subprocess.Popen(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = p.communicate()
    return p.returncode, out.strip(), err.strip()


def _init_repo() -> Path:
    """Initialise un dépôt Git éphémère pour les tests E2E.

    Le dépôt est configuré avec un user/email locaux et contient un
    premier commit (via un `.gitkeep`) afin de simplifier les diffs.

    Returns:
        Chemin `Path` racine du dépôt initialisé.

    Raises:
        RuntimeError: si `git init` échoue.
    """
    repo = Path(tempfile.mkdtemp(prefix="arch_e2e_"))
    rc, _, err = _run_git(["init", "."], cwd=repo)
    if rc != 0:
        raise RuntimeError(f"git init failed: {err}")
    # config minimale pour commit
    _run_git(["config", "user.name", "arch-e2e"], cwd=repo)
    _run_git(["config", "user.email", "arch-e2e@example.com"], cwd=repo)
    # commit initial vide (facilite diff)
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _run_git(["add", ".gitkeep"], cwd=repo)
    _run_git(["commit", "-m", "init"], cwd=repo)
    return repo


# --------------- adaptateurs réels (écriture + commit git) ---------------

class ApplyAndCommit(Protocol):
    """Protocol pour appliquer un PatchBlock et effectuer un commit Git."""
    def __call__(self, pb: PatchBlock, decision: Decision) -> None: ...
    """Applique un patch et crée un commit.

    Args:
        pb: PatchBlock à appliquer.
        decision: Décision d’orchestration associée.
    """


class RegenerateWithACW(Protocol):
    """Protocol pour régénérer un PatchBlock via ACW (re-génération automatisée)."""
    def __call__(
        self,
        pb: PatchBlock,
        decision: Decision,
        reasoner: Optional[Reasoner] = None
    ) -> None: ...
    """Déclenche une régénération ciblée.

    Args:
        pb: PatchBlock à régénérer.
        decision: Décision d’orchestration.
        reasoner: Optionnel, normaliseur de raisons/indices.
    """


class RollbackAndLog(Protocol):
    """Protocol pour effectuer un rollback local et journaliser l'opération."""
    def __call__(self, pb: PatchBlock, decision: Decision) -> None: ...
    """Exclut un patch et journalise l’action.

    Args:
        pb: PatchBlock en cause.
        decision: Décision d’orchestration.
    """


@dataclass
class RealGitAdapters(OrchestrationAdapters):
    """Adapter réel pour tester l’intégration Git dans les scénarios end-to-end.

    Attributes:
        repo_root: Racine du dépôt dans lequel écrire et committer.
    """
    repo_root: Path

    def __init__(self, repo_root: Path) -> None:
        """Construit l’adaptateur avec callbacks d’écriture/commit/rollback.

        Args:
            repo_root: Racine du dépôt Git temporaire utilisé pendant le test.
        """

        def _apply_and_commit(pb: PatchBlock, decision: Decision) -> None:
            """Écrit le patch (full-write MVP) puis effectue un commit Git.

            Args:
                pb: PatchBlock à écrire.
                decision: Décision d’orchestration (non utilisée ici).
            """
            rel = pb.meta.file
            if not rel:
                raise ValueError("meta.file requis")
            target = self.repo_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(pb.code, encoding="utf-8")
            rc, _, err = _run_git(["add", rel], cwd=self.repo_root)
            if rc != 0:
                raise RuntimeError(f"git add failed: {err}")
            rc, sha, err = _run_git(["commit", "-m", f"e2e: {pb.meta.plan_line_id or 'PL-?'}"], cwd=self.repo_root)
            if rc != 0:
                raise RuntimeError(f"git commit failed: {err}")
            # on place le sha dans pb.meta.commit_sha si disponible
            if hasattr(pb.meta, "commit_sha"):
                setattr(pb.meta, "commit_sha", _last_commit_sha(self.repo_root))

        def _retry(pb: PatchBlock, decision: Decision, reasoner: Optional[Reasoner] = None) -> None:
            """No-op pour le smoke test : pas de régénération ACW.

            Args:
                pb: PatchBlock à régénérer (ignoré).
                decision: Décision d’orchestration (ignorée).
                reasoner: Optionnel, non utilisé ici.
            """
            pass

        def _rollback(pb: PatchBlock, decision: Decision) -> None:
            """Écrit un marqueur de rollback dans le dépôt (no-op minimal).

            Args:
                pb: PatchBlock exclu.
                decision: Décision d’orchestration (ignorée).
            """
            (self.repo_root / ".rollback_marker").write_text("rollback", encoding="utf-8")

        super().__init__(
            apply_and_commit=_apply_and_commit,
            regenerate_with_acw=_retry,
            rollback_and_log=_rollback,
        )
        self.repo_root = repo_root


def _last_commit_sha(repo_root: Path) -> str:
    """Retourne le SHA du dernier commit HEAD du dépôt.

    Args:
        repo_root: Racine du dépôt Git.

    Returns:
        SHA complet (hex) du commit courant.

    Raises:
        RuntimeError: si `git rev-parse HEAD` échoue.
    """
    rc, out, err = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    if rc != 0:
        raise RuntimeError(f"rev-parse failed: {err}")
    return out.strip()


# ------------------------------ fabrique PB ------------------------------

def _make_minimal_pb() -> PatchBlock:
    """Construit un PatchBlock minimal (balises + petite fonction).

    Le FileChecker/ModuleChecker MVP accepte la présence des balises et
    la détection d’un `def `, ce qui doit conduire à une action APPLY.

    Returns:
        PatchBlock prêt pour le pipeline local.
    """
    meta_inline = (
        "#{begin_meta: { file: demo/hello.py, module: demo, role: utility, "
        "plan_line_id: PL-0001, status_agent_file_checker: pending, "
        "status_agent_module_checker: pending }}\n"
    )
    body = (
        "def hello(name: str) -> str:\n"
        "    \"\"\"mARCHCode/ACW\n"
        "    Rôle: utility\n"
        "    Acceptance (rappel):\n"
        "    - retourne une salutation\n"
        "    \"\"\"\n"
        "    return f\"Hello {name}!\"\n"
    )
    code = f"{meta_inline}{body}#{'{' }end_meta{'}'}\n"

    meta = MetaBlock(
        file="demo/hello.py",
        module="demo",
        role="utility",
        plan_line_id="PL-0001",
    )
    pb = PatchBlock(
        code=code,
        meta=meta,
        global_status=None,
        next_action=None,
        source_agent="e2e_test",
    )
    return pb


# --------------------------------- test ----------------------------------

def test_e2e_smoke_commit():
    """Teste le scénario e2e minimal avec commit Git simulé.

    Pré-conditions :
        - Git disponible sur l’hôte de test.

    Étapes :
        1) Initialise un dépôt Git temporaire.
        2) Construit un PatchBlock minimal (balises + def).
        3) Exécute `run_patch_local` avec de vrais adaptateurs Git.
        4) Vérifie qu’un commit a bien été créé.

    Assertions :
        - La décision vaut APPLY (conditions remplies).
        - Le fichier cible existe dans le dépôt.
        - Un SHA valide est présent en HEAD.
    """
    repo = _init_repo()
    adapters = RealGitAdapters(repo_root=repo)
    pb = _make_minimal_pb()

    # Run end-to-end (Phase 3)
    pb_out, decision = run_patch_local(
        pb,
        adapters,
        policy=None,
        diff_stats=None,
        branch_name=None,
        archive_dir=None,
    )

    # Sanity assertions
    assert isinstance(decision, DRDecision)
    assert decision.action in (Action.APPLY, Action.RETRY, Action.ROLLBACK)

    # On attend APPLY dans ce smoke (balises + def)
    assert decision.action == Action.APPLY, f"action={decision.action}, summary={decision.summary}"

    # Le fichier doit exister et être committé
    target = repo / pb.meta.file
    assert target.exists(), f"file not found: {target}"
    sha = _last_commit_sha(repo)
    assert len(sha) >= 7, "commit SHA manquant ou invalide"
