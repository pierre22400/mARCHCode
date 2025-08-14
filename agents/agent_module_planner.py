
# agents/agent_module_planner.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import re
import yaml

"""
===============================================================================
ARCHCode — agent_module_planner (PHASE 2 : Planification modulaire)
-------------------------------------------------------------------------------
But (aligné pipeline) :
  - Lire `.archcode/execution_context.yaml` (EC) et `project_draft.yaml` (PD).
  - Produire, pour chaque module, un artefact déterministe :
        `.archcode/modules/<module_name>/module_draft.yaml`
    au format :
      module_draft:
        module_name: str                 ✅
        user_story_id: str | null       🟡
        responsibilities: list[str]     🟡
        inputs: list[str]               🟡
        outputs: list[str]              🟡
        files_expected: list[str]       ✅
        entrypoint: str | null          🟡
        depends_on: list[str]           🟡
        technical_constraints: list[str]🟡
        non_functional_objectives: list[str] 🟡
        validator_status: "pending"     (par défaut)
        meta:
          priority: str | null
          phase: str | null
          comment: str | null
          loop_iteration: int
          bus_message_id: str
          spec_version_ref: str | null

Contraintes MVP :
  - Zéro LLM, zéro réseau. Heuristiques simples, documentées, déterministes.
  - Respect des conventions utilisées par `agent_module_compilator` et
    `execution_plan_transformer` (liste `files_expected[]` minimale, nom module).

CLI :
  - Planifier un module :
      python -m agents.agent_module_planner plan auth
  - Planifier tous les modules déclarés par `project_draft.initial_modules` :
      python -m agents.agent_module_planner plan-all
  - Afficher un résumé d’un module_draft :
      python -m agents.agent_module_planner show .archcode/modules/auth/module_draft.yaml

Auteur : Alex
===============================================================================
"""


# -----------------------------------------------------------------------------
# Utilitaires généraux
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    """Retourne un horodatage ISO-8601 à la seconde.

    Retour
    ------
    str
        Date/heure locale au format ISO-8601 (timespec="seconds").
    """
    return datetime.now().isoformat(timespec="seconds")


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Charge un fichier YAML en dictionnaire Python.

    Paramètres
    ----------
    path : Path
        Chemin du fichier YAML à lire.

    Retour
    ------
    Dict[str, Any]
        Dictionnaire résultant ; {} si le document est vide.

    Exceptions
    ----------
    FileNotFoundError
        Si le fichier n'existe pas.
    yaml.YAMLError
        Si le contenu YAML est invalide.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def _write_yaml(doc: Dict[str, Any], path: Path) -> None:
    """Écrit un dictionnaire dans un fichier YAML (création des dossiers incluse).

    Paramètres
    ----------
    doc : Dict[str, Any]
        Données à sérialiser.
    path : Path
        Destination du fichier YAML.

    Retour
    ------
    None
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def _dedup_str_list(values: Optional[List[str]]) -> List[str]:
    """Déduplique une liste de chaînes en préservant l'ordre d'apparition.

    Paramètres
    ----------
    values : Optional[List[str]]
        Liste d'entrée potentiellement None.

    Retour
    ------
    List[str]
        Liste nettoyée, sans doublon ni chaînes vides.
    """
    if not values:
        return []
    out, seen = [], set()
    for v in values:
        s = str(v).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# -----------------------------------------------------------------------------
# Chargement / validations de base
# -----------------------------------------------------------------------------

def _load_ec(path: Path) -> Dict[str, Any]:
    """Charge `.archcode/execution_context.yaml` et contrôle des champs critiques.

    Paramètres
    ----------
    path : Path
        Chemin du fichier ExecutionContext.

    Retour
    ------
    Dict[str, Any]
        Dictionnaire ExecutionContext.

    Exceptions
    ----------
    ValueError
        Si `bus_message_id` est manquant.
    """
    ec = _read_yaml(path)
    if not ec.get("bus_message_id"):
        raise ValueError("ExecutionContext : champ `bus_message_id` manquant.")
    return ec


def _load_pd(path: Path) -> Dict[str, Any]:
    """Charge `.archcode/project_draft.yaml` et extrait la section utile.

    Paramètres
    ----------
    path : Path
        Chemin du project_draft.

    Retour
    ------
    Dict[str, Any]
        Section `project_draft` ; {} si absente.
    """
    if not path.exists():
        return {}
    doc = _read_yaml(path)
    return doc.get("project_draft") or {}


# -----------------------------------------------------------------------------
# Heuristiques déterministes : priorités, dépendances, phase
# -----------------------------------------------------------------------------

def _priority_for_module(pd: Dict[str, Any], module_name: str) -> Optional[str]:
    """Retourne la priorité d’un module en s’appuyant sur `project_draft.priority_map`.

    Paramètres
    ----------
    pd : Dict[str, Any]
        Section `project_draft`.
    module_name : str
        Nom du module.

    Retour
    ------
    Optional[str]
        Priorité ('haute'|'moyenne'|'basse') si disponible, sinon None.
    """
    prio_map = pd.get("priority_map") or {}
    return prio_map.get(module_name)


def _dependencies_for_module(pd: Dict[str, Any], module_name: str) -> List[str]:
    """Calcule la liste `depends_on[]` d’un module à partir de `project_draft.dependencies`.

    Paramètres
    ----------
    pd : Dict[str, Any]
        Section `project_draft`.
    module_name : str
        Nom du module.

    Retour
    ------
    List[str]
        Liste dédupliquée des dépendances (noms de modules).
    """
    deps_spec = pd.get("dependencies") or []
    depends: List[str] = []
    for expr in deps_spec:
        # Exemples attendus : "api → core", "tests → core"
        parts = re.split(r"→|->|=>", str(expr))
        if len(parts) != 2:
            continue
        a = parts[0].strip()
        b = parts[1].strip()
        if a == module_name and b:
            depends.append(b)
    return _dedup_str_list(depends)


def _phase_for_module(module_name: str) -> Optional[str]:
    """Retourne une étiquette de phase pour un module (informatif).

    Paramètres
    ----------
    module_name : str
        Nom du module.

    Retour
    ------
    Optional[str]
        Phase textuelle (ex. 'authentification', 'api', 'cœur', 'tests', ...).
    """
    mapping = {
        "auth": "authentification",
        "api": "interface_api",
        "core": "coeur_metier",
        "ui_layer": "interface_utilisateur",
        "utils": "transverse",
        "billing": "gestion_facturation",
        "reports": "reporting",
        "tests": "tests",
    }
    return mapping.get(module_name)


# -----------------------------------------------------------------------------
# Heuristiques : responsibilities, files_expected, entrypoint, I/O
# -----------------------------------------------------------------------------

def _responsibilities_for_module(ec: Dict[str, Any], module_name: str) -> List[str]:
    """Génère une liste de responsabilités typiques par module, avec coloration EC.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext (objectifs, stories, contraintes...).
    module_name : str
        Nom du module.

    Retour
    ------
    List[str]
        Liste de responsabilités suggérées (déterministe).
    """
    base: Dict[str, List[str]] = {
        "core": [
            "Modéliser les entités métier",
            "Exposer les services métier (sans I/O réseau)",
            "Garantir l'intégrité des règles de gestion",
        ],
        "api": [
            "Exposer des endpoints REST/HTTP",
            "Valider les payloads d'entrée",
            "Mapper les erreurs métier en statuts HTTP",
        ],
        "auth": [
            "Gérer l'identité et l'authentification",
            "Émettre et vérifier les tokens",
            "Gérer les permissions d'accès",
        ],
        "ui_layer": [
            "Offrir une interface CLI/Web pour les actions clés",
            "Orchestrer les interactions utilisateur",
        ],
        "utils": [
            "Fournir des helpers transverses",
            "Factoriser la logique utilitaire",
        ],
        "billing": [
            "Gérer les factures et paiements",
            "Calculer les montants et taxes",
        ],
        "reports": [
            "Générer des rapports et exports",
            "Assembler des données issues des services",
        ],
        "tests": [
            "Couvrir les fonctionnalités critiques",
            "Valider les contrats d'API",
        ],
    }
    out = list(base.get(module_name, []))

    # Coloration par mots-clés depuis objectives & stories
    text = " ".join([
        " ".join(ec.get("functional_objectives") or []),
        " ".join([us.get("story", "") for us in (ec.get("user_stories") or [])]),
    ]).lower()

    def add_if(keyword: str, resp: str) -> None:
        if keyword in text and resp not in out:
            out.append(resp)

    if module_name == "auth":
        add_if("sso", "Intégrer SSO/IdP si requis")
        add_if("jwt", "Supporter JWT avec refresh tokens")
    if module_name == "api":
        add_if("graphql", "Option GraphQL si nécessaire")
        add_if("pagination", "Fournir la pagination standard")
    if module_name == "reports":
        add_if("pdf", "Exporter en PDF/A si demandé")
    if module_name == "billing":
        add_if("stripe", "Intégrer un PSP (ex. Stripe) de façon abstraite")

    return out


def _files_for_module(module_name: str, present_modules: List[str]) -> List[str]:
    """Propose une liste déterministe de fichiers attendus par module.

    Paramètres
    ----------
    module_name : str
        Nom du module.
    present_modules : List[str]
        Modules présents dans le projet (pour adapter certains tests).

    Retour
    ------
    List[str]
        Liste `files_expected[]` (chemins relatifs à la racine du module).
    """
    if module_name == "core":
        return ["__init__.py", "models.py", "services.py"]
    if module_name == "api":
        return ["__init__.py", "routes.py", "handlers.py", "schemas.py"]
    if module_name == "auth":
        return ["__init__.py", "models.py", "service.py", "routes.py", "tokens.py"]
    if module_name == "ui_layer":
        return ["__init__.py", "cli.py", "views.py"]
    if module_name == "utils":
        return ["__init__.py", "helpers.py"]
    if module_name == "billing":
        return ["__init__.py", "models.py", "service.py", "invoices.py"]
    if module_name == "reports":
        return ["__init__.py", "report_generator.py", "pdf.py"]
    if module_name == "tests":
        base = ["test_core.py"]
        if "api" in present_modules:
            base.append("test_api.py")
        if "auth" in present_modules:
            base.append("test_auth.py")
        return base
    # Fallback
    return ["__init__.py", f"{module_name}.py"]


def _entrypoint_for_module(module_name: str) -> Optional[str]:
    """Suggère un entrypoint (indicatif) pour le module.

    Paramètres
    ----------
    module_name : str
        Nom du module.

    Retour
    ------
    Optional[str]
        Chemin/fonction ou nom de fichier principal, sinon None.
    """
    mapping = {
        "api": "routes.py",
        "ui_layer": "cli.py:main",
        "auth": "service.py",
        "core": "services.py",
        "reports": "report_generator.py",
        "billing": "service.py",
        "utils": None,
        "tests": None,
    }
    return mapping.get(module_name)


def _inputs_outputs_for_module(ec: Dict[str, Any], module_name: str) -> Tuple[List[str], List[str]]:
    """Déduit des entrées/sorties génériques pour un module à partir de l'EC.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext (sources/targets).
    module_name : str
        Nom du module.

    Retour
    ------
    Tuple[List[str], List[str]]
        (inputs[], outputs[]) déterministes.
    """
    inputs = _dedup_str_list(ec.get("input_sources") or [])
    outputs = _dedup_str_list(ec.get("output_targets") or [])
    # Spécialisation légère selon module
    if module_name == "api":
        inputs = _dedup_str_list(inputs + ["HTTP request"])
        outputs = _dedup_str_list(outputs + ["HTTP response"])
    if module_name == "reports":
        outputs = _dedup_str_list(outputs + ["PDF file", "CSV export"])
    if module_name == "tests":
        inputs = _dedup_str_list(inputs + ["fixtures"])
        outputs = _dedup_str_list(outputs + ["test reports"])
    return inputs, outputs


def _technical_constraints(ec: Dict[str, Any]) -> List[str]:
    """Récupère les contraintes techniques/non-fonctionnelles utiles au draft.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext (non_functional_constraints, deployment_context...).

    Retour
    ------
    List[str]
        Contraintes (liste dédupliquée).
    """
    nfc = _dedup_str_list(ec.get("non_functional_constraints") or [])
    ctx = str(ec.get("deployment_context") or "").strip()
    if ctx:
        nfc.append(f"deployment:{ctx}")
    return _dedup_str_list(nfc)


def _user_story_for_module(ec: Dict[str, Any], module_name: str) -> Optional[str]:
    """Associe une user_story pertinente au module par recherche de mots-clés.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext (user_stories[] avec champs {id, story}).
    module_name : str
        Nom du module.

    Retour
    ------
    Optional[str]
        user_story_id si trouvée, sinon None.
    """
    stories: List[Dict[str, Any]] = ec.get("user_stories") or []
    text_map = {
        "api": ["api", "endpoint", "route", "http", "rest", "graphql"],
        "auth": ["auth", "login", "token", "jwt", "sso", "identity", "identité"],
        "reports": ["report", "rapport", "pdf", "export"],
        "billing": ["billing", "paiement", "payment", "facture", "invoice"],
        "ui_layer": ["ui", "interface", "console", "web", "screen"],
        "core": ["core", "métier", "business", "domain"],
        "utils": ["outil", "helper", "utils"],
        "tests": ["test", "qa", "quality"],
    }
    keywords = text_map.get(module_name, [])
    for us in stories:
        text = (us.get("story") or "").lower()
        if any(k in text for k in keywords):
            return us.get("id")
    # fallback : première story si aucune correspondance
    return stories[0]["id"] if stories and stories[0].get("id") else None


# -----------------------------------------------------------------------------
# Construction du module_draft
# -----------------------------------------------------------------------------

def build_module_draft(
    *,
    ec: Dict[str, Any],
    pd: Dict[str, Any],
    module_name: str,
) -> Dict[str, Any]:
    """Construit un dictionnaire `module_draft` déterministe pour un module donné.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext chargé.
    pd : Dict[str, Any]
        Section `project_draft` (peut être vide).
    module_name : str
        Nom du module à planifier.

    Retour
    ------
    Dict[str, Any]
        Document YAML prêt à sérialiser sous la racine `module_draft`.
    """
    present = list(pd.get("initial_modules") or [])
    responsibilities = _responsibilities_for_module(ec, module_name)
    files_expected = _files_for_module(module_name, present)
    entrypoint = _entrypoint_for_module(module_name)
    depends_on = _dependencies_for_module(pd, module_name)
    inputs, outputs = _inputs_outputs_for_module(ec, module_name)
    constraints = _technical_constraints(ec)
    user_story_id = _user_story_for_module(ec, module_name)
    priority = _priority_for_module(pd, module_name)
    phase = _phase_for_module(module_name)

    doc = {
        "module_draft": {
            "module_name": module_name,
            "user_story_id": user_story_id,
            "responsibilities": responsibilities,
            "inputs": inputs,
            "outputs": outputs,
            "files_expected": files_expected,
            "entrypoint": entrypoint,
            "depends_on": depends_on,
            "technical_constraints": constraints,
            "non_functional_objectives": _dedup_str_list(ec.get("non_functional_constraints") or []),
            "validator_status": "pending",
            "meta": {
                "priority": priority,
                "phase": phase,
                "comment": f"Draft généré le { _now_iso() }",
                "loop_iteration": int(ec.get("loop_iteration") or 0),
                "bus_message_id": ec.get("bus_message_id"),
                "spec_version_ref": ec.get("spec_version"),
            },
        }
    }
    return doc


# -----------------------------------------------------------------------------
# Écriture / chemins de sortie
# -----------------------------------------------------------------------------

def _module_out_path(out_root: Path, module_name: str) -> Path:
    """Calcule le chemin de sortie canonicalisé pour un module_draft.

    Paramètres
    ----------
    out_root : Path
        Dossier racine des modules (ex. `.archcode/modules`).
    module_name : str
        Nom du module.

    Retour
    ------
    Path
        Chemin complet vers `.../<module_name>/module_draft.yaml`.
    """
    return out_root / module_name / "module_draft.yaml"


def write_module_draft(
    *,
    ec: Dict[str, Any],
    pd: Dict[str, Any],
    module_name: str,
    out_root: Path,
    overwrite: bool,
) -> Path:
    """Construit et persiste un `module_draft.yaml` pour un module donné.

    Paramètres
    ----------
    ec : Dict[str, Any]
        ExecutionContext chargé.
    pd : Dict[str, Any]
        Section `project_draft`.
    module_name : str
        Nom du module.
    out_root : Path
        Racine de sortie (par défaut `.archcode/modules`).
    overwrite : bool
        Écraser un fichier existant si True.

    Retour
    ------
    Path
        Chemin du fichier écrit.

    Exceptions
    ----------
    FileExistsError
        Si le fichier existe et `overwrite` est False.
    """
    path = _module_out_path(out_root, module_name)
    if path.exists() and not overwrite:
        raise FileExistsError(f"Existe déjà (utilisez --overwrite) : {path}")
    doc = build_module_draft(ec=ec, pd=pd, module_name=module_name)
    _write_yaml(doc, path)
    return path


# -----------------------------------------------------------------------------
# Commandes haut niveau
# -----------------------------------------------------------------------------

def cmd_plan_single(
    *,
    module_name: str,
    ec_path: Path,
    pd_path: Path,
    out_root: Path,
    overwrite: bool,
) -> None:
    """Planifie un module unique et écrit son `module_draft.yaml`.

    Paramètres
    ----------
    module_name : str
        Nom du module à planifier.
    ec_path : Path
        Chemin de `.archcode/execution_context.yaml`.
    pd_path : Path
        Chemin de `.archcode/project_draft.yaml`.
    out_root : Path
        Racine de sortie `.archcode/modules`.
    overwrite : bool
        Écrasement autorisé si True.

    Retour
    ------
    None
    """
    ec = _load_ec(ec_path)
    pd = _load_pd(pd_path)
    out = write_module_draft(ec=ec, pd=pd, module_name=module_name, out_root=out_root, overwrite=overwrite)
    print(f"[OK] module_draft écrit → {out}")


def cmd_plan_all(
    *,
    ec_path: Path,
    pd_path: Path,
    out_root: Path,
    overwrite: bool,
) -> None:
    """Planifie tous les modules listés dans `project_draft.initial_modules`.

    Paramètres
    ----------
    ec_path : Path
        Chemin de `.archcode/execution_context.yaml`.
    pd_path : Path
        Chemin de `.archcode/project_draft.yaml`.
    out_root : Path
        Racine de sortie `.archcode/modules`.
    overwrite : bool
        Écrasement autorisé si True.

    Retour
    ------
    None
    """
    ec = _load_ec(ec_path)
    pd = _load_pd(pd_path)
    modules = pd.get("initial_modules") or []
    if not modules:
        print("[INFO] Aucun module déclaré dans project_draft.initial_modules.")
        return
    ok = 0
    for m in modules:
        try:
            out = write_module_draft(ec=ec, pd=pd, module_name=str(m), out_root=out_root, overwrite=overwrite)
            print(f"[OK] {m:12s} → {out}")
            ok += 1
        except FileExistsError as e:
            print(f"[SKIP] {e}")
    print(f"[DONE] Planification terminée : {ok}/{len(modules)} module(s) généré(s).")


def cmd_show(md_path: Path) -> None:
    """Affiche un résumé lisible d'un `module_draft.yaml`.

    Paramètres
    ----------
    md_path : Path
        Chemin vers un fichier `module_draft.yaml`.

    Retour
    ------
    None
    """
    doc = _read_yaml(md_path)
    md = doc.get("module_draft") or {}
    print("\n".join([
        f"module_name      : {md.get('module_name')}",
        f"user_story_id    : {md.get('user_story_id')}",
        f"files_expected   : {', '.join(md.get('files_expected') or []) or '∅'}",
        f"depends_on       : {', '.join(md.get('depends_on') or []) or '∅'}",
        f"entrypoint       : {md.get('entrypoint')}",
        f"validator_status : {md.get('validator_status')}",
        f"priority         : {(md.get('meta') or {}).get('priority')}",
        f"phase            : {(md.get('meta') or {}).get('phase')}",
    ]))


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Construit le parseur d'arguments pour plan/plan-all/show.

    Retour
    ------
    argparse.ArgumentParser
        Parseur configuré avec ses sous-commandes et options.
    """
    p = argparse.ArgumentParser(
        prog="agent_module_planner",
        description="ARCHCode — Planifie des modules → module_draft.yaml (déterministe, sans LLM).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_plan = sub.add_parser("plan", help="Planifier un module unique")
    sp_plan.add_argument("module_name", type=str, help="Nom du module à planifier")
    sp_plan.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml",
                         help="Chemin vers .archcode/execution_context.yaml")
    sp_plan.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml",
                         help="Chemin vers .archcode/project_draft.yaml")
    sp_plan.add_argument("--out-root", type=Path, default=Path(".archcode") / "modules",
                         help="Racine de sortie des modules")
    sp_plan.add_argument("--overwrite", action="store_true", help="Écraser si le fichier existe déjà")

    sp_plan_all = sub.add_parser("plan-all", help="Planifier tous les modules déclarés")
    sp_plan_all.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml",
                             help="Chemin vers .archcode/execution_context.yaml")
    sp_plan_all.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml",
                             help="Chemin vers .archcode/project_draft.yaml")
    sp_plan_all.add_argument("--out-root", type=Path, default=Path(".archcode") / "modules",
                             help="Racine de sortie des modules")
    sp_plan_all.add_argument("--overwrite", action="store_true", help="Écraser les fichiers existants")

    sp_show = sub.add_parser("show", help="Afficher un résumé d’un module_draft.yaml")
    sp_show.add_argument("path", type=Path, help="Chemin vers un module_draft.yaml")

    return p


def main(argv: Optional[List[str]] = None) -> None:
    """Point d'entrée CLI : plan / plan-all / show.

    Paramètres
    ----------
    argv : Optional[List[str]]
        Arguments de ligne de commande (None → sys.argv[1:]).

    Retour
    ------
    None

    Effets
    ------
    Exécute la sous-commande demandée et gère les erreurs courantes.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "plan":
            cmd_plan_single(
                module_name=args.module_name,
                ec_path=args.ec,
                pd_path=args.pd,
                out_root=args.out_root,
                overwrite=args.overwrite,
            )
        elif args.cmd == "plan-all":
            cmd_plan_all(
                ec_path=args.ec,
                pd_path=args.pd,
                out_root=args.out_root,
                overwrite=args.overwrite,
            )
        elif args.cmd == "show":
            cmd_show(md_path=args.path)
        else:
            parser.print_help()
            raise SystemExit(1)

    except FileNotFoundError as e:
        print(f"[ERREUR] {e}")
        raise SystemExit(1)
    except yaml.YAMLError as e:
        print(f"[ERREUR YAML] {e}")
        raise SystemExit(2)
    except (ValueError, FileExistsError) as e:
        print(f"[ERREUR] {e}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
