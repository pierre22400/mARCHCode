test_smoke_phase3.py



import tempfile
import subprocess
from pathlib import Path
from core.types import PatchBlock, PatchMeta
from core.orchestrator import run_patch_local, DefaultConsoleAdapters
from core.decision_router import Decision, Action

"""
============================================================
Golden Test e2e (Small) — mARCHCode Phase 3
============================================================

But du test
-----------
Exécuter un pipeline Phase 3 minimal dans un repo éphémère :
  - Création d’un repo Git vide
  - Injection d’un PatchBlock minimal
  - Appel à run_patch_local()
  - Vérification qu’un commit est créé OU qu’un fichier apparaît (dry-run)

Usage
-----------
Lance un run Phase 3 local depuis un PatchBlock.

Vérifie que l’action est bien une des trois possibles (APPLY/RETRY/ROLLBACK).

Peut détecter qu’un fichier a été créé (en APPLY).
-----
pytest -s tests/test_smoke_phase3.py
"""
"""
Tests end-to-end pour la phase 3 de mARCHCode.
Permet de vérifier que la génération et l’application des patchs
fonctionnent sur un scénario réduit.
"""

def init_temp_repo() -> Path:
    """Crée un repo Git temporaire."""
    repo_dir = Path(tempfile.mkdtemp(prefix="marchcode_e2e_"))
    subprocess.run(["git", "init"], cwd=repo_dir, check=True)
    return repo_dir


def make_dummy_patch(repo_dir: Path) -> PatchBlock:
    """Construit un PatchBlock minimal avec fichier Python fictif."""
    file_path = repo_dir / "dummy.py"
    pb = PatchBlock(
        code="# test mARCHCode\nprint('hello world')\n",
        meta=PatchMeta(
            file="dummy.py",
            module="demo_module",
            role="utility",
            plan_line_id="PL-0001"
        )
    )
    return pb


def test_e2e_small():
     """Teste un petit scénario e2e sur la phase 3."""
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
        archive_dir=None  # désactive l’archivage
    )

    # Vérifications basiques
    assert isinstance(decision, Decision)
    assert decision.action in (Action.APPLY, Action.RETRY, Action.ROLLBACK)

    # Dry-run : on vérifierait juste la présence du fichier
    if decision.action == Action.APPLY:
        assert (repo_dir / pb.meta.file).exists()
