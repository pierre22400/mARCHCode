from __future__ import annotations

"""
mARCHCode — Golden Test e2e (Small) — Phase 3
=============================================

But du test
-----------
Exécuter un pipeline Phase 3 minimal dans un repo éphémère :
  - Création d’un repo Git vide
  - Injection d’un PatchBlock minimal
  - Appel à run_patch_local()
  - Vérification que l’action est bien l’une des trois (APPLY/RETRY/ROLLBACK)

Note
----
Le PatchBlock volontairement simple ne contient pas de `def` ni de balises
ARCHCode ; le ModuleChecker le classera généralement en RETRY. Si l’action
devient APPLY via d’autres adaptateurs, le test vérifie en option la
présence du fichier créé.

Exécution
---------
pytest -s tests/test_smoke_phase3.py
"""

import tempfile
import subprocess
from pathlib import Path

from core.types import PatchBlock, MetaBlock
from core.orchestrator import run_patch_local, DefaultConsoleAdapters
from core.decision_router import Decision, Action


def init_temp_repo() -> Path:
    """Crée un dépôt Git temporaire initialisé pour le test.

    Returns:
        Path: Répertoire racine du dépôt Git temporaire.
    """
    repo_dir = Path(tempfile.mkdtemp(prefix="marchcode_e2e_"))
    subprocess.run(["git", "init"], cwd=repo_dir, check=True)
    return repo_dir


def make_dummy_patch(repo_dir: Path) -> PatchBlock:
    """Construit un PatchBlock minimal pointant vers `dummy.py`.

    Le code ne contient ni balises ARCHCode ni `def`, afin de rester
    neutre vis-à-vis du ModuleChecker (souvent → RETRY).

    Args:
        repo_dir: Répertoire du dépôt temporaire (non utilisé ici, gardé pour signature stable).

    Returns:
        PatchBlock: Patch minimal prêt à être passé au pipeline.
    """
    _ = repo_dir  # utilisé pour signature stable / future extension
    pb = PatchBlock(
        code="# test mARCHCode\nprint('hello world')\n",
        meta=MetaBlock(
            file="dummy.py",
            module="demo_module",
            role="utility",
            plan_line_id="PL-0001",
        ),
    )
    return pb


def test_e2e_small() -> None:
    """Teste un scénario e2e réduit de la phase 3 avec adaptateurs console.

    Étapes:
        1) Initialise un repo Git vide.
        2) Construit un PatchBlock minimal.
        3) Exécute `run_patch_local` avec `DefaultConsoleAdapters`.
        4) Vérifie que la décision est valide et, en cas d'APPLY, que le
           fichier attendu existe.
    """
    repo_dir = init_temp_repo()
    pb = make_dummy_patch(repo_dir)

    adapters = DefaultConsoleAdapters()

    # Exécution locale
    pb, decision = run_patch_local(
        pb,
        adapters,
        policy=None,
        branch_name="test_branch",
        diff_stats=None,
        archive_dir=None,  # désactive l’archivage
    )

    # Vérifications basiques
    assert isinstance(decision, Decision)
    assert decision.action in (Action.APPLY, Action.RETRY, Action.ROLLBACK)

    # En cas d’APPLY (selon adaptateurs), le fichier doit exister
    if decision.action == Action.APPLY:
        assert (repo_dir / pb.meta.file).exists()
