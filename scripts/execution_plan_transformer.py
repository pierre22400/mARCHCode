
# scripts/execution_plan_transformer.py
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime
import os
import yaml

"""
===============================================================================
ARCHCode — Transformeur déterministe plan_validated.yaml → execution_plan.yaml
-------------------------------------------------------------------------------
Rôle (non agentique) — PHASE 2 → 3.5 :
  - Lire `.archcode/plan_validated.yaml` (plan figé et référencé).
  - Produire `.archcode/execution_plan.yaml` :
      * propagation stricte : bus_message_id, plan_validated_id,
        spec_version_ref, loop_iteration
      * expansion déterministe des modules en lignes (PlanLine)
      * construction de chemins cibles (file_target) à partir d’une
        folder structure canonique ou de project_draft.yaml si disponible
      * génération stable de plan_line_id
      * métadonnées `meta` prêtes à injecter en balises #{meta}

Zéro LLM, zéro réseau. Comportement 100% déterministe et rejouable.

Entrées :
  - .archcode/plan_validated.yaml (obligatoire)
  - .archcode/project_draft.yaml (optionnel ; pour root & folder_structure)
  - .archcode/execution_context.yaml (optionnel ; cohérence bus/spec)

Sortie :
  - .archcode/execution_plan.yaml

Usage :
  python -m scripts.execution_plan_transformer build
  python -m scripts.execution_plan_transformer show
  python -m scripts.execution_plan_transformer build --pv other/plan_validated.yaml --pd other/project_draft.yaml --ec other/execution_context.yaml --out other/execution_plan.yaml
===============================================================================
"""


# -----------------------------------------------------------------------------
# Utils
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
    yaml.YAMLError
        Si le contenu n'est pas un YAML valide.
    FileNotFoundError
        Si le fichier n'existe pas.
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


def _short_hash(s: str, n: int = 6) -> str:
    """Calcule un hash court hexadécimal (SHA-1 tronqué).

    Paramètres
    ----------
    s : str
        Chaîne d'entrée.
    n : int, optionnel
        Longueur souhaitée (défaut : 6).

    Retour
    ------
    str
        Tranche initiale du digest SHA-1 de longueur n.
    """
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


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
# Chargement / validations minimales
# -----------------------------------------------------------------------------

def _load_plan_validated(pv_path: Path) -> Dict[str, Any]:
    """Charge et valide le contenu de `plan_validated.yaml`.

    Paramètres
    ----------
    pv_path : Path
        Chemin du plan validé.

    Retour
    ------
    Dict[str, Any]
        Racine normalisée (clé `plan_validated` si présente, sinon le document).

    Exceptions
    ----------
    ValueError
        Si des champs requis sont absents ou mal typés.
    FileNotFoundError
        Si le fichier n'existe pas.
    yaml.YAMLError
        Si le YAML est invalide.
    """
    doc = _read_yaml(pv_path)
    root = doc.get("plan_validated") or doc  # tolérance
    required = ("plan_validated_id", "bus_message_id", "spec_version_ref", "modules")
    missing = [k for k in required if root.get(k) in (None, "", [])]
    if missing:
        raise ValueError(f"plan_validated.yaml incomplet : champs manquants {missing}")
    if not isinstance(root["modules"], list):
        raise ValueError("plan_validated.modules doit être une liste")
    return root


def _load_project_draft(pd_path: Path) -> Dict[str, Any]:
    """Charge le `project_draft.yaml` et extrait la section utile.

    Paramètres
    ----------
    pd_path : Path
        Chemin du project_draft.

    Retour
    ------
    Dict[str, Any]
        Section `project_draft` si présente, sinon {}.
    """
    if not pd_path.exists():
        return {}
    doc = _read_yaml(pd_path)
    return doc.get("project_draft") or {}


def _load_ec(ec_path: Path) -> Dict[str, Any]:
    """Charge l'ExecutionContext s'il existe.

    Paramètres
    ----------
    ec_path : Path
        Chemin du fichier `.archcode/execution_context.yaml`.

    Retour
    ------
    Dict[str, Any]
        Dictionnaire EC ou {} si absent.
    """
    if not ec_path.exists():
        return {}
    return _read_yaml(ec_path)


# -----------------------------------------------------------------------------
# Chemins cibles (racine + mapping module → dossier)
# -----------------------------------------------------------------------------

_CANON_MODULE_DIR = {
    "core": "core/",
    "api": "api/",
    "auth": "auth/",
    "ui_layer": "ui/",
    "utils": "utils/",
    "billing": "billing/",
    "reports": "reports/",
    "tests": "tests/",
}

def _folder_root(pd: Dict[str, Any]) -> str:
    """Détermine la racine de dossier (`folder_root`) du projet.

    Paramètres
    ----------
    pd : Dict[str, Any]
        Section `project_draft` (peut être vide).

    Retour
    ------
    str
        Racine utilisée (par défaut "archcode_app/").
    """
    fs = pd.get("folder_structure") or {}
    root = fs.get("root")
    return str(root or "archcode_app/")


def _module_dir(name: str, pd: Dict[str, Any]) -> str:
    """Mappe un nom de module vers son sous-dossier, selon `project_draft` si possible.

    Paramètres
    ----------
    name : str
        Nom du module (ex. "api", "auth", "ui_layer"...).
    pd : Dict[str, Any]
        Section `project_draft` (peut préciser folder_structure.structure[]).

    Retour
    ------
    str
        Chemin relatif (avec '/' final) du dossier du module.
    """
    # Si project_draft fournit une structure explicite, on la respecte
    fs = pd.get("folder_structure") or {}
    struct = fs.get("structure") or []
    # Cherche un item dont name sans '/' matche le module canonique
    name_l = name.lower().strip("/")
    for it in struct:
        n = (it.get("name") or "").strip("/")
        if not n:
            continue
        base = n.split("/")[0].lower()
        if base == name_l:
            return n if n.endswith("/") else (n + "/")
    # Fallback canonique
    return _CANON_MODULE_DIR.get(name, f"{name}/")


# -----------------------------------------------------------------------------
# Heuristiques déterministes : role_hint & file_kind
# -----------------------------------------------------------------------------

def _basename(path: str) -> str:
    """Retourne la composante nom de fichier d'un chemin.

    Paramètres
    ----------
    path : str
        Chemin relatif ou absolu.

    Retour
    ------
    str
        basename (ex. 'file.py').
    """
    return os.path.basename(path)


def _role_hint(file_name: str, module_name: str) -> Optional[str]:
    """Infère un rôle indicatif pour le fichier (piste pour ACWP).

    Paramètres
    ----------
    file_name : str
        Nom du fichier (basename).
    module_name : str
        Nom du module contenant.

    Retour
    ------
    Optional[str]
        Rôle suggéré ('dto', 'schema', 'model', 'api', 'handler', 'test') ou None.
    """
    fn = file_name.lower()
    if "dto" in fn or fn.endswith("_dto.py"):
        return "dto"
    if "schema" in fn:
        return "schema"
    if "model" in fn:
        return "model"
    if "router" in fn or "routes" in fn:
        return "api"
    if "handler" in fn:
        return "handler"
    if module_name == "tests" or fn.startswith("test_") or fn.endswith("_test.py"):
        return "test"
    return None


def _file_kind(rel_path: str) -> str:
    """Classe le fichier en 'code' ou 'test' selon son chemin.

    Paramètres
    ----------
    rel_path : str
        Chemin relatif dans le projet.

    Retour
    ------
    str
        'test' si le chemin correspond à une convention de test, sinon 'code'.
    """
    rp = rel_path.lower()
    if rp.startswith("tests/") or "/tests/" in rp or rp.endswith("_test.py") or _basename(rp).startswith("test_"):
        return "test"
    return "code"


# -----------------------------------------------------------------------------
# Construction des lignes d'exécution
# -----------------------------------------------------------------------------

def _ensure_module_shape(mod: Any) -> Dict[str, Any]:
    """Vérifie la forme d'un module issu de plan_validated.modules[].

    Supporte deux formes :
      - dict complet (attendu),
      - str (nom de module) → lève ValueError (insuffisant).

    Paramètres
    ----------
    mod : Any
        Élément de la liste `modules`.

    Retour
    ------
    Dict[str, Any]
        Module normalisé (dict).

    Exceptions
    ----------
    ValueError
        Si la forme n'est pas conforme.
    """
    if isinstance(mod, dict):
        return mod
    if isinstance(mod, str):
        raise ValueError(
            "plan_validated.modules contient des chaînes simples. "
            "Un module structuré est requis (module_name, files_expected[], ...)."
        )
    raise ValueError("plan_validated.modules contient un élément invalide.")


def _build_lines(
    pv: Dict[str, Any],
    *,
    pd: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Construit les PlanLines déterministes à partir du plan validé.

    Paramètres
    ----------
    pv : Dict[str, Any]
        Racine du plan validé (normalisée).
    pd : Dict[str, Any]
        Section `project_draft` (pour folder_root et structure modules).

    Retour
    ------
    List[Dict[str, Any]]
        Liste ordonnée de lignes d'exécution (PlanLine).
    """
    root = _folder_root(pd)
    lines: List[Dict[str, Any]] = []
    seq = 1

    for mod in pv["modules"]:
        m = _ensure_module_shape(mod)
        name = str(m.get("module_name") or "").strip()
        if not name:
            raise ValueError("Module sans `module_name` dans plan_validated.modules[]")
        files = m.get("files_expected")
        if not isinstance(files, list) or not files:
            raise ValueError(f"Module '{name}' sans `files_expected[]`")

        user_story_id = m.get("user_story_id")
        responsibilities = m.get("responsibilities") or []
        depends_on = m.get("depends_on") or []
        priority = ((m.get("meta") or {}).get("priority")) or None

        base_dir = _module_dir(name, pd)
        for f in files:
            f = str(f).strip()
            if not f:
                continue
            # Si le fichier contient déjà des sous-répertoires, on les respecte,
            # sinon on le place sous le dossier du module.
            if "/" in f or "\\" in f:
                rel = f.replace("\\", "/").lstrip("./")
            else:
                rel = f"{base_dir}{f}"
            file_target = f"{root}{rel}"

            # ID stable : index séquentiel + hash stable module+rel
            h = _short_hash(f"{name}:{rel}", 8)
            plan_line_id = f"pl-{seq:04d}-{name}-{h}"
            seq += 1

            role = _role_hint(_basename(rel), name)
            kind = _file_kind(rel)

            line = {
                "plan_line_id": plan_line_id,
                "module_name": name,
                "user_story_id": user_story_id,
                "responsibilities": responsibilities or None,
                "depends_on": _dedup_str_list(depends_on),
                "priority": priority,
                "file_target": file_target,
                "file_kind": kind,  # "code" | "test"
                "action": "create_or_update",
                "role_hint": role,  # optionnel, pour ACWP
                "meta": {
                    "bus_message_id": pv.get("bus_message_id"),
                    "plan_validated_id": pv.get("plan_validated_id"),
                    "plan_line_ref": plan_line_id,
                    "loop_iteration": int(pv.get("loop_iteration") or 0),
                },
            }
            lines.append(line)

    return lines


# -----------------------------------------------------------------------------
# Build & Show
# -----------------------------------------------------------------------------

def build_execution_plan(
    *,
    pv_path: Path,
    pd_path: Path,
    ec_path: Path,
    out_path: Path,
) -> None:
    """Construit et écrit `.archcode/execution_plan.yaml` à partir du plan validé.

    Paramètres
    ----------
    pv_path : Path
        Chemin vers `.archcode/plan_validated.yaml`.
    pd_path : Path
        Chemin vers `.archcode/project_draft.yaml` (optionnel).
    ec_path : Path
        Chemin vers `.archcode/execution_context.yaml` (optionnel ; cohérence).
    out_path : Path
        Destination de l'`execution_plan.yaml`.

    Retour
    ------
    None

    Exceptions
    ----------
    ValueError
        Incohérence EC ↔ plan_validated (bus_message_id) ou modules invalides.
    FileNotFoundError, yaml.YAMLError
        Problèmes d'E/S ou de parsing YAML.
    """
    pv = _load_plan_validated(pv_path)
    pd = _load_project_draft(pd_path)
    ec = _load_ec(ec_path)

    # Cohérence (sans bloquer si EC absent)
    if ec:
        if str(ec.get("bus_message_id") or "") != str(pv.get("bus_message_id") or ""):
            raise ValueError("Incohérence : EC.bus_message_id ≠ plan_validated.bus_message_id")
        # Si spec_version est fournie, informative :
        # on ne bloque pas si elle diffère, la détection d'obsolescence
        # est du ressort de la vérification (PlanDiff TYPE_OUTDATED_SPEC_VERSION).

    lines = _build_lines(pv, pd=pd)
    doc = {
        "execution_plan": {
            # propagation stricte / traçabilité
            "project_name": pv.get("project_name") or (pd.get("project_name") if pd else None),
            "bus_message_id": pv.get("bus_message_id"),
            "plan_validated_id": pv.get("plan_validated_id"),
            "spec_version_ref": pv.get("spec_version_ref"),
            "loop_iteration": int(pv.get("loop_iteration") or 0),
            # infos de contexte (utiles aux outils)
            "folder_root": _folder_root(pd),
            "generated_at": _now_iso(),
            "total_lines": len(lines),
            "lines": lines,
        }
    }
    _write_yaml(doc, out_path)
    print(f"[OK] execution_plan écrit → {out_path} (lignes: {len(lines)})")


def show_execution_plan(out_path: Path) -> None:
    """Affiche un résumé synthétique d'un `execution_plan.yaml`.

    Paramètres
    ----------
    out_path : Path
        Chemin du fichier d'exécution.

    Retour
    ------
    None
    """
    if not out_path.exists():
        print("[INFO] Aucun execution_plan.yaml n'existe encore.")
        return
    doc = _read_yaml(out_path)
    ep = doc.get("execution_plan") or {}
    total = int(ep.get("total_lines") or 0)
    print("\n".join([
        f"project_name     : {ep.get('project_name')}",
        f"bus_message_id   : {ep.get('bus_message_id')}",
        f"plan_validated_id: {ep.get('plan_validated_id')}",
        f"spec_version_ref : {ep.get('spec_version_ref')}",
        f"loop_iteration   : {ep.get('loop_iteration')}",
        f"folder_root      : {ep.get('folder_root')}",
        f"total_lines      : {total}",
        f"generated_at     : {ep.get('generated_at')}",
    ]))
    # aperçu par module
    by_mod: Dict[str, int] = {}
    for ln in ep.get("lines") or []:
        m = ln.get("module_name") or "∅"
        by_mod[m] = by_mod.get(m, 0) + 1
    if by_mod:
        print("— Lignes par module —")
        for m, n in by_mod.items():
            print(f"  - {m:12s}: {n}")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Construit le parseur CLI (sous-commandes: build, show).

    Retour
    ------
    argparse.ArgumentParser
        Parseur configuré avec ses sous-commandes et options.
    """
    p = argparse.ArgumentParser(
        prog="execution_plan_transformer",
        description="Transformeur déterministe plan_validated.yaml → execution_plan.yaml (sans LLM).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_build = sub.add_parser("build", help="Génère execution_plan.yaml à partir du plan_validated.yaml")
    sp_build.add_argument("--pv", type=Path, default=Path(".archcode") / "plan_validated.yaml",
                          help="Chemin du plan_validated.yaml")
    sp_build.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml",
                          help="Chemin du project_draft.yaml (optionnel)")
    sp_build.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml",
                          help="Chemin de l'ExecutionContext (optionnel)")
    sp_build.add_argument("--out", type=Path, default=Path(".archcode") / "execution_plan.yaml",
                          help="Destination de l'execution_plan.yaml")

    sp_show = sub.add_parser("show", help="Affiche un résumé d’un execution_plan.yaml")
    sp_show.add_argument("--out", type=Path, default=Path(".archcode") / "execution_plan.yaml",
                         help="Chemin de l'execution_plan.yaml")

    return p


def main(argv: Optional[List[str]] = None) -> None:
    """Point d'entrée du module CLI.

    Paramètres
    ----------
    argv : Optional[List[str]]
        Arguments de ligne de commande (None → sys.argv[1:]).

    Retour
    ------
    None

    Effets
    ------
    Exécute la sous-commande demandée et gère les erreurs standards.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            build_execution_plan(pv_path=args.pv, pd_path=args.pd, ec_path=args.ec, out_path=args.out)
        elif args.cmd == "show":
            show_execution_plan(out_path=args.out)
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
