# core/types.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Literal
from datetime import datetime
import uuid

from core.error_policy import ErrorCategory

# ------------------------------------------------------------
# Types canoniques mARCHCode — MetaBlock, PatchBlock, PlanLine
# ------------------------------------------------------------
# Objectif :
#   - Conserver la légèreté des dataclasses (MVP phase 3 locale)
#   - Réintégrer les champs utiles de la V1 (patch_id, timestamp, commit_sha…)
#   - Rester tolérant à l’existant : le pipeline manipule parfois un meta
#     en SimpleNamespace → on ne force pas strictement MetaBlock à l’exécution.
#
# Conventions clés :
#   GlobalStatus ∈ {"ok","pending","rejected","partial_ok"}
#   NextAction   ∈ {"accept","retry","rollback"}
#
# Notes d’intégration :
#   - FileChecker n’écrit PAS global_status / next_action.
#   - ModuleChecker fixe global_status & next_action et commente meta.
#   - history : on garde une liste simple de chaînes (logs lisibles),
#               et history_ext pour des entrées structurées (Dict) si besoin.
# ------------------------------------------------------------

GlobalStatus = Literal["ok", "pending", "rejected", "partial_ok"]
NextAction   = Literal["accept", "retry", "rollback"]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class MetaBlock:
    # Traçabilité principale
    bus_message_id: Optional[str] = None
    module: Optional[str] = None
    file: Optional[str] = None
    role: Optional[str] = None
    plan_line_id: Optional[str] = None

    # Annotations agents
    status_agent_file_checker: Optional[str] = None    # "ok" | "rejected"
    status_agent_module_checker: Optional[str] = None  # "ok" | "rejected"
    comment_agent_file_checker: Optional[str] = None
    comment_agent_module_checker: Optional[str] = None

    # Versioning / SCM / horodatage
    timestamp: Optional[str] = None                    # ISO-8601 (now_iso())
    commit_sha: Optional[str] = None                   # injecté au moment du commit FS/Git

    # Extensions futures (placeholder)
    # spec_version: Optional[str] = None
    # diff_id: Optional[str] = None
    # rollback_id: Optional[str] = None

    def brief(self) -> str:
        f = self.file or "∅"
        m = self.module or "∅"
        pl = self.plan_line_id or "∅"
        return f"file={f}, module={m}, plan_line_id={pl}"


@dataclass
class PatchBlock:
    """
    Enveloppe standard d’un patch de code.

    - code : bloc source complet, déjà entouré de :
        #{begin_meta: ...}
        ...
        #{end_meta}

    - meta : informations de traçabilité + annotations agents
    - global_status / next_action : décision modulaire (ModuleChecker)
    - error_trace : trace courte si rejet FileChecker
    """
    code: str
    meta: MetaBlock | object  # tolérant : SimpleNamespace possible dans le runner

    # Identité et cycle de vie
    patch_id: str = field(default_factory=lambda: f"PATCH-{uuid.uuid4().hex[:8]}")
    version: int = 1
    source_agent: Optional[str] = None

    # Décision globale (module)
    global_status: Optional[GlobalStatus] = None
    next_action: Optional[NextAction] = None

    # Traces & historique
    warning_level: Optional[int] = None
    previous_hash: Optional[str] = None
    error_trace: Optional[str] = None
    fatal_error: Optional[str] = None
    history: List[str] = field(default_factory=list)          # logs lisibles
    history_ext: List[Dict] = field(default_factory=list)     # logs structurés (option)
    error_category: Optional[ErrorCategory] = None

    def append_history(self, line: str) -> None:
        self.history.append(line)

    def append_history_ext(self, entry: Dict) -> None:
        self.history_ext.append(entry)

    def is_accepted(self) -> bool:
        return (self.global_status or "") == "ok" and (self.next_action or "") == "accept"


@dataclass
class PlanLine:
    """
    PlanLine minimale mais exploitable par ACWP/ACW (mARCHCode) :

    Champs indispensables :
      - plan_line_id : identifiant stable et unique (trace)
      - file         : fichier cible (.py)
      - op           : create | modify
      - role         : guide de responsabilité (route_handler, service, repo, dto, test, data_accessor, interface)
      - target_symbol: unité visée (nom de fonction/route/classe)
      - signature    : signature attendue (ex. "def get_user(user_id: int) -> dict")
      - acceptance   : 2–4 assertions simples et testables (liste de chaînes)
      - constraints  : style/typing/règles/outils (mapping libre)

    Champs utiles (optionnels) alignés sur le tiddler :
      - path               : chemin logique/API pour les handlers (ex. "/users/{user_id}")
      - depends_on         : autres PlanLine dont celle-ci dépend (id list)
      - allow_create       : autoriser la création de fichier si absent (par défaut True)
      - markers            : repères d’insertion (ex. {"begin": "# <ARCH:BEGIN>", "end": "# <ARCH:END>"})
      - description        : commentaire humain court
      - plan_line_ref      : alias externe si tu veux conserver un id « affiché »
      - intent_fingerprint : empreinte courte (hash de signature/intent) pour idempotence

    Remarques :
      - Préfère des PlanLines atomiques (une intention = une unité testable).
      - Garde acceptance concis (2–4 points).
      - Place les normes (pep8/typing/isort…) dans constraints.
    """
    plan_line_id: str
    file: str
    op: Literal["create", "modify"]
    role: Literal["route_handler", "service", "repo", "dto", "test", "data_accessor", "interface"]
    target_symbol: str
    signature: str

    # Champs requis du tiddler (acceptance/constraints)
    acceptance: List[str] = field(default_factory=list)
    constraints: Dict[str, object] = field(default_factory=dict)

    # Options recommandées
    allow_create: bool = True
    markers: Optional[Dict[str, str]] = None
    path: Optional[str] = None
    depends_on: List[str] = field(default_factory=list)

    # Métadonnées d’ergonomie
    description: Optional[str] = None
    plan_line_ref: Optional[str] = None
    intent_fingerprint: Optional[str] = None
