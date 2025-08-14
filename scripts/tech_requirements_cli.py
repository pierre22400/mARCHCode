
# scripts/tech_requirements_cli.py
from __future__ import annotations

import argparse
import os
import platform
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from core.context import (
    load_bus_message,
    save_bus_message,
    enrich_with_internal_annotations,
    SpecBlock,
)

"""
===============================================================================
mARCHCode — Technical Requirements CLI (PHASE 1)
-------------------------------------------------------------------------------
Rôle du module
  - Générer un template `.archcode/technical_requirements.template.yaml`
  - Valider ce YAML (présence/forme des champs critiques)
  - Attacher les TR dans le SpecBlock (free_field_1.technical_requirements)
    et réécrire `bus_message.yaml` (conforme au nœud TR du diagramme).

Conception
  - Zéro effet de bord système : collecte auto *facultative* (best-effort)
  - Pas de réseau / pas de LLM : 100% local
  - Aligné avec core/context.py (utilise enrich_with_internal_annotations)

Commandes
  - init       : crée le template TR (pré-rempli avec détection locale optionnelle)
  - validate   : contrôle la conformité d’un TR YAML
  - attach     : injecte les TR dans un bus_message (SpecBlock), puis sauvegarde

Exemples
  - python scripts/tech_requirements_cli.py init
  - python scripts/tech_requirements_cli.py validate .archcode/technical_requirements.template.yaml
  - python scripts/tech_requirements_cli.py attach .archcode/technical_requirements.template.yaml bus_message.yaml


===============================================================================
"""

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _detect_local_defaults() -> Dict[str, Any]:
    """
    Détecte prudemment quelques valeurs locales (sans effets de bord).

    Retour
    ------
    Dict[str, Any]
        Valeurs par défaut à insérer dans le template (os, python, proxy).
    """
    os_name = platform.system()
    os_version = platform.version()
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    net_proxy = proxy or https_proxy

    return {
        "os": {"name": os_name, "version": os_version},
        "python": {"installed": True, "version": py_ver},
        "network": {
            "internet_access": "unknown",  # yes/no/unknown
            "proxy": net_proxy,
        },
        "admin_rights": "unknown",  # yes/no/unknown
        "package_install_policy": "unknown",
        "antivirus_restrictions": [],
        "third_party_software_constraints": [],
        "reuse_existing_dependencies": False,
        "notes": "",
    }


def _write_text(path: Path, content: str, *, overwrite: bool) -> None:
    """
    Écrit `content` dans `path` avec création de dossier si nécessaire.

    Paramètres
    ----------
    path : Path
        Chemin de sortie.
    content : str
        Contenu à écrire.
    overwrite : bool
        Vrai pour autoriser l’écrasement.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Fichier déjà présent, utilise --force : {path}")
    path.write_text(content, encoding="utf-8")


def _safe_load_yaml(path: Path) -> Dict[str, Any]:
    """
    Charge un YAML en dict.

    Paramètres
    ----------
    path : Path
        Chemin du YAML.

    Retour
    ------
    Dict[str, Any]
        Contenu YAML (dict).

    Exceptions
    ----------
    FileNotFoundError, yaml.YAMLError
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def _render_template(defaults: Dict[str, Any]) -> str:
    """
    Rend le YAML template TR avec `defaults`.

    Paramètres
    ----------
    defaults : Dict[str, Any]
        Valeurs de départ à injecter.

    Retour
    ------
    str
        Contenu YAML prêt à écrire.
    """
    doc: Dict[str, Any] = {
        "technical_requirements": {
            # --- Système d’exploitation ---
            "os": {
                "name": defaults.get("os", {}).get("name", "Windows/Linux/macOS"),
                "version": defaults.get("os", {}).get("version", "10/11/Ubuntu 22.04/macOS 14"),
            },
            # --- Python ---
            "python": {
                "installed": defaults.get("python", {}).get("installed", False),
                "version": defaults.get("python", {}).get("version", "3.11.6"),
            },
            # --- Réseau ---
            "network": {
                "internet_access": defaults.get("network", {}).get("internet_access", "unknown"),
                "proxy": defaults.get("network", {}).get("proxy", ""),
            },
            # --- Droits / Politique sécurité ---
            "admin_rights": defaults.get("admin_rights", "unknown"),  # yes/no/unknown
            "package_install_policy": defaults.get("package_install_policy", "unknown"),
            "antivirus_restrictions": defaults.get("antivirus_restrictions", []),  # list[str]
            # --- Contrainte logicielle tierce ---
            "third_party_software_constraints": defaults.get("third_party_software_constraints", []),
            # --- Réemploi de dépendances système ---
            "reuse_existing_dependencies": defaults.get("reuse_existing_dependencies", False),
            # --- Notes libres ---
            "notes": defaults.get("notes", ""),
        }
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


# --------------------------------------------------------------------------- #
# Validation logique du document TR
# --------------------------------------------------------------------------- #

_REQUIRED_PATHS = [
    ("technical_requirements", dict),
    ("technical_requirements.os", dict),
    ("technical_requirements.os.name", str),
    ("technical_requirements.os.version", (str, int)),
    ("technical_requirements.python", dict),
    ("technical_requirements.python.installed", (bool, str)),
    ("technical_requirements.python.version", str),
    ("technical_requirements.network", dict),
    ("technical_requirements.network.internet_access", (str, bool)),
    ("technical_requirements.network.proxy", (str, type(None))),
    ("technical_requirements.admin_rights", (str, bool)),
    ("technical_requirements.package_install_policy", str),
    ("technical_requirements.antivirus_restrictions", list),
    ("technical_requirements.third_party_software_constraints", list),
    ("technical_requirements.reuse_existing_dependencies", bool),
    ("technical_requirements.notes", str),
]

def _dig(d: Dict[str, Any], path: str) -> Any:
    """
    Récupère une valeur dans un dict via un chemin pointé 'a.b.c'.

    Paramètres
    ----------
    d : Dict[str, Any]
        Dictionnaire racine.
    path : str
        Chemin 'dot-notation'.

    Retour
    ------
    Any
        Valeur trouvée ou None si absente.
    """
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def validate_tr_doc(doc: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Valide la structure du document TR.

    Paramètres
    ----------
    doc : Dict[str, Any]
        Document YAML chargé.

    Retour
    ------
    (ok, errors) : Tuple[bool, List[str]]
        ok=True si conforme, sinon liste d’erreurs lisibles.
    """
    errors: List[str] = []
    for dotted, expected_type in _REQUIRED_PATHS:
        val = _dig(doc, dotted)
        if val is None:
            errors.append(f"Champ manquant : {dotted}")
            continue
        if isinstance(expected_type, tuple):
            if not isinstance(val, expected_type):
                errors.append(f"Type invalide pour {dotted} (attendu {expected_type}, obtenu {type(val).__name__})")
        else:
            if not isinstance(val, expected_type):
                errors.append(f"Type invalide pour {dotted} (attendu {expected_type.__name__}, obtenu {type(val).__name__})")
    return (len(errors) == 0, errors)


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #

def cmd_init(dest: Path, force: bool, no_detect: bool) -> None:
    """
    Crée le template TR à compléter par l'utilisateur.

    Paramètres
    ----------
    dest : Path
        Emplacement du template (par défaut `.archcode/technical_requirements.template.yaml`).
    force : bool
        Écrase si le fichier existe déjà.
    no_detect : bool
        Si True, n’injecte pas les valeurs détectées localement.
    """
    defaults = {} if no_detect else _detect_local_defaults()
    content = _render_template(defaults)
    _write_text(dest, content, overwrite=force)
    print(f"[OK] Template TR créé : {dest}")


def cmd_validate(tr_yaml: Path) -> Tuple[bool, int]:
    """
    Valide un YAML de Technical Requirements.

    Paramètres
    ----------
    tr_yaml : Path
        Chemin du fichier TR YAML.

    Retour
    ------
    (ok, exit_code) : Tuple[bool, int]
        ok=True si valide ; exit_code adapté pour la CI.
    """
    doc = _safe_load_yaml(tr_yaml)
    ok, errs = validate_tr_doc(doc)
    if ok:
        print("[OK] TR valide ✅")
        return True, 0
    print("[ERREUR] TR invalide ❌")
    for e in errs:
        print(f"  - {e}")
    return False, 2


def cmd_attach(tr_yaml: Path, bus_yaml_in: Path, bus_yaml_out: Path | None) -> None:
    """
    Attache les TR dans un SpecBlock et sauvegarde le bus_message.

    Étapes
    ------
    1) charge TR + valide
    2) charge bus_message → SpecBlock
    3) enrichit free_field_1.technical_requirements
    4) sauvegarde bus_message (in-place ou vers --out)

    Paramètres
    ----------
    tr_yaml : Path
        Chemin du fichier TR.
    bus_yaml_in : Path
        Chemin de `bus_message.yaml` (ou son template gelable).
    bus_yaml_out : Path | None
        Destination (défaut : écriture in-place dans bus_yaml_in).
    """
    doc = _safe_load_yaml(tr_yaml)
    ok, errs = validate_tr_doc(doc)
    if not ok:
        print("[ERREUR] Impossible d'attacher : TR invalide.")
        for e in errs:
            print(f"  - {e}")
        raise SystemExit(2)

    spec: SpecBlock = load_bus_message(bus_yaml_in, auto_fill=True)

    # Merge non destructif : on place tout sous free_field_1.technical_requirements
    # en conservant free_field_1 éventuel (dict) si déjà présent.
    current_ff1 = spec.free_field_1 if isinstance(spec.free_field_1, dict) else {}
    merged = dict(current_ff1)
    merged["technical_requirements"] = doc.get("technical_requirements", {})
    spec = enrich_with_internal_annotations(spec, {"free_field_1": merged})

    out_path = bus_yaml_out or bus_yaml_in
    save_bus_message(spec, out_path)
    print(f"[OK] TR attachés → {out_path}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    """
    Construit le parseur d’arguments (init/validate/attach).

    Retour
    ------
    argparse.ArgumentParser
        Parseur configuré.
    """
    p = argparse.ArgumentParser(
        prog="tech-req",
        description="mARCHCode — Technical Requirements (PHASE 1) : template, validation, attache au bus_message.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_init = sub.add_parser("init", help="Génère le template TR")
    sp_init.add_argument(
        "--dest",
        type=Path,
        default=Path(".archcode") / "technical_requirements.template.yaml",
        help="Chemin du template TR",
    )
    sp_init.add_argument(
        "--force",
        action="store_true",
        help="Écraser le fichier s’il existe déjà",
    )
    sp_init.add_argument(
        "--no-detect",
        action="store_true",
        help="Ne pas pré-remplir avec détection locale (OS/Python/proxy)",
    )

    sp_val = sub.add_parser("validate", help="Valide un fichier TR YAML")
    sp_val.add_argument("tr_yaml", type=Path, help="Chemin du TR YAML")

    sp_att = sub.add_parser("attach", help="Attache TR dans bus_message.yaml")
    sp_att.add_argument("tr_yaml", type=Path, help="Chemin du TR YAML")
    sp_att.add_argument("bus_yaml", type=Path, help="Chemin du bus_message.yaml (ou template)")
    sp_att.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Chemin de sortie (défaut: écriture in-place)",
    )

    return p


def main(argv: List[str] | None = None) -> None:
    """
    Point d’entrée CLI.

    Paramètres
    ----------
    argv : List[str] | None
        Arguments transmis (None → sys.argv[1:]).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "init":
            cmd_init(dest=args.dest, force=args.force, no_detect=args.no_detect)

        elif args.cmd == "validate":
            ok, code = cmd_validate(tr_yaml=args.tr_yaml)
            raise SystemExit(code)

        elif args.cmd == "attach":
            cmd_attach(tr_yaml=args.tr_yaml, bus_yaml_in=args.bus_yaml, bus_yaml_out=args.out)

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
