# core/yaml_io.py
from __future__ import annotations

"""
# ------------------------------------------------------------
# YAML I/O — ExecutionPlan (dataclass) & PatchBlock dump (YAML)
# ------------------------------------------------------------
# Rôle
#   - Charger un `execution_plan.yaml` en dataclass légère `ExecutionPlan`
#   - Valider MINIMALEMENT la structure (modules/plan_lines + champs clés)
#   - Archiver un `PatchBlock` au format YAML lisible (pas de JSON)
#
# Pourquoi pas Pydantic ici ?
#   - mARCHCode v1 privilégie les dataclasses légères et une validation
#     explicite (messages d’erreurs clairs). On garde PyYAML côté I/O.
#
# API
#   load_execution_plan(path) -> ExecutionPlan
#   dump_patchblock_yaml(pb, path) -> None
#
# Invariants vérifiés (par PlanLine) :
#   - plan_line_id, file(.py), op∈{create,modify}, role∈ROLES,
#     target_symbol, signature (commence par 'def ' recommandé)
#   - acceptance (list), constraints (dict)
# ------------------------------------------------------------
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.types import PatchBlock


# --- Modèle ExecutionPlan léger (dataclass) ------------------

@dataclass
class ExecutionPlan:
    execution_plan_id: str = "EXEC-UNKNOWN"
    modules: List[Dict[str, Any]] = field(default_factory=list)

    # Champs additionnels (optionnels) vus dans les tiddlers
    bus_message_id: Optional[str] = None
    spec_version_ref: Optional[str] = None
    loop_iteration: Optional[int] = None
    llm: Optional[str] = None


_ALLOWED_OPS = {"create", "modify"}
_ALLOWED_ROLES = {
    "route_handler",
    "service",
    "repo",
    "dto",
    "test",
    "data_accessor",
    "interface",
}


# --- Validation minimale -------------------------------------

def _errors_for_plan(ep: ExecutionPlan) -> List[str]:
    errs: List[str] = []
    if not isinstance(ep.modules, list) or not ep.modules:
        errs.append("`modules` doit être une liste non vide.")

    for mi, mod in enumerate(ep.modules or []):
        if not isinstance(mod, dict):
            errs.append(f"modules[{mi}] doit être un objet/dict.")
            continue

        module_name = mod.get("module")
        if not module_name or not isinstance(module_name, str):
            errs.append(f"modules[{mi}].module est requis (str).")

        plan_lines = mod.get("plan_lines")
        if not isinstance(plan_lines, list) or not plan_lines:
            errs.append(f"modules[{mi}].plan_lines doit être une liste non vide.")
            continue

        for pi, pl in enumerate(plan_lines):
            ctx = f"modules[{mi}].plan_lines[{pi}]"
            if not isinstance(pl, dict):
                errs.append(f"{ctx} doit être un objet/dict.")
                continue

            # Champs requis
            req = ["plan_line_id", "file", "op", "role", "target_symbol", "signature"]
            for k in req:
                if not pl.get(k):
                    errs.append(f"{ctx}.{k} est requis.")

            # Types / valeurs minimales
            fpath = pl.get("file")
            if isinstance(fpath, str) and not fpath.endswith(".py"):
                errs.append(f"{ctx}.file doit cibler un fichier .py")
            op = pl.get("op")
            if op and op not in _ALLOWED_OPS:
                errs.append(f"{ctx}.op doit être 'create' ou 'modify' (reçu: {op})")
            role = pl.get("role")
            if role and role not in _ALLOWED_ROLES:
                errs.append(f"{ctx}.role invalide (reçu: {role})")
            sig = pl.get("signature")
            if isinstance(sig, str) and not sig.strip().startswith("def "):
                errs.append(f"{ctx}.signature devrait commencer par 'def ' (reçu: {sig!r})")

            # acceptance & constraints
            acc = pl.get("acceptance")
            if acc is None or not isinstance(acc, list) or len(acc) == 0:
                errs.append(f"{ctx}.acceptance doit être une liste non vide (2–4 points).")
            cons = pl.get("constraints")
            if cons is None or not isinstance(cons, dict):
                errs.append(f"{ctx}.constraints doit être un mapping (dict).")

    return errs


# --- Chargeur public -----------------------------------------

def load_execution_plan(path: str | Path) -> ExecutionPlan:
    """
    Charge un execution_plan.yaml et renvoie un `ExecutionPlan` typé.
    Lève `ValueError` avec messages clairs si la structure est invalide.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"execution_plan introuvable: {p}")

    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML invalide ({p}) : {e}") from e

    # Dataclass (pas Pydantic) → on passe tel quel les champs connus
    ep = ExecutionPlan(
        execution_plan_id=str(data.get("execution_plan_id", "EXEC-UNKNOWN")),
        modules=list(data.get("modules", []) or []),
        bus_message_id=data.get("bus_message_id"),
        spec_version_ref=data.get("spec_version_ref"),
        loop_iteration=data.get("loop_iteration"),
        llm=data.get("llm"),
    )

    problems = _errors_for_plan(ep)
    if problems:
        msg = ";\n- ".join([""] + problems)
        raise ValueError(f"execution_plan invalide ({p}) :{msg}")

    return ep


# --- Export YAML d’un PatchBlock ------------------------------

def _extract_meta_dict(pb: PatchBlock) -> Dict[str, Any]:
    """
    Tolérant : `pb.meta` peut être une dataclass, un SimpleNamespace ou un objet.
    On récupère uniquement les clés connues/stables.
    """
    keys = [
        "bus_message_id",
        "module",
        "file",
        "role",
        "plan_line_id",
        "status_agent_file_checker",
        "status_agent_module_checker",
        "comment_agent_file_checker",
        "comment_agent_module_checker",
        "timestamp",
        "commit_sha",
    ]
    meta_obj = getattr(pb, "meta", None)
    out: Dict[str, Any] = {}
    for k in keys:
        try:
            val = getattr(meta_obj, k, None)  # type: ignore[attr-defined]
        except Exception:
            val = None
        if val is not None:
            out[k] = val
    return out


def dump_patchblock_yaml(pb: PatchBlock, path: str | Path) -> None:
    """
    Sérialise un `PatchBlock` en YAML lisible :
      code: |   (bloc multilignes si nécessaire)
      meta: { ... }
      global_status / next_action / patch_id / version / warning_level / ...
    """
    # Construction d’un dict sûr (pas d’objets non-sérialisables)
    doc: Dict[str, Any] = {
        "patch_id": getattr(pb, "patch_id", None),
        "code": getattr(pb, "code", ""),
        "meta": _extract_meta_dict(pb),
        "global_status": getattr(pb, "global_status", None),
        "next_action": getattr(pb, "next_action", None),
        "version": getattr(pb, "version", None),
        "warning_level": getattr(pb, "warning_level", None),
        "previous_hash": getattr(pb, "previous_hash", None),
        "source_agent": getattr(pb, "source_agent", None),
        "error_trace": getattr(pb, "error_trace", None),
        "fatal_error": getattr(pb, "fatal_error", None),
    }

    # Historique lisible si présent
    hist = getattr(pb, "history", None)
    if isinstance(hist, list) and hist:
        doc["history"] = list(hist)

    hist_ext = getattr(pb, "history_ext", None)
    if isinstance(hist_ext, list) and hist_ext:
        # on évite les objets non YAML → cast best-effort
        safe_ext: List[Any] = []
        for it in hist_ext:
            safe_ext.append(dict(it) if isinstance(it, dict) else str(it))
        doc["history_ext"] = safe_ext

    # Écriture YAML (PyYAML choisira '|' automatiquement pour les longues chaînes)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_dump(
        doc,
        p.open("w", encoding="utf-8"),
        sort_keys=False,
        allow_unicode=True,
        width=100,           # favorise l'utilisation de blocs pour les longues lignes
        default_flow_style=False,
    )
