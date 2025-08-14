
# scripts/context_bridge_cli.py
from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import uuid

import yaml

from core.context import (
    load_bus_message,
    validate_specblock,
    create_execution_context,  # contrôle MVP (pas d'effets de bord)
    SpecBlock,
)

"""
===============================================================================
mARCHCode — Context Bridge CLI (PHASE_1 → PHASE_2) + Project Planner (Étape 1.5)
-------------------------------------------------------------------------------
Rôle du fichier
  - Construire de façon déterministe l’ExecutionContext (EC) depuis un SpecBlock
    gelé dans `bus_message.yaml` → `.archcode/execution_context.yaml`.
  - Centraliser `bus_message_id`, initialiser `loop_iteration`, appliquer la
    gouvernance via `max_planning_attempts`, réserver `plan_validated_id`.
  - Fournir des commandes de contrôle :
      * `show`        : affichage synthétique de l’EC
      * `bump-loop`   : incrémente `loop_iteration` avec garde-fou et peut
                        déclencher un `spec_amendment.yaml`
  - **NOUVEAU** : Étape 1.5 (sans LLM) — `planify` :
      * génère un `project_draft.yaml` (déterministe, lisible)
      * met à jour l’EC avec `project_name` et `modules` (pilotage initial)

Invariants
  - 100% local : aucun réseau / aucun LLM.
  - `validation_mode` démarre à "pending" dans l’EC.
  - `planify` alimente une ébauche `project_draft.yaml` + reflète les modules
    dans l’EC (champ `modules`) sans avancer sur la validation globale (PV).
  - Artefacts produits :
      · `.archcode/execution_context.yaml`
      · `.archcode/project_draft.yaml`
      · `.archcode/spec_amendment.yaml` (si gouvernance dépassée)

Commandes
  - build       : bus_message.yaml → .archcode/execution_context.yaml
  - show        : affiche un résumé de l’EC
  - bump-loop   : +1 sur loop_iteration (peut déclencher spec_amendment)
  - planify     : EC → project_draft.yaml (+ mise à jour EC.project_name/modules)

Exemples
  - Générer l’EC :
      python scripts/context_bridge_cli.py build bus_message.yaml
  - Voir le résumé :
      python scripts/context_bridge_cli.py show .archcode/execution_context.yaml
  - Incrémenter la boucle :
      python scripts/context_bridge_cli.py bump-loop .archcode/execution_context.yaml
  - Produire le project_draft :
      python scripts/context_bridge_cli.py planify .archcode/execution_context.yaml

Auteur : Alex
===============================================================================
"""


# -----------------------------------------------------------------------------
# Utilitaires généraux (dates, IDs)
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    """
    Renvoie un horodatage ISO-8601 (secondes) pour traçabilité.
    """
    return datetime.now().isoformat(timespec="seconds")


def _new_plan_validated_id() -> str:
    """
    Génère un identifiant unique de plan validé (réservé pour plus tard).
    """
    return f"PLV-{uuid.uuid4().hex[:8]}"


def _dedup_str_list(values: Optional[List[str]]) -> List[str]:
    """
    Déduplique une liste de chaînes en préservant l'ordre (robuste à None).
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
    Convertit un titre en nom de projet snake_case simple (ASCII tolérant).

    Exemples
    --------
    "ARCHCode App — Pilotage" → "archcode_app_pilotage"
    """
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_{2,}", "_", s).strip("_")
    return s or "project"


# -----------------------------------------------------------------------------
# Construction de l'ExecutionContext (dict YAML)
# -----------------------------------------------------------------------------

def spec_to_ec_dict(
    spec: SpecBlock,
    *,
    loop_iteration: int = 0,
    max_planning_attempts: int = 3,
    plan_validated_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Transforme un SpecBlock valide en dictionnaire ExecutionContext prêt à écrire.

    Paramètres
    ----------
    spec : SpecBlock
        Spécification gelée (bus_message.yaml) déjà validée.
    loop_iteration : int
        Compteur initial de boucle APP ↔ APV (défaut 0).
    max_planning_attempts : int
        Limite stricte de boucles (défaut 3).
    plan_validated_id : Optional[str]
        Identifiant du plan validé si déjà connu (sinon None).

    Retour
    ------
    Dict[str, Any]
        Représentation EC à sérialiser en YAML.
    """
    ok, errs = validate_specblock(spec)
    if not ok:
        raise ValueError("SpecBlock invalide : " + " | ".join(errs))

    fo = _dedup_str_list(spec.functional_objectives)
    ins = _dedup_str_list(spec.input_sources)
    outs = _dedup_str_list(spec.output_targets)
    arch = _dedup_str_list(spec.architectural_preferences)
    nfc = _dedup_str_list(spec.non_functional_constraints)

    ec: Dict[str, Any] = {
        # --- Identifiants & gouvernance ---
        "bus_message_id": spec.bus_message_id,
        "spec_version": spec.spec_version,
        "validation_mode": "pending",
        "plan_validated_id": plan_validated_id,
        "loop_iteration": int(loop_iteration),
        "max_planning_attempts": int(max_planning_attempts),
        "created_at": _now_iso(),

        # --- Résumés utiles ---
        "title": spec.title,
        "summary": spec.summary,
        "functional_objectives": fo,

        # Aliases doc (confort humains/outils)
        "spec_title": spec.title,
        "spec_objectives": fo,

        # --- Stories & contraintes ---
        "user_stories": list(spec.user_stories or []),
        "non_functional_constraints": nfc,
        "architectural_preferences": arch,
        "deployment_context": spec.deployment_context,
        "target_audience": spec.target_audience,
        "input_sources": ins,
        "output_targets": outs,
        "preferred_llm": spec.preferred_llm,

        # --- Provenance & commentaires ---
        "source_mode": spec.source_mode,
        "llm_aid": bool(spec.llm_aid),
        "comment_human": spec.comment_human,
        "comment_llm": spec.comment_llm,

        # --- Placeholders PHASE_2/3 ---
        "project_name": None,
        "modules": [],
        "current_module": None,
        "current_file": None,
        "user_story_id": None,

        # --- Référence plan validé (avant PV: vide) ---
        "plan_validated": {},

        # --- Pass-through internes ---
        "free_field_1": spec.free_field_1,
        "free_field_2": spec.free_field_2,
    }
    return ec


# -----------------------------------------------------------------------------
# I/O YAML génériques
# -----------------------------------------------------------------------------

def write_yaml(doc: Dict[str, Any], path: Path) -> None:
    """
    Écrit un dictionnaire dans un fichier YAML.

    Paramètres
    ----------
    doc : Dict[str, Any]
        Dictionnaire à sérialiser.
    path : Path
        Destination du fichier.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def read_yaml(path: Path) -> Dict[str, Any]:
    """
    Charge un YAML en dictionnaire (doc vide si YAML vide).

    Paramètres
    ----------
    path : Path

    Retour
    ------
    Dict[str, Any]
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


# -----------------------------------------------------------------------------
# Gouvernance : incrément de boucle & déclenchement d'amendement
# -----------------------------------------------------------------------------

def bump_loop_iteration(ec_path: Path) -> Tuple[Dict[str, Any], Optional[Path]]:
    """
    Incrémente `loop_iteration` dans l'EC et applique la limite stricte.

    Paramètres
    ----------
    ec_path : Path
        Chemin de `.archcode/execution_context.yaml`.

    Retour
    ------
    (ec, amendment_path) : Tuple[Dict[str, Any], Optional[Path]]
        - ec : ExecutionContext mis à jour (en mémoire).
        - amendment_path : chemin du `spec_amendment.yaml` si déclenché, sinon None.
    """
    ec = read_yaml(ec_path)
    required = ("loop_iteration", "max_planning_attempts", "bus_message_id")
    if any(k not in ec for k in required):
        raise ValueError("EC incomplet : champs requis manquants (loop_iteration, max_planning_attempts, bus_message_id).")

    next_value = int(ec.get("loop_iteration", 0)) + 1
    limit = int(ec.get("max_planning_attempts", 1))

    if next_value > limit:
        amendment = {
            "type": "spec_amendment",
            "reason": "max_planning_attempts_exceeded",
            "bus_message_id": ec["bus_message_id"],
            "spec_version_ref": ec.get("spec_version"),
            "issued_at": _now_iso(),
            "details": {
                "loop_iteration": ec.get("loop_iteration", 0),
                "max_planning_attempts": limit,
                "note": "Cycle APP ↔ APV dépassé. Revoir la spécification fonctionnelle (amendement).",
            },
        }
        out = ec_path.parent / "spec_amendment.yaml"
        write_yaml(amendment, out)
        return ec, out

    ec["loop_iteration"] = next_value
    write_yaml(ec, ec_path)
    return ec, None


# -----------------------------------------------------------------------------
# Étape 1.5 — Project Planner (sans LLM)
# -----------------------------------------------------------------------------

_CANON_MODULES = ("core", "api", "auth", "ui_layer", "utils", "tests", "billing", "reports")

def _infer_modules_from_ec(ec: Dict[str, Any]) -> List[str]:
    """
    Infère une liste de modules initiaux depuis l’EC (objectifs/contraintes).

    Règles simples (sans LLM)
    -------------------------
    - Toujours inclure 'core' et 'tests'.
    - 'auth' si mots-clés auth/login/token/jwt/sso/identité.
    - 'api' si api/endpoint/route/rest/graphql.
    - 'ui_layer' si ui/interface/web/screen/page.
    - 'reports' si pdf/report/rapport/export.
    - 'billing' si billing/payment/paiement/facturation.
    - 'utils' si csv/export/import/outils.
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
    Déduit des dépendances simples (liste de chaînes 'A → B').
    """
    deps: List[str] = []
    for m in mods:
        if m in {"api", "auth", "ui_layer", "utils", "billing", "reports", "tests"}:
            if "core" in mods and m != "core":
                deps.append(f"{m} → core")
        # Lier tests aux modules majeurs si présents
    if "tests" in mods:
        for m in ("api", "auth", "core"):
            if m in mods:
                deps.append(f"tests → {m}")
    return _dedup_str_list(deps)


def _derive_priority(mods: List[str]) -> Dict[str, str]:
    """
    Attribue des priorités 'haute'/'moyenne'/'basse' selon une heuristique simple.
    """
    pr: Dict[str, str] = {}
    for m in mods:
        if m in {"auth", "api"}:
            pr[m] = "haute"
        elif m == "core":
            pr[m] = "moyenne"
        elif m == "ui_layer":
            pr[m] = "basse"
        else:
            pr[m] = "basse"
    return pr


def _derive_validation_mode(ec: Dict[str, Any]) -> str:
    """
    Choisit 'strict' si contraintes sensibles détectées, sinon 'standard'.
    """
    nfc = " ".join(ec.get("non_functional_constraints") or []).lower()
    if any(k in nfc for k in ("rgpd", "hipaa", "sécurité", "security", "pii", "gdpr")):
        return "strict"
    return "standard"


def _derive_folder_structure(mods: List[str]) -> Dict[str, Any]:
    """
    Propose une arborescence réutilisable, modulée par la présence de certains modules.
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
        structure.append({"name": "reports/", "description": "Génération de rapports/PDF"})
    return {
        "root": "archcode_app/",
        "structure": structure,
    }


def build_project_draft(ec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construit le document `project_draft` (dict) à partir de l’EC.

    Paramètres
    ----------
    ec : Dict[str, Any]
        Dictionnaire ExecutionContext chargé depuis YAML.

    Retour
    ------
    Dict[str, Any]
        Dictionnaire prêt à sérialiser sous la clé 'project_draft'.
    """
    title = str(ec.get("title") or "Projet")
    project_name = _slugify_name(title)
    mods = _infer_modules_from_ec(ec)
    deps = _derive_dependencies(mods)
    prio = _derive_priority(mods)
    vmode = _derive_validation_mode(ec)
    folders = _derive_folder_structure(mods)

    pd: Dict[str, Any] = {
        "project_draft": {
            "project_name": project_name,
            "global_objectives": list(ec.get("functional_objectives") or []),
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
    Construit le parseur d’arguments pour build/show/bump-loop/planify.
    """
    p = argparse.ArgumentParser(
        prog="context-bridge",
        description="mARCHCode — PHASE_1 → PHASE_2 : EC & project_draft (sans LLM).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_build = sub.add_parser("build", help="Construit execution_context.yaml depuis bus_message.yaml")
    sp_build.add_argument("bus_message", type=Path, help="Chemin vers bus_message.yaml")
    sp_build.add_argument(
        "--out",
        type=Path,
        default=Path(".archcode") / "execution_context.yaml",
        help="Destination de l'EC (défaut: .archcode/execution_context.yaml)",
    )
    sp_build.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Limite stricte de boucles de planification (défaut: 3)",
    )

    sp_show = sub.add_parser("show", help="Affiche un résumé d'un execution_context.yaml")
    sp_show.add_argument("ec_yaml", type=Path, help="Chemin vers execution_context.yaml")

    sp_bump = sub.add_parser("bump-loop", help="Incrémente loop_iteration avec garde-fou")
    sp_bump.add_argument("ec_yaml", type=Path, help="Chemin vers execution_context.yaml")

    sp_plan = sub.add_parser("planify", help="Génère project_draft.yaml depuis execution_context.yaml (sans LLM)")
    sp_plan.add_argument(
        "ec_yaml",
        type=Path,
        help="Chemin vers .archcode/execution_context.yaml",
    )
    sp_plan.add_argument(
        "--out",
        type=Path,
        default=Path(".archcode") / "project_draft.yaml",
        help="Destination de project_draft (défaut: .archcode/project_draft.yaml)",
    )
    sp_plan.add_argument(
        "--update-ec",
        action="store_true",
        help="Met à jour EC.project_name et EC.modules d'après le project_draft",
    )

    return p


def cmd_build(bus_message: Path, out: Path, max_attempts: int) -> None:
    """
    Construit l’EC à partir d’un SpecBlock gelé et l’écrit en YAML.

    Étapes
    ------
    1) Charge bus_message.yaml → SpecBlock
    2) Valide SpecBlock (fail-fast si invalide)
    3) Construit un dict EC gouverné
    4) Écrit .archcode/execution_context.yaml (ou --out)
    5) Contrôle MVP via create_execution_context (SpecBlock exploitable)
    """
    spec: SpecBlock = load_bus_message(bus_message, auto_fill=True)
    ok, errs = validate_specblock(spec)
    if not ok:
        print("[ERREUR] bus_message.yaml invalide :")
        for e in errs:
            print(f"  - {e}")
        raise SystemExit(2)

    ec = spec_to_ec_dict(spec, loop_iteration=0, max_planning_attempts=max_attempts, plan_validated_id=None)
    write_yaml(ec, out)
    print(f"[OK] ExecutionContext écrit → {out}")

    _ = create_execution_context(spec, bus_message_path=bus_message)
    print("[OK] SpecBlock contrôlé pour PHASE_2 (create_execution_context).")


def cmd_show(ec_yaml: Path) -> None:
    """
    Affiche un résumé concis de l’EC (identifiants & gouvernance).
    """
    ec = read_yaml(ec_yaml)
    msg = [
        f"bus_message_id       : {ec.get('bus_message_id')}",
        f"spec_version         : {ec.get('spec_version')}",
        f"title                : {ec.get('title')}",
        f"loop_iteration       : {ec.get('loop_iteration')}",
        f"max_planning_attempts: {ec.get('max_planning_attempts')}",
        f"plan_validated_id    : {ec.get('plan_validated_id')}",
        f"validation_mode      : {ec.get('validation_mode')}",
        f"modules              : {', '.join(ec.get('modules') or []) or '∅'}",
    ]
    print("\n".join(msg))


def cmd_bump_loop(ec_yaml: Path) -> None:
    """
    Incrémente `loop_iteration` avec garde-fou `max_planning_attempts`.

    - Émet `.archcode/spec_amendment.yaml` si la limite est dépassée.
    - Sinon réécrit l’EC incrémenté.
    """
    ec, amendment = bump_loop_iteration(ec_yaml)
    if amendment:
        print(f"[ALERTE] Limite atteinte → spec_amendment émis : {amendment}")
        raise SystemExit(3)
    print(f"[OK] loop_iteration → {ec.get('loop_iteration')}")


def cmd_planify(ec_yaml: Path, out: Path, update_ec: bool) -> None:
    """
    Produit `.archcode/project_draft.yaml` (sans LLM) à partir de l’EC.

    Paramètres
    ----------
    ec_yaml : Path
        Chemin vers l'EC YAML.
    out : Path
        Destination du project_draft.
    update_ec : bool
        Si True, met à jour EC.project_name et EC.modules selon l’ébauche.

    Sorties
    -------
    - `.archcode/project_draft.yaml` écrit.
    - Optionnellement, `.archcode/execution_context.yaml` mis à jour (project_name/modules).
    """
    ec = read_yaml(ec_yaml)
    required = ("bus_message_id", "title", "functional_objectives")
    if any(k not in ec for k in required):
        raise SystemExit("[ERREUR] EC invalide : champs requis manquants (bus_message_id, title, functional_objectives).")

    draft = build_project_draft(ec)
    write_yaml(draft, out)
    print(f"[OK] project_draft écrit → {out}")

    if update_ec:
        ec["project_name"] = draft["project_draft"]["project_name"]
        ec["modules"] = list(draft["project_draft"]["initial_modules"])
        write_yaml(ec, ec_yaml)
        print("[OK] EC mis à jour (project_name, modules).")


def main(argv: Optional[List[str]] = None) -> None:
    """
    Point d'entrée CLI pour build/show/bump-loop/planify.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "build":
            cmd_build(bus_message=args.bus_message, out=args.out, max_attempts=args.max_attempts)
        elif args.cmd == "show":
            cmd_show(ec_yaml=args.ec_yaml)
        elif args.cmd == "bump-loop":
            cmd_bump_loop(ec_yaml=args.ec_yaml)
        elif args.cmd == "planify":
            cmd_planify(ec_yaml=args.ec_yaml, out=args.out, update_ec=args.update_ec)
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
