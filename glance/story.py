"""The change as a story, in the order people learn a feature:

  1. goal        — what this accomplishes (model pass; commit messages as a fallback)
  2. database    — the records we store (tables, enums, migrations)
  3. routes      — the endpoints that read/write them
  4. schemas     — the shapes those endpoints accept and return (and other in-app data shapes)
  5. background  — jobs, workflows and queues that run on their own
  6. supporting  — everything else (helpers, UI, config, env)

Within each chapter, units are split into new / changed / removed. A unit is one thing a reader thinks of as
one thing: a table, a route, a schema, a whole workflow (its steps are folded in), a function.
Deterministic: the model pass only adds prose to units by id, so every sentence stays tied to real hunks.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

CHAPTERS = [
    ("goal", "What this accomplishes", "The outcome, in plain language."),
    ("database", "Database records", "What gets stored — tables, enums and the migration that creates them."),
    ("routes", "Routes", "The endpoints that create, read, change or delete those records."),
    ("schemas", "Schemas", "The shapes data takes in the app — what routes accept and return come first."),
    ("background", "Background work", "Jobs, workflows and queues that run on a schedule or after a request."),
    ("supporting", "Supporting changes", "Helpers, UI, config and env vars that make the rest work."),
]
STATUS_ORDER = {"added": 0, "modified": 1, "moved": 1, "removed": 2}
BACKGROUND_PATH = re.compile(r"(^|/)(workers?|jobs?|cron|crons|queues?|workflows?|tasks?|schedulers?|consumers?)(/|\.|$)", re.I)
ROUTE_PATH = re.compile(r"(^|/)(routes?|routers?|controllers?|handlers?|endpoints?|api|views?|pages/api|app/api)(/|\.|$)", re.I)


def _is_test(path: str, files: dict[str, dict]) -> bool:
    f = files.get(path)
    return bool(f and f.get("category") == "test") or bool(re.search(r"(^|/)(__tests__|tests?|__mocks__)/|\.(test|spec)\.", path))


def _unit_status(statuses: list[str]) -> str:
    s = set(statuses)
    if s == {"added"}:
        return "added"
    if s == {"removed"}:
        return "removed"
    return "modified"


def _detail(c: dict) -> str:
    a = c.get("after") or c.get("before") or {}
    meta = a.get("meta") or {}
    fc = c.get("field_changes") or []
    if c["kind"] == "migration":
        ops = meta.get("upgrade") or []
        names = [f"{o['op']} {o.get('table') or ''}{'.' + o['column'] if o.get('column') else ''}".strip() for o in ops]
        return "; ".join(names[:6]) + (f"; +{len(names) - 6} more" if len(names) > 6 else "")
    if c["kind"] == "route":
        return f"{' '.join(m.upper() for m in meta.get('methods') or [])} {meta.get('path') or c['name']}".strip() + (f" → {meta['handler']}" if meta.get("handler") and meta.get("handler") != "inline" else "")
    if c["kind"] in ("table", "model", "type"):
        if c["status"] == "added":
            fields = list((a.get("fields") or {}).keys())
            return f"{len(fields)} fields: " + ", ".join(fields[:8]) + (" …" if len(fields) > 8 else "") if fields else (a.get("signature") or "")
        if fc:
            sym = {"added": "+", "removed": "−", "changed": "~", "renamed": "↷"}
            return " ".join(f"{sym.get(f['change'], '~')}{f['name']}" for f in fc[:10]) + (" …" if len(fc) > 10 else "")
    if c["kind"] == "env":
        return "no longer read" if c["status"] == "removed" else ("required — no default" if (a.get("fields") or {}).get("default") == "(required)" else f"default {(a.get('fields') or {}).get('default')}")
    if c["kind"] == "config":
        return a.get("signature") or ""
    if c.get("signature_changed") and c.get("before") and c.get("after"):
        return f"{c['before'].get('signature')} → {c['after'].get('signature')}"
    return a.get("signature") or ""


ROUTE_CALL = re.compile(r"""\.(get|post|put|patch|delete|all)\(\s*['"`](/[^'"`]*)['"`](.*)$""", re.I)


def route_handlers(report: dict) -> dict[str, list[str]]:
    """handler name -> ["POST /deposit", ...] from every Express/Fastify/Hono-style route registration in the repo
    (not just the diff), so a changed controller function is recognised as the route it serves."""
    from .git import git

    args = ["grep", "-n", "-I", "-E", r"\.(get|post|put|patch|delete|all)\(\s*['\"`]/"]
    if not report["head"].get("worktree") and report["head"].get("rev"):
        args.append(report["head"]["rev"])
    try:
        out = git(Path(report["repo_path"]), *args, check=False)
    except Exception:
        return {}
    found: dict[str, list[str]] = {}
    for line in out.splitlines():
        m = ROUTE_CALL.search(line)
        if not m:
            continue
        label = f"{m.group(1).upper()} {m.group(2)}"
        for name in set(re.findall(r"\b([A-Za-z_]\w{3,})\b", m.group(3))):
            found.setdefault(name, [])
            if label not in found[name]:
                found[name].append(label)
    return found


def _hunk_text(report: dict, paths: set[str]) -> str:
    return "\n".join(l[3] for f in report["files"] if f["path"] in paths for h in f["hunks"] for l in h["lines"] if l[0] != "-")


def _churn(report: dict, cs: list[dict]) -> int:
    """Changed lines inside these entities (not the whole hunk), the size of what a reviewer would read."""
    files = {f["path"]: f for f in report["files"]}
    n = 0
    for c in cs:
        e = c.get("after") or c.get("before") or {}
        lo, hi = e.get("line", 0), e.get("end_line", 0) or 10**9
        side = 2 if c.get("after") else 1
        for f in [files.get(c["file"])] if files.get(c["file"]) else []:
            for h in f["hunks"]:
                if h["id"] in (c.get("hunk_ids") or []):
                    n += sum(1 for l in h["lines"] if l[0] != " " and lo <= (l[side] if l[side] is not None else (l[3 - side] or 0)) <= hi) or 0
    return n


MONEY_OR_AUTH = re.compile(r"auth|permission|password|token|jwt|session|payment|billing|ledger|transfer|withdraw|deposit|"
                           r"margin|loan|invoice|charge|fee|interest|price|amount|balance|journal|payout|refund|kyc|subscription", re.I)
SMALL = 6        # changed lines: at or under this, an edit is usually mechanical


def _attention(u: dict, cs: list[dict], risky_hunks: set[str]) -> tuple[str, str]:
    """Default triage: 'read' (needs a human) or 'routine' (pre-checked; "by the way, this is done").
    The model pass may override it with a reason."""
    if risky_hunks & set(u["hunk_ids"]):
        return "read", "a risk flag points here"
    ch, st = u["chapter"], u["status"]
    if ch == "database":
        return "read", "changes what's stored"
    if ch == "routes":
        return "read", "changes an endpoint's behaviour" if st != "removed" else "an endpoint goes away"
    if ch == "schemas":
        if u.get("used_by_routes"):
            return "read", "part of an API contract"
        return "routine", "internal data shape"
    if any(c.get("signature_changed") for c in cs) and st == "modified":
        return "read", "signature changed"
    if ch == "background":
        if st == "added" and u["churn"] <= SMALL and u["kind"] != "task":
            return "routine", f"small helper ({u['churn']} lines)"
        if st == "added":
            return "read", "new background work"
        if u["churn"] <= SMALL:
            return "routine", f"small edit ({u['churn']} lines)"
        return "read", "background logic changed"
    # supporting
    if u["kind"] in ("config", "env"):
        return "routine", "configuration or copy"
    if u["file"].endswith((".tsx", ".jsx", ".vue", ".svelte", ".css", ".html")):
        big_money_ui = MONEY_OR_AUTH.search(u["name"]) and u["churn"] > 40
        return ("read", "large change to a money screen") if big_money_ui else ("routine", "UI")
    if st == "removed":
        return "routine", "removed helper"
    if MONEY_OR_AUTH.search(u["name"]) and u["churn"] > SMALL:
        return "read", "money/auth logic"
    if u["churn"] <= SMALL:
        return "routine", f"small edit ({u['churn']} lines)"
    return "read" if st == "added" and u["churn"] > 40 else "routine", "sizeable new code" if st == "added" and u["churn"] > 40 else "supporting change"


def build_story(report: dict[str, Any]) -> dict[str, Any]:
    files = {f["path"]: f for f in report["files"]}
    changes = [c for c in report["changes"] if not _is_test(c["file"], files)
               and not (c["kind"] == "function" and c["status"] == "modified" and not c.get("body_changed") and not c.get("signature_changed"))]

    handlers = route_handlers(report)
    # names that are also middleware/wrappers on every route say nothing about which route a function serves
    generic = {n for n, rs in handlers.items() if len(rs) > 3}

    def served(c: dict) -> list[str]:
        short = c["name"].split(".")[-1]
        return [] if c["kind"] != "function" or short in generic else handlers.get(short, [])

    def chapter_of(c: dict) -> str:
        if c.get("layer") == "database":
            return "database"
        if c["kind"] == "route" or served(c):
            return "routes"
        if c.get("layer") == "schema":
            return "schemas"
        if c["kind"] == "task" or (c["kind"] == "function" and BACKGROUND_PATH.search(c["file"])):
            return "background"
        return "supporting"

    # Fold a workflow's steps (and the function/task twins the extractors emit for them) into one unit.
    groups: dict[tuple[str, str], list[dict]] = {}
    for c in changes:
        ch = chapter_of(c)
        key_name = c["name"].split(".")[0] if ch == "background" and "." in c["name"] else c["name"]
        groups.setdefault((ch, f"{c['kind'] if ch != 'background' else 'bg'}:{key_name}:{c['file'] if ch != 'background' else ''}"), []).append(c)

    route_paths = {c["file"] for c in changes if c["kind"] == "route"} | {p for p in files if ROUTE_PATH.search(p) and not _is_test(p, files)}
    route_text = _hunk_text(report, route_paths)

    risky_hunks = {h for r in report.get("risks", []) if r["severity"] in ("high", "medium") for h in r.get("hunk_ids") or []}
    units: list[dict] = []
    for (ch, _), cs in groups.items():
        cs.sort(key=lambda c: (c["name"].count("."), c["line"]))
        head = cs[0]
        name = head["name"].split(".")[0] if ch == "background" and "." in head["name"] else head["name"]
        steps = sorted({c["name"].split(".", 1)[1] for c in cs if "." in c["name"]}) if ch == "background" else []
        u = {
            "chapter": ch, "name": name, "kind": head["kind"], "framework": head.get("framework") or "",
            "status": _unit_status([c["status"] for c in cs]), "file": head["file"], "line": head["line"],
            "topic": head.get("topic") or "other",
            "change_ids": [c["id"] for c in cs],
            "hunk_ids": list(dict.fromkeys(h for c in cs for h in c.get("hunk_ids") or [])),
            "detail": (f"{len(steps)} steps: " + ", ".join(steps[:8]) + (" …" if len(steps) > 8 else "")) if steps else _detail(head),
        }
        if ch == "routes" and head["kind"] == "function":
            u["serves"] = served(head)
            u["detail"] = "handler for " + ", ".join(u["serves"][:3]) + (" · " + _detail(head) if head.get("signature_changed") else "")
        if ch == "schemas":
            u["used_by_routes"] = bool(re.search(rf"\b{re.escape(name)}\b", route_text))
        u["churn"] = _churn(report, cs)
        u["attention"], u["attention_why"] = _attention(u, cs, risky_hunks)
        units.append(u)

    kind_order = {"table": 0, "type": 1, "model": 2, "migration": 3}
    units.sort(key=lambda u: ([c[0] for c in CHAPTERS].index(u["chapter"]), STATUS_ORDER.get(u["status"], 1),
                              not u.get("used_by_routes", False), kind_order.get(u["kind"], 5), u["file"], u["line"]))
    for i, u in enumerate(units, 1):
        u["id"] = f"u{i}"

    tests = sorted({c["file"] for c in report["changes"] if _is_test(c["file"], files)} | {p for p in files if _is_test(p, files)})
    return {
        "chapters": [{"id": cid, "title": t, "blurb": b, "unit_ids": [u["id"] for u in units if u["chapter"] == cid]} for cid, t, b in CHAPTERS],
        "units": units,
        "test_files": tests,
    }
