# core/context_loader.py
from __future__ import annotations
"""
Context Loader — Injection du contexte YAML dans les prompts ACW
================================================================

Rôle du module
--------------
- Charger le fichier context_snapshot.yaml
- Le transformer en dict Python
- Fournir un accès simple à des sous-sections utiles (règles, styles, conventions…)
- Éviter tout parsing lourd côté agent_code_writer

Entrées / Sorties
-----------------
Entrée :
  - Chemin vers un fichier YAML de contexte (par défaut: config/context_snapshot.yaml)
Sortie :
  - Dict Python exploitable directement dans un PlanLine ou un prompt
"""

import yaml
from pathlib import Path
from typing import Any

DEFAULT_CONTEXT_FILE = Path(__file__).resolve().parent.parent / "config" / "context_snapshot.yaml"


def load_context_snapshot(path: Path = DEFAULT_CONTEXT_FILE) -> dict[str, Any]:
    """
    Charge un fichier `context_snapshot.yaml` et retourne sa représentation Python.

    Args:
        path: Chemin du fichier YAML à charger. Par défaut, `config/context_snapshot.yaml`.

    Returns:
        dict[str, Any]: Le contenu décodé du YAML ({} si le fichier est vide).

    Raises:
        FileNotFoundError: Si le fichier n'existe pas à l'emplacement fourni.
        yaml.YAMLError: Si le contenu YAML est invalide ou non lisible.
    """
    if not path.exists():
        raise FileNotFoundError(f"[context_loader] Fichier introuvable: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data
