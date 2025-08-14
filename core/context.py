
# core/context.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple, Literal
from pathlib import Path
from datetime import datetime
import uuid
import yaml



"""
===============================================================================
ARCHCode / mARCHCode — PHASE 1 : Spécification initiale
-------------------------------------------------------------------------------
Rôle du module
  - Définir la classe canonique `SpecBlock` (socle sémantique du projet).
  - Gérer l’artefact `bus_message.yaml` : chargement, validation, normalisation,
    persistance, et réinstanciation contrôlée.
  - Produire un `ExecutionContext` minimal prêt pour PHASE_2.

Principes clés (MVP local, sans effets de bord) :
  - Aucune I/O réseau, aucun appel LLM direct : ce module reste pur et testable.
  - Champs et casse strictement alignés sur la table « Classe SpecBlock » du
    tiddler (pipeline fourni).
  - `source_mode` ∈ {"manual", "dialogue"} et `llm_aid` ∈ {True, False}.
  - `bus_message.yaml` est la source d’intention "figée" (frozen intent) ;
    `SpecBlock` peut être enrichi dynamiquement (annotations internes).

Compatibilité brownfield :
  - Lecture tolérante : injection de valeurs par défaut sûres.
  - Génération d’identifiants stables si absents (BUS-xxxx / US-xxxx).
  - Jamais d’installation de dépendances ni d’effets système.

Sorties principales :
  - `SpecBlock`: instance validée/enrichie.
  - `ExecutionContext`: conteneur léger pour propager `SpecBlock` en PHASE_2.

Tests rapides conseillés (ex. console) :
  - sb = load_bus_message("bus_message.yaml")
  - ok, errs = validate_specblock(sb)
  - if ok: ctx = create_execution_context(sb, Path("bus_message.yaml"))

NOTE: imports internes (facultatifs ici, sans effets de bord)
from core.types import PlanLine  # potentiellement utile en phase 2
from core.error_policy import ErrorCategory  # réservé aux vérifications avancées
===============================================================================
"""


# --------------------------------------------------------------------------- #
# Constantes de schéma / garde-fous
# --------------------------------------------------------------------------- #

SCHEMA_VERSION: str = "1.0.0"
_ALLOWED_SOURCE_MODES: Tuple[str, str] = ("manual", "dialogue")


def _now_hhmm() -> str:
    """
    Renvoie la date/heure locale au format 'YYYY-MM-DD HH:MM' (sans secondes).

    Utilité :
        Respecter le format attendu par le champ `timestamp` du SpecBlock.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _new_bus_id() -> str:
    """
    Génère un identifiant court et stable de type 'BUS-xxxxxxxx'.

    Utilité :
        Assigner un `bus_message_id` si absent dans un YAML hérité/brownfield.
    """
    return f"BUS-{uuid.uuid4().hex[:8]}"


def _new_user_story_id(n: int) -> str:
    """
    Génère un identifiant de user story séquentiel 'US-0001', 'US-0002', etc.

    Paramètres
    ----------
    n : int
        Index (1-based) de la story.

    Retour
    ------
    str
        Identifiant normalisé de la forme 'US-000n'.
    """
    return f"US-{n:04d}"


# --------------------------------------------------------------------------- #
# Dataclasses : SpecBlock & ExecutionContext
# --------------------------------------------------------------------------- #

@dataclass
class SpecBlock:
    """
    Représentation formelle du bus_message, enrichissable et validable.

    Champs canoniques (alignés avec le tiddler) :
      - bus_message_id : str (✅, auto si manquant)
      - timestamp : str 'YYYY-MM-DD HH:MM' (✅, auto si manquant)
      - title : str (✅)
      - summary : str (✅)
      - functional_objectives : list[str] (✅)
      - user_stories : list[dict] (🟡) — ex. {"id": "US-0001", "story": "..."}
      - non_functional_constraints : list[str] (🟡)
      - target_audience : str (🟡)
      - deployment_context : str (🟡)
      - input_sources : list[str] (🔁)
      - output_targets : list[str] (🔁)
      - architectural_preferences : list[str] (🟡)
      - preferred_llm : str (🟡)
      - source_mode : Literal['manual','dialogue'] (✅)
      - llm_aid : bool (✅)
      - spec_version : str (🔁) — défaut 'v1'
      - comment_human : str (🟡)
      - comment_llm : str (🔁)
      - free_field_1 : str | dict | None (🔁)
      - free_field_2 : str | list | None (🔁)

    Champs internes invisibles :
      - _schema_version : str — suivi du schéma local de SpecBlock.
    """

    # Obligatoires/primaires
    bus_message_id: str
    timestamp: str
    title: str
    summary: str
    functional_objectives: List[str]
    source_mode: Literal["manual", "dialogue"]
    llm_aid: bool

    # Optionnels (avec défauts sûrs)
    user_stories: List[Dict[str, str]] = field(default_factory=list)
    non_functional_constraints: List[str] = field(default_factory=list)
    target_audience: Optional[str] = None
    deployment_context: Optional[str] = None
    input_sources: List[str] = field(default_factory=list)
    output_targets: List[str] = field(default_factory=list)
    architectural_preferences: List[str] = field(default_factory=list)
    preferred_llm: Optional[str] = None
    spec_version: str = "v1"
    comment_human: Optional[str] = None
    comment_llm: Optional[str] = None
    free_field_1: Optional[Any] = None
    free_field_2: Optional[Any] = None

    # Interne (non exposé côté utilisateur)
    _schema_version: str = SCHEMA_VERSION

    # ----------------------------- Méthodes -------------------------------- #

    def to_yaml_dict(self) -> Dict[str, Any]:
        """
        Retourne une projection sérialisable YAML du SpecBlock (sans fuite interne).

        Notes :
            - `_schema_version` est conservé pour le suivi interne.
            - Aucune transformation destructive : les champs optionnels vides
              restent présents s’ils ont un sens pour la lecture humaine.
        """
        d = asdict(self)
        # Rien à masquer pour MVP — conserver `_schema_version` utile en audit.
        return d

    def normalize(self) -> None:
        """
        Normalise certains champs (liste/chaîne, tri léger, complétions d’IDs).

        Règles :
            - Assigne des IDs de user stories si absents.
            - Déduplique `functional_objectives`, `input_sources`, `output_targets`.
        """
        # Déduplication simple, ordre conservé
        def _dedup(seq: List[str]) -> List[str]:
            seen = set()
            out: List[str] = []
            for x in seq:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
            return out

        self.functional_objectives = _dedup([s.strip() for s in self.functional_objectives if str(s).strip()])
        self.input_sources = _dedup([s.strip() for s in self.input_sources if str(s).strip()])
        self.output_targets = _dedup([s.strip() for s in self.output_targets if str(s).strip()])

        # User stories : impose la présence d'un 'id' (US-xxxx)
        normalized_us: List[Dict[str, str]] = []
        counter = 1
        for item in self.user_stories:
            story = str(item.get("story", "")).strip()
            if not story:
                continue
            uid = str(item.get("id", "")).strip()
            if not uid:
