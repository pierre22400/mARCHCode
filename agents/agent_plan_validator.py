# agents/agent_plan_validator.py
from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import yaml

"""
===============================================================================
ARCHCode — agent_plan_validator (PHASE 2 : Validation d'ensemble)
-------------------------------------------------------------------------------
Rôle (aligné avec le tiddler "Mapping réflexif" et le pipeline) :
  - Lire `.archcode/plan_draft_aggregated.yaml` (PGA) + `.archcode/execution_context.yaml` (EC).
  - Effectuer des contrôles déterministes (non-LLM) :
      • Alignement spec_version_ref ↔ EC.spec_version (fail → PlanDiff TYPE_OUTDATED_SPEC_VERSION)
      • Présence et unicité des modules
      • Contrats de mapping : user_story_id présent, responsibilities non vides, depends_on connus
      • Cohérence basique I/O vs SpecBlock (si fournis dans EC)
      • Rejet des modules pending/untagged/rejected par défaut (conforme flux AMDV → AMC → APV)
  - Produire si conforme :
      `.archcode/plan_validated.yaml` avec :
        plan_validated_id, bus_message_id, spec_version_ref, loop_iteration, modules[]
        meta.comment_agent_plan_validator (synthèse des avertissements non bloquants)
  - Sinon :
      `.archcode/comment_agent_plan_validator.yaml` (diagnostic actionnable) et sortie non nulle.
  - Option : mise à jour de l’EC (plan_validated_id).

Zéro LLM, zéro réseau. Tout est déterministe.

CLI (exemples) :
  - Validation stricte (par défaut) + mise à jour EC :
      python -m agents.agent_plan_validator validate --update-ec
  - Autoriser modules "pending" (et "untagged" traités comme pending) :
      python -m agents.agent_plan_validator validate --allow-pending
  - Tolérer un spec_version_ref obsolète (déconseillé) :
      python -m agents.agent_plan_validator validate --allow-outdated-spec
  - Afficher un PV existant :
      python -m agents.agent_plan_validator show ./.archcode/plan_validated.yaml
===============================================================================
"""


# -----------------------------------------------------------------------------
# Utilitaires généraux
# -----------------------------------------------------------------------------

def _now_iso() -> str:
    """Retourne un horodatage ISO-8601 à la seconde (traçabilité)."""
    return datetime.now().isoformat(timespec="seconds")


def _read_yaml(path: Path) -> Dict[str, Any]:
    """Charge un fichier YAML en dictionnaire ({} si vide)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def _write_yaml(doc: Dict[str, Any], path: Path) -> None:
    """Écrit un dictionnaire dans un fichier YAML, en créant les répertoires si besoin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def _gen_plan_validated_id() -> str:
    """Génère un identifiant court pour le plan validé (ex. 'PV-1a2b3c4d')."""
    return f"PV-{uuid.uuid4().hex[:8]}"


def _dedup_str_list(values: Optional[List[str]]) -> List[str]:
    """Déduplique une liste de chaînes en préservant l'ordre et en filtrant le vide."""
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


# -----------------------------------------------------------------------------
# Chargement artefacts
# -----------------------------------------------------------------------------

def _load_ec(ec_path: Path) -> Dict[str, Any]:
    """
    Charge l'ExecutionContext et vérifie les champs critiques.
    Exige : bus_message_id, spec_version, title.
    """
    ec = _read_yaml(ec_path)
    required = ("bus_message_id", "spec_version", "title")
    missing = [k for k in required if k not in ec or ec.get(k) in (None, "")]
    if missing:
        raise ValueError(f"ExecutionContext incomplet : champs manquants {missing}")
    return ec


def _load_pga(pga_path: Path) -> Dict[str, Any]:
    """
    Charge le plan agrégé (PGA) et retourne la racine `plan_draft_aggregated`.
    Lève ValueError si la clé racine est absente.
    """
    doc = _read_yaml(pga_path)
    root = doc.get("plan_draft_aggregated")
    if not isinstance(root, dict):
        raise ValueError("Fichier PGA invalide (clé `plan_draft_aggregated` absente).")
    return root


# -----------------------------------------------------------------------------
# Validation — règles déterministes
# -----------------------------------------------------------------------------

def _index_user_stories(ec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Construit un index id → user_story depuis EC.user_stories[]."""
    idx: Dict[str, Dict[str, Any]] = {}
    for us in ec.get("user_stories") or []:
        uid = str(us.get("id") or "").strip()
        if uid:
            idx[uid] = us
    return idx


def _check_spec_version(root_pga: Dict[str, Any], ec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Vérifie spec_version_ref (PGA) ↔ spec_version (EC).
    Retourne un PlanDiff-like si divergence détectée, sinon None.
    """
    pga_ver = root_pga.get("spec_version_ref")
    ec_ver = ec.get("spec_version")
    if pga_ver and ec_ver and pga_ver != ec_ver:
        return {
            "type": "TYPE_OUTDATED_SPEC_VERSION",
            "reason": "spec_version_ref du plan ≠ spec_version courante",
            "pga_spec_version_ref": pga_ver,
            "current_spec_version": ec_ver,
        }
    return None


def _validate_modules(
    root_pga: Dict[str, Any],
    ec: Dict[str, Any],
    *,
    allow_pending: bool,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """
    Valide les modules agrégés (root_pga.items[]) et prépare la liste structurée PV.modules[].

    Règles principales :
      - unicité des module_name
      - statut admissible : ok|validated (toujours) ; pending|untagged → admis si allow_pending ; rejected → refusé
      - files_expected[] requis, non vide, et typé str[]
      - user_story_id obligatoire et existant dans EC.user_stories
      - responsibilities[] non vide
      - depends_on : avertissement si référence non déclarée dans PGA.modules
      - inputs/outputs : avertissement si hors du vocabulaire SpecBlock (si connu)

    Retour (pv_modules, errors, warnings).
    """
    items = root_pga.get("items") or []
    declared_modules = list(root_pga.get("modules") or [])
    declared_set = set(declared_modules)
    known_inputs = set(ec.get("input_sources") or [])
    known_outputs = set(ec.get("output_targets") or [])
    us_index = _index_user_stories(ec)

    pv_modules: List[Dict[str, Any]] = []
    errors: List[str] = []
    warnings: List[str] = []

    seen_names: set[str] = set()

    for it in items:
        md = it.get("module_draft") or {}
        name = str(md.get("module_name") or "").strip()
        status = str(it.get("status") or md.get("validator_status") or "").lower()

        # 1) Existence + unicité
        if not name:
            errors.append("Module sans 'module_name'.")
            continue
        if name in seen_names:
            errors.append(f"Module dupliqué : '{name}'.")
            continue
        seen_names.add(name)

        # 2) Statuts admissibles (untagged traité comme pending)
        if status == "rejected":
            errors.append(f"Module '{name}' rejeté par validator (status=rejected).")
            continue
        if status in {"pending", "untagged", ""} and not allow_pending:
            shown = status or "∅"
            errors.append(f"Module '{name}' en statut '{shown}' non autorisé (utiliser --allow-pending si voulu).")
            continue

        # 3) Présence dans la liste des modules
        if declared_set and name not in declared_set:
            warnings.append(f"Module '{name}' non listé dans PGA.modules (sera inclus).")

        # 4) files_expected : requis, non vide, str[]
        files_expected = md.get("files_expected")
        if not isinstance(files_expected, list) or not files_expected:
            errors.append(f"Module '{name}': files_expected[] manquant ou vide.")
            continue
        if not all(isinstance(x, str) and x.strip() for x in files_expected):
            errors.append(f"Module '{name}': files_expected[] doit être une liste de chaînes non vides.")
            continue

        # 5) user_story_id mapping
        user_story_id = str(md.get("user_story_id") or "").strip()
        if not user_story_id:
            errors.append(f"Module '{name}': user_story_id manquant (contrat de mapping).")
            continue
        if user_story_id not in us_index:
            errors.append(f"Module '{name}': user_story_id inconnu dans SpecBlock/EC ('{user_story_id}').")
            continue

        # 6) responsibilities
        responsibilities = md.get("responsibilities")
        if not isinstance(responsibilities, list) or len(responsibilities) == 0:
            errors.append(f"Module '{name}': responsibilities[] vide (contrat de mapping).")
            continue

        # 7) depends_on (références inter-modules)
        depends_on = [str(d).strip() for d in (md.get("depends_on") or []) if str(d).strip()]
        for dep in depends_on:
            if dep == name:
                errors.append(f"Module '{name}': dépendance circulaire sur lui-même.")
            elif declared_set and dep not in declared_set:
                warnings.append(f"Module '{name}': dépendance '{dep}' non déclarée dans PGA.modules.")

        # 8) I/O (si vocabulaire connu)
        inputs = [str(x).strip() for x in (md.get("inputs") or []) if str(x).strip()]
        outputs = [str(x).strip() for x in (md.get("outputs") or []) if str(x).strip()]
        if inputs and known_inputs:
            unknown_in = [i for i in inputs if i not in known_inputs]
            if unknown_in:
                warnings.append(f"Module '{name}': inputs inconnus vs SpecBlock : {unknown_in}")
        if outputs and known_outputs:
            unknown_out = [o for o in outputs if o not in known_outputs]
            if unknown_out:
                warnings.append(f"Module '{name}': outputs inconnus vs SpecBlock : {unknown_out}")

        # 9) Contraintes techniques (tolérance MVP)
        tech = [str(x).strip() for x in (md.get("technical_constraints") or []) if str(x).strip()]

        # 10) Priority/meta éventuelle
        meta = md.get("meta") or {}
        priority = meta.get("priority")

        # Construction item PV
        pv_modules.append({
            "module_name": name,
            "user_story_id": user_story_id,
            "responsibilities": responsibilities,
            "inputs": inputs,
            "outputs": outputs,
            "files_expected": files_expected,
            "depends_on": depends_on,
            "technical_constraints": tech,
            "meta": {"priority": priority} if priority else {},
        })

    # Avertir si des modules déclarés n'ont pas d'item correspondant
    if declared_set:
        missing = [m for m in declared_modules if m not in seen_names]
        if missing:
            warnings.append(f"Modules déclarés sans draft agrégé: {missing}")

    # Ordonner PV.modules selon l’ordre PGA.modules, puis les autres
    if declared_modules:
        order = {name: i for i, name in enumerate(declared_modules)}
        pv_modules.sort(key=lambda m: (order.get(m.get("module_name"), 10_000), m.get("module_name")))

    return pv_modules, errors, warnings


# -----------------------------------------------------------------------------
# Émission des artefacts
# -----------------------------------------------------------------------------

def _emit_comment(comment_path: Path, reasons: List[str], plan_diffs: List[Dict[str, Any]], *, ec: Dict[str, Any], pga: Dict[str, Any]) -> None:
    """
    Écrit un commentaire d'agent (bloquant) à destination du planner/compilator.
    Contient contexte de version pour faciliter la correction.
    """
    doc = {
        "comment_agent_plan_validator": {
            "issued_at": _now_iso(),
            "bus_message_id": ec.get("bus_message_id"),
            "ec_spec_version": ec.get("spec_version"),
            "pga_spec_version_ref": pga.get("spec_version_ref"),
            "blocking_errors": reasons,
            "plan_diffs": plan_diffs,
            "action": (
                "Corriger le PGA / modules puis relancer la validation. "
                "En cas d'impasse systémique, déclencher un spec_amendment.yaml."
            ),
        }
    }
    _write_yaml(doc, comment_path)


def _emit_plan_validated(
    out_path: Path,
    *,
    pv_id: str,
    ec: Dict[str, Any],
    root_pga: Dict[str, Any],
    pv_modules: List[Dict[str, Any]],
    warnings: List[str],
    plan_diffs: List[Dict[str, Any]],
) -> None:
    """
    Écrit `plan_validated.yaml` avec synthèse des avertissements et diffs.
    Respecte la philosophie « SpecBlock ↔ PV ↔ EP » (référencement croisé).
    """
    pv = {
        "plan_validated": {
            "plan_validated_id": pv_id,
            "bus_message_id": ec.get("bus_message_id"),
            "spec_version_ref": ec.get("spec_version"),
            "loop_iteration": int(ec.get("loop_iteration") or 0),
            "project_name": root_pga.get("project_name") or "project",
            "modules": pv_modules,
            "meta": {
                "comment_agent_plan_validator": "\n".join(warnings) if warnings else "",
                "created_at": _now_iso(),
                "validated_at": _now_iso(),
            },
            # Exposé pour audit réflexif (PlanDiffBlock-like)
            "plan_diffs": plan_diffs,
        }
    }
    _write_yaml(pv, out_path)


def _maybe_update_ec_with_pv_id(ec_path: Path, pv_id: str) -> None:
    """Met à jour EC.plan_validated_id si demandé."""
    ec = _read_yaml(ec_path)
    ec["plan_validated_id"] = pv_id
    _write_yaml(ec, ec_path)


# -----------------------------------------------------------------------------
# Commandes
# -----------------------------------------------------------------------------

def cmd_validate(
    *,
    ec_path: Path,
    pga_path: Path,
    out_path: Path,
    comment_path: Path,
    allow_pending: bool,
    allow_outdated_spec: bool,
    update_ec: bool,
) -> int:
    """
    Exécute la validation globale : succès → plan_validated.yaml, sinon commentaire bloquant.

    Retourne code 0 en cas de succès ; >0 si échec.
    """
    ec = _load_ec(ec_path)
    root_pga = _load_pga(pga_path)

    # 1) Diff de version Spec
    plan_diffs: List[Dict[str, Any]] = []
    diff = _check_spec_version(root_pga, ec)
    if diff:
        if allow_outdated_spec:
            plan_diffs.append(diff)
        else:
            _emit_comment(comment_path, ["SpecBlock version mismatch (see plan_diffs)."], [diff], ec=ec, pga=root_pga)
            print("[KO] spec_version_ref ≠ spec_version (bloquant).")
            return 2

    # 2) Validation des modules
    pv_modules, errors, warnings = _validate_modules(root_pga, ec, allow_pending=allow_pending)
    if errors:
        _emit_comment(comment_path, errors, plan_diffs, ec=ec, pga=root_pga)
        print(f"[KO] Validation échouée ({len(errors)} erreurs). Commentaire émis → {comment_path}")
        return 2

    # 3) Émission du plan_validated
    pv_id = _gen_plan_validated_id()
    _emit_plan_validated(
        out_path,
        pv_id=pv_id,
        ec=ec,
        root_pga=root_pga,
        pv_modules=pv_modules,
        warnings=warnings,
        plan_diffs=plan_diffs,
    )
    print(f"[OK] plan_validated.yaml émis → {out_path} (id={pv_id})")

    # 4) Mise à jour EC si demandé
    if update_ec:
        _maybe_update_ec_with_pv_id(ec_path, pv_id)
        print("[OK] ExecutionContext mis à jour (plan_validated_id).")

    # 5) Avertissements éventuels
    if warnings or plan_diffs:
        print(f"[WARN] {len(warnings)} avertissement(s), {len(plan_diffs)} diff(s) non bloquants.")

    return 0


def cmd_show(pv_path: Path) -> None:
    """Affiche un résumé d'un plan_validated.yaml."""
    doc = _read_yaml(pv_path)
    pv = doc.get("plan_validated") or {}
    mods = ", ".join([m.get("module_name", "∅") for m in (pv.get("modules") or [])]) or "∅"
    print("\n".join([
        f"plan_validated_id : {pv.get('plan_validated_id')}",
        f"bus_message_id    : {pv.get('bus_message_id')}",
        f"spec_version_ref  : {pv.get('spec_version_ref')}",
        f"loop_iteration    : {pv.get('loop_iteration')}",
        f"project_name      : {pv.get('project_name')}",
        f"modules           : {mods}",
        f"created_at        : {(pv.get('meta') or {}).get('created_at')}",
    ]))


def _build_parser() -> argparse.ArgumentParser:
    """Construit le parseur CLI pour validate/show."""
    p = argparse.ArgumentParser(
        prog="agent_plan_validator",
        description="ARCHCode — Valide le plan agrégé et produit plan_validated.yaml (déterministe).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_val = sub.add_parser("validate", help="Valide PGA+EC → plan_validated.yaml ou commentaire bloquant")
    sp_val.add_argument(
        "--ec",
        type=Path,
        default=Path(".archcode") / "execution_context.yaml",
        help="Chemin de l'ExecutionContext",
    )
    sp_val.add_argument(
        "--pga",
        type=Path,
        default=Path(".archcode") / "plan_draft_aggregated.yaml",
        help="Chemin du plan agrégé",
    )
    sp_val.add_argument(
        "--out",
        type=Path,
        default=Path(".archcode") / "plan_validated.yaml",
        help="Destination du plan validé",
    )
    sp_val.add_argument(
        "--comment-out",
        type=Path,
        default=Path(".archcode") / "comment_agent_plan_validator.yaml",
        help="Destination du commentaire bloquant en cas d'échec",
    )
    sp_val.add_argument(
        "--allow-pending",
        action="store_true",
        help="Autorise l'inclusion de modules au statut 'pending' et 'untagged'",
    )
    sp_val.add_argument(
        "--allow-outdated-spec",
        action="store_true",
        help="Tolère un spec_version_ref obsolète (émet un diff non bloquant)",
    )
    sp_val.add_argument(
        "--update-ec",
        action="store_true",
        help="Met à jour EC.plan_validated_id après succès",
    )

    sp_show = sub.add_parser("show", help="Affiche un résumé d'un plan_validated.yaml")
    sp_show.add_argument(
        "pv_yaml",
        type=Path,
        nargs="?",
        default=Path(".archcode") / "plan_validated.yaml",
        help="Chemin du plan validé",
    )

    return p


def main(argv: Optional[List[str]] = None) -> None:
    """Point d'entrée CLI : validate/show."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "validate":
            code = cmd_validate(
                ec_path=args.ec,
                pga_path=args.pga,
                out_path=args.out,
                comment_path=args.comment_out,
                allow_pending=args.allow_pending,
                allow_outdated_spec=args.allow_outdated_spec,
                update_ec=args.update_ec,
            )
            raise SystemExit(code)
        elif args.cmd == "show":
            cmd_show(pv_path=args.pv_yaml)
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
