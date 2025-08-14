
# agents/agent_spec_inferer.py
from __future__ import annotations

import os
import argparse
from pathlib import Path
from typing import Any, Dict, Tuple

from core.context import (
    load_bus_message,
    save_bus_message,
    enrich_with_internal_annotations,
    SpecBlock,
)

"""
===============================================================================
mARCHCode — agent_spec_inferer (Bouchon MVP : désactivé)
-------------------------------------------------------------------------------
Rôle du module
  - Préserver l’API et le câblage du pipeline (nœud ASI) sans implémenter
    la recherche/extraction/fusion (RAG, embeddings, etc.).
  - Refuser proprement l’inférence : retour no-op (SpecBlock inchangé) et
    annotation interne expliquant que l’agent est "disabled" au stade MVP.
  - Zero réseau, zero LLM, zero dépendance externe.

Utilisation rapide (exemples)
  - Vérifier le statut :
      python -m agents.agent_spec_inferer status
  - Annoter explicitement un bus_message pour tracer le refus (sans modifier
    le contenu métier) :
      python -m agents.agent_spec_inferer annotate bus_message.yaml --persist

Décisions clés
  - Par défaut, l’agent est désactivé (SPEC_INFERER_ENABLED=False).
  - Une variable d’environnement ARCHCODE_ENABLE_SPEC_INFERER=1 activerait
    *théoriquement* l’agent, mais ici nous levons NotImplementedError pour
    garantir qu’aucune fonctionnalité "IA" non voulue ne s’exécute en MVP.

Sorties
  - SpecBlock renvoyé tel quel (no-op).
  - Optionnel : insertion d’une note interne dans free_field_2.agent_spec_inferer
    pour tracer le statut "disabled" (persisté si --persist).
===============================================================================
"""


def _env_enabled() -> bool:
    """
    Détermine si l’agent est marqué comme activable via l’environnement.

    Retour
    ------
    bool
        True si ARCHCODE_ENABLE_SPEC_INFERER ∈ {"1","true","yes"} (case-insensitive).
    """
    val = os.environ.get("ARCHCODE_ENABLE_SPEC_INFERER", "").strip().lower()
    return val in {"1", "true", "yes"}


# Bascule globale (reste False en MVP ; si True → on lève NotImplementedError)
SPEC_INFERER_ENABLED: bool = _env_enabled()


def agent_spec_inferer(bus_message_path: Path, *, persist: bool = False) -> SpecBlock:
    """
    Point d’entrée bouchon de l’agent d’inférence.

    Paramètres
    ----------
    bus_message_path : Path
        Chemin du fichier `bus_message.yaml` à charger.
    persist : bool
        Si True, persiste une annotation interne indiquant que l’agent est désactivé.

    Retour
    ------
    SpecBlock
        Spécification rechargée telle quelle (no-op), éventuellement annotée.

    Comportement
    ------------
    - Si SPEC_INFERER_ENABLED est False (MVP) :
        * charge et renvoie le SpecBlock
        * ajoute une note interne sous free_field_2.agent_spec_inferer
          (status="disabled") ; persist si demandé.
    - Si SPEC_INFERER_ENABLED est True :
        * lève NotImplementedError pour garantir qu’aucune logique IA ne tourne.
    """
    spec: SpecBlock = load_bus_message(bus_message_path, auto_fill=True)

    if SPEC_INFERER_ENABLED:
        raise NotImplementedError(
            "agent_spec_inferer est marqué 'enabled' mais l’implémentation IA "
            "est volontairement absente en MVP (pas de RAG/embeddings)."
        )

    # Annotation interne (non destructive) pour tracer le refus contrôlé
    note: Dict[str, Any] = {
        "agent": "agent_spec_inferer",
        "status": "disabled",
        "reason": "Prototype MVP — mode assisté non implémenté (pas de RAG/LLM).",
    }
    current_ff2 = spec.free_field_2 if isinstance(spec.free_field_2, dict) else {}
    merged = dict(current_ff2)
    merged["agent_spec_inferer"] = note
    spec = enrich_with_internal_annotations(spec, {"free_field_2": merged})

    if persist:
        save_bus_message(spec, bus_message_path)

    return spec


def get_status() -> Tuple[bool, str]:
    """
    Retourne le statut de l’agent.

    Retour
    ------
    (enabled, message) : Tuple[bool, str]
        enabled = False en MVP ; message explicite pour les logs/UX.
    """
    if SPEC_INFERER_ENABLED:
        return True, (
            "agent_spec_inferer marqué 'enabled' via ENV, "
            "mais l’implémentation IA est bloquée en MVP (NotImplementedError)."
        )
    return False, "agent_spec_inferer désactivé (MVP, no-op sécurisé)."


# -----------------------------------------------------------------------------
# CLI minimale (facultative, utile en local et CI)
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """
    Construit le parseur CLI (status, annotate).

    Retour
    ------
    argparse.ArgumentParser
        Parseur prêt à l’emploi.
    """
    p = argparse.ArgumentParser(
        prog="agent_spec_inferer",
        description="Bouchon MVP — désactive l’inférence analogique (pas de RAG/LLM).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_status = sub.add_parser("status", help="Affiche le statut de l’agent (enabled/disabled)")

    sp_annot = sub.add_parser(
        "annotate",
        help="Charge un bus_message.yaml et annote free_field_2.agent_spec_inferer (status=disabled).",
    )
    sp_annot.add_argument("bus_message", type=Path, help="Chemin vers bus_message.yaml")
    sp_annot.add_argument(
        "--persist",
        action="store_true",
        help="Persiste l’annotation directement dans le YAML",
    )

    return p


def _cmd_status() -> int:
    """
    Affiche le statut courant de l’agent.

    Retour
    ------
    int
        Code de sortie (0 si disabled en MVP, 3 si marqué enabled).
    """
    enabled, msg = get_status()
    print(f"[agent_spec_inferer] {msg}")
    return 3 if enabled else 0


def _cmd_annotate(bus_message: Path, persist: bool) -> int:
    """
    Annote un bus_message avec le statut 'disabled' et éventuellement persiste.

    Paramètres
    ----------
    bus_message : Path
        Chemin du bus_message.yaml à traiter.
    persist : bool
        Si True, réécrit le fichier avec l’annotation interne.

    Retour
    ------
    int
        Code de sortie (0 si OK).
    """
    _ = agent_spec_inferer(bus_message, persist=persist)
    print(f"[agent_spec_inferer] Annotation 'disabled' appliquée ({'persistée' if persist else 'non persistée'}).")
    return 0


def main(argv: list[str] | None = None) -> None:
    """
    Point d’entrée CLI.

    Paramètres
    ----------
    argv : list[str] | None
        Arguments CLI (None → sys.argv[1:]).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "status":
        raise SystemExit(_cmd_status())
    elif args.cmd == "annotate":
        raise SystemExit(_cmd_annotate(args.bus_message, args.persist))
    else:
        parser.print_help()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
