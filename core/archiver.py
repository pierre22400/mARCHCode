# core/archiver.py
from __future__ import annotations

"""
Archiver (MVP, sans dépendances) — sorties au format YAML + KV
==============================================================

Pourquoi YAML ?
- Lisible humainement, cohérent avec nos autres artefacts (execution_plan).
- Moins sujet aux erreurs manuelles qu’un JSON verbeux.

Ce module écrit dans: .arch_runs/<run_id>/
  - execution_plan.yaml         (tel quel, passé en texte)
  - patch_before.yaml           (snapshot d’entrée)
  - patch_after.yaml            (après checkers/routeur)
  - patch_post_commit.yaml      (optionnel, après injection commit_sha)
  - decision.yaml               (résumé de la décision)
  - console.log                 (append)
  - .run.kv                     (métadonnées de run en KV simple: key=value)
  - index.yaml                  (tiny index ordonné des artefacts produits)

Notes:
- Aucun parse YAML n’est requis ici; on fournit un mini *émetteur* YAML robuste
  couvrant dict/list/scalaires (+ block scalars pour les strings multi-lignes).
- L’ordre d’apparition dans `index.yaml` suit strictement l’ordre d’appel des
  fonctions d’archivage.
"""

from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Mapping


# ---------- utilitaires basiques ----------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure_dir(root: str | Path) -> Path:
    p = Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _meta_to_dict(meta: Any) -> Dict[str, Any]:
    if meta is None:
        return {}
    if is_dataclass(meta):
        return asdict(meta)
    if isinstance(meta, SimpleNamespace):
        return vars(meta).copy()
    # Duck-typing: récupère attributs publics non callables
    out: Dict[str, Any] = {}
    for k in dir(meta):
        if k.startswith("_"):
            continue
        try:
            v = getattr(meta, k)
        except Exception:
            continue
        if callable(v):
            continue
        out[k] = v
    return out


def patchblock_to_mapping(pb: Any) -> Dict[str, Any]:
    """Transforme un PatchBlock en mapping sérialisable YAML (souple sur le type réel)."""
    meta = _meta_to_dict(getattr(pb, "meta", None))
    return {
        "patch_id": getattr(pb, "patch_id", None),
        "version": getattr(pb, "version", None),
        "source_agent": getattr(pb, "source_agent", None),
        "code": getattr(pb, "code", None),
        "meta": meta,
        "global_status": getattr(pb, "global_status", None),
        "next_action": getattr(pb, "next_action", None),
        "warning_level": getattr(pb, "warning_level", None),
        "previous_hash": getattr(pb, "previous_hash", None),
        "error_trace": getattr(pb, "error_trace", None),
        "fatal_error": getattr(pb, "fatal_error", None),
        "history": list(getattr(pb, "history", []) or []),
        "history_ext": list(getattr(pb, "history_ext", []) or []),
        "_archived_at": _now_iso(),
    }


def decision_to_mapping(decision: Any) -> Dict[str, Any]:
    act = getattr(decision, "action", None)
    return {
        "action": getattr(act, "value", None) if act is not None else None,
        "global_status": getattr(decision, "global_status", None),
        "next_action": getattr(decision, "next_action", None),
        "reasons": list(getattr(decision, "reasons", []) or []),
        "summary": getattr(decision, "summary", None),
        "_archived_at": _now_iso(),
    }


# ---------- mini-émetteur YAML (zéro dépendance) ----------

def _is_simple_scalar(s: str) -> bool:
    # autorise sans guillemets : alnum + quelques ponctuations safe
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9._/+-]+", s))


def _yaml_escape(s: str) -> str:
    # double quotes avec échappement minimal
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _emit_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if "\n" in s:
        # block scalar
        return "|\n" + "\n".join("  " + line for line in s.splitlines())
    if _is_simple_scalar(s):
        return s
    return _yaml_escape(s)


def _yamlify(obj: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(obj, Mapping):
        lines: list[str] = []
        # tri des clés pour stabilité
        for k in sorted(obj.keys(), key=lambda x: str(x)):
            v = obj[k]
            key = str(k)
            if isinstance(v, Mapping):
                lines.append(f"{pad}{key}:")
                lines.append(_yamlify(v, indent + 2))
            elif isinstance(v, (list, tuple)):
                if len(v) == 0:
                    lines.append(f"{pad}{key}: []")
                else:
                    lines.append(f"{pad}{key}:")
                    for item in v:
                        if isinstance(item, (Mapping, list, tuple)):
                            lines.append(f"{pad}  -")
                            lines.append(_yamlify(item, indent + 4))
                        else:
                            lines.append(f"{pad}  - {_emit_scalar(item)}")
            else:
                lines.append(f"{pad}{key}: {_emit_scalar(v)}")
        return "\n".join(lines) if lines else f"{pad}{{}}"

    if isinstance(obj, (list, tuple)):
        if not obj:
            return pad + "[]"
        lines = []
        for item in obj:
            if isinstance(item, (Mapping, list, tuple)):
                lines.append(f"{pad}-")
                lines.append(_yamlify(item, indent + 2))
            else:
                lines.append(f"{pad}- {_emit_scalar(item)}")
        return "\n".join(lines)

    # scalaire
    return pad + _emit_scalar(obj)


def _write_yaml(root: str | Path, name: str, payload: Any) -> Path:
    d = _ensure_dir(root)
    p = d / name
    p.write_text(_yamlify(payload) + "\n", encoding="utf-8")
    _update_index(d, name)
    return p


def _write_text(root: str | Path, name: str, text: str) -> Path:
    d = _ensure_dir(root)
    p = d / name
    p.write_text(text, encoding="utf-8")
    _update_index(d, name)
    return p


def _append_text(root: str | Path, name: str, line: str) -> Path:
    d = _ensure_dir(root)
    p = d / name
    with p.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
    # append ne modifie pas l’ordre des artefacts (déjà indexé à la création)
    if not (d / "index.yaml").exists():
        # si append sur fichier inexistant jusque-là (création implicite), indexe-le
        _update_index(d, name)
    return p


# ---------- index YAML ordonné ----------

def _update_index(run_dir: Path, filename: str) -> None:
    """
    Maintient un index YAML minimal sous la forme:
      items:
        - file: patch_before.yaml
          at:   2025-08-12T12:34:56
    L’ordre reflète la séquence réelle d’écriture.
    """
    idx = run_dir / "index.yaml"
    if not idx.exists():
        idx.write_text("items:\n", encoding="utf-8")
    with idx.open("a", encoding="utf-8") as f:
        f.write(f"- file: {filename}\n")
        f.write(f"  at: {_now_iso()}\n")


# ---------- API publique ----------

def archive_execution_plan(ep_yaml_text: str, *, run_dir: str | Path) -> Path:
    """Sauve execution_plan tel quel (déjà YAML)."""
    return _write_text(run_dir, "execution_plan.yaml", ep_yaml_text)


def archive_patch_before(pb: Any, *, run_dir: str | Path) -> Path:
    return _write_yaml(run_dir, "patch_before.yaml", patchblock_to_mapping(pb))


def archive_patch_after(pb: Any, *, run_dir: str | Path) -> Path:
    return _write_yaml(run_dir, "patch_after.yaml", patchblock_to_mapping(pb))


def archive_patch_post_commit(pb: Any, *, run_dir: str | Path) -> Path:
    """Optionnel, utile si meta.commit_sha a été injecté par l’adaptateur Git."""
    return _write_yaml(run_dir, "patch_post_commit.yaml", patchblock_to_mapping(pb))


def archive_decision(decision: Any, *, run_dir: str | Path) -> Path:
    return _write_yaml(run_dir, "decision.yaml", decision_to_mapping(decision))


def append_console_log(line: str, *, run_dir: str | Path) -> Path:
    return _append_text(run_dir, "console.log", line)


def archive_run_info(run_dir: str | Path, **kv: Any) -> Path:
    """
    Écrit un fichier .run.kv (KV simple, trié) du type:
      run_id=RUN-2025-08-12-123456
      branch=archcode-self/20250812-123456
      repo=.

    Utile pour “résumer” la session, lisible à l’œil nu.
    """
    d = _ensure_dir(run_dir)
    p = d / ".run.kv"
    # écriture complète (remplace), tri sur les clés pour stabilité
    lines = []
    for k in sorted(kv.keys(), key=str):
        v = kv[k]
        lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _update_index(d, ".run.kv")
    return p
