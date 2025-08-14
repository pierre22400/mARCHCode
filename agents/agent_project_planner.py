
# agents/agent_project_planner.py
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime

import yaml

"""
===============================================================================
ARCHCode — agent_project_planner (PHASE 2, Étape 1.5)
-------------------------------------------------------------------------------
But (aligné pipeline) :
  - Lire `.archcode/execution_context.yaml` (EC) — mémoire active.
  - Générer un `project_draft.yaml` déterministe, gouverné et lisible :
      project_draft:
        project_name: ...
        global_objectives: [...]
        initial_modules: [...]
        dependencies: [...]
        priority_map: {module: priorité}
        validation_mode: standard|strict
        folder_structure: {root, structure: [...]}
        issued_at: ISO-8601
        bus_message_id: ...
        spec_version_ref: ...
  - (option) Mettre à jour l’EC avec `project_name` et `modules` (lecture seule
    sémantique côté agents, mais ici on matérialise l’intention de pilotage).

Contraintes MVP :
  - Zéro LLM, zéro réseau. Heuristiques simples et documentées.
  - Compatible avec le diagramme Mermaid : nœud APP → PD["project_draft.yaml"].

Utilisation :
  - Générer le brouillon projet :
      python -m agents.agent_project_planner planify ./.archcode/execution_context.yaml
  - Mettre à jour EC.project_name et EC.modules en même temps :
      python -m agents.agent_project_planner planify ./.archcode/execution_context.yaml --update-ec
  - Afficher un résumé du brouillon :
      python -m agents.agent_project_planner show ./.archcode/project_draft.yaml

Auteur : Alex
===============================================================================
"""


# -----------------------------------------------------------------------------
# Utilitaires de base
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    """
    Retourne l'horodatage ISO-8601 (secondes) pour tracer l'émission d'un artefact.
    """
    return datetime.now().isoformat(timespec="seconds")


def _read_yaml(path: Path) -> Dict[str, Any]:
    """
    Charge un fichier YAML en dictionnaire. Retourne {} si vide.

    Paramètres
    ----------
    path : Path
        Chemin du fichier YAML.

    Retour
    ------
    Dict[str, Any]
        Contenu du YAML.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def _write_yaml(doc: Dict[str, Any], path: Path) -> None:
    """
    Écrit un dictionnaire dans un fichier YAML (création de dossiers incluse).

    Paramètres
    ----------
    doc : Dict[str, Any]
        Document à sérialiser.
    path : Path
        Destination.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def _dedup_str_list(values: Optional[List[str]]) -> List[str]:
    """
    Déduplique une liste de chaînes tout en conservant l'ordre.

    Paramètres
    ----------
    values : Optional[List[str]]
        Liste (potentiellement None).

    Retour
    ------
    List[str]
        Liste nettoyée, sans doublons vides.
    """
    if not values:
        return []
    seen = set()
    out: List[str] = []
    for v in values:
        s = str(v).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _slugify_name(name: str) -> str:
    """
    Transforme un titre libre en nom de projet snake_case simple.

    Paramètres
    ----------
    name : str
        Titre ou nom humain.

    Retour
    ------
    str
        Slug snake_case (ASCII tolérant).
    """
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_{2,}", "_", s).strip("_")
    return s or "project"


# -----------------------------------------------------------------------------
# Inférence déterministe (sans LLM) depuis l'ExecutionContext
# -----------------------------------------------------------------------------

_CANON_MODULES = ("core", "api", "auth", "ui_layer", "utils", "tests", "billing", "reports")

def _infer_modules_from_ec(ec: Dict[str, Any]) -> List[str]:
    """
    Infère une liste de modules initiaux en se basant sur EC (objectifs, contraintes, stories).

    Heuristiques (déterministes) :
      - Toujours inclure 'core' et 'tests'.
      - 'auth' si mots-clés auth/login/token/jwt/sso/identité.
      - 'api' si api/endpoint/route/rest/graphql.
      - 'ui_layer' si ui/interface/web/screen/page.
      - 'reports' si pdf/report/rapport/export.
      - 'billing' si billing/payment/paiement/facturation.
      - 'utils' si csv/export/import/outils.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext chargé.

    Retour
    ------
    List[str]
        Liste ordonnée de modules canoniques.
    """
    txt = " ".join([
        " ".join(ec.get("functional_objectives") or []),
        " ".join(ec.get("non_functional_constraints") or []),
        str(ec.get("deployment_context") or ""),
        " ".join([us.get("story", "") for us in (ec.get("user_stories") or [])]),
    ]).lower()

    mods = {"core", "tests"}

    def present(*keys: str) -> bool:
        return any(k in txt for k in keys)

    if present("auth", "login", "token", "jwt", "sso", "identité"):
        mods.add("auth")
    if present("api", "endpoint", "route", "rest", "graphql"):
        mods.add("api")
    if present("ui", "interface", "web", "screen", "page"):
        mods.add("ui_layer")
    if present("pdf", "report", "rapport", "export pdf"):
        mods.add("reports")
    if present("billing", "payment", "paiement", "facturation"):
        mods.add("billing")
    if present("csv", "export", "import", "outil", "outils"):
        mods.add("utils")

    ordered = [m for m in _CANON_MODULES if m in mods]
    return ordered


def _derive_dependencies(mods: List[str]) -> List[str]:
    """
    Déduit des dépendances simples sous forme 'A → B'.

    Paramètres
    ----------
    mods : List[str]
        Modules détectés.

    Retour
    ------
    List[str]
        Liens de dépendance (sans doublon).
    """
    links: List[str] = []
    for m in mods:
        if m in {"api", "auth", "ui_layer", "utils", "billing", "reports", "tests"}:
            if "core" in mods and m != "core":
                links.append(f"{m} → core")
    if "tests" in mods:
        for m in ("api", "auth", "core"):
            if m in mods:
                links.append(f"tests → {m}")
    return _dedup_str_list(links)


def _derive_priority(mods: List[str]) -> Dict[str, str]:
    """
    Assigne une priorité 'haute'/'moyenne'/'basse' selon l'impact typique.

    Paramètres
    ----------
    mods : List[str]
        Modules détectés.

    Retour
    ------
    Dict[str, str]
        Mapping module → priorité.
    """
    pr: Dict[str, str] = {}
    for m in mods:
        if m in {"auth", "api"}:
            pr[m] = "haute"
        elif m == "core":
            pr[m] = "moyenne"
        else:
            pr[m] = "basse"
    return pr


def _derive_validation_mode(ec: Dict[str, Any]) -> str:
    """
    Retourne 'strict' si contraintes sensibles (sécurité/données) détectées, sinon 'standard'.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext.

    Retour
    ------
    str
        'strict' ou 'standard'.
    """
    nfc = " ".join(ec.get("non_functional_constraints") or []).lower()
    if any(k in nfc for k in ("rgpd", "gdpr", "hipaa", "sécurité", "security", "pii")):
        return "strict"
    return "standard"


def _derive_folder_structure(mods: List[str]) -> Dict[str, Any]:
    """
    Génère une arborescence de dossiers en fonction des modules présents.

    Paramètres
    ----------
    mods : List[str]
        Modules détectés.

    Retour
    ------
    Dict[str, Any]
        Structure de dossiers attendue.
    """
    structure = [
        {"name": "core/", "description": "Composants fonctionnels métier"},
        {"name": "api/", "description": "Points d’entrée (routes, handlers)"},
        {"name": "auth/", "description": "Identité, accès, tokens"},
        {"name": "ui/", "description": "Interface (console/web)"},
        {"name": "utils/", "description": "Helpers et fonctions transverses"},
        {"name": "tests/", "description": "Tests unitaires et intégration"},
    ]
    if "billing" in mods:
        structure.append({"name": "billing/", "description": "Facturation, paiements"})
    if "reports" in mods:
        structure.append({"name": "reports/", "description": "Rapports/PDF"})
    return {"root": "archcode_app/", "structure": structure}


# -----------------------------------------------------------------------------
# Cœur agent : construction du project_draft
# -----------------------------------------------------------------------------

def _validate_ec_minimum(ec: Dict[str, Any]) -> None:
    """
    Vérifie la présence des champs EC requis pour planifier.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext.

    Exceptions
    ----------
    ValueError si des champs clés sont absents.
    """
    required = ("bus_message_id", "title", "functional_objectives")
    missing = [k for k in required if k not in ec or ec.get(k) in (None, "", [])]
    if missing:
        raise ValueError(f"ExecutionContext incomplet : champs manquants {missing}")


def build_project_draft(ec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construit le dictionnaire `project_draft` depuis l'EC (sans LLM).

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext.

    Retour
    ------
    Dict[str, Any]
        Document à sérialiser sous la clé 'project_draft'.
    """
    _validate_ec_minimum(ec)
    title = str(ec.get("title") or "Projet")
    project_name = _slugify_name(title)
    objs = _dedup_str_list(ec.get("functional_objectives"))
    mods = _infer_modules_from_ec(ec)
    deps = _derive_dependencies(mods)
    prio = _derive_priority(mods)
    vmode = _derive_validation_mode(ec)
    folders = _derive_folder_structure(mods)

    pd: Dict[str, Any] = {
        "project_draft": {
            "project_name": project_name,
            "global_objectives": objs,
            "initial_modules": mods,
            "dependencies": deps,
            "priority_map": prio,
            "validation_mode": vmode,
            "folder_structure": folders,
            "issued_at": _now_iso(),
            "bus_message_id": ec.get("bus_message_id"),
            "spec_version_ref": ec.get("spec_version"),
        }
    }
    return pd


# -----------------------------------------------------------------------------
# Commandes CLI
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """
    Construit le parseur d'arguments de l'agent (planify/show).

    Retour
    ------
    argparse.ArgumentParser
        Parseur prêt.
    """
    p = argparse.ArgumentParser(
        prog="agent_project_planner",
        description="ARCHCode — agent_project_planner : génère project_draft.yaml (sans LLM).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_plan = sub.add_parser("planify", help="Génère project_draft.yaml à partir de l'EC")
    sp_plan.add_argument("ec_yaml", type=Path, help="Chemin vers .archcode/execution_context.yaml")
    sp_plan.add_argument(
        "--out",
        type=Path,
        default=Path(".archcode") / "project_draft.yaml",
        help="Destination du project_draft (défaut: .archcode/project_draft.yaml)",
    )
    sp_plan.add_argument(
        "--update-ec",
        action="store_true",
        help="Met à jour EC.project_name et EC.modules d'après le draft",
    )

    sp_show = sub.add_parser("show", help="Affiche un résumé d'un project_draft.yaml")
    sp_show.add_argument("pd_yaml", type=Path, help="Chemin vers .archcode/project_draft.yaml")

    return p


def cmd_planify(ec_yaml: Path, out: Path, update_ec: bool) -> None:
    """
    Construit et écrit `project_draft.yaml` ; met à jour EC si demandé.

    Paramètres
    ----------
    ec_yaml : Path
        Chemin de l'ExecutionContext.
    out : Path
        Fichier de sortie project_draft.
    update_ec : bool
        Si True, reflète `project_name` et `modules` dans l'EC.
    """
    ec = _read_yaml(ec_yaml)
    draft = build_project_draft(ec)
    _write_yaml(draft, out)
    print(f"[OK] project_draft écrit → {out}")

    if update_ec:
        ec["project_name"] = draft["project_draft"]["project_name"]
        ec["modules"] = list(draft["project_draft"]["initial_modules"])
        _write_yaml(ec, ec_yaml)
        print("[OK] EC mis à jour (project_name, modules).")


def cmd_show(pd_yaml: Path) -> None:
    """
    Affiche un résumé utile du project_draft (modules, dépendances, mode).

    Paramètres
    ----------
    pd_yaml : Path
        Chemin du fichier project_draft.yaml.
    """
    doc = _read_yaml(pd_yaml)
    pd = doc.get("project_draft") or {}
    mods = ", ".join(pd.get("initial_modules") or []) or "∅"
    deps = ", ".join(pd.get("dependencies") or []) or "∅"
    print("\n".join([
        f"project_name     : {pd.get('project_name')}",
        f"validation_mode  : {pd.get('validation_mode')}",
        f"modules          : {mods}",
        f"dependencies     : {deps}",
        f"bus_message_id   : {pd.get('bus_message_id')}",
        f"spec_version_ref : {pd.get('spec_version_ref')}",
        f"issued_at        : {pd.get('issued_at')}",
    ]))


def main(argv: Optional[List[str]] = None) -> None:
    """
    Point d'entrée CLI : planify/show.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "planify":
            cmd_planify(ec_yaml=args.ec_yaml, out=args.out, update_ec=args.update_ec)
        elif args.cmd == "show":
            cmd_show(pd_yaml=args.pd_yaml)
        else:
            parser.print_help()
            raise SystemExit(1)
    except FileNotFoundError as e:
        print(f"[ERREUR] {e}")
        raise SystemExit(1)
    except yaml.YAMLError as e:
        print(f"[ERREUR YAML] {e}")
        raise SystemExit(2)
    except ValueError as e:
        print(f"[ERREUR] {e}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
