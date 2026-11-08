"""Deterministic risk rules.

Two layers:
  * line rules look at added/removed lines (secrets, swallowed errors, raw SQL, ...)
  * entity rules look at structural changes (destructive migrations, schema drift,
    breaking API changes, removed auth, task signature changes, ...)

Every flag points at a file, a line and the hunks that justify it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterator

from .git import ChangeSet
from .model import Entity, EntityChange, FileChange, RiskFlag

AUTH_RE = re.compile(r"auth|user|token|guard|jwt|session|permission|require|admin|role|protect|login|current_?user|verify|api_?key|scope", re.I)
SENSITIVE_PATH_RE = re.compile(r"auth|permission|password|token|jwt|session|payment|billing|ledger|transfer|withdraw|deposit|margin|loan|invoice|charge|stripe|plaid|alpaca|wallet|payout|refund|kyc", re.I)
MONEY_WORD = r"(amount|price|balance|interest|fee|principal|margin|loan|payment|cents|equity|collateral|apr|apy|pnl|notional|payout|refund|cost|total|subtotal|tax|rate|leverage|nav)"
BUSINESS_CONST_RE = re.compile(r"LIMIT|MAX|MIN|RATE|FEE|THRESHOLD|LEVERAGE|TIMEOUT|RETR|PCT|PERCENT|RATIO|APR|APY|TTL|EXPIR|WINDOW|QUOTA|CAP|FLOOR|SPREAD|MARGIN", re.I)
PLACEHOLDER_RE = re.compile(r"changeme|example|xxx|dummy|placeholder|your[-_]|test|fake|sample|<|\$\{|\{\{|process\.env|os\.environ|getenv", re.I)
# Style rules (not security rules) are skipped when the pattern is already established in the codebase.
HOUSE_STYLE_RULES = {"money-float", "debug", "swallowed", "suppression", "localhost"}
HOUSE_STYLE_MIN_USES, HOUSE_STYLE_MIN_FILES = 8, 3
# `.catch(() => {})` on these is deliberate fire-and-forget, not a lost error.
FIRE_AND_FORGET_RE = re.compile(r"\b(\w*(alert|notify|notification|email|mail|log|logger|track|capture|analytics|telemetry|metric|report|sentry|posthog|beacon)\w*)\s*[.(]", re.I)
DOC_PATH_RE = re.compile(r"\.(md|mdx|rst|txt|adoc)$|(^|/)(docs?|CHANGELOG)", re.I)
SWALLOW_PROMISE_RE = re.compile(r"\.catch\(\s*\(\)\s*=>\s*(\{\s*\}|null|undefined)\s*\)")
ORM_FRAMEWORKS = {"sqlalchemy", "sqlmodel", "prisma", "drizzle", "typeorm", "mongoose"}


@dataclass
class Context:
    cs: ChangeSet
    changes: list[EntityChange]
    new_entities: dict[str, list[Entity]]
    old_entities: dict[str, list[Entity]]
    tracked_files: list[str]
    parse_failures: list[tuple[FileChange, str]] = field(default_factory=list)
    suppressed: dict[str, dict] = field(default_factory=dict)        # rule -> {title, existing, files, would_flag}
    _style: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)

    def file(self, path: str) -> FileChange | None:
        return next((f for f in self.cs.files if f.path == path), None)

    def existing_uses(self, rule: str, pat: re.Pattern, lang: str) -> tuple[int, int]:
        """How often `pat` already appears in the code as it was BEFORE this change (unchanged files from disk,
        changed files from their old text): (matching lines, files). Cached per rule+language."""
        key = (rule, lang)
        if key in self._style:
            return self._style[key]
        changed = {f.path: f for f in self.cs.files}
        exts = {"typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"), "python": (".py",)}.get(lang, ())
        hits = files = budget = 0
        for path in self.tracked_files:
            if not path.endswith(exts) or re.search(r"(^|/)(node_modules|dist|build|vendor|generated|__generated__)/", path):
                continue
            if re.search(r"(^|/)(__tests__|tests?|__mocks__)/|\.(test|spec)\.", path):
                continue                                    # house style = what production code does
            fc = changed.get(path)
            try:
                text = fc.old_text if fc is not None else (self.cs.repo / path).read_text(errors="replace")
            except OSError:
                continue
            if not text or len(text) > 400_000:
                continue
            budget += len(text)
            n = sum(1 for line in text.splitlines() if pat.search(line) and not is_comment(lang, line))
            if n:
                hits, files = hits + n, files + 1
            if budget > 40_000_000:
                break
        self._style[key] = (hits, files)
        return hits, files

    def house_style(self, rule: str, title: str, pat: re.Pattern, lang: str) -> bool:
        """A style rule says nothing when the codebase already does this all over: that's convention, not risk."""
        hits, files = self.existing_uses(rule, pat, lang)
        if hits >= HOUSE_STYLE_MIN_USES and files >= HOUSE_STYLE_MIN_FILES:
            s = self.suppressed.setdefault(rule, {"rule": rule, "title": title, "existing": hits, "files": files, "would_flag": 0})
            s["would_flag"] += 1
            return True
        return False


def _flag(rule: str, sev: str, title: str, detail: str, fc_path: str = "", line: int = 0, hunks: list[str] | None = None) -> RiskFlag:
    return RiskFlag(rule=rule, severity=sev, title=title, detail=detail, file=fc_path, line=line, hunk_ids=hunks or [])


def added_lines(fc: FileChange) -> Iterator[tuple[int, str, str]]:
    for h in fc.hunks:
        for l in h.lines:
            if l[0] == "+":
                yield l[2], l[3], h.id


def removed_lines(fc: FileChange) -> Iterator[tuple[int, str, str]]:
    for h in fc.hunks:
        for l in h.lines:
            if l[0] == "-":
                yield l[1], l[3], h.id


def is_comment(lang: str, text: str) -> bool:
    s = text.strip()
    return s.startswith("#") if lang == "python" else s.startswith(("//", "/*", "*"))


# ============================================================== line rules

LINE_RULES: list[tuple[str, str, str, re.Pattern, set[str] | None, set[str], str]] = [
    # rule, severity, title, pattern, langs (None = any code), categories, detail
    ("secret", "high", "Possible hard-coded secret",
     re.compile(r"(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|sk_live_[0-9a-zA-Z]{10,}|rk_live_[0-9a-zA-Z]{10,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:(password|passwd|secret|api_?key|access_?token|auth_?token|client_?secret)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]))"),
     None, {"source", "config", "other", "migration", "test"}, "Credentials in code end up in git history. Move it to an env var or secret manager and rotate it."),
    ("raw-sql", "high", "SQL built with string interpolation",
     re.compile(r"(execute|executemany|exec_driver_sql|text|raw|query|\$queryRawUnsafe|\$executeRawUnsafe|unsafe|sql\.raw)\s*\(\s*(f['\"]|['\"][^'\"]*['\"]\s*(%|\.format\(|\+)|`[^`]*\$\{)", re.I),
     {"python", "typescript"}, {"source", "migration"}, "Interpolating values into SQL is an injection risk. Use bound parameters."),
    ("unsafe-raw", "high", "Unsafe raw query API",
     re.compile(r"\$(queryRawUnsafe|executeRawUnsafe)\(|sql\.raw\("), {"typescript"}, {"source"},
     "These APIs skip parameterization. Prefer $queryRaw`...` tagged templates."),
    ("tls-cors", "high", "Security check disabled",
     re.compile(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED|allow_origins\s*=\s*\[\s*['\"]\*['\"]|origin\s*:\s*['\"]\*['\"]|check_hostname\s*=\s*False|CERT_NONE"),
     None, {"source", "config"}, "Disables TLS verification or opens CORS to every origin."),
    ("bypass", "high", "Looks like an auth/validation bypass",
     re.compile(r"(?i)\b(skip|bypass|disable)[_ -]?(auth|authentication|authorization|verification|validation|csrf|permission)s?\b"),
     None, {"source", "config"}, "Confirm this can't be enabled in production."),
    ("eval", "high", "Dynamic code execution",
     re.compile(r"(^|[^\w.])(eval|exec)\s*\(|new Function\s*\(|shell\s*=\s*True|child_process|execSync\("),
     {"python", "typescript"}, {"source"}, "Executing strings or shells with input is an injection risk."),
    ("bulk-delete", "high", "Unscoped delete",
     re.compile(r"deleteMany\(\s*(\{\s*\})?\s*\)|\.query\([^)]*\)\.delete\(\)|DELETE\s+FROM\s+\w+\s*;?\s*['\"`]?\s*\)?\s*$|TRUNCATE\s", re.I),
     {"python", "typescript", "sql"}, {"source", "migration"}, "Deletes every row unless something upstream scopes it."),
    ("bare-except", "medium", "Bare except",
     re.compile(r"^\s*except\s*:\s*(#.*)?$"), {"python"}, {"source"}, "Catches everything, including KeyboardInterrupt and bugs. Catch specific exceptions."),
    ("money-float", "medium", "Money handled as a float",
     re.compile(rf"(?i)({MONEY_WORD}\w*\s*:\s*(Mapped\[)?float\b|{MONEY_WORD}\w*['\"]?,\s*sa\.Float\(|float\([^)]*{MONEY_WORD}|{MONEY_WORD}\w*\s*=\s*float\(|parseFloat\([^)]*{MONEY_WORD}|Number\([^)]*{MONEY_WORD}|{MONEY_WORD}\w*\s*=\s*(parseFloat|Number)\(|{MONEY_WORD}\w*\)?\.toFixed\()"),
     {"python", "typescript"}, {"source", "migration"}, "Binary floats drift on currency math. Use Decimal (Python) or integer cents / a decimal library (TS)."),
    ("skipped-test", "medium", "Test skipped or focused",
     re.compile(r"@pytest\.mark\.(skip|xfail)|\b(it|test|describe)\.(skip|only|todo)\(|\bx(it|describe|test)\(|\bf(it|describe)\("),
     {"python", "typescript"}, {"test"}, "Skipped tests hide regressions; .only() silently disables the rest of the suite."),
    ("suppression", "low", "Type/lint check suppressed",
     re.compile(r"#\s*type:\s*ignore|#\s*noqa|#\s*pragma:\s*no cover|@ts-ignore|@ts-expect-error|@ts-nocheck|eslint-disable|\bas any\b|:\s*any\b|cast\(\s*Any"),
     {"python", "typescript"}, {"source"}, "Often how AI code gets past the type checker without fixing the issue."),
    ("debug", "low", "Debug leftover",
     re.compile(r"^\s*(print\(|console\.(log|debug|dir)\(|debugger;?|breakpoint\(\)|import pdb|pdb\.set_trace)"),
     {"python", "typescript"}, {"source"}, "Remove or switch to the project logger."),
    ("todo", "low", "TODO / FIXME added",
     re.compile(r"(#|//|/\*|\*)\s*(TODO|FIXME|XXX|HACK)\b"), None, {"source", "migration", "config"}, "Unfinished work left in the change."),
    ("localhost", "low", "Hard-coded localhost URL",
     re.compile(r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)"), {"python", "typescript"}, {"source"}, "Should probably come from config."),
]

SKIP_DEBUG_FILES = re.compile(r"(^|/)(cli|__main__|manage|scripts?)(/|\.py$|\.ts$)|(^|/)bin/")


def line_rules(ctx: Context) -> Iterator[RiskFlag]:
    for fc in ctx.cs.files:
        if fc.binary or fc.category in ("lockfile", "generated", "docs"):
            continue
        counts: dict[str, int] = defaultdict(int)
        for ln, text, hid in added_lines(fc):
            for rule, sev, title, pat, langs, cats, detail in LINE_RULES:
                if langs is not None and fc.lang not in langs:
                    continue
                if fc.category not in cats:
                    continue
                if rule == "debug" and SKIP_DEBUG_FILES.search(fc.path):
                    continue
                if rule not in ("todo", "suppression", "secret") and is_comment(fc.lang, text):
                    continue
                if not pat.search(text):
                    continue
                if rule == "secret" and PLACEHOLDER_RE.search(text):
                    continue
                if rule == "money-float" and re.search(r"(?i)\brate[_-]?limit", text):
                    continue
                if rule in HOUSE_STYLE_RULES and ctx.house_style(rule, title, pat, fc.lang):
                    continue
                counts[rule] += 1
                if counts[rule] <= 3:
                    yield _flag(rule, sev, title, f"{detail}\n`{text.strip()[:160]}`", fc.path, ln, [hid])
        for rule, n in counts.items():
            if n > 3:
                meta = next(r for r in LINE_RULES if r[0] == rule)
                yield _flag(rule, meta[1], f"{meta[2]} (+{n - 3} more)", f"{n} occurrences in this file.", fc.path, 0, [])
        yield from swallowed_exceptions(fc, ctx)


def _statement_start(lines: list[str], i: int) -> str:
    """The few lines leading up to line i, so a `}).catch(() => {})` can be traced back to its call."""
    return " ".join(l.strip() for l in lines[max(0, i - 8):i + 1])


def _caught_callee(text: str) -> str | None:
    """For `...foo.bar(args).catch(() => {})`, return `foo.bar`: walk back from `.catch` over balanced parens."""
    end = text.rfind(".catch(")
    if end <= 0 or text[end - 1] != ")":
        return None
    depth, j = 0, end - 1
    while j >= 0:
        depth += {")": 1, "(": -1}.get(text[j], 0)
        if depth == 0:
            break
        j -= 1
    m = re.search(r"([\w$.]+)\s*$", text[:j]) if j > 0 else None
    return m.group(1) if m else None


def swallowed_exceptions(fc: FileChange, ctx: "Context | None" = None) -> Iterator[RiskFlag]:
    if fc.lang not in ("python", "typescript") or fc.category != "source" or not fc.new_text:
        return
    lines = fc.new_text.splitlines()
    added = {ln: hid for ln, _, hid in added_lines(fc)}
    for ln, hid in added.items():
        i = ln - 1
        if i >= len(lines):
            continue
        cur = lines[i]
        nxt = [l.strip() for l in lines[i + 1:i + 4] if l.strip()]
        if fc.lang == "python" and re.match(r"^\s*except\b.*:\s*$", cur):
            if nxt and re.match(r"^(pass|\.\.\.|continue|return( None)?)$", nxt[0]):
                yield _flag("swallowed", "medium", "Exception swallowed", f"`{cur.strip()}` followed by `{nxt[0]}` hides failures.\nLog it, re-raise, or handle it explicitly.", fc.path, ln, [hid])
            elif nxt and re.match(r"^(logger|logging|log)\.\w+\(", nxt[0]) and len(nxt) > 1 and re.match(r"^(pass|continue|return( None)?)$", nxt[1]):
                yield _flag("swallowed", "low", "Exception logged and ignored", f"`{cur.strip()}` logs then continues. Make sure callers can tell it failed.", fc.path, ln, [hid])
        elif fc.lang == "typescript":
            m = re.search(r"catch\s*(\([^)]*\))?\s*\{\s*(\}|$)", cur)
            if m:
                empty = m.group(2) == "}" or (nxt and nxt[0].startswith("}"))
                only_log = nxt and re.match(r"^console\.\w+\(", nxt[0]) and len(nxt) > 1 and nxt[1].startswith("}")
                if empty:
                    yield _flag("swallowed", "medium", "Empty catch block", "Errors disappear silently here.", fc.path, ln, [hid])
                elif only_log:
                    yield _flag("swallowed", "low", "Catch only logs", "Error is logged and execution continues as if it succeeded.", fc.path, ln, [hid])
            if SWALLOW_PROMISE_RE.search(cur):
                callee = _caught_callee(_statement_start(lines, i))
                if callee and FIRE_AND_FORGET_RE.search(callee + "("):
                    continue                     # e.g. EmailClient.sendAlert(...).catch(() => {}): best-effort by design
                if ctx is not None and ctx.house_style("swallowed", "Promise rejection swallowed", SWALLOW_PROMISE_RE, fc.lang):
                    continue
                yield _flag("swallowed", "medium", "Promise rejection swallowed", "`.catch(() => {})` hides failures.", fc.path, ln, [hid])


# ============================================================== entity rules

def migration_rules(ctx: Context) -> Iterator[RiskFlag]:
    for c in ctx.changes:
        if c.kind != "migration" or c.status == "removed":
            continue
        meta = (c.after or {}).get("meta", {})
        for op in meta.get("upgrade", []):
            o, t, col, det = op["op"], op.get("table"), op.get("column"), op.get("detail", "")
            where = f"{t}.{col}" if col else (t or "")
            ln = op.get("line", c.line)
            if o in ("drop_table", "drop_column", "truncate", "delete_rows"):
                yield _flag("destructive-migration", "high", f"Destructive migration: {o.replace('_', ' ')} {where}",
                            "Data is deleted when this runs. Confirm it's intended and backed up, and that no code still reads it.",
                            c.file, ln, c.hunk_ids)
            elif o == "add_column":
                not_null = re.search(r"nullable\s*=\s*False|NOT\s+NULL|\.notNullable\(\)|\.notNull\(\)", det, re.I)
                has_default = re.search(r"server_default|DEFAULT|defaultTo|\.default\(", det, re.I)
                if not_null and not has_default:
                    yield _flag("not-null-no-default", "high", f"NOT NULL column without default: {where}",
                                "Fails on any table that already has rows. Add a server default or backfill in steps.", c.file, ln, c.hunk_ids)
            elif o == "alter_column" and re.search(r"type_|TYPE\s|SET DATA TYPE|existing_type", det, re.I):
                yield _flag("column-type-change", "medium", f"Column type change: {where}",
                            "Can rewrite/lock the table and lose precision. Check the cast on existing data.", c.file, ln, c.hunk_ids)
            elif o == "rename_column" or o == "rename_table":
                yield _flag("rename-migration", "medium", f"Rename: {where} {det}",
                            "Code deployed before this migration (or rolled back after) will reference the old name.", c.file, ln, c.hunk_ids)
            elif o in ("update_rows", "execute") and re.search(r"\bUPDATE\b", det, re.I):
                yield _flag("data-migration", "medium", f"Data rewrite in migration{(': ' + t) if t else ''}",
                            "Bulk UPDATE in a migration. Check it's idempotent and safe on large tables.", c.file, ln, c.hunk_ids)
        if meta.get("downgrade_empty") and not meta.get("forward_only"):
            yield _flag("irreversible-migration", "medium", f"Migration can't be rolled back: {c.name}",
                        "downgrade() is empty or raises.", c.file, c.line, c.hunk_ids)
    # alembic: two revisions with the same parent in this diff = multiple heads
    parents = defaultdict(list)
    for c in ctx.changes:
        if c.kind == "migration" and c.framework == "alembic" and c.status == "added":
            parents[(c.after or {}).get("meta", {}).get("down_revision")].append(c)
    for parent, kids in parents.items():
        if len(kids) > 1:
            yield _flag("alembic-heads", "high", "Alembic branch: two revisions share a parent",
                        f"{', '.join(k.name for k in kids)} both revise {parent}. `alembic upgrade head` will fail.", kids[0].file, 1, kids[0].hunk_ids)


def schema_drift(ctx: Context) -> Iterator[RiskFlag]:
    has_migrations_dir = any(re.search(r"(alembic/versions|/migrations/|^migrations/|prisma/migrations|drizzle/)", p) for p in ctx.tracked_files) \
        or any(f.category == "migration" for f in ctx.cs.files)
    table_changes = [c for c in ctx.changes if c.kind == "table" and c.framework in ORM_FRAMEWORKS
                     and (c.status in ("added", "removed") or c.field_changes
                          or any(m["key"] in ("tablename", "table_args") for m in c.meta_changes))]
    migs = [c for c in ctx.changes if c.kind == "migration" and c.status != "removed"]
    if table_changes and not migs and has_migrations_dir:
        mongo = all(c.framework == "mongoose" for c in table_changes)
        if not mongo:
            names = ", ".join(sorted({c.name for c in table_changes})[:5])
            yield _flag("schema-no-migration", "high", "Models changed but no migration",
                        f"Table definitions changed ({names}) and this diff has no migration. The database won't match the code.",
                        table_changes[0].file, table_changes[0].line, table_changes[0].hunk_ids)
    # migration touches a table whose model didn't change
    model_tables = set()
    for c in ctx.changes:
        if c.kind == "table":
            m = ((c.after or c.before or {}).get("meta") or {})
            model_tables.add((m.get("tablename") or c.name).lower())
            model_tables.add(c.name.lower())
    repo_has_models = any(c.kind == "table" for c in ctx.changes) or any(
        re.search(r"(models?|entities|schema)(/|\.py$|\.ts$|\.prisma$)", p) for p in ctx.tracked_files)
    for m in migs if repo_has_models else []:
        for op in (m.after or {}).get("meta", {}).get("upgrade", []):
            t = (op.get("table") or "").split(".")[-1].lower()
            if op["op"] in ("add_column", "drop_column", "alter_column", "rename_column") and t and t not in model_tables \
                    and t.rstrip("s") not in model_tables and t + "s" not in model_tables:
                yield _flag("migration-no-model", "medium", f"Migration changes {t} but its model didn't change",
                            f"`{op['op']}` on {t}.{op.get('column') or ''}. If the ORM model lives in this repo it's now out of sync.",
                            m.file, op.get("line", m.line), m.hunk_ids)
                break


def api_rules(ctx: Context) -> Iterator[RiskFlag]:
    file_has_router_auth: dict[str, bool] = {}
    for fc in ctx.cs.files:
        t = fc.new_text or ""
        file_has_router_auth[fc.path] = bool(re.search(
            r"APIRouter\([^)]*dependencies\s*=\s*\[[^\]]*(auth|user|token|guard|verify)"
            r"|\.use\(\s*\w*(auth|guard|jwt|session|require|protect)", t, re.I))
    for c in ctx.changes:
        if c.kind != "route":
            continue
        bm = (c.before or {}).get("meta", {})
        am = (c.after or {}).get("meta", {})
        if c.status == "removed":
            yield _flag("route-removed", "high", f"Endpoint removed: {c.name}", "Breaking change for any client still calling it.", c.file, c.line, c.hunk_ids)
            continue
        if c.status == "modified":
            if bm.get("path") != am.get("path") or bm.get("methods") != am.get("methods"):
                yield _flag("route-changed", "high", f"Endpoint path/method changed: {c.name}", "Existing clients will 404/405.", c.file, c.line, c.hunk_ids)
            had = [d for d in bm.get("dependencies", []) if AUTH_RE.search(d)]
            has = [d for d in am.get("dependencies", []) if AUTH_RE.search(d)]
            if had and not has:
                yield _flag("auth-removed", "high", f"Auth removed from {c.name}",
                            f"Before: {', '.join(had)}. Now no auth dependency/guard.", c.file, c.line, c.hunk_ids)
        if c.status == "added":
            deps = am.get("dependencies", [])
            siblings = [e for e in ctx.new_entities.get(c.file, []) if e.kind == "route" and e.name != c.name]
            sib_auth = [e for e in siblings if any(AUTH_RE.search(d) for d in e.meta.get("dependencies", []))]
            if not any(AUTH_RE.search(d) for d in deps) and sib_auth and not file_has_router_auth.get(c.file):
                yield _flag("route-no-auth", "medium", f"New endpoint without auth: {c.name}",
                            f"Other routes in this file use {sib_auth[0].meta.get('dependencies', [''])[0]}; this one has none.",
                            c.file, c.line, c.hunk_ids)


def model_rules(ctx: Context) -> Iterator[RiskFlag]:
    for c in ctx.changes:
        if c.kind == "model" and c.framework in ("pydantic", "zod") and c.status in ("modified", "moved"):
            removed = [f["name"] for f in c.field_changes if f["change"] == "removed"]
            retyped = [f["name"] for f in c.field_changes if f["change"] == "changed"]
            is_response = bool(re.search(r"(Out|Response|Resp|Read|Public|View|Result|Dto)$", c.name))
            required_new = [] if is_response else [
                f["name"] for f in c.field_changes if f["change"] == "added" and _required(c.framework, f["after"] or "")]
            if removed:
                yield _flag("field-removed", "medium", f"{c.name}: field{'s' if len(removed) > 1 else ''} removed ({', '.join(removed)})",
                            "If this is an API request/response shape, clients relying on it will break.", c.file, c.line, c.hunk_ids)
            if retyped:
                yield _flag("field-retyped", "low", f"{c.name}: field definition changed ({', '.join(retyped[:4])})",
                            "Check validation and serialization still match what clients send/expect.", c.file, c.line, c.hunk_ids)
            if required_new:
                yield _flag("required-field", "medium", f"{c.name}: new required field{'s' if len(required_new) > 1 else ''} ({', '.join(required_new)})",
                            "Existing callers that don't send it will now fail validation.", c.file, c.line, c.hunk_ids)
        if c.kind == "type" and c.framework in ("enum", "ts-enum", "prisma-enum", "ts-union") and c.status == "modified":
            removed = [f["name"] for f in c.field_changes if f["change"] == "removed"]
            if removed:
                yield _flag("enum-value-removed", "medium", f"{c.name}: value removed ({', '.join(removed)})",
                            "Rows or messages that still hold this value will fail to load.", c.file, c.line, c.hunk_ids)


def _required(framework: str, desc: str) -> bool:
    if framework == "pydantic":
        return "=" not in desc and "Optional" not in desc and "| None" not in desc
    if framework == "zod":
        return not re.search(r"\.(optional|nullish|default|catch)\(", desc)
    return False


def task_rules(ctx: Context) -> Iterator[RiskFlag]:
    fn_by = {(c.file, c.name): c for c in ctx.changes if c.kind == "function"}
    for c in ctx.changes:
        if c.kind != "task" or c.status != "modified":
            continue
        fcn = fn_by.get((c.file, c.name))
        params_changed = any(f["change"] in ("added", "removed", "renamed") for f in c.field_changes)
        if c.framework == "celery" and (params_changed or (fcn and fcn.signature_changed)):
            yield _flag("task-signature", "high", f"Celery task signature changed: {c.name}",
                        "Messages already queued with the old arguments will fail when workers pick them up. Deploy workers and producers carefully or add defaults.",
                        c.file, c.line, c.hunk_ids)
        if c.framework == "dbos":
            deco = ((c.after or {}).get("meta") or {}).get("decorator")
            if params_changed:
                yield _flag("workflow-signature", "high", f"DBOS {deco} signature changed: {c.name}",
                            "In-flight workflows recover with their recorded arguments. Changing the signature can break recovery.",
                            c.file, c.line, c.hunk_ids)
            if deco == "workflow" and fcn and fcn.body_changed:
                yield _flag("workflow-body", "medium", f"DBOS workflow logic changed: {c.name}",
                            "DBOS replays workflows step by step during recovery. Reordering/adding/removing steps can break in-flight workflows; consider versioning.",
                            c.file, c.line, c.hunk_ids)
    # nondeterminism inside DBOS workflows
    for path, ents in ctx.new_entities.items():
        fc = ctx.file(path)
        if fc is None:
            continue
        for e in ents:
            if e.kind == "task" and e.framework == "dbos" and e.meta.get("decorator") == "workflow":
                for ln, text, hid in added_lines(fc):
                    if e.line <= ln <= e.end_line and re.search(r"Date\.now\(|new Date\(|Math\.random\(|crypto\.randomUUID\(|\bfetch\(|axios\.|setTimeout\(", text):
                        yield _flag("workflow-nondeterminism", "high", f"Non-deterministic call inside DBOS workflow {e.name}",
                                    f"`{text.strip()[:120]}`\nWorkflows must be deterministic; move this into a @DBOS.step().", path, ln, [hid])


def function_rules(ctx: Context) -> Iterator[RiskFlag]:
    changed_paths = {f.path for f in ctx.cs.files}
    for c in ctx.changes:
        if c.kind != "function":
            continue
        outside = [x for x in c.callers if not x["in_diff"]]
        code_refs = [x for x in c.callers if not DOC_PATH_RE.search(x["file"])]
        if c.status == "removed" and code_refs and not c.still_defined:
            yield _flag("removed-still-used", "high", f"Removed {c.name}() is still referenced",
                        f"{len(code_refs)} reference(s), e.g. {code_refs[0]['file']}:{code_refs[0]['line']}", c.file, c.line, c.hunk_ids)
        elif c.status in ("modified", "moved") and c.signature_changed and outside:
            yield _flag("signature-blast-radius", "medium", f"{c.name}() signature changed; {len(outside)} call site(s) not in this diff",
                        f"e.g. {outside[0]['file']}:{outside[0]['line']}. Check they still pass the right arguments.", c.file, c.line, c.hunk_ids)
        elif c.status == "added":
            a = c.after or {}
            short = c.name.split(".")[-1]
            lines = (a.get("meta") or {}).get("lines", 0)
            if lines > 80:
                yield _flag("long-function", "low", f"New {lines}-line function: {c.name}", "Long AI-written functions hide logic. Consider splitting.", c.file, c.line, c.hunk_ids)
            if c.callers_checked and not c.callers and not short.startswith(("_", "test", "Test")) and short not in ("main", "handler", "default") \
                    and not short[:1].isupper() and not _is_framework_hook(ctx, c) and ctx.file(c.file) and ctx.file(c.file).category == "source":
                yield _flag("unused", "low", f"New function {c.name}() isn't referenced anywhere",
                            "Dead code or a missing call site.", c.file, c.line, c.hunk_ids)


def _is_framework_hook(ctx: Context, c: EntityChange) -> bool:
    for e in ctx.new_entities.get(c.file, []):
        if e.kind in ("route", "task") and (e.meta.get("handler") == c.name or e.name == c.name):
            return True
        if e.kind == "agent" and any(c.name in t for t in e.meta.get("tools", [])):
            return True
    a = c.after or {}
    return (a.get("meta") or {}).get("exported") and "/" in c.file and bool(re.search(r"(index|route|page|layout|middleware)\.[jt]sx?$", c.file))


def test_rules(ctx: Context) -> Iterator[RiskFlag]:
    tests = [f for f in ctx.cs.files if f.category == "test"]
    for f in tests:
        if f.status == "removed":
            yield _flag("test-deleted", "high", f"Test file deleted: {f.path}", "Coverage removed. Make sure this was intended.", f.path, 0, [])
            continue
        rm_asserts = sum(1 for _, t, _ in removed_lines(f) if re.search(r"\bassert\b|expect\(|\.toBe|\.assert", t))
        add_asserts = sum(1 for _, t, _ in added_lines(f) if re.search(r"\bassert\b|expect\(|\.toBe|\.assert", t))
        rm_tests = sum(1 for _, t, _ in removed_lines(f) if re.search(r"^\s*(async\s+)?def test_|^\s*(it|test)\(", t))
        add_tests = sum(1 for _, t, _ in added_lines(f) if re.search(r"^\s*(async\s+)?def test_|^\s*(it|test)\(", t))
        if rm_tests > add_tests:
            yield _flag("tests-removed", "medium", f"{rm_tests - add_tests} test case(s) removed in {PurePosixPath(f.path).name}",
                        "Net loss of test cases.", f.path, 0, [h.id for h in f.hunks][:3])
        elif rm_asserts > add_asserts + 1:
            yield _flag("asserts-weakened", "medium", f"Assertions removed in {PurePosixPath(f.path).name}",
                        f"{rm_asserts} assertion lines removed, {add_asserts} added. Check the tests still check the behavior.",
                        f.path, 0, [h.id for h in f.hunks][:3])
    logic = [c for c in ctx.changes if c.kind in ("function", "route", "task") and (c.body_changed or c.status == "added")
             and (ctx.file(c.file) and ctx.file(c.file).category == "source")]
    if logic and not tests:
        yield _flag("no-tests", "medium", "Logic changed with no test changes",
                    f"{len(logic)} function/endpoint change(s) and no test files touched.", "", 0, [])


def config_rules(ctx: Context) -> Iterator[RiskFlag]:
    seen_env: set[str] = set()
    for c in ctx.changes:
        if c.kind == "config" and c.status == "modified" and c.framework == "constant" and BUSINESS_CONST_RE.search(c.name):
            b = (c.before or {}).get("signature", "")
            a = (c.after or {}).get("signature", "")
            yield _flag("business-constant", "medium", f"Business constant changed: {c.name}  {b} → {a}",
                        "Thresholds and limits are business logic. Confirm the new value is intended.", c.file, c.line, c.hunk_ids)
        elif c.kind == "agent" and c.status == "modified":
            yield _flag("prompt-changed", "low", f"LLM prompt/agent changed: {c.name}",
                        "Prompt changes alter behavior without changing code paths. Re-run evals if you have them.", c.file, c.line, c.hunk_ids)
        elif c.kind == "env" and c.status == "added":
            fc = ctx.file(c.file)
            if (fc and fc.category == "test") or c.name in seen_env:
                continue
            seen_env.add(c.name)
            default = ((c.after or {}).get("fields") or {}).get("default") or ""
            required = default == "(required)"
            if default.startswith("flag:"):
                title, detail = f"New feature flag: {c.name}", f"{default[5:].strip().capitalize()}. Decide its value per environment before deploy."
            elif required:
                title, detail = f"New env var: {c.name} (required)", "Must be set in every environment (local, CI, staging, prod) before deploy."
            else:
                title, detail = f"New env var: {c.name}", "Has a fallback default."
            yield _flag("new-env", "medium" if required else "low", title, detail, c.file, c.line, c.hunk_ids)
        elif c.kind == "dependency" and c.status == "added":
            yield _flag("new-dependency", "low", f"New dependency: {c.name} {((c.after or {}).get('signature') or '')}",
                        "Check it's maintained, licensed appropriately and actually needed.", c.file, c.line, c.hunk_ids)
        elif c.kind == "dependency" and c.status == "modified":
            b = re.search(r"\d+", (c.before or {}).get("signature", "") or "")
            a = re.search(r"\d+", (c.after or {}).get("signature", "") or "")
            if a and b and a.group() != b.group():
                yield _flag("major-bump", "medium", f"Major version change: {c.name} {(c.before or {}).get('signature')} → {(c.after or {}).get('signature')}",
                            "Major upgrades usually include breaking changes.", c.file, c.line, c.hunk_ids)


def sensitive_rules(ctx: Context) -> Iterator[RiskFlag]:
    for fc in ctx.cs.files:
        if fc.category != "source":
            continue
        fns = [c for c in ctx.changes if c.file == fc.path and c.kind == "function" and (c.body_changed or c.status != "modified")]
        hit_path = SENSITIVE_PATH_RE.search(fc.path)
        hit_fns = [c for c in fns if SENSITIVE_PATH_RE.search(c.name)]
        if (hit_path and fns) or hit_fns:
            word = (hit_path or SENSITIVE_PATH_RE.search(hit_fns[0].name)).group(0).lower()
            names = ", ".join(c.name for c in (hit_fns or fns)[:3])
            yield _flag("sensitive-area", "medium", f"Touches {word}-related code",
                        f"Changed: {names}. Auth and money paths deserve a line-by-line read.", fc.path,
                        (hit_fns or fns)[0].line, sorted({h for c in (hit_fns or fns) for h in c.hunk_ids})[:6])


def parse_rules(ctx: Context) -> Iterator[RiskFlag]:
    for fc, err in ctx.parse_failures:
        yield _flag("parse-error", "high", f"File doesn't parse: {fc.path}", err, fc.path, 0, [h.id for h in fc.hunks][:2])


# sensitive_rules is not run: "touches money code" is true of most files in a fintech repo, so as a flag it was
# pure noise. The same signal now raises the file in the reading order (analyze.file_rank).
RULES = [parse_rules, line_rules, migration_rules, schema_drift, api_rules, model_rules, task_rules, function_rules,
         test_rules, config_rules]


def run_all(ctx: Context) -> list[RiskFlag]:
    out: dict[str, RiskFlag] = {}
    for rule in RULES:
        try:
            for f in rule(ctx):
                out.setdefault(f.id, f)
        except Exception as e:  # a buggy rule should never kill the review
            f = _flag("internal", "low", f"Rule {rule.__name__} failed", repr(e))
            out.setdefault(f.id, f)
    return list(out.values())
