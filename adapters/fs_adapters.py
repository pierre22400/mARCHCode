# adapters/fs_adapters.py


from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
import re
import datetime as _dt

from core.types import PatchBlock
from core.orchestrator import (
    OrchestrationAdapters,
    ApplyAndCommit,
    RegenerateWithACW,
    RollbackAndLog,
    Decision,
    Reasoner,
)

# ------------------------------------------------------------
# FSAdapters (File Systeme Adapters) — Application locale des PatchBlocks (MVP)
# ------------------------------------------------------------
# Rôle du fichier :
#   Offrir des adaptateurs concrets pour :
#     1) APPLY    → écrire/mettre à jour un bloc meta dans un fichier cible
#     2) RETRY    → journaliser une demande de régénération (file locale)
#     3) ROLLBACK → retirer le bloc meta du fichier + tracer dans rollback_bundle
#
# Entrées :
#   - PatchBlock (pb.code contient déjà le bloc complet avec #{begin_meta: ...} ... #{end_meta})
#   - pb.meta.file (chemin relatif du fichier cible)
#   - pb.meta.plan_line_id (ancre de remplacement/suppression si présente dans le bloc)
#
# Sorties :
#   - Écritures dans le FS projet :
#       * création/maj du fichier cible
#       * logs sous ./var/
#
# Hypothèses MVP :
#   - Le bloc à insérer/remplacer est parfaitement délimité par
#         '#{begin_meta:'  ...  '#{end_meta}'
#   - Si un bloc existant contenant le même plan_line_id est trouvé,
#     il est remplacé in situ ; sinon il est ajouté en fin de fichier.
#   - Aucune dépendance Git ici (commit simulable plus tard).
#
# Points d’attention :
#   - Pas de modification de pb.global_status / pb.next_action (décidé en amont).
#   - Pas d’interaction LLM ici ; le Reasoner n’est utilisé qu’en RETRY pour log.
# ------------------------------------------------------------

_BEGIN = "#"+"{begin_meta:"
_END   = "#{end_meta}"

# ---------- Helpers bas niveau (FS + recherche de blocs) ----------

def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")

def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""

def _write_text(p: Path, txt: str) -> None:
    _ensure_parent(p)
    p.write_text(txt, encoding="utf-8")

def _append_line(p: Path, line: str) -> None:
    _ensure_parent(p)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

def _find_block_spans(text: str, plan_line_id: Optional[str]) -> List[Tuple[int, int]]:
    """
    Retourne la liste des (start, end) des blocs meta présents dans `text`.
    Si plan_line_id est fourni, on ne retient que les blocs qui contiennent cette valeur.
    """
    spans: List[Tuple[int, int]] = []
    pos = 0
    while True:
        b = text.find(_BEGIN, pos)
        if b == -1:
            break
        e = text.find(_END, b)
        if e == -1:
            break
        e2 = e + len(_END)
        block = text[b:e2]
        if (not plan_line_id) or (plan_line_id in block):
            spans.append((b, e2))
        pos = e2
    return spans

def _upsert_meta_block(file_path: Path, new_block: str, plan_line_id: Optional[str]) -> Tuple[str, bool]:
    """
    Insère ou remplace un bloc meta dans file_path.
      - plan_line_id permet de cibler un bloc existant ; si absent, append.
      - Retourne (nouveau_contenu, replaced_flag).
    """
    src = _read_text(file_path)
    if not src.strip():
        # fichier neuf → écrire le bloc tel quel
        content = new_block.rstrip() + "\n"
        return content, False

    # Si bloc cible existant avec ce plan_line_id → remplacer le premier match
    spans = _find_block_spans(src, plan_line_id)
    if spans:
        start, end = spans[0]
        content = src[:start] + new_block.rstrip() + src[end:]
        # garantir fin de fichier propre
        if not content.endswith("\n"):
            content += "\n"
        return content, True

    # Sinon, append (avec séparation)
    sep = "" if src.endswith("\n\n") else ("\n" if src.endswith("\n") else "\n\n")
    content = src + sep + new_block.rstrip() + "\n"
    return content, False

def _remove_meta_block(file_path: Path, plan_line_id: Optional[str]) -> Tuple[str, bool]:
    """
    Supprime le premier bloc meta portant plan_line_id ; si non trouvé, noop.
    Retourne (nouveau_contenu, removed_flag).
    """
    src = _read_text(file_path)
    if not src:
        return src, False
    spans = _find_block_spans(src, plan_line_id)
    if not spans:
        return src, False
    start, end = spans[0]
    content = (src[:start] + src[end:]).lstrip("\n")
    return content, True

# ------------------------ Adaptateurs concrets ------------------------

@dataclass
class FSAdapters(OrchestrationAdapters):
    """
    Adaptateurs concrets pour écrire/retirer les blocs dans le système de fichiers.
    Les chemins var/logs sont relatifs à la racine du projet courant.
    """

    root: Path = Path(".")
    logs_dir: Path = Path("var")
    rollback_bundle: Path = Path("var/rollback_bundle.yaml")
    regen_queue: Path = Path("var/regeneration_queue.txt")

    def __init__(self) -> None:
        super().__init__(
            apply_and_commit=self.apply_and_commit,      # type: ignore[arg-type]
            regenerate_with_acw=self.regenerate_with_acw,# type: ignore[arg-type]
            rollback_and_log=self.rollback_and_log,      # type: ignore[arg-type]
        )

    # ---- APPLY ----
    def apply_and_commit(self, pb: PatchBlock, decision: Decision) -> None:
        """
        Écrit/Met à jour le bloc meta dans le fichier cible.
        Commit Git non géré ici (MVP) ; à brancher ultérieurement.
        """
        rel = (getattr(pb.meta, "file", None) or "").strip()
        if not rel:
            _append_line(self.logs_dir / "errors.log", f"[{_now_iso()}] APPLY sans meta.file (plan_line_id={getattr(pb.meta,'plan_line_id',None)})")
            return

        target = (self.root / rel).resolve()
        new_body, replaced = _upsert_meta_block(
            target, pb.code, getattr(pb.meta, "plan_line_id", None)
        )
        _write_text(target, new_body)

        # log applicatif minimal
        action = "REPLACED" if replaced else "APPENDED"
        _append_line(self.logs_dir / "apply.log",
                     f"[{_now_iso()}] {action} file={rel} plan_line_id={getattr(pb.meta,'plan_line_id',None)} status={decision.global_status}/{decision.next_action}")

    # ---- RETRY ----
    def regenerate_with_acw(self, pb: PatchBlock, decision: Decision, reasoner: Optional[Reasoner] = None) -> None:
        """
        File de régénération simple : on empile une entrée textuelle.
        (Le branchement réel vers agent_code_writer viendra plus tard.)
        """
        fused = " | ".join(decision.reasons) if decision.reasons else ""
        if reasoner and fused:
            try:
                tags = reasoner(fused)
                fused = " | ".join(tags) or fused
            except Exception:
                pass

        _append_line(self.regen_queue,
                     f"[{_now_iso()}] RETRY file={getattr(pb.meta,'file',None)} plan_line_id={getattr(pb.meta,'plan_line_id',None)} reasons={fused}")

    # ---- ROLLBACK ----
    def rollback_and_log(self, pb: PatchBlock, decision: Decision) -> None:
        """
        Retire le bloc ciblé du fichier si présent et journalise dans rollback_bundle.yaml.
        """
        rel = (getattr(pb.meta, "file", None) or "").strip()
        plan_id = getattr(pb.meta, "plan_line_id", None)
        if not rel:
            _append_line(self.logs_dir / "errors.log", f"[{_now_iso()}] ROLLBACK sans meta.file (plan_line_id={plan_id})")
            return

        target = (self.root / rel).resolve()
        if target.exists():
            new_body, removed = _remove_meta_block(target, plan_id)
            if removed:
                _write_text(target, new_body)

        # Append YAML minimal dans rollback_bundle
        _append_line(self.rollback_bundle,
                     f"- ts: '{_now_iso()}'\n  file: '{rel}'\n  plan_line_id: '{plan_id}'\n  reason: 'router:{decision.global_status}/{decision.next_action}'")

