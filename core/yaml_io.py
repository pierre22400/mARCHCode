# core/yaml_io.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from core.types import PatchBlock


"""
# ============================================================
# YAML I/O — ExecutionPlan (dataclass) & PatchBlock (dump YAML)
# ============================================================
# Rôle du module
#   - Charger un `execution_plan.yaml` en dataclass légère `ExecutionPlan`
#   - Valider MINIMALEMENT la structure (modules/plan_lines + champs clés)
#   - Sérialiser lisiblement un `PatchBlock` en YAML (bloc littéral `|` pour le code)
#
# Pourquoi pas Pydantic ici ?
#   - mARCHCode v1 privilégie des dataclasses simples et une validation
#     explicite (messages d’erreurs clairs). PyYAML reste l’I/O de base.
#
# Intégration ARCHCode (mature) — Git/CI (sandbox → master)
#   - Les métadonnées du PatchBlock incluent désormais des champs utiles
#     au nouveau workflow : `commit_sha`, `ci_status`, `sandbox_branch`,
#     `target_branch`, `run_id`. Cela permet de tracer la navette
#     sandbox → PR → main et de piloter les purges côté sandbox.
#
# API
#   load_execution_plan(path: str|Path) -> ExecutionPlan
#     • Lève FileNotFoundError si le fichier manque
#     • Lève ValueError si la structure est invalide (messages agrégés)
#
#   dump_patchblock_yaml(pb: PatchBlock, path: str|Path) -> None
#     • Écrit un YAML lisible : code en bloc `|`, méta filtrées/stables
#     • Historique (history / history_ext) sérialisé prudemment
#
# Invariants vérifiés (par PlanLine) :
#   - plan_line_id, file(.py), op∈{create,modify}, role∈ROLES,
#     target_symbol, signature (recommandé: commence par 'def ')
#   - acceptance (list), constraints (dict)
#
# Changements v0.2 — 2025-08-13
#   - Bannière docstrings déplacée après les imports (exigence projet)
#   - dump YAML : forcer le style bloc `|` pour toute chaîne multi-ligne
#   - meta PatchBlock : tolère dict / SimpleNamespace / objet arbitraire
#   - champs meta étendus : ci_status, sandbox_branch, target_branch, run_id
#   - messages d’erreurs de validation clarifiés
# ============================================================
"""

__all__ = [
    "ExecutionPlan",
    "load_execution_plan",
    "dump_patchblock_yaml",
]


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


# --- YAML Dumper (force style bloc '|' pour multi-lignes) ----

class _LiteralDumper(yaml.SafeDumper):
    """Dumper qui sérialise toute chaîne multi-ligne en bloc littéral `|`."""


def _repr_str_literal_or_plain(dumper: yaml.Dumper, data: str):  # type: ignore[name-defined]
    style = "|" if ("\n" in data) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LiteralDumper.add_representer(str, _repr_str_literal_or_plain)


# --- Export YAML d’un PatchBlock ------------------------------

def _extract_meta_dict(pb: PatchBlock) -> Dict[str, Any]:
    """
    Tolérant : `pb.meta` peut être un dict, une dataclass, un SimpleNamespace ou un objet.
    On récupère uniquement un sous-ensemble de clés stables et utiles au pipeline.
    """
    keys = [
        # Traçabilité bus/spec
        "bus_message_id",
        "module",
        "file",
        "role",
        "plan_line_id",
        # États checkers
        "status_agent_file_checker",
        "status_agent_module_checker",
        "comment_agent_file_checker",
        "comment_agent_module_checker",
        # Git/CI moderne (sandbox → master)
        "timestamp",
        "commit_sha",
        "ci_status",
        "sandbox_branch",
        "target_branch",
        "run_id",
    ]
    meta_obj = getattr(pb, "meta", None)
