"""The data map: which parts of the change touch which, as a graph over story units.

Nodes are story units (routes, tasks, functions, schemas, tables, migrations). An edge A → B means A's code names
B: a route that takes a model, a service that writes a table, a migration that alters a table, a model nested
in another. Deterministic and grounded: every edge comes from a name that appears in the code. Edges are
labelled by what the pair of kinds usually means, with a light read/write heuristic for tables.
"""

from __future__ import annotations

import re
from typing import Any

# Map columns, left to right in the order data usually moves through a backend.
COLUMNS = [
    ("entry", "Entry points", "Routes and jobs: where work starts"),
    ("logic", "Logic", "Functions that do the work"),
    ("shape", "Data shapes", "Schemas, models and types passed around"),
    ("store", "Storage", "Tables and the migrations that change them"),
]
GENERIC = {"base", "data", "model", "item", "items", "result", "value", "config", "settings", "user_id", "main",
           "create", "update", "delete", "list", "init", "setup", "handler", "handle", "process", "run", "get", "set",
           "response", "request", "error", "status", "type", "types", "index", "default", "helper", "utils"}
WRITE = re.compile(r"\b(add|add_all|insert|insert_into|update|upsert|create|create_many|save|delete|destroy|merge|bulk_create|"
                   r"bulk_update|execute|values|set)\s*\(", re.I)


def column_of(u: dict) -> str | None:
    k, ch = u["kind"], u["chapter"]
    if k in ("table", "migration") or ch == "database":
        return "store"
    if k in ("route", "task") or ch == "routes":     # incl. handler functions for routes registered elsewhere
        return "entry"
    if k in ("model", "type") or ch == "schemas":
        return "shape"
    if k == "function" or ch == "background":
        return "logic"
    return None                        # config, env, deps, UI copy: not part of the data map


def _span(c: dict, texts: dict[str, str | None], old_texts: dict[str, str | None]) -> str:
    e = c.get("after") or c.get("before") or {}
    text = (texts if c.get("after") else old_texts).get(c["file"]) or ""
    lines = text.splitlines()
    lo, hi = max(1, int(e.get("line") or 1)), int(e.get("end_line") or e.get("line") or 1)
    body = "\n".join(lines[lo - 1:hi])
    # field types and signature carry references too (e.g. a Pydantic field typed as another model)
    return body + "\n" + (e.get("signature") or "") + "\n" + " ".join(f"{k}: {v}" for k, v in (e.get("fields") or {}).items())


def _patterns(u: dict, changes: dict[str, dict]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """How other code refers to this unit: (name, regex) pairs for code, and for migrations / raw SQL.
    A table's SQL name only counts inside quotes in a migration, never in code (it's also every URL path)."""
    if u["kind"] in ("route", "migration"):
        return [], []
    names = {n for n in {u["name"].split(".")[-1], u["name"].split(".")[0]} if len(n) >= 4 and n.lower() not in GENERIC}
    code = [(n, rf"(?<![\w.]){re.escape(n)}\b") for n in sorted(names, key=len, reverse=True)]
    sql = []
    if u["kind"] == "table":
        for n in names:                # Prisma client accessor: prisma.order.update(...)
            code.append((n, rf"\.{re.escape(n[0].lower() + n[1:])}\.\w+\("))
        for cid in u["change_ids"]:
            c = changes.get(cid) or {}
            tn = ((c.get("after") or c.get("before") or {}).get("meta") or {}).get("tablename")
            if tn:
                sql.append((str(tn), rf"['\"`]{re.escape(str(tn))}['\"`]"))
    return code, sql


def _label(src: dict, dst: dict, body: str, name: str) -> str:
    sk, dk = src["kind"], dst["kind"]
    if sk == "migration":
        return "alters"
    if dk == "table" and sk == "table":
        return "references"
    if dk == "table":
        # a write call on the same line as the table's name, or a constructor call Name(...)
        lower = name[0].lower() + name[1:]
        for line in body.splitlines():
            if re.search(rf"\b({re.escape(name)}|{re.escape(lower)})\b", line) and (WRITE.search(line) or re.search(rf"\b{re.escape(name)}\s*\(", line)):
                return "writes"
        return "reads"
    if dst["kind"] in ("model", "type") or dst.get("col") == "shape":
        if sk in ("model", "type") or src.get("col") == "shape":
            return "contains"
        return "uses"
    if dk == "task":
        return "enqueues" if re.search(rf"\b{re.escape(name)}\s*\.\s*(delay|apply_async|s|si|send)\b|\.add\(", body) else "calls"
    return "calls"


def build_graph(report: dict[str, Any], texts: dict[str, str | None], old_texts: dict[str, str | None]) -> dict[str, Any]:
    story = report.get("story") or {}
    changes = {c["id"]: c for c in report["changes"]}
    units = []
    for u in story.get("units", []):
        col = column_of(u)
        if col:
            units.append(dict(u, col=col))
    by_id = {u["id"]: u for u in units}

    bodies = {u["id"]: "\n".join(_span(changes[cid], texts, old_texts) for cid in u["change_ids"] if cid in changes) for u in units}
    # a route's handler body is where it uses things: fold the handler function in
    fn_by_name = {}
    for c in report["changes"]:
        if c["kind"] == "function":
            fn_by_name.setdefault((c["file"], c["name"]), c)
    for u in units:
        if u["kind"] != "route":
            continue
        for cid in u["change_ids"]:
            c = changes.get(cid) or {}
            h = ((c.get("after") or c.get("before") or {}).get("meta") or {}).get("handler")
            fc = fn_by_name.get((c.get("file"), h))
            if fc:
                bodies[u["id"]] += "\n" + _span(fc, texts, old_texts)

    pats = {u["id"]: _patterns(u, changes) for u in units}
    edges: list[dict] = []
    seen = set()
    for src in units:
        body = bodies[src["id"]]
        for dst in units:
            if dst["id"] == src["id"]:
                continue
            code, sql = pats[dst["id"]]
            hit = next((n for n, rx in (sql if src["kind"] == "migration" else code) if re.search(rx, body)), None)
            if not hit:
                continue
            key = (src["id"], dst["id"])
            if key in seen:
                continue
            seen.add(key)
            edges.append({"from": src["id"], "to": dst["id"], "kind": _label(src, dst, body, hit), "via": hit})

    connected = {e["from"] for e in edges} | {e["to"] for e in edges}
    nodes = []
    for u in units:
        # helpers that touch nothing else on the map are noise there; they stay in Step by step
        if u["col"] == "logic" and u["id"] not in connected and u["chapter"] == "supporting":
            continue
        c0 = changes.get(u["change_ids"][0]) or {}
        e = c0.get("after") or c0.get("before") or {}
        node = {"id": u["id"], "col": u["col"], "name": u["name"], "kind": u["kind"], "status": u["status"],
                "file": u["file"], "line": u["line"], "topic": u.get("topic", "other"), "detail": u.get("detail", "")}
        if u["kind"] in ("table", "model", "type"):
            fc = {f["name"]: f["change"] for f in c0.get("field_changes") or []}
            fields = [{"name": k, "type": str(v)[:80], "change": fc.get(k) or ("added" if u["status"] == "added" else "")}
                      for k, v in (e.get("fields") or {}).items()]
            fields += [{"name": f["name"], "type": str(f.get("before") or "")[:80], "change": "removed"}
                       for f in c0.get("field_changes") or [] if f["change"] == "removed"]
            node["fields"] = fields
            if (e.get("meta") or {}).get("tablename"):
                node["tablename"] = e["meta"]["tablename"]
        if u["kind"] == "migration":
            node["ops"] = [f"{o['op']} {o.get('table') or ''}{'.' + o['column'] if o.get('column') else ''}".strip()
                           for o in (e.get("meta") or {}).get("upgrade", [])][:12]
        nodes.append(node)
    keep = {n["id"] for n in nodes}
    edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
    return {"columns": [{"id": c, "title": t, "blurb": b} for c, t, b in COLUMNS], "nodes": nodes, "edges": edges}
