# core/context_formatter.py
from __future__ import annotations
"""
Context Formatter — normalise le context_snapshot.yaml en texte compact
======================================================================

Rôle du module
--------------
Fournir une représentation textuelle compacte et lisible du contexte
chargé depuis `context_snapshot.yaml` pour l'injection dans les prompts
des agents (ex: ACW / agent_code_writer).

Usage
-----
from core.context_loader import load_context_snapshot
from core.context_formatter import normalize_context_for_prompt

ctx = load_context_snapshot()
text = normalize_context_for_prompt(ctx)

Contrats
--------
- Output concis, limité en taille (paramétrable) pour tenir dans les prompts.
- Priorise : meta snapshot, erreurs de parsing, fichiers avec bannières, routes.
- Tolérant : accepte un dict vide / manquant.
"""

from typing import Any, Dict, List, Tuple


def _short(s: str, n: int = 120) -> str:
    """Compacte `s` (max 3 lignes) et tronque à `n` caractères avec ellipsis."""
    s = (s or "").replace("\r", " ").replace("\t", " ")
    s = " ".join(s.splitlines()[:3])  # conserve jusqu'à 3 lignes
    return (s[: n - 3] + "...") if len(s) > n else s


def _pick_top(items: List[Any], n: int) -> List[Any]:
    """Retourne les `n` premiers éléments de `items` (sans copie profonde)."""
    return items[:n]


def normalize_context_for_prompt(
    ctx: Dict[str, Any],
    *,
    max_sections: int = 6,
    max_chars: int = 1600,
) -> str:
    """
    Sérialise `ctx` (issu du YAML) en texte compact pour prompt LLM.

    Comportement :
      - Résume snapshot (project, generated_at, python, platform, counts).
      - Liste erreurs de parse (exemples), fichiers avec bannières (exemples).
      - Extrait rapidement les routes détectées (framework, méthode, path).
      - Tronque l'ensemble à `max_chars`.

    Retour:
      Chaîne texte prête à l’injection dans un prompt (peut être vide si `ctx` vide).
    """
    if not ctx:
        return ""

    lines: List[str] = []
    snap = ctx.get("snapshot", {}) or {}

    # Header minimal
    project = snap.get("project") or "-"
    gen = snap.get("generated_at") or "-"
    python_v = snap.get("python") or "-"
    platform_v = snap.get("platform") or "-"
    files_count = snap.get("files_count", 0)
    py_files_count = snap.get("py_files_count", 0)

    lines.append(f"Project: {project}")
    lines.append(f"Generated: {gen}; Python: {python_v}; Platform: {platform_v}")
    lines.append(f"Files: {files_count}; Py files: {py_files_count}")

    # Files analysis
    files = ctx.get("files", []) or []
    # parse errors
    err_files = [f for f in files if f.get("error")]
    if err_files:
        lines.append(f"Parse errors: {len(err_files)} (exemples):")
        for f in _pick_top(err_files, 3):
            rel = f.get("relpath") or f.get("path") or "<unknown>"
            err = _short(str(f.get("error")), 140)
            lines.append(f" - {rel}: {err}")

    # banners
    banner_files = [f for f in files if f.get("banner")]
    if banner_files:
        lines.append(f"Files with banners: {len(banner_files)} (ex.):")
        for f in _pick_top(banner_files, 3):
            rel = f.get("relpath") or f.get("path") or "<unknown>"
            first = _short(str(f.get("banner")).splitlines()[0] if f.get("banner") else "", 140)
            lines.append(f" - {rel}: {first}")

    # routes
    routes: List[Tuple[str, str, Dict[str, Any]]] = []
    for f in files:
        rel = f.get("relpath") or f.get("path") or "<unknown>"
        for d in f.get("defs", []) or []:
            r = d.get("route")
            if isinstance(r, dict):
                routes.append((rel, d.get("qualname") or d.get("name") or "<fn>", r))
    if routes:
        lines.append(f"Detected routes/endpoints: {len(routes)} (ex.):")
        for rel, qual, r in _pick_top(routes, 5):
            method = r.get("method")
            if isinstance(method, list):
                method = ",".join(map(str, method))
            lines.append(f" - {rel}:{qual} -> {r.get('framework') or '-'} {method or '-'} {r.get('path') or ''}")

    # If nothing special, show some top files
    if not any([err_files, banner_files, routes]) and files:
        lines.append("Top files:")
        for f in _pick_top(files, 6):
            lines.append(f" - {f.get('relpath') or f.get('path') or '<unknown>'}")

    # Compact and truncate
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 3].rstrip() + "..."

    return out
