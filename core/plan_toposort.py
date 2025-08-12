# core/plan_toposort.py
from __future__ import annotations

"""
mARCHCode — Tri topologique sur plan_lines par depends_on (placeholder)
=======================================================================

Rôle
----
Ce module fournira une fonction de tri topologique sur les `plan_lines`
d’un module, en tenant compte du champ `depends_on` de chaque plan_line.

Contexte d’utilisation
----------------------
- À utiliser dans la phase de planification (ex. agent_project_planner ou
  agent_module_compilator) pour ordonner les plan_lines avant passage à ACWP.
- Permet d'exécuter les plan_lines dans un ordre qui respecte leurs dépendances.
- Si un cycle est détecté (dépendances circulaires), le plan du module
  sera rejeté (erreur).

Statut
------
MVP mARCHCode : non implémenté (placeholder).  
À activer uniquement lorsque l’on aura un lot conséquent de plan_lines avec
dépendances croisées.

Notes techniques
----------------
- Algorithme envisagé : tri topologique simple (Kahn) ou DFS avec détection
  de cycle.
- Les dépendances sont exprimées par plan_line_id dans la clé `depends_on`.
- Hypothèse : toutes les plan_lines appartiennent au même module.
"""

from typing import List, Dict, Any


def toposort_plan_lines(plan_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Trie les plan_lines d’un module en respectant les dépendances (`depends_on`).

    Paramètres
    ----------
    plan_lines : list[dict]
        Liste des plan_lines du module. Chaque dict contient au moins :
        - plan_line_id : str
        - depends_on   : list[str] (optionnel)

    Retour
    ------
    list[dict]
        Liste des plan_lines réordonnée pour respecter les dépendances.

    Lève
    ----
    ValueError
        Si un cycle de dépendances est détecté.

    Statut
    ------
    Placeholder — non implémenté dans le MVP.
    """
    raise NotImplementedError("Tri topologique non implémenté dans le MVP mARCHCode.")

