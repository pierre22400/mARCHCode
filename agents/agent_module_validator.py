
# agents/agent_module_validator.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import fnmatch
import os
import re
import yaml

"""
===============================================================================
ARCHCode — agent_module_validator (PHASE 2 : Validation locale des modules)
-------------------------------------------------------------------------------
But (aligné pipeline) :
  - Vérifier la cohérence d’un `module_draft.yaml` produit par agent_module_planner.
  - Émettre un verdict déterministe : `ok` | `pending` | `rejected`.
  - Produire un artefact de commentaire local :
        `.archcode/modules/<module_name>/comment_module_validator.yaml`
    avec erreurs, avertissements, suggestions, horodatage et méta.
  - (option) Mettre à jour in-place `validator_status` dans `module_draft.yaml`.

Gouvernance :
  - Zéro LLM, zéro réseau. Règles statiques et déterministes.
  - Traçabilité stricte : on contrôle la cohérence avec `.archcode/execution_context.yaml`
    et on croise certaines infos avec `.archcode/project_draft.yaml`.

Entrées :
  - `.archcode/modules/**/module_draft.yaml` (un ou plusieurs)
  - `.archcode/execution_context.yaml` (requis)
  - `.archcode/project_draft.yaml` (optionnel, améliore les contrôles)

Sorties :
  - `comment_module_validator.yaml` à côté du module_draft
  - Mise à jour (optionnelle) du champ `validator_status` dans le module_draft
  - Codes de sortie CLI adaptés (0 = succès de la commande, pas du verdict)

CLI — Exemples :
  # Valider un module et écrire le commentaire + status en place
  python -m agents.agent_module_validator validate .archcode/modules/auth/module_draft.yaml --write-status

  # Valider tous les modules trouvés récursivement (strict + échec sur avertissements)
  python -m agents.agent_module_validator validate-all --strict --fail-on-warn

  # Résumé d’un commentaire
  python -m agents.agent_module_validator show .archcode/modules/auth/comment_module_validator.yaml


===============================================================================
"""


# -----------------------------------------------------------------------------
# Utilitaires I/O
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    """Retourne l’horodatage ISO-8601 (seconde)."""
    return datetime.now().isoformat(timespec="seconds")


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Charge un fichier YAML en dict ({} si vide)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def _write_yaml(doc: Dict[str, Any], path: Path) -> None:
    """Écrit un dict dans un fichier YAML (crée les dossiers si besoin)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def _dedup_str_list(values: Optional[List[str]]) -> List[str]:
    """Déduplique une liste de chaînes en préservant l’ordre."""
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
# Chargement des artefacts de contexte
# -----------------------------------------------------------------------------

def _load_ec(path: Path) -> Dict[str, Any]:
    """Charge `.archcode/execution_context.yaml` et contrôle des champs critiques."""
    ec = _read_yaml(path)
    missing = [k for k in ("bus_message_id",) if not ec.get(k)]
    if missing:
        raise ValueError(f"ExecutionContext incomplet : champs manquants {missing}")
    return ec


def _load_pd(path: Path) -> Dict[str, Any]:
    """Charge `.archcode/project_draft.yaml` (section `project_draft`) si présent."""
    if not path.exists():
        return {}
    doc = _read_yaml(path)
    return doc.get("project_draft") or {}


# -----------------------------------------------------------------------------
# Localisation des fichiers module_draft
# -----------------------------------------------------------------------------

_DEFAULT_PATTERNS = [
    "**/module_draft.yaml",
    "**/*_module_draft.yaml",           # tolérance
    ".archcode/module_draft.yaml",
    ".archcode/*_module_draft.yaml",
]

def _find_module_drafts(roots: List[Path], patterns: Optional[List[str]] = None) -> List[Path]:
    """Recherche récursivement des fichiers module_draft selon plusieurs motifs."""
    patterns = patterns or _DEFAULT_PATTERNS
    found: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        root = root.resolve()
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            for pat in patterns:
                if fnmatch.fnmatch(rel, pat):
                    rp = str(p.resolve())
                    if rp not in seen:
                        seen.add(rp)
                        found.append(p.resolve())
                    break
    return found


# -----------------------------------------------------------------------------
# Règles de validation (déterministes)
# -----------------------------------------------------------------------------

_ALLOWED_FILE_EXT = {".py", ".md", ".txt", ".yml", ".yaml", ".json", ".ini"}
_MOD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

def _extract_module(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extrait la section `module_draft` ou tolère une racine équivalente."""
    if isinstance(doc, dict) and "module_draft" in doc and isinstance(doc["module_draft"], dict):
        return doc["module_draft"]
    if isinstance(doc, dict) and "module_name" in doc:
        return doc
    return None


def _ext(path: str) -> str:
    """Retourne l’extension (minuscule) du fichier."""
    return os.path.splitext(path)[1].lower()


def _validate_module_draft(
    md: Dict[str, Any],
    *,
    ec: Dict[str, Any],
    pd: Dict[str, Any],
    strict: bool,
    fail_on_warn: bool,
) -> Tuple[str, List[str], List[str], List[str]]:
    """
    Applique des règles déterministes et retourne (verdict, errors, warnings, suggestions).

    - `verdict` : "ok" | "pending" | "rejected"
    """
    errors: List[str] = []
    warns: List[str] = []
    suggs: List[str] = []

    # --- Champs obligatoires minimaux
    name = str(md.get("module_name") or "").strip()
    if not name:
        errors.append("Champ `module_name` manquant ou vide.")
    elif not _MOD_NAME_RE.match(name):
        errors.append(f"`module_name` invalide : {name!r} (attendu : [A-Za-z][A-Za-z0-9_]*).")

    files = md.get("files_expected")
    if not isinstance(files, list) or not files:
        errors.append("Champ `files_expected[]` manquant ou vide.")
        files = []

    # --- Listes facultatives typées
    responsibilities = md.get("responsibilities")
    if responsibilities is not None and not isinstance(responsibilities, list):
        errors.append("`responsibilities` doit être une liste de chaînes.")
    elif strict and not responsibilities:
        warns.append("Aucune `responsibilities[]` précisée (mode strict).")

    depends_on = md.get("depends_on") or []
    if not isinstance(depends_on, list):
        errors.append("`depends_on` doit être une liste.")
        depends_on = []

    tech_constraints = md.get("technical_constraints") or []
    if tech_constraints is not None and not isinstance(tech_constraints, list):
        errors.append("`technical_constraints` doit être une liste.")

    # --- Entrypoint
    entrypoint = md.get("entrypoint")
    if entrypoint is not None and not isinstance(entrypoint, str):
        errors.append("`entrypoint` doit être une chaîne si présent.")
    if isinstance(entrypoint, str) and entrypoint.strip():
        ep = entrypoint.strip()
        if ":" in ep:
            fpart = ep.split(":", 1)[0].strip()
        else:
            fpart = ep
        if fpart not in files:
            warns.append(f"`entrypoint` réfère '{fpart}' qui n’est pas dans `files_expected[]`.")

    # --- Sanitation files_expected
    deduped = _dedup_str_list(files)
    bad_paths = []
    ext_warns = 0
    for f in deduped:
        if f.startswith("/") or f.startswith("\\"):
            bad_paths.append(f"chemin absolu interdit : {f}")
        if ".." in f.split("/"):
            bad_paths.append(f"chemin parent '..' interdit : {f}")
        if _ext(f) not in _ALLOWED_FILE_EXT:
            ext_warns += 1
    if bad_paths:
        errors.extend([f"`files_expected[]`: {bp}" for bp in bad_paths])
    if ext_warns and strict:
        warns.append(f"{ext_warns} fichier(s) avec extension non standard (mode strict).")

    # --- Cohérence avec project_draft (dépendances existantes)
    pd_mods = set(pd.get("initial_modules") or [])
    for dep in depends_on:
        dep_s = str(dep).strip()
        if not dep_s:
            continue
        if dep_s == name:
            warns.append("`depends_on` contient le module lui-même (auto-dépendance).")
        if pd_mods and dep_s not in pd_mods:
            warns.append(f"`depends_on` référence un module inconnu dans project_draft : {dep_s}")

    # --- user_story_id cohérente avec EC (si dispo)
    user_story_id = md.get("user_story_id")
    if user_story_id:
        ec_ids = {str(us.get("id")) for us in (ec.get("user_stories") or []) if us.get("id")}
        if ec_ids and str(user_story_id) not in ec_ids:
            warns.append(f"`user_story_id` inconnue dans ExecutionContext : {user_story_id}")

    # --- Méta de traçabilité (présence & cohérence bus/spec)
    meta = md.get("meta") or {}
    bus = meta.get("bus_message_id")
    spec_ref = meta.get("spec_version_ref")
    if not bus:
        warns.append("meta.bus_message_id absent (traçabilité recommandée).")
    elif bus and str(bus) != str(ec.get("bus_message_id")):
        errors.append("meta.bus_message_id ≠ EC.bus_message_id (incohérence de traçabilité).")
    if spec_ref and ec.get("spec_version") and str(spec_ref) != str(ec.get("spec_version")):
        # Pas bloquant : la politique d'outdated est traitée plus loin dans le pipeline
        warns.append("meta.spec_version_ref ≠ EC.spec_version (possible outdated).")

    # --- Politique de verdict
    if errors:
        verdict = "rejected"
    else:
        if strict and (not responsibilities or ext_warns):
            # strict peut forcer 'pending' si certains champs faibles
            verdict = "pending"
        elif fail_on_warn and warns:
            verdict = "rejected"
        else:
            verdict = "ok"

    # --- Suggestions légères
    if not responsibilities:
        suggs.append("Ajouter des `responsibilities[]` pour clarifier le périmètre.")
    if name == "api" and "tests" not in pd_mods:
        suggs.append("Prévoir un module `tests` pour couvrir les endpoints.")
    if name == "auth" and "tokens.py" not in files:
        suggs.append("Ajouter `tokens.py` pour centraliser la logique JWT/refresh.")
    if entrypoint is None and name in {"api", "ui_layer"}:
        suggs.append("Définir `entrypoint` (ex. `routes.py` ou `cli.py:main`).")

    return verdict, errors, warns, suggs


# -----------------------------------------------------------------------------
# Écriture du commentaire et mise à jour du module
# -----------------------------------------------------------------------------

def _comment_path_for(module_yaml: Path) -> Path:
    """Calcule le chemin du fichier de commentaire à côté du module_draft."""
    mod_dir = module_yaml.parent
    return mod_dir / "comment_module_validator.yaml"


def _write_comment(
    *,
    module_yaml: Path,
    md: Dict[str, Any],
    verdict: str,
    errors: List[str],
    warns: List[str],
    suggs: List[str],
    ec: Dict[str, Any],
) -> Path:
    """Écrit `comment_module_validator.yaml` avec diagnostic complet."""
    name = str(md.get("module_name") or "∅")
    meta = md.get("meta") or {}
    doc = {
        "comment_module_validator": {
            "module_name": name,
            "status": verdict,
            "checked_at": _now_iso(),
            "bus_message_id": meta.get("bus_message_id") or ec.get("bus_message_id"),
            "spec_version_ref": meta.get("spec_version_ref") or ec.get("spec_version"),
            "loop_iteration": meta.get("loop_iteration"),
            "errors": errors,
            "warnings": warns,
            "suggestions": suggs,
            "validator_rules_version": "1.0",
            "summary": (
                "Aucun problème bloquant détecté."
                if verdict == "ok" else
                f"{len(errors)} erreur(s), {len(warns)} avertissement(s)."
            ),
        }
    }
    path = _comment_path_for(module_yaml)
    _write_yaml(doc, path)
    return path


def _update_validator_status(module_yaml: Path, verdict: str) -> None:
    """Met à jour in-place `validator_status` dans le module_draft."""
    doc = _read_yaml(module_yaml)
    md = _extract_module(doc) or {}
    if "module_draft" in doc:
        doc["module_draft"]["validator_status"] = verdict
    else:
        doc["validator_status"] = verdict
    _write_yaml(doc, module_yaml)


# -----------------------------------------------------------------------------
# Commandes haut niveau
# -----------------------------------------------------------------------------

def validate_single(
    *,
    module_yaml: Path,
    ec_path: Path,
    pd_path: Path,
    strict: bool,
    fail_on_warn: bool,
    write_status: bool,
) -> str:
    """Valide un module_draft unique et retourne le verdict."""
    ec = _load_ec(ec_path)
    pd = _load_pd(pd_path)
    doc = _read_yaml(module_yaml)
    md = _extract_module(doc)
    if not md:
        raise ValueError(f"{module_yaml} n’est pas un module_draft valide.")

    verdict, errors, warns, suggs = _validate_module_draft(
        md, ec=ec, pd=pd, strict=strict, fail_on_warn=fail_on_warn
    )
    cpath = _write_comment(
        module_yaml=module_yaml, md=md, verdict=verdict,
        errors=errors, warns=warns, suggs=suggs, ec=ec
    )
    print(f"[OK] Commentaire écrit → {cpath} [{verdict}]")
    if write_status:
        _update_validator_status(module_yaml, verdict)
        print(f"[OK] validator_status mis à jour → {module_yaml}")
    return verdict


def validate_all(
    *,
    roots: List[Path],
    patterns: Optional[List[str]],
    ec_path: Path,
    pd_path: Path,
    strict: bool,
    fail_on_warn: bool,
    write_status: bool,
) -> None:
    """Valide tous les module_draft trouvés récursivement et affiche un résumé."""
    files = _find_module_drafts(roots, patterns)
    if not files:
        print("[INFO] Aucun module_draft.yaml trouvé.")
        return

    counts = {"ok": 0, "pending": 0, "rejected": 0}
    for f in files:
        try:
            v = validate_single(
                module_yaml=f, ec_path=ec_path, pd_path=pd_path,
                strict=strict, fail_on_warn=fail_on_warn, write_status=write_status
            )
            counts[v] = counts.get(v, 0) + 1
        except Exception as e:
            print(f"[ERR ] {f}: {e}")
            counts["rejected"] += 1

    total = sum(counts.values())
    print("\n— Résultat global —")
    print(f"  total     : {total}")
    print(f"  ok        : {counts['ok']}")
    print(f"  pending   : {counts['pending']}")
    print(f"  rejected  : {counts['rejected']}")


def show_comment(path: Path) -> None:
    """Affiche un résumé lisible d’un `comment_module_validator.yaml`."""
    doc = _read_yaml(path)
    cmv = doc.get("comment_module_validator") or {}
    print("\n".join([
        f"module_name   : {cmv.get('module_name')}",
        f"status        : {cmv.get('status')}",
        f"checked_at    : {cmv.get('checked_at')}",
        f"errors        : {len(cmv.get('errors') or [])}",
        f"warnings      : {len(cmv.get('warnings') or [])}",
        f"suggestions   : {len(cmv.get('suggestions') or [])}",
        f"bus_message_id: {cmv.get('bus_message_id')}",
        f"spec_version  : {cmv.get('spec_version_ref')}",
        f"loop_iteration: {cmv.get('loop_iteration')}",
        f"summary       : {cmv.get('summary')}",
    ]))


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Construit le parseur CLI pour validate/validate-all/show."""
    p = argparse.ArgumentParser(
        prog="agent_module_validator",
        description="ARCHCode — Valide des module_draft.yaml (déterministe, sans LLM).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_validate = sub.add_parser("validate", help="Valider un module_draft unique")
    sp_validate.add_argument("module_yaml", type=Path, help="Chemin d’un module_draft.yaml")
    sp_validate.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml",
                             help="Chemin vers .archcode/execution_context.yaml")
    sp_validate.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml",
                             help="Chemin vers .archcode/project_draft.yaml (optionnel)")
    sp_validate.add_argument("--strict", action="store_true", help="Active des contrôles renforcés")
    sp_validate.add_argument("--fail-on-warn", action="store_true",
                             help="Transforme les warnings en rejet")
    sp_validate.add_argument("--write-status", action="store_true",
                             help="Met à jour `validator_status` dans le module_draft")

    sp_validate_all = sub.add_parser("validate-all", help="Valider tous les modules trouvés")
    sp_validate_all.add_argument("--roots", type=Path, nargs="*", default=[Path(".archcode"), Path(".")],
                                 help="Racines de scan des modules")
    sp_validate_all.add_argument("--pattern", action="append", default=None,
                                 help="Motif glob supplémentaire (répétable)")
    sp_validate_all.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml",
                                 help="Chemin vers .archcode/execution_context.yaml")
    sp_validate_all.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml",
                                 help="Chemin vers .archcode/project_draft.yaml (optionnel)")
    sp_validate_all.add_argument("--strict", action="store_true", help="Active des contrôles renforcés")
    sp_validate_all.add_argument("--fail-on-warn", action="store_true",
                                 help="Transforme les warnings en rejet")
    sp_validate_all.add_argument("--write-status", action="store_true",
                                 help="Met à jour `validator_status` dans chaque module")

    sp_show = sub.add_parser("show", help="Afficher un résumé d’un commentaire validator")
    sp_show.add_argument("comment_yaml", type=Path, help="Chemin d’un comment_module_validator.yaml")

    return p


def main(argv: Optional[List[str]] = None) -> None:
    """Point d’entrée CLI : validate / validate-all / show."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "validate":
            validate_single(
                module_yaml=args.module_yaml,
                ec_path=args.ec,
                pd_path=args.pd,
                strict=args.strict,
                fail_on_warn=args.fail_on_warn,
                write_status=args.write_status,
            )
        elif args.cmd == "validate-all":
            validate_all(
                roots=list(args.roots or []),
                patterns=args.pattern if args.pattern else None,
                ec_path=args.ec,
                pd_path=args.pd,
                strict=args.strict,
                fail_on_warn=args.fail_on_w ar n if hasattr(args, "fail_on_warn") else False,  # defensive
                write_status=args.write_status,
            )
        elif args.cmd == "show":
            show_comment(args.comment_yaml)
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
