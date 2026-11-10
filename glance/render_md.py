"""Markdown rendering of a report — for terminals, PR descriptions and Claude Code."""

from __future__ import annotations

from typing import Any

SEV_ICON = {"high": "🔴", "medium": "🟠", "low": "⚪"}
STATUS = {"added": "+", "removed": "−", "modified": "~", "moved": "→"}


def _fields_table(c: dict) -> list[str]:
    rows = [f for f in c.get("field_changes") or []]
    if not rows:
        return []
    out = ["", "| | field | before | after |", "|---|---|---|---|"]
    for f in rows[:30]:
        mark = {"added": "+", "removed": "−", "changed": "~", "renamed": "↷"}[f["change"]]
        b = (f.get("before") or "").replace("|", "\\|")
        a = (f.get("after") or "").replace("|", "\\|")
        out.append(f"| {mark} | `{f['name']}` | {('`' + b + '`') if b else ''} | {('`' + a + '`') if a else ''} |")
    return out + [""]


def render(report: dict[str, Any], max_items: int = 40) -> str:
    L: list[str] = []
    r = report
    head = "working tree" if r["head"]["worktree"] else r["head"]["ref"]
    L.append(f"# glance · {r['repo']} · {r['base']['ref']}…{head}")
    L.append(f"_{r['summary']['headline']}_")
    L.append("")
    llm = r.get("llm") or {}
    if llm.get("status") == "ok" and llm.get("summary"):
        L += ["## Summary", llm["summary"], ""]
    if r["summary"]["bullets"]:
        L += ["## At a glance", *("- " + b for b in r["summary"]["bullets"]), ""]
    intent = llm.get("intent") if llm.get("status") == "ok" else None
    if intent:
        L.append("## Intent vs. actual")
        if intent.get("asked"):
            L.append(f"**Asked:** {intent['asked']}")
        for d in intent.get("done") or []:
            L.append(f"- ✅ {d}")
        for e in intent.get("extra") or []:
            L.append(f"- ➕ not asked for: {e.get('text') if isinstance(e, dict) else e}")
        for m in intent.get("missing") or []:
            L.append(f"- ❌ missing: {m}")
        L.append("")

    decisions = llm.get("decisions") or [] if llm.get("status") == "ok" else []
    if decisions:
        L.append("## Decisions to agree with")
        for d in decisions:
            L.append(f"- {SEV_ICON.get(d.get('weight'), '')} **{d['title']}**")
            if d.get("chose"):
                L.append(f"  - chose: {d['chose']}")
            if d.get("alternative"):
                L.append(f"  - instead of: {d['alternative']}")
            if d.get("tradeoff"):
                L.append(f"  - tradeoff: {d['tradeoff']}")
        L.append("")

    flows = llm.get("flows") or [] if llm.get("status") == "ok" else []
    if flows:
        names = {u["id"]: u["name"] for u in (r.get("story") or {}).get("units", [])}
        L.append("## How data flows")
        for f in flows:
            L.append(f"**{f['title']}**" + (f" · _{f['trigger']}_" if f.get("trigger") else ""))
            for i, st in enumerate(f["steps"], 1):
                L.append(f"{i}. `{names.get(st['unit'], st['unit'])}` {st.get('text', '')}" + (f" → {st['data']}" if st.get("data") else ""))
            L.append("")

    scenarios = llm.get("scenarios") or [] if llm.get("status") == "ok" else []
    if scenarios:
        L.append("## What behaves differently")
        cell = lambda x: str(x).replace("|", "\\|").replace("\n", " ")
        for sc in scenarios:
            L.append(f"**{sc['title']}**" + (f" · `{sc['subject']}`" if sc.get("subject") else ""))
            if sc.get("rule"):
                L.append(f"_{sc['rule']}_")
            L += ["", "| case | before | after |", "|---|---|---|"]
            for row in sc["rows"]:
                after = f"**{cell(row['after'])}**" if row.get("changed") else cell(row["after"])
                L.append(f"| {cell(row['case'])} | {cell(row['before'])} | {after} |")
            L.append("")

    questions = llm.get("questions") or [] if llm.get("status") == "ok" else []
    if questions:
        L.append("## Can you answer these before merging?")
        for q in questions:
            L.append(f"- **{q['q']}**" + (f"  \n  _{q['answer']}_" if q.get("answer") else ""))
        L.append("")

    risks = r["risks"]
    concerns = llm.get("concerns") or [] if llm.get("status") == "ok" else []
    if risks or concerns:
        L.append("## Risks")
        for c in concerns:
            L.append(f"- {SEV_ICON.get(c['severity'], '')} **{c['title']}** (model) — {c.get('detail', '')}")
        for x in risks[:max_items]:
            loc = f" `{x['file']}:{x['line']}`" if x["file"] else ""
            L.append(f"- {SEV_ICON[x['severity']]} **{x['title']}**{loc} — {x['detail'].splitlines()[0]}")
        if len(risks) > max_items:
            L.append(f"- … {len(risks) - max_items} more")
        L.append("")

    bl = llm.get("business_logic") or [] if llm.get("status") == "ok" else []
    consts = [c for c in r["changes"] if c["kind"] in ("config", "agent") and c["status"] == "modified"]
    if bl or consts:
        L.append("## Business logic")
        for b in bl:
            L.append(f"- **{b['title']}** — before: {b.get('before', '')} → after: {b.get('after', '')}"
                     + (f" _{b['why_it_matters']}_" if b.get("why_it_matters") else ""))
        for c in consts:
            L.append(f"- `{c['name']}`: `{(c['before'] or {}).get('signature', '')}` → `{(c['after'] or {}).get('signature', '')}` ({c['file']})")
        L.append("")

    for layer, heading in (("database", "## Database (tables, migrations)"), ("schema", "## Schemas & types (data in the app)")):
        data = [c for c in r["changes"] if c.get("layer") == layer]
        if data:
            L.append(heading)
            for c in data[:max_items]:
                L.append(f"### {STATUS[c['status']]} {c['kind']} `{c['name']}` · {c['framework']} · `{c['file']}:{c['line']}`")
                if c["kind"] == "migration":
                    meta = (c.get("after") or c.get("before") or {}).get("meta", {})
                    if meta.get("message"):
                        L.append(f"_{meta['message']}_")
                    for op in meta.get("upgrade", []):
                        tgt = (op.get("table") or "") + ("." + op["column"] if op.get("column") else "")
                        L.append(f"- `{op['op']}` {tgt} {op.get('detail', '')[:140]}")
                    if meta.get("downgrade_empty"):
                        L.append("- ⚠️ no downgrade")
                else:
                    L += _fields_table(c)
            L.append("")

    iface = [c for c in r["changes"] if c["kind"] in ("route", "task", "env", "dependency")]
    if iface:
        L.append("## Interfaces")
        for c in iface[:max_items]:
            extra = ""
            m = (c.get("after") or c.get("before") or {}).get("meta", {})
            if c["kind"] == "route" and m.get("dependencies"):
                extra = f" · deps: {', '.join(m['dependencies'])[:120]}"
            if m.get("handler_body_changed"):
                extra += " · handler logic changed"
            L.append(f"- {STATUS[c['status']]} **{c['kind']}** `{c['name']}` ({c['framework']}){extra}")
        L.append("")

    sigs = [c for c in r["changes"] if c["kind"] == "function" and (c["signature_changed"] or c["status"] in ("added", "removed"))]
    if sigs:
        L.append("## Functions")
        for c in sigs[:max_items]:
            b = (c.get("before") or {}).get("signature", "")
            a = (c.get("after") or {}).get("signature", "")
            if c["status"] == "added":
                L.append(f"- + `{a}` ({c['file']})")
            elif c["status"] == "removed":
                L.append(f"- − `{b}` ({c['file']})")
            else:
                L.append(f"- ~ `{b}` → `{a}` ({c['file']}, {len(c.get('callers') or [])} callers)")
        L.append("")

    L.append("## Reading order")
    for f in r["files"][:max_items]:
        L.append(f"{f['rank']}. `{f['path']}` (+{f['additions']} −{f['deletions']}) — {', '.join(f['reasons'])}")
    L.append("")
    if llm.get("status") not in ("ok", None):
        L.append(f"_Model pass: {llm.get('status')} — {llm.get('reason') or llm.get('error') or ''}_")
    return "\n".join(L)
