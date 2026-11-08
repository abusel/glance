"""Lightweight SQL migration / schema parsing (Postgres-flavoured, tolerant of MySQL/SQLite)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from ..model import Entity, short_hash

IDENT = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[\w.]+)'


def _unq(s: str) -> str:
    s = s.strip()
    parts = [p.strip('"`[]') for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", s)]
    return ".".join(parts)


def _compact(s: str, limit: int = 240) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", "", sql)


def split_top(s: str, sep: str = ",") -> list[str]:
    out, depth, cur, q = [], 0, [], None
    for ch in s:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            continue
        if ch in "'\"`":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


def statements(sql: str) -> list[tuple[int, str]]:
    """Split into (line, statement) pairs, respecting quotes and $$ bodies."""
    sql_nc = strip_comments(sql)
    out, cur, q, start_line, ln = [], [], None, 1, 1
    i = 0
    while i < len(sql_nc):
        ch = sql_nc[i]
        if ch == "\n":
            ln += 1
        if q:
            cur.append(ch)
            if sql_nc.startswith(q, i):
                if len(q) > 1:
                    cur.append(q[1:])
                    i += len(q) - 1
                q = None
            i += 1
            continue
        if sql_nc.startswith("$$", i):
            q = "$$"
            cur.append("$$")
            i += 2
            continue
        if ch in "'\"":
            q = ch
        if ch == ";":
            stmt = "".join(cur).strip()
            if stmt:
                out.append((start_line, stmt))
            cur = []
            start_line = ln
            i += 1
            continue
        if not "".join(cur).strip():
            start_line = ln
        cur.append(ch)
        i += 1
    stmt = "".join(cur).strip()
    if stmt:
        out.append((start_line, stmt))
    return out


def _alter_action(table: str, action: str) -> dict:
    a = action.strip()
    m = re.match(rf"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?({IDENT})\s+(.*)$", a, re.I | re.S)
    if m and not re.match(r"ADD\s+(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK|INDEX|KEY)\b", a, re.I):
        return {"op": "add_column", "table": table, "column": _unq(m.group(1)), "detail": _compact(m.group(2))}
    m = re.match(rf"DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?({IDENT})(\s+CASCADE)?\s*$", a, re.I)
    if m and not re.match(r"DROP\s+(CONSTRAINT|INDEX|PRIMARY|FOREIGN|DEFAULT)\b", a, re.I):
        return {"op": "drop_column", "table": table, "column": _unq(m.group(1)), "detail": _compact(a)}
    m = re.match(rf"(?:ALTER|MODIFY)\s+(?:COLUMN\s+)?({IDENT})\s+(.*)$", a, re.I | re.S)
    if m:
        return {"op": "alter_column", "table": table, "column": _unq(m.group(1)), "detail": _compact(m.group(2))}
    m = re.match(rf"RENAME\s+COLUMN\s+({IDENT})\s+TO\s+({IDENT})", a, re.I)
    if m:
        return {"op": "rename_column", "table": table, "column": _unq(m.group(1)), "detail": f"→ {_unq(m.group(2))}"}
    m = re.match(rf"RENAME\s+TO\s+({IDENT})", a, re.I)
    if m:
        return {"op": "rename_table", "table": table, "column": None, "detail": f"→ {_unq(m.group(1))}"}
    if re.match(r"ADD\s+CONSTRAINT|ADD\s+(PRIMARY|UNIQUE|FOREIGN|CHECK)", a, re.I):
        return {"op": "add_constraint", "table": table, "column": None, "detail": _compact(a)}
    if re.match(r"DROP\s+CONSTRAINT", a, re.I):
        return {"op": "drop_constraint", "table": table, "column": None, "detail": _compact(a)}
    return {"op": "alter_table", "table": table, "column": None, "detail": _compact(a)}


def parse_ops(sql: str) -> list[dict]:
    ops: list[dict] = []
    for ln, st in statements(sql):
        s = st.strip()
        m = re.match(rf"CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({IDENT})\s*\((.*)\)\s*[^)]*$", s, re.I | re.S)
        if m:
            cols = split_top(m.group(2))
            ops.append({"op": "create_table", "table": _unq(m.group(1)), "column": None,
                        "detail": "; ".join(_compact(c, 120) for c in cols), "columns": cols, "line": ln})
            continue
        m = re.match(rf"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?({IDENT})\s+(.*)$", s, re.I | re.S)
        if m:
            table = _unq(m.group(1))
            for action in split_top(m.group(2)):
                op = _alter_action(table, action)
                op["line"] = ln
                ops.append(op)
            continue
        m = re.match(rf"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?({IDENT}(?:\s*,\s*{IDENT})*)", s, re.I)
        if m:
            for t in m.group(1).split(","):
                ops.append({"op": "drop_table", "table": _unq(t), "column": None, "detail": _compact(s), "line": ln})
            continue
        m = re.match(rf"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?({IDENT})?\s*ON\s+(?:ONLY\s+)?({IDENT})\s*(.*)$", s, re.I | re.S)
        if m:
            ops.append({"op": "create_unique_index" if m.group(1) else "create_index", "table": _unq(m.group(3)),
                        "column": None, "detail": _compact(f"{_unq(m.group(2) or '')} {m.group(4)}"), "line": ln})
            continue
        m = re.match(rf"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?({IDENT})", s, re.I)
        if m:
            ops.append({"op": "drop_index", "table": None, "column": None, "detail": _unq(m.group(1)), "line": ln})
            continue
        m = re.match(rf"CREATE\s+TYPE\s+({IDENT})\s+AS\s+ENUM\s*\((.*)\)", s, re.I | re.S)
        if m:
            ops.append({"op": "create_enum", "table": _unq(m.group(1)), "column": None, "detail": _compact(m.group(2)), "line": ln})
            continue
        m = re.match(rf"ALTER\s+TYPE\s+({IDENT})\s+(.*)$", s, re.I | re.S)
        if m:
            ops.append({"op": "alter_enum", "table": _unq(m.group(1)), "column": None, "detail": _compact(m.group(2)), "line": ln})
            continue
        m = re.match(rf"(DELETE\s+FROM|UPDATE|INSERT\s+INTO|TRUNCATE(?:\s+TABLE)?)\s+({IDENT})", s, re.I)
        if m:
            verb = m.group(1).split()[0].upper()
            op = {"DELETE": "delete_rows", "UPDATE": "update_rows", "INSERT": "insert_rows", "TRUNCATE": "truncate"}[verb]
            ops.append({"op": op, "table": _unq(m.group(2)), "column": None, "detail": _compact(s), "line": ln})
            continue
        ops.append({"op": "execute", "table": None, "column": None, "detail": _compact(s), "line": ln})
    return ops


def _framework(path: str) -> str:
    p = path.lower()
    if "prisma" in p:
        return "prisma"
    if "drizzle" in p:
        return "drizzle"
    if "supabase" in p:
        return "supabase"
    return "sql"


def extract(path: str, text: str, category: str = "source") -> list[Entity]:
    ops = parse_ops(text)
    p = PurePosixPath(path)
    if category == "migration":
        name = p.parent.name if p.stem in ("migration", "up", "down") else p.stem
        is_down = bool(re.search(r"(^|[._-])down([._-]|$)", p.name))
        e = Entity(kind="migration", name=name + (" (down)" if is_down else ""), file=path, line=1,
                   end_line=max(1, len(text.splitlines())), lang="sql", framework=_framework(path), signature=name,
                   meta={"upgrade": [] if is_down else ops, "downgrade": ops if is_down else [], "downgrade_empty": None,
                         "message": name, "forward_only": _framework(path) in ("prisma", "drizzle")},
                   body_hash=short_hash(text))
        return [e]
    out: list[Entity] = []
    for op in ops:
        if op["op"] == "create_table":
            fields = {}
            for c in op.get("columns", []):
                m = re.match(rf"({IDENT})\s+(.*)$", c, re.S)
                if m and not re.match(r"(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK|INDEX|KEY)\b", c, re.I):
                    fields[_unq(m.group(1))] = _compact(m.group(2), 160)
            out.append(Entity(kind="table", name=op["table"], file=path, line=op["line"], end_line=op["line"],
                              lang="sql", framework="sql", fields=fields, signature=f"CREATE TABLE {op['table']}",
                              meta={"tablename": op["table"]}))
    return out
