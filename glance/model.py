"""Core data types shared by extractors, the analyzer and the report."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

# Entity kinds, roughly in the order a reviewer cares about them.
KINDS = (
    "migration",   # Alembic revision, SQL migration, Knex migration
    "table",       # SQLAlchemy / SQLModel / Prisma / Drizzle / TypeORM / Mongoose
    "model",       # Pydantic / Zod / settings — request, response and validation shapes
    "type",        # TS interfaces and object type aliases
    "route",       # FastAPI / Express / Fastify / Hono / Nest / Next.js / tRPC
    "task",        # Celery tasks, DBOS workflows/steps/transactions, BullMQ queues/workers
    "agent",       # Pydantic AI agents and prompt constants
    "config",      # module-level UPPER_CASE constants with literal values
    "env",         # environment variables read by the code
    "function",    # functions and methods
    "dependency",  # package.json / pyproject / requirements entries
)


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


@dataclass
class Entity:
    kind: str
    name: str
    file: str
    line: int
    end_line: int
    lang: str
    framework: str = ""
    signature: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    body_hash: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hunk:
    id: str
    file: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    # each line: [type, old_no, new_no, text] where type is " ", "+" or "-"
    lines: list[list[Any]] = field(default_factory=list)

    def touches_new(self, start: int, end: int) -> bool:
        lo, hi = self.new_start, self.new_start + max(self.new_len, 1) - 1
        return not (hi < start or lo > end)

    def touches_old(self, start: int, end: int) -> bool:
        lo, hi = self.old_start, self.old_start + max(self.old_len, 1) - 1
        return not (hi < start or lo > end)

    def added(self) -> list[tuple[int, str]]:
        return [(l[2], l[3]) for l in self.lines if l[0] == "+"]

    def removed(self) -> list[tuple[int, str]]:
        return [(l[1], l[3]) for l in self.lines if l[0] == "-"]


@dataclass
class FileChange:
    path: str
    status: str               # added | removed | modified | renamed
    old_path: str | None
    lang: str
    category: str             # source | test | migration | config | docs | lockfile | generated | other
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    old_text: str | None = None
    new_text: str | None = None
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class EntityChange:
    status: str               # added | removed | modified
    kind: str
    name: str
    file: str
    lang: str
    framework: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    field_changes: list[dict[str, Any]] = field(default_factory=list)
    signature_changed: bool = False
    body_changed: bool = False
    meta_changes: list[dict[str, Any]] = field(default_factory=list)
    hunk_ids: list[str] = field(default_factory=list)
    callers: list[dict[str, Any]] = field(default_factory=list)
    callers_checked: bool = False      # False = never looked up (generic/short name): "no callers" means unknown
    still_defined: bool = False        # a removed name that's still defined elsewhere (a rename/move/twin)
    line: int = 0

    @property
    def id(self) -> str:
        return "e_" + short_hash(f"{self.kind}|{self.file}|{self.name}")


@dataclass
class RiskFlag:
    rule: str
    severity: str             # high | medium | low
    title: str
    detail: str
    file: str = ""
    line: int = 0
    hunk_ids: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return "r_" + short_hash(f"{self.rule}|{self.file}|{self.line}|{self.title}")
