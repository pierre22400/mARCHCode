# core/self_dev_policy.py


from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol, Tuple
import fnmatch
import json

from core.types import PatchBlock

# ------------------------------------------------------------
# self_dev_policy — charge/valide une policy et évalue un patch
# ------------------------------------------------------------
# Usage rapide :
#   policy = SelfDevPolicy.load_from_yaml_text(open("self_dev_policy.yaml").read())
#   ok, violations = policy.evaluate_patch(pb, diff_stats, branch_name="archcode-self/2025-08-12")
#   if not ok and policy.mode == "enforce": raise RuntimeError("\n".join(violations))
#
# Ce module ne dépend d'aucun parseur YAML externe pour rester MVP.
# On accepte JSON ou YAML "simple" (key: value ; listes - item) via un parseur tolérant.
# ------------------------------------------------------------

class DiffStats(Protocol):
    """Contrat minimal attendu par la policy (à fournir par l’adaptateur Git/FS)."""
    files_changed: int
    loc_added: int
    loc_deleted: int
    patch_size_bytes: int
    paths: List[str]               # chemins des fichiers touchés (relatifs)
    has_binary: bool
    # Optionnel : extensions détectées
    # exts: List[str]

# --- Utils YAML très simple (fallback JSON) ---

def _parse_lenient_yaml(text: str) -> dict:
    """
    Parseur 'pauvre' : tente JSON d'abord, sinon YAML ultra-simple.
    Suffisant pour notre template (clé: valeur, listes - item).
    """
    text = text.strip()
    # 1) tentative JSON stricte
    try:
        return json.loads(text)
    except Exception:
        pass

    # 2) YAML pauvre -> on reconstruit en dict / listes
    data: dict = {}
    stack: list[tuple[int, object]] = [(0, data)]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and indent < stack[-1][0]:
            stack.pop()
        container = stack[-1][1]

        if line.lstrip().startswith("- "):
            # élément de liste
            item = line.lstrip()[2:].strip()
            # convert rudimentaire
            val: object = _coerce_scalar(item)
            if isinstance(container, list):
                container.append(val)
            else:
                # erreur de structure → on ignore pour MVP
                pass
            continue

        if ":" in line:
            k, v = line.lstrip().split(":", 1)
            key = k.strip()
            val_text = v.strip()
            if val_text == "":
                # nouvelle map ou liste
                # heuristique : si la ligne suivante commence par "-" on crée une liste
                # ici, on crée une map par défaut
                new_map: dict = {}
                if isinstance(container, dict):
                    container[key] = new_map
                elif isinstance(container, list):
                    container.append({key: new_map})
                stack.append((indent + 2, new_map))
            else:
                val = _coerce_scalar(val_text)
                if isinstance(container, dict):
                    container[key] = val
                elif isinstance(container, list):
                    container.append({key: val})
    return data

def _coerce_scalar(s: str):
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low.isdigit():
        return int(low)
    try:
        return float(s)
    except Exception:
        return s.strip('"').strip("'")

# --- Modèle de policy ---

@dataclass
class Limits:
    max_files_changed: int = 5
    max_loc_added: int = 160
    max_loc_deleted: int = 80
    max_patch_size_bytes: int = 20000

@dataclass
class Paths:
    forbidden: List[str] = field(default_factory=lambda: ["infra/**", "secrets/**"])
    allowed: List[str] = field(default_factory=list)  # si non vide -> liste blanche stricte

@dataclass
class Markers:
    require_begin_end: bool = True
    begin: str = "#{begin_meta:"
    end: str = "#{end_meta}"

@dataclass
class Binaries:
    allow_binary_changes: bool = False
    forbidden_extensions: List[str] = field(default_factory=lambda: [".png", ".jpg", ".pdf", ".exe", ".dll", ".so"])

@dataclass
class Budgets:
    llm_tokens_max: int = 0
    total_run_timeout_seconds: int = 180
    checker_timeout_seconds: int = 60
    retry_limit: int = 2

@dataclass
class CommitGate:
    require_file_checker_ok: bool = True
    module_status_allow: List[str] = field(default_factory=lambda: ["ok", "partial_ok"])
    max_partial_ok_allowed: int = 2

@dataclass
class SelfDevPolicy:
    policy_id: str = "SDP-0001"
    version: int = 1
    mode: str = "enforce"          # enforce | warn | off
    require_clone: bool = True

    limits: Limits = field(default_factory=Limits)
    paths: Paths = field(default_factory=Paths)
    protected_files: List[str] = field(default_factory=lambda: ["core/types.py", ".github/workflows/**"])
    markers: Markers = field(default_factory=Markers)
    binaries: Binaries = field(default_factory=Binaries)
    budgets: Budgets = field(default_factory=Budgets)
    commit_gate: CommitGate = field(default_factory=CommitGate)
    notes: Optional[str] = None

    # ---------- Chargement ----------
    @classmethod
    def load_from_yaml_text(cls, text: str) -> "SelfDevPolicy":
        raw = _parse_lenient_yaml(text)
        def get(path, default=None):
            cur = raw
            for p in path.split("."):
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return default
            return cur

        policy = cls(
            policy_id=get("policy_id", "SDP-0001"),
            version=int(get("version", 1)),
            mode=str(get("mode", "enforce")).lower(),
            require_clone=bool(get("require_clone", True)),
            notes=get("notes"),
        )
        policy.limits = Limits(
            max_files_changed=int(get("limits.max_files_changed", 5)),
            max_loc_added=int(get("limits.max_loc_added", 160)),
            max_loc_deleted=int(get("limits.max_loc_deleted", 80)),
            max_patch_size_bytes=int(get("limits.max_patch_size_bytes", 20000)),
        )
        policy.paths = Paths(
            forbidden=list(get("paths.forbidden", ["infra/**", "secrets/**"])),
            allowed=list(get("paths.allowed", [])),
        )
        policy.protected_files = list(get("protected_files", policy.protected_files))
        policy.markers = Markers(
            require_begin_end=bool(get("markers.require_begin_end", True)),
            begin=str(get("markers.begin", "#{begin_meta:")),
            end=str(get("markers.end", "#{end_meta}")),
        )
        policy.binaries = Binaries(
            allow_binary_changes=bool(get("binaries.allow_binary_changes", False)),
            forbidden_extensions=list(get("binaries.forbidden_extensions", policy.binaries.forbidden_extensions)),
        )
        policy.budgets = Budgets(
            llm_tokens_max=int(get("budgets.llm_tokens_max", 0)),
            total_run_timeout_seconds=int(get("budgets.total_run_timeout_seconds", 180)),
            checker_timeout_seconds=int(get("budgets.checker_timeout_seconds", 60)),
            retry_limit=int(get("budgets.retry_limit", 2)),
        )
        policy.commit_gate = CommitGate(
            require_file_checker_ok=bool(get("commit_gate.require_file_checker_ok", True)),
            module_status_allow=list(get("commit_gate.module_status_allow", ["ok", "partial_ok"])),
            max_partial_ok_allowed=int(get("commit_gate.max_partial_ok_allowed", 2)),
        )
        return policy

    # ---------- Évaluation patch ----------
    def evaluate_patch(
        self,
        pb: PatchBlock,
        diff: DiffStats,
        *,
        branch_name: Optional[str] = None,
        partial_ok_count_so_far: int = 0,
    ) -> Tuple[bool, List[str]]:
        v: List[str] = []

        # Mode off → toujours ok (mais on calcule quand même les violations pour logs)
        mode = (self.mode or "enforce").lower()

        # 0) clone / branche
        if self.require_clone and branch_name and not branch_name.startswith("archcode-self/"):
            v.append(f"branch '{branch_name}' n'est pas un clone autorisé (archcode-self/* requis)")

        # 1) blast radius
        if diff.files_changed > self.limits.max_files_changed:
            v.append(f"files_changed={diff.files_changed} > {self.limits.max_files_changed}")
        if diff.loc_added > self.limits.max_loc_added:
            v.append(f"loc_added={diff.loc_added} > {self.limits.max_loc_added}")
        if diff.loc_deleted > self.limits.max_loc_deleted:
            v.append(f"loc_deleted={diff.loc_deleted} > {self.limits.max_loc_deleted}")
        if diff.patch_size_bytes > self.limits.max_patch_size_bytes:
            v.append(f"patch_size_bytes={diff.patch_size_bytes} > {self.limits.max_patch_size_bytes}")

        # 2) chemins interdits / protégés / liste blanche
        paths = diff.paths or []
        if self.paths.allowed:
            for p in paths:
                if not any(fnmatch.fnmatch(p, pat) for pat in self.paths.allowed):
                    v.append(f"chemin non autorisé (whitelist) : {p}")
        for p in paths:
            if any(fnmatch.fnmatch(p, pat) for pat in self.paths.forbidden):
                v.append(f"chemin interdit : {p}")
            if any(fnmatch.fnmatch(p, pat) for pat in self.protected_files):
                v.append(f"fichier protégé : {p}")

        # 3) binaires / extensions interdites
        if diff.has_binary and not self.binaries.allow_binary_changes:
            v.append("modification binaire détectée (non autorisée)")
        for p in paths:
            for ext in self.binaries.forbidden_extensions:
                if p.lower().endswith(ext):
                    v.append(f"extension interdite : {p}")

        # 4) marqueurs dans le code généré
        if self.markers.require_begin_end:
            code = pb.code or ""
            if self.markers.begin not in code or self.markers.end not in code:
                v.append("marqueurs #{begin_meta}/#{end_meta} absents du patch")

        # 5) portes d’acceptation minimales (si déjà connues)
        if self.commit_gate.require_file_checker_ok:
            if (pb.meta.status_agent_file_checker or "").lower() != "ok":
                v.append("file_checker != ok")
        status_global = (pb.global_status or "").lower()
        if status_global and status_global not in [s.lower() for s in self.commit_gate.module_status_allow]:
            v.append(f"module_checker.status={status_global} non autorisé par la policy")
        if status_global == "partial_ok" and partial_ok_count_so_far >= self.commit_gate.max_partial_ok_allowed:
            v.append(f"partial_ok quota dépassé ({partial_ok_count_so_far} ≥ {self.commit_gate.max_partial_ok_allowed})")

        ok = len(v) == 0 or mode == "off" or mode == "warn"
        return ok, v

