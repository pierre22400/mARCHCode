
# scripts/spec_table_cli.py
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Tuple
import argparse
import textwrap

import yaml

from core.context import (
    load_bus_message,
    save_bus_message,
    validate_specblock,
    create_execution_context,
    SpecBlock,
)

"""
===============================================================================
mARCHCode — Spec Table CLI (Option 2 : tableau expert, sans frontend)
-------------------------------------------------------------------------------
But du fichier
  - Générer un template YAML éditable à la main (.archcode/bus_message.template.yaml)
  - Valider le fichier complété par l'utilisateur
  - Geler une version propre en bus_message.yaml (avec auto-complétion sûre)

Pourquoi ici ?
  - mARCHCode (MVP) vise la Phase 1 sans interface : on travaille en fichiers.
  - Le module s'appuie sur core/context.py (SpecBlock & API) et n'ajoute aucun
    effet de bord réseau ni LLM.

Commandes
  - init       : crée/écrase prudemment le template YAML à remplir
  - validate   : vérifie le YAML fourni (forme, champs requis)
  - freeze     : valide + sérialise en bus_message.yaml, prêt pour PHASE_2

Exemples d'utilisation (Windows / Linux)
  - python scripts/spec_table_cli.py init
  - python scripts/spec_table_cli.py validate .archcode/bus_message.template.yaml
  - python scripts/spec_table_cli.py freeze .archcode/bus_message.template.yaml

Notes
  - Le template est volontairement explicite : listes vides à compléter.
  - La validation s’appuie sur SpecBlock.validate() via core/context.py.
  - En cas d’erreurs, le process sort avec code 2 (pratique pour CI/automation).


===============================================================================
"""

# --------------------------------------------------------------------------- #
# Contenu du template YAML (lisible et prêt à éditer)
# --------------------------------------------------------------------------- #

_TEMPLATE_YAML = textwrap.dedent(
    """\
    # =====================================================================
    # ARCHCode — bus_message.template.yaml (Tableau expert à compléter)
    # =====================================================================
    # Remplis les champs clés (title, summary, functional_objectives, etc.)
    # Les listes [] doivent contenir des valeurs explicites (une par ligne).
    # ---------------------------------------------------------------------

    bus_message_id: ""     # laisse vide pour auto-assignation (BUS-xxxx)
    timestamp: ""          # laisse vide pour auto-remplissage 'YYYY-MM-DD HH:MM'

    # ---- Spécification fonctionnelle (OBLIGATOIRE) -----------------------
    title: "À RENSEIGNER — titre court du projet"
    summary: "À RENSEIGNER — résumé synthétique (2–4 lignes)"
    functional_objectives: []    # ex: ["Créer un compte", "Exporter un PDF"]

    # ---- Histoires utilisateur (OPTIONNEL mais recommandé) ---------------
    user_stories:
      # - { id: "US-0001", story: "En tant que RH, je peux créer un salarié…" }

    # ---- Contraintes et contexte (FACULTATIF) ----------------------------
    non_functional_constraints: []      # ex: ["RGPD", "Temps de réponse < 200 ms"]
    target_audience: ""
    deployment_context: ""              # ex: "API-only", "on-premise", "mobile"
    input_sources: []                   # ex: ["formulaire", "CSV", "voix"]
    output_targets: []                  # ex: ["tableau", "PDF", "email"]
    architectural_preferences: []       # ex: ["REST", "event-driven"]
    preferred_llm: ""                   # ex: "GPT-5 Thinking", "Claude 3.5"

    # ---- Mode & traçabilité ----------------------------------------------
    source_mode: "manual"               # "manual" ou "dialogue"
    llm_aid: false                      # true si un LLM a aidé la saisie
    spec_version: "v1"

    # ---- Commentaires libres ---------------------------------------------
    comment_human: ""
    comment_llm: ""

    # ---- Champs libres internes (option) ---------------------------------
    free_field_1: null
    free_field_2: null

    # ---- Suivi de schéma interne (ne pas modifier) -----------------------
    _schema_version: "1.0.0"
    """
)

# --------------------------------------------------------------------------- #
# Fonctions utilitaires (docstrings obligatoires)
# --------------------------------------------------------------------------- #

def _write_text(path: Path, content: str, *, overwrite: bool) -> None:
    """
    Écrit `content` dans `path`.

    Paramètres
    ----------
    path : Path
        Destination du fichier.
    content : str
        Contenu à écrire.
    overwrite : bool
        Autorise l'écrasement si True.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Déjà présent, utilise --force : {path}")
    path.write_text(content, encoding="utf-8")


def cmd_init_template(dest: Path, force: bool = False) -> None:
    """
    Génère le template YAML à compléter manuellement.

    Paramètres
    ----------
    dest : Path
        Emplacement du template (ex: .archcode/bus_message.template.yaml).
    force : bool
        Écrase si déjà présent.
    """
    _write_text(dest, _TEMPLATE_YAML, overwrite=force)
    print(f"[OK] Template créé : {dest}")


def _safe_load_yaml(path: Path) -> Dict[str, Any]:
    """
    Charge un YAML en dict (sécurisé), avec message explicite si vide.

    Paramètres
    ----------
    path : Path
        Chemin du fichier YAML.

    Retour
    ------
    Dict[str, Any]
        Données chargées (dict éventuellement vide).

    Exceptions
    ----------
    FileNotFoundError si absent ; yaml.YAMLError si invalide.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def cmd_validate(yaml_path: Path) -> Tuple[bool, Tuple[int, ...]]:
    """
    Valide un bus_message YAML en s'appuyant sur SpecBlock.

    Paramètres
    ----------
    yaml_path : Path
        Chemin du YAML (template complété par l'utilisateur).

    Retour
    ------
    (ok, exit_codes) : Tuple[bool, Tuple[int,...]]
        ok=True si conforme ; exit_codes guide la sortie process.
    """
    # load_bus_message applique des complétions sûres (bus_id/timestamp)
    spec = load_bus_message(yaml_path, auto_fill=True)
    ok, errors = validate_specblock(spec)

    if ok:
        print("[OK] Validation SpecBlock : conforme ✅")
        return True, (0,)
    else:
        print("[ERREUR] Spécification invalide ❌")
        for e in errors:
            print(f"  - {e}")
        return False, (2,)


def cmd_freeze(yaml_path: Path, out_path: Path) -> None:
    """
    Gèle le YAML validé en `bus_message.yaml` (frozen intent).

    Étapes
    ------
    1) Réinstancie via load_bus_message(auto_fill=True)
    2) Valide via validate_specblock
    3) Écrit proprement via save_bus_message

    Paramètres
    ----------
    yaml_path : Path
        YAML source (complété par l'utilisateur).
    out_path : Path
        Destination finale (ex: bus_message.yaml).
    """
    spec = load_bus_message(yaml_path, auto_fill=True)
    ok, errors = validate_specblock(spec)
    if not ok:
        print("[ERREUR] Impossible de geler : la spécification est invalide.")
        for e in errors:
            print(f"  - {e}")
        raise SystemExit(2)

    save_bus_message(spec, out_path)
    print(f"[OK] bus_message gelé → {out_path}")

    # Optionnel : on démontre que c'est exploitable immédiatement en PHASE_2.
    # (Ne fait rien d'autre que construire l'ExecutionContext sans side-effects)
    _ = create_execution_context(spec, bus_message_path=out_path)
    print("[OK] ExecutionContext prêt (Phase 2 pourra consommer).")


# --------------------------------------------------------------------------- #
# CLI (argparse, standard lib)
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    """
    Construit le parseur d'arguments (sous-commandes init/validate/freeze).

    Retour
    ------
    argparse.ArgumentParser
        Parseur prêt à l'emploi.
    """
    p = argparse.ArgumentParser(
        prog="spec-table",
        description="mARCHCode — Option 2 (tableau expert) : template, validation, gel.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # init
    sp_init = sub.add_parser("init", help="Génère le template à compléter")
    sp_init.add_argument(
        "--dest",
        type=Path,
        default=Path(".archcode") / "bus_message.template.yaml",
        help="Chemin du template (défaut: .archcode/bus_message.template.yaml)",
    )
    sp_init.add_argument(
        "--force",
        action="store_true",
        help="Écrase le fichier s'il existe déjà",
    )

    # validate
    sp_val = sub.add_parser("validate", help="Valide le template complété")
    sp_val.add_argument(
        "yaml_path",
        type=Path,
        help="Chemin du YAML à valider (template complété)",
    )

    # freeze
    sp_freeze = sub.add_parser("freeze", help="Valide puis gèle en bus_message.yaml")
    sp_freeze.add_argument(
        "yaml_path",
        type=Path,
        help="Chemin du YAML à geler",
    )
    sp_freeze.add_argument(
        "--out",
        type=Path,
        default=Path("bus_message.yaml"),
        help="Destination finale (défaut: ./bus_message.yaml)",
    )

    return p


def main(argv: list[str] | None = None) -> None:
    """
    Point d'entrée CLI.

    Paramètres
    ----------
    argv : list[str] | None
        Arguments de la ligne de commande (None → sys.argv[1:]).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "init":
            cmd_init_template(dest=args.dest, force=args.force)

        elif args.cmd == "validate":
            ok, exits = cmd_validate(yaml_path=args.yaml_path)
            raise SystemExit(exits[0])

        elif args.cmd == "freeze":
            cmd_freeze(yaml_path=args.yaml_path, out_path=args.out)

        else:
            parser.print_help()
            raise SystemExit(1)

    except FileExistsError as e:
        print(f"[ERREUR] {e}")
        raise SystemExit(1)
    except FileNotFoundError as e:
        print(f"[ERREUR] {e}")
        raise SystemExit(1)
    except yaml.YAMLError as e:
        print(f"[ERREUR YAML] {e}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
