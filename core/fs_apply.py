# core/fs_apply.py
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Tuple

from core.types import PatchBlock  # attendu par mARCHCode
from rich.console import Console

"""
mARCHCode — Adaptateur FS (insertion par marqueurs + idempotence)
=================================================================
Rôle
----
Appliquer un PatchBlock (émis par agent_code_writer) dans un fichier cible
avec une logique simple et sûre :

1) Détection de bloc balisé :
   Le PatchBlock contient toujours un en-tête `#{begin_meta: ...}` et un
   pied `#{end_meta}`. Entre les deux :
     - soit un payload "plein fichier" (aucun marqueur),
     - soit un payload encadré par deux *marqueurs* (lignes libres) qui servent
       d’ancres idempotentes dans le fichier cible.

2) Stratégie d’écriture idempotente :
   - Si des *marqueurs* sont présents dans le PatchBlock :
       • Si le fichier cible contient déjà ces marqueurs :
           - comparer le payload courant (entre marqueurs) au nouveau payload
             (hash SHA-256 court, 12 hexdigits) ;
           - si identique → SKIP (pas d’écriture) ;
           - sinon → REPLACE (remplacer le segment entre marqueurs).
       • Si le fichier ne contient pas encore ces marqueurs :
           - INSERT → on *append* en fin de fichier : begin_meta, marqueur début,
             payload, marqueur fin, end_meta.
   - S’il n’y a PAS de marqueurs :
       • on considère que c’est un *plein fichier* :
           - si le contenu actuel est identique → SKIP ;
           - sinon → REPLACE (écrase la totalité du fichier par le bloc).

3) Journalisation minimale (PatchBlock.history + console optionnelle) :
   - "fs:insert", "fs:replace" ou "fs:skip" avec tailles et hashes utiles.

Notes d’implémentation (MVP aligné ACW)
---------------------------------------
- L’agent_code_writer insère déjà `content_hash` dans la ligne begin_meta et,
  en cas de markers, encadre le payload par les lignes *exactes* des marqueurs.
- Ici, on ne requiert PAS de parser JSON/YAML de la meta inline : pour un MVP
  robuste, on opère au niveau texte (délimiteurs + comparaison de hashes).
- Les comparaisons de contenus se font via un hash court (SHA-256, 12 chars)
  identique à celui calculé côté ACW (sur le *payload utile*, sans balises).

API
---
apply_patchblock_to_file(pb: PatchBlock, console: Optional[Console] = None) -> Tuple[str, int]
    Retourne (action, bytes_written) avec action ∈ {"insert", "replace", "skip"}.
"""

_BEGIN = "#" + "{begin_meta:"
_END = "#{end_meta}"


def _sha256_short(s: str) -> str:
    """Retourne le SHA-256 tronqué à 12 hex chars pour une chaîne donnée (usage: comparaison idempotente)."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _split_block(code: str) -> Tuple[str, Optional[str], str, Optional[str], str]:
    """
    Découpe le code du PatchBlock en 5 segments :
      (begin_meta_line, marker_begin|None, payload, marker_end|None, end_meta_line)

    Hypothèses :
      - La 1ère occurrence de ligne commençant par '#{begin_meta:' ouvre le bloc.
      - La 1ère occurrence de ligne égale à '#{end_meta}' après l'ouverture le ferme.
      - Si des marqueurs existent, ils occupent la 1ère et la dernière ligne *internes* :
          begin_meta
          <marker_begin>
          <payload...>
          <marker_end>
          end_meta
        Sinon, le payload s’étend directement entre begin_meta et end_meta.
    """
    lines = code.splitlines()
    try:
        i_begin = next(i for i, ln in enumerate(lines) if ln.startswith(_BEGIN))
    except StopIteration:
        raise ValueError("Bloc invalide: ligne begin_meta introuvable.")
    try:
        i_end = i_begin + 1 + next(
            i for i, ln in enumerate(lines[i_begin + 1 :]) if ln.strip() == _END
        )
    except StopIteration:
        raise ValueError("Bloc invalide: ligne end_meta introuvable.")

    inner = lines[i_begin + 1 : i_end]  # peut être vide
    marker_begin: Optional[str] = None
    marker_end: Optional[str] = None
    payload_lines = inner[:]

    if len(inner) >= 3:
        # Heuristique markers: on prend la 1ère et la dernière ligne internes comme marqueurs,
        # en supposant qu’elles ne ressemblent pas à du code Python généré (c’est ACW qui les injecte).
        marker_begin = inner[0]
        marker_end = inner[-1]
        payload_lines = inner[1:-1]

    payload = "\n".join(payload_lines).rstrip("\n")
    return lines[i_begin], marker_begin, payload, marker_end, lines[i_end]


def _extract_between_markers(
    text: str, m_begin: str, m_end: str
) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """
    Dans `text`, trouve la 1ère fenêtre délimitée par `m_begin` et `m_end`.
    Retourne (start_index, end_index, payload_entre_marqueurs) ou (None, None, None) si absent.
    Les index englobent uniquement le *payload*, pas les lignes des marqueurs.
    """
    start = text.find(m_begin)
    if start == -1:
        return None, None, None
    # position après la ligne m_begin
    after_begin = start + len(m_begin)
    # Pour travailler par lignes, on reconstruit depuis after_begin :
    tail = text[after_begin:]
    # Cherche m_end dans la suite
    end_rel = tail.find(m_end)
    if end_rel == -1:
        return None, None, None
    # bornes exactes du payload
    payload_start = after_begin + tail.find("\n") + 1 if "\n" in tail[:end_rel] else after_begin
    payload_end = after_begin + end_rel
    payload = text[payload_start:payload_end]
    return payload_start, payload_end, payload


def apply_patchblock_to_file(pb: PatchBlock, console: Optional[Console] = None) -> Tuple[str, int]:
    """
    Applique un PatchBlock au fichier cible en respectant les marqueurs (si présents)
    et en garantissant l’idempotence (skip si contenu identique, sinon replace/insert).

    Effets :
      - Écrit sur disque si nécessaire.
      - Pousse un log succinct dans pb.history.
      - Retourne (action, bytes_written) avec action ∈ {"insert","replace","skip"}.

    Comportements :
      - Avec marqueurs :
          • présents dans le fichier  → compare & remplace ou skip
          • absents dans le fichier   → append du bloc complet (INSERT)
      - Sans marqueurs :
          • compare l’intégralité du fichier courant vs bloc → replace/skip
    """
    file_path = Path(pb.meta.file)
    code = pb.code

    begin_meta_line, marker_begin, payload_new, marker_end, end_meta_line = _split_block(code)
    hash_new = _sha256_short(payload_new)

    # Charge l’état courant du fichier (s’il existe)
    current = file_path.read_text(encoding="utf-8") if file_path.exists() else ""

    # Cas avec marqueurs
    if marker_begin is not None and marker_end is not None:
        # 1) Les marqueurs existent déjà ? → comparer/remplacer
        start_idx, end_idx, payload_current = _extract_between_markers(
            current, marker_begin, marker_end
        )
        if payload_current is not None:
            hash_cur = _sha256_short(payload_current)
            if hash_cur == hash_new:
                msg = f"fs:skip markers payload_hash={hash_new} file={file_path}"
                pb.append_history(msg)
                if console:
                    console.log(msg)
                return "skip", 0

            # replace entre marqueurs (on préserve les marqueurs eux-mêmes)
            before = current[:start_idx]
            after = current[end_idx:]
            # normaliser fins de ligne
            replacement = payload_new
            if not replacement.endswith("\n") and after.startswith("\n"):
                replacement += ""  # éviter double LF
            new_text = before + replacement + after
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(new_text, encoding="utf-8")
            written = len(new_text.encode("utf-8"))
            msg = (
                f"fs:replace markers payload_hash_old={hash_cur} "
                f"payload_hash_new={hash_new} file={file_path}"
            )
            pb.append_history(msg)
            if console:
                console.log(msg)
            return "replace", written

        # 2) Marqueurs absents → INSERT (append du bloc complet tel quel)
        sep = "" if (current.endswith("\n") or current == "") else "\n"
        new_text = current + sep + code + ("\n" if not code.endswith("\n") else "")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(new_text, encoding="utf-8")
        written = len(new_text.encode("utf-8")) - len(current.encode("utf-8"))
        msg = f"fs:insert markers payload_hash={hash_new} file={file_path}"
        pb.append_history(msg)
        if console:
            console.log(msg)
        return "insert", written

    # Cas *plein fichier* (sans marqueurs) : on compare tout le fichier à la *payload utile*
    # On reconstruit le *bloc complet* "plein fichier" tel que pb.code, et compare à current.
    # Idempotence : si identique → skip ; sinon → replace (overwrite).
    if current == code or _sha256_short(current) == _sha256_short(code):
        msg = f"fs:skip fullfile payload_hash={hash_new} file={file_path}"
        pb.append_history(msg)
        if console:
            console.log(msg)
        return "skip", 0

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(code, encoding="utf-8")
    written = len(code.encode("utf-8"))
    msg = f"fs:replace fullfile payload_hash_new={hash_new} file={file_path}"
    pb.append_history(msg)
    if console:
        console.log(msg)
    return "replace", written
