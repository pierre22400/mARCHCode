# agents/agent_module_compilator.py
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import fnmatch
import yaml

"""
===============================================================================
ARCHCode — agent_module_compilator (PHASE 2 : Agrégation progressive)
-------------------------------------------------------------------------------
Rôle :
  - Agréger des `module_draft.yaml` (issus d’agent_module_planner, idéalement
    passés par agent_module_validator) dans `.archcode/plan_draft_aggregated.yaml`.
  - Propager la traçabilité depuis ExecutionContext :
      bus_message_id, spec_version_ref (← EC.spec_version), loop_iteration.
  - Conserver le contexte de pilotage depuis project_draft.yaml :
      project_name, folder_structure, dependencies (si présents).
  - Dédupliquer / upsert par module_name, conserver `source_path` + `ingested_at`.
  - Zéro LLM, zéro réseau. Comportement déterministe.

Structure cible :
  plan_draft_aggregated:
    project_name: str
    bus_message_id: str
    spec_version_ref: str
    loop_iteration: int
    modules: [str, ...]
    dependencies: [str, ...]
    folder_structure: { root: str, structure: [ {name, description}, ... ] }
    items:
      - status: ok|pending|rejected|validated|untagged
        source_path: "…/module_draft.yaml"
        ingested_at: ISO-8601
        module_draft: { module_name, files_expected[], depends_on[], ... }
    warnings: [ "IGNORED path: reason", ... ]
    stats: { total_items, validated, pending, rejected }
    issued_at: ISO-8601
    aggregated_at: ISO-8601

Gouvernance (alignée diagramme AMDV → AMC) :
  - PAR DÉFAUT : on n’agrège QUE les modules **validés** (status `ok|validated`).
  - `--allow-non-ok`    → inclure aussi `pending`.
  - `--accept-untagged` → autoriser l’agrégation des drafts **sans** validator_status
                          (effet seulement si `--allow-non-ok` est présent).
  - Les `rejected` sont toujours ignorés (warning).

CLI principaux :
  - collect  : scan récursif (multi-racines, motifs), agrège dans le PGA.
  - add      : upsert d’un module_draft unique.
  - remove   : supprime un module par nom (recalcule les stats).
  - reset    : initialise un PGA vide (depuis EC + PD).
  - show     : résumé synthétique (statuts, deps, stats, warnings).

Compatibilité Mermaid :
  AMC[agent_module_compilator] --> PGA["plan_draft_aggregated.yaml"]

Auteur : Alex (fusion + gouvernance only-ok par défaut)
===============================================================================
"""


# -----------------------------------------------------------------------------
# Utils
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    """Retourne un horodatage ISO-8601 à la seconde."""
    return datetime.now().isoformat(timespec="seconds")


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Charge un fichier YAML en dict ({} si vide)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def _write_yaml(doc: Dict[str, Any], path: Path) -> None:
    """Écrit un dict dans un fichier YAML, en créant les dossiers si besoin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def _dedup_str_list(values: Optional[List[str]]) -> List[str]:
    """Déduplique une liste de chaînes en préservant l’ordre et en filtrant le vide."""
    if not values:
        return []
    out: List[str] = []
    seen = set()
    for v in values:
        s = str(v).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# -----------------------------------------------------------------------------
# Chargement EC / PD / PGA
# -----------------------------------------------------------------------------

def _load_ec(ec_path: Path) -> Dict[str, Any]:
    """Charge l’ExecutionContext et contrôle la présence de champs critiques."""
    ec = _read_yaml(ec_path)
    missing = [k for k in ("bus_message_id", "spec_version") if not ec.get(k)]
    if missing:
        raise ValueError(f"ExecutionContext incomplet : champs manquants {missing}")
    return ec


def _load_project_draft(pd_path: Path) -> Dict[str, Any]:
    """Charge project_draft.yaml (section project_draft) si disponible."""
    if not pd_path.exists():
        return {}
    doc = _read_yaml(pd_path)
    return doc.get("project_draft") or {}


def _init_pga_root(*, ec: Dict[str, Any], pd: Dict[str, Any]) -> Dict[str, Any]:
    """Construit une racine plan_draft_aggregated minimale depuis EC + PD."""
    return {
        "project_name": pd.get("project_name") or "project",
        "bus_message_id": ec.get("bus_message_id"),
        "spec_version_ref": ec.get("spec_version"),
        "loop_iteration": int(ec.get("loop_iteration") or 0),
        "modules": list(pd.get("initial_modules") or []),
        "dependencies": list(pd.get("dependencies") or []),
        "folder_structure": pd.get("folder_structure") or {},
        "items": [],
        "warnings": [],
        "stats": {"total_items": 0, "validated": 0, "pending": 0, "rejected": 0},
        "issued_at": _now_iso(),
        "aggregated_at": _now_iso(),
    }


def _load_or_init_pga(
    pga_path: Path,
    *,
    ec: Dict[str, Any],
    pd: Dict[str, Any],
    reset: bool,
    reset_if_missing: bool,
) -> Dict[str, Any]:
    """
    Charge le PGA existant, ou l’initialise si reset/reset_if_missing.
    Rafraîchit spec_version_ref / loop_iteration et complète deps/structure.
    """
    if reset or (reset_if_missing and not pga_path.exists()):
        return _init_pga_root(ec=ec, pd=pd)

    if pga_path.exists():
        doc = _read_yaml(pga_path)
        root = doc.get("plan_draft_aggregated")
        if not isinstance(root, dict):
            raise ValueError("PGA invalide (clé `plan_draft_aggregated` absente).")
        if str(root.get("bus_message_id") or "") != str(ec.get("bus_message_id") or ""):
            raise ValueError("bus_message_id PGA ≠ EC.bus_message_id (utilisez --reset).")
        # rafraîchit les champs volatiles
        root["spec_version_ref"] = ec.get("spec_version")
        root["loop_iteration"] = int(ec.get("loop_iteration") or 0)
        if not root.get("project_name"):
            root["project_name"] = pd.get("project_name") or root.get("project_name") or "project"
        # complète deps / structure si PD apporte de nouvelles infos
        if pd:
            root["dependencies"] = _dedup_str_list((root.get("dependencies") or []) + (pd.get("dependencies") or []))
            if not root.get("folder_structure"):
                root["folder_structure"] = pd.get("folder_structure") or {}
        return root

    # fichier manquant sans reset_if_missing → init propre
    return _init_pga_root(ec=ec, pd=pd)


# -----------------------------------------------------------------------------
# Validation et fusion de module_draft
# -----------------------------------------------------------------------------

def _extract_module_draft(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extrait la section module_draft ou tolère une racine directement structurée."""
    if "module_draft" in doc and isinstance(doc["module_draft"], dict):
        return doc["module_draft"]
    if isinstance(doc, dict) and "module_name" in doc:
        return doc
    return None


def _validate_module_draft(
    md: Dict[str, Any],
    *,
    allow_non_ok: bool,
    accept_untagged: bool,
) -> Tuple[bool, str]:
    """
    Retourne (accept, reason/status)

    Règles :
      - `module_name` et `files_expected[]` requis.
      - `validator_status` interprété :
          - ok|validated → acceptés (toujours)
          - pending      → acceptés seulement si allow_non_ok True
          - rejected     → toujours refusés
          - absent       → acceptés seulement si (allow_non_ok ET accept_untagged)
    """
    name = str(md.get("module_name") or "").strip()
    files = md.get("files_expected")
    if not name:
        return False, "Champ `module_name` manquant"
    if not isinstance(files, list) or not files:
        return False, "Champ `files_expected[]` manquant ou vide"

    status = str(md.get("validator_status") or "").strip().lower()
    if status in {"ok", "validated"}:
        return True, status
    if status == "rejected":
        return False, "validator_status=rejected"
    if status == "pending":
        return (allow_non_ok, "pending" if allow_non_ok else "pending (refusé: non-ok)")
    # non taggé
    if allow_non_ok and accept_untagged:
        return True, "untagged"
    return False, "non validé et non taggé (utilisez --allow-non-ok + --accept-untagged)"


def _bump_stats(pga_root: Dict[str, Any], status: str) -> None:
    """Met à jour les compteurs statistiques selon un statut texte."""
    stats = pga_root.get("stats") or {}
    stats.setdefault("total_items", 0)
    stats.setdefault("validated", 0)
    stats.setdefault("pending", 0)
    stats.setdefault("rejected", 0)

    stats["total_items"] += 1
    s = (status or "").lower()
    if s in {"ok", "validated"}:
        stats["validated"] += 1
    elif s == "rejected":
        stats["rejected"] += 1
    else:
        stats["pending"] += 1
    pga_root["stats"] = stats


def _recompute_stats(pga_root: Dict[str, Any]) -> None:
    """Recalcule intégralement les stats à partir des items actuels."""
    stats = {"total_items": 0, "validated": 0, "pending": 0, "rejected": 0}
    for it in pga_root.get("items") or []:
        s = str(it.get("status") or "").lower()
        stats["total_items"] += 1
        if s in {"ok", "validated"}:
            stats["validated"] += 1
        elif s == "rejected":
            stats["rejected"] += 1
        else:
            stats["pending"] += 1
    pga_root["stats"] = stats


def _upsert_item(pga_root: Dict[str, Any], *, md: Dict[str, Any], source_path: Path, status: str) -> None:
    """Ajoute/remplace un item pour `module_name` et met à jour modules/deps/stats."""
    name = str(md.get("module_name") or "").strip()
    if not name:
        return
    items = pga_root.get("items") or []
    new_item = {
        "status": status,
        "source_path": str(source_path.resolve()),
        "ingested_at": _now_iso(),
        "module_draft": md,
    }
    replaced = False
    for i, ex in enumerate(items):
        ex_md = (ex or {}).get("module_draft") or {}
        if str(ex_md.get("module_name") or "").strip() == name:
            items[i] = new_item
            replaced = True
            break
    if not replaced:
        items.append(new_item)
    pga_root["items"] = items

    # Maintenir la liste déclarative des modules
    pga_root["modules"] = _dedup_str_list((pga_root.get("modules") or []) + [name])

    # Dépendances (depuis le module)
    dep_strings = [str(d).strip() for d in (md.get("depends_on") or []) if str(d).strip()]
    if dep_strings:
        pga_root["dependencies"] = _dedup_str_list((pga_root.get("dependencies") or []) + dep_strings)

    # Stats
    _bump_stats(pga_root, status)


# -----------------------------------------------------------------------------
# Scan récursif des drafts
# -----------------------------------------------------------------------------

_DEFAULT_PATTERNS = [
    "**/module_draft.yaml",
    "**/*_module_draft.yaml",
    ".archcode/module_draft.yaml",
    ".archcode/*_module_draft.yaml",
]

def _find_module_drafts(roots: List[Path], patterns: Optional[List[str]] = None) -> List[Path]:
    """Retourne la liste des fichiers module_draft.yaml trouvés sous plusieurs racines."""
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
                    key = str(p.resolve())
                    if key not in seen:
                        seen.add(key)
                        found.append(p.resolve())
                    break
    return found


# -----------------------------------------------------------------------------
# Commandes
# -----------------------------------------------------------------------------

def cmd_reset(ec_yaml: Path, pd_yaml: Path, out: Path) -> None:
    """Initialise un PGA vide depuis EC + PD."""
    ec = _load_ec(ec_yaml)
    pd = _load_project_draft(pd_yaml)
    root = _init_pga_root(ec=ec, pd=pd)
    _write_yaml({"plan_draft_aggregated": root}, out)
    print(f"[OK] plan_draft_aggregated initialisé → {out}")


def cmd_add(
    *,
    ec_yaml: Path,
    pd_yaml: Path,
    pga_yaml: Path,
    module_yaml: Path,
    accept_untagged: bool,
    allow_non_ok: bool,
) -> None:
    """Ajoute (upsert) un module_draft unique dans un PGA existant."""
    if not pga_yaml.exists():
        raise FileNotFoundError(f"{pga_yaml} introuvable. Lancez `reset` ou `collect --reset-if-missing`.")
    doc_pga = _read_yaml(pga_yaml)
    pga_root = doc_pga.get("plan_draft_aggregated")
    if not isinstance(pga_root, dict):
        raise ValueError("PGA invalide (clé `plan_draft_aggregated` absente).")

    # cohérence EC (et rafraîchit quelques champs)
    ec = _load_ec(ec_yaml)
    pd = _load_project_draft(pd_yaml)
    pga_root["spec_version_ref"] = ec.get("spec_version")
    pga_root["loop_iteration"] = int(ec.get("loop_iteration") or 0)
    if not pga_root.get("project_name"):
        pga_root["project_name"] = pd.get("project_name") or pga_root.get("project_name") or "project"
    if pd and not pga_root.get("folder_structure"):
        pga_root["folder_structure"] = pd.get("folder_structure") or {}

    # lecture du module
    doc = _read_yaml(module_yaml)
    md = _extract_module_draft(doc)
    if not md:
        pga_root.setdefault("warnings", []).append(f"IGNORED {module_yaml}: pas de module_draft")
        _write_yaml({"plan_draft_aggregated": pga_root}, pga_yaml)
        print(f"[WARN] {module_yaml} ignoré (pas de module_draft)")
        return

    ok, reason = _validate_module_draft(md, allow_non_ok=allow_non_ok, accept_untagged=accept_untagged)
    if not ok:
        pga_root.setdefault("warnings", []).append(f"IGNORED {module_yaml}: {reason}")
        _write_yaml({"plan_draft_aggregated": pga_root}, pga_yaml)
        print(f"[WARN] {module_yaml} ignoré ({reason})")
        return

    _upsert_item(pga_root, md=md, source_path=module_yaml, status=(md.get("validator_status") or reason))
    pga_root["aggregated_at"] = _now_iso()
    _write_yaml({"plan_draft_aggregated": pga_root}, pga_yaml)
    print(f"[OK] Ajouté : {module_yaml}")


def cmd_collect(
    *,
    ec_yaml: Path,
    pd_yaml: Path,
    out: Path,
    roots: List[Path],
    patterns: Optional[List[str]],
    reset: bool,
    reset_if_missing: bool,
    allow_non_ok: bool,
    accept_untagged: bool,
    update_ec: bool,
) -> None:
    """
    Scanne récursivement des module_draft.yaml, applique la politique d’inclusion,
    agrège (upsert) et persiste le plan_draft_aggregated.yaml.
    """
    ec = _load_ec(ec_yaml)
    pd = _load_project_draft(pd_yaml)
    pga_root = _load_or_init_pga(out, ec=ec, pd=pd, reset=reset, reset_if_missing=reset_if_missing)

    files = _find_module_drafts(roots, patterns)
    if not files:
        print("[INFO] Aucun module_draft.yaml trouvé (scan terminé).")

    added = 0
    skipped = 0
    for f in files:
        try:
            doc = _read_yaml(f)
            md = _extract_module_draft(doc)
            if not md:
                pga_root.setdefault("warnings", []).append(f"IGNORED {f}: pas de module_draft")
                skipped += 1
                continue

            ok, reason = _validate_module_draft(md, allow_non_ok=allow_non_ok, accept_untagged=accept_untagged)
            if not ok:
                pga_root.setdefault("warnings", []).append(f"IGNORED {f}: {reason}")
                skipped += 1
                continue

            _upsert_item(pga_root, md=md, source_path=f, status=(md.get("validator_status") or reason))
            added += 1
        except yaml.YAMLError as e:
            pga_root.setdefault("warnings", []).append(f"IGNORED {f}: YAML invalide ({e})")
            skipped += 1

    # Persister PGA
    pga_root["aggregated_at"] = _now_iso()
    _write_yaml({"plan_draft_aggregated": pga_root}, out)
    print(f"[OK] Agrégation terminée : {added} ajouté(s), {skipped} ignoré(s). → {out}")

    # Option : mise à jour EC.modules
    if update_ec:
        ec["modules"] = list(pga_root.get("modules") or [])
        _write_yaml(ec, ec_yaml)
        print("[OK] ExecutionContext mis à jour (modules).")


def cmd_remove(
    *,
    ec_yaml: Path,
    out: Path,
    module_name: str,
) -> None:
    """Retire complètement un module du PGA (items + modules[]) et recalcule les stats."""
    _ = _load_ec(ec_yaml)  # contrôle min
    if not out.exists():
        raise FileNotFoundError(f"PGA introuvable : {out}")
    doc = _read_yaml(out)
    root = doc.get("plan_draft_aggregated") or {}
    items = root.get("items") or []
    keep: List[Dict[str, Any]] = []
    removed = False
    for it in items:
        md = (it or {}).get("module_draft") or {}
        name = str(md.get("module_name") or "").strip()
        if name == module_name:
            removed = True
            continue
        keep.append(it)
    root["items"] = keep
    root["modules"] = [m for m in (root.get("modules") or []) if m != module_name]
    _recompute_stats(root)
    _write_yaml({"plan_draft_aggregated": root}, out)
    if removed:
        print(f"[OK] Module '{module_name}' retiré du PGA.")
    else:
        print(f"[INFO] Aucun module '{module_name}' à retirer.")


def cmd_show(out: Path) -> None:
    """Affiche un résumé synthétique du PGA (modules, deps, stats, warnings)."""
    if not out.exists():
        print("[INFO] Aucun plan_draft_aggregated.yaml n'existe encore.")
        return
    doc = _read_yaml(out)
    root = doc.get("plan_draft_aggregated") or {}
    mods: List[str] = list(root.get("modules") or [])
    items: List[Dict[str, Any]] = list(root.get("items") or [])
    statuses: Dict[str, str] = {}
    for it in items:
        md = (it or {}).get("module_draft") or {}
        name = str(md.get("module_name") or "").strip()
        st = str(it.get("status") or "").strip()
        if name:
            statuses[name] = st or "∅"
    deps = ", ".join(root.get("dependencies") or []) or "∅"
    warns = root.get("warnings") or []
    stats = root.get("stats") or {}

    print("\n".join([
        f"project_name     : {root.get('project_name')}",
        f"bus_message_id   : {root.get('bus_message_id')}",
        f"spec_version_ref : {root.get('spec_version_ref')}",
        f"loop_iteration   : {root.get('loop_iteration')}",
        f"modules          : {', '.join(mods) or '∅'}",
        f"dependencies     : {deps}",
        f"items            : {len(items)}",
        f"stats            : {stats}",
        f"warnings         : {len(warns)}",
        f"issued_at        : {root.get('issued_at')}",
        f"aggregated_at    : {root.get('aggregated_at')}",
    ]))
    if items:
        print("— Statuts modules —")
        for m in mods:
            print(f"  - {m:12s} : {statuses.get(m, '∅')}")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Construit le parseur CLI pour collect/add/remove/reset/show."""
    p = argparse.ArgumentParser(
        prog="agent_module_compilator",
        description="ARCHCode — Agrège des module_draft.yaml → plan_draft_aggregated.yaml (déterministe).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # collect
    sp_collect = sub.add_parser("collect", help="Scanner et agréger les module_draft.yaml")
    sp_collect.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml",
                            help="Chemin vers .archcode/execution_context.yaml")
    sp_collect.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml",
                            help="Chemin vers .archcode/project_draft.yaml (optionnel)")
    sp_collect.add_argument("--out", type=Path, default=Path(".archcode") / "plan_draft_aggregated.yaml",
                            help="Destination du plan agrégé")
    sp_collect.add_argument("--roots", type=Path, nargs="*", default=[Path(".archcode/modules"), Path(".archcode"), Path(".")],
                            help="Racines de scan (répétables)")
    sp_collect.add_argument("--pattern", action="append", default=None,
                            help="Motif glob additionnel (répétable)")
    sp_collect.add_argument("--reset", action="store_true", help="Ré-initialise le PGA avant agrégation")
    sp_collect.add_argument("--reset-if-missing", action="store_true", help="Initialise le PGA s'il est manquant")
    sp_collect.add_argument("--allow-non-ok", action="store_true", help="Inclure aussi les modules 'pending'")
    sp_collect.add_argument("--accept-untagged", action="store_true",
                            help="Autoriser les modules sans validator_status (nécessite --allow-non-ok)")
    sp_collect.add_argument("--update-ec", action="store_true", help="Met à jour EC.modules avec la liste déclarée")

    # add
    sp_add = sub.add_parser("add", help="Ajouter (upsert) un module_draft.yaml unique")
    sp_add.add_argument("module_path", type=Path, help="Chemin vers un module_draft.yaml")
    sp_add.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml", help="Chemin EC")
    sp_add.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml", help="Chemin PD (optionnel)")
    sp_add.add_argument("--out", type=Path, default=Path(".archcode") / "plan_draft_aggregated.yaml", help="Destination du PGA")
    sp_add.add_argument("--allow-non-ok", action="store_true", help="Inclure un module 'pending'")
    sp_add.add_argument("--accept-untagged", action="store_true",
                        help="Autoriser un module sans validator_status (nécessite --allow-non-ok)")

    # remove
    sp_remove = sub.add_parser("remove", help="Retirer un module par nom")
    sp_remove.add_argument("module_name", type=str, help="Nom du module à retirer")
    sp_remove.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml", help="Chemin EC")
    sp_remove.add_argument("--out", type=Path, default=Path(".archcode") / "plan_draft_aggregated.yaml", help="Chemin du PGA")

    # reset
    sp_reset = sub.add_parser("reset", help="Initialiser un PGA vide (depuis EC + PD)")
    sp_reset.add_argument("--ec", type=Path, default=Path(".archcode") / "execution_context.yaml", help="Chemin EC")
    sp_reset.add_argument("--pd", type=Path, default=Path(".archcode") / "project_draft.yaml", help="Chemin PD")
    sp_reset.add_argument("--out", type=Path, default=Path(".archcode") / "plan_draft_aggregated.yaml", help="Destination du PGA")

    # show
    sp_show = sub.add_parser("show", help="Afficher un résumé du PGA")
    sp_show.add_argument("--out", type=Path, default=Path(".archcode") / "plan_draft_aggregated.yaml", help="Chemin du PGA")

    return p


def main(argv: Optional[List[str]] = None) -> None:
    """Point d’entrée CLI de l’agent : collect/add/remove/reset/show."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "collect":
            collect_modules(
                ec_yaml=args.ec,
                pd_yaml=args.pd,
                out=args.out,
                roots=list(args.roots or []),
                patterns=(args.pattern if args.pattern else None),
                reset=args.reset,
                reset_if_missing=args.reset_if_missing,
                allow_non_ok=args.allow_non_ok,
                accept_untagged=args.accept_untagged,
                update_ec=args.update_ec,
            )
        elif args.cmd == "add":
            cmd_add(
                ec_yaml=args.ec,
                pd_yaml=args.pd,
                pga_yaml=args.out,
                module_yaml=args.module_path,
                accept_untagged=args.accept_untagged,
                allow_non_ok=args.allow_non_ok,
            )
        elif args.cmd == "remove":
            cmd_remove(
                ec_yaml=args.ec,
                out=args.out,
                module_name=args.module_name,
            )
        elif args.cmd == "reset":
            cmd_reset(ec_yaml=args.ec, pd_yaml=args.pd, out=args.out)
        elif args.cmd == "show":
            cmd_show(out=args.out)
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
