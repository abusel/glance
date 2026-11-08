"""Turn a git ChangeSet into entity-level changes, risks and a ranked reading order."""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from typing import Any

from . import risk
from .extractors import ExtractError, extract
from .git import ChangeSet, git, grep_word
from .model import Entity, EntityChange, FileChange, RiskFlag, short_hash

# Meta keys whose change makes an entity "modified" even when fields/signature match.
WATCHED_META = (
    "methods", "path", "dependencies", "response_model", "status_code", "tablename", "table_args", "options",
    "validators", "tools", "upgrade", "downgrade", "decorator", "modifiers", "schema", "config", "handler",
)
GENERIC_NAMES = {
    "get", "set", "run", "main", "create", "update", "delete", "list", "init", "setup", "handler", "handle",
    "index", "find", "save", "load", "build", "call", "execute", "process", "render", "default", "start", "stop",
    "__init__", "__call__", "__repr__", "__str__", "constructor", "toString", "ngOnInit", "up", "down",
}
KIND_WEIGHT = {"migration": 6, "table": 6, "route": 5, "model": 4, "task": 4, "agent": 4, "config": 3, "env": 3,
               "type": 2, "function": 1, "dependency": 1}
CATEGORY_ORDER = {"migration": 0, "source": 0, "config": 1, "other": 1, "test": 2, "docs": 3, "generated": 4, "lockfile": 4}
SEV_WEIGHT = {"high": 25, "medium": 5, "low": 1}


def _ent_dict(e: Entity) -> dict[str, Any]:
    return e.to_dict()


def diff_fields(before: dict[str, str], after: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    removed = [k for k in before if k not in after]
    added = [k for k in after if k not in before]
    # rename heuristic: same descriptor, different name
    renamed: dict[str, str] = {}
    for r in removed:
        for a in added:
            if a not in renamed.values() and before[r] and before[r] == after[a]:
                renamed[r] = a
                break
    for k in before:
        if k in after:
            if before[k] != after[k]:
                out.append({"name": k, "change": "changed", "before": before[k], "after": after[k]})
        elif k in renamed:
            out.append({"name": renamed[k], "change": "renamed", "before": f"{k}: {before[k]}", "after": f"{renamed[k]}: {after[renamed[k]]}", "from": k})
        else:
            out.append({"name": k, "change": "removed", "before": before[k], "after": None})
    for k in after:
        if k not in before and k not in renamed.values():
            out.append({"name": k, "change": "added", "before": None, "after": after[k]})
    return out


def diff_meta(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k in WATCHED_META:
        b, a = before.get(k), after.get(k)
        if (b or a) and b != a:
            out.append({"key": k, "before": b, "after": a})
    return out


def diff_entities(old: list[Entity], new: list[Entity], fc: FileChange) -> list[EntityChange]:
    old_map = {e.key: e for e in old}
    new_map = {e.key: e for e in new}
    changes: list[EntityChange] = []
    for key in list(old_map) + [k for k in new_map if k not in old_map]:
        b, a = old_map.get(key), new_map.get(key)
        kind, name = key
        if b and a:
            fch = diff_fields(b.fields, a.fields)
            sig = b.signature != a.signature
            body = bool(b.body_hash or a.body_hash) and b.body_hash != a.body_hash
            mch = diff_meta(b.meta, a.meta)
            if not (fch or sig or body or mch):
                continue
            ch = EntityChange("modified", kind, name, fc.path, a.lang, a.framework, _ent_dict(b), _ent_dict(a),
                              field_changes=fch, signature_changed=sig, body_changed=body, meta_changes=mch, line=a.line)
        elif a:
            ch = EntityChange("added", kind, name, fc.path, a.lang, a.framework, None, _ent_dict(a),
                              field_changes=[{"name": k, "change": "added", "before": None, "after": v} for k, v in a.fields.items()],
                              line=a.line)
        else:
            assert b is not None
            ch = EntityChange("removed", kind, name, fc.path, b.lang, b.framework, _ent_dict(b), None,
                              field_changes=[{"name": k, "change": "removed", "before": v, "after": None} for k, v in b.fields.items()],
                              line=b.line)
        ch.hunk_ids = link_hunks(fc, b, a)
        changes.append(ch)
    return changes


def link_hunks(fc: FileChange, before: Entity | None, after: Entity | None) -> list[str]:
    ids: list[str] = []
    for h in fc.hunks:
        hit = False
        # only count hunks whose changed lines fall inside the entity
        if after is not None and h.touches_new(after.line, after.end_line):
            hit = any(l[0] == "+" and after.line <= l[2] <= after.end_line for l in h.lines)
        if not hit and before is not None and h.touches_old(before.line, before.end_line):
            hit = any(l[0] == "-" and before.line <= l[1] <= before.end_line for l in h.lines)
        if hit:
            ids.append(h.id)
    return ids


def pair_moves(changes: list[EntityChange]) -> list[EntityChange]:
    """An entity removed from one file and added to another is a move, not a delete + create."""
    removed = defaultdict(list)
    for c in changes:
        if c.status == "removed" and c.kind not in ("dependency", "env"):
            removed[(c.kind, c.name.split(".")[-1])].append(c)
    out: list[EntityChange] = []
    consumed: set[int] = set()
    for c in changes:
        if c.status == "added" and c.kind not in ("dependency", "env"):
            cands = [r for r in removed.get((c.kind, c.name.split(".")[-1]), []) if id(r) not in consumed and r.file != c.file]
            if cands:
                r = cands[0]
                consumed.add(id(r))
                b, a = r.before or {}, c.after or {}
                fch = diff_fields(b.get("fields", {}), a.get("fields", {}))
                sig = b.get("signature") != a.get("signature")
                body = b.get("body_hash") != a.get("body_hash") and bool(b.get("body_hash") or a.get("body_hash"))
                moved = EntityChange("moved", c.kind, c.name, c.file, c.lang, c.framework, b, a, field_changes=fch,
                                     signature_changed=sig, body_changed=body, meta_changes=diff_meta(b.get("meta", {}), a.get("meta", {})),
                                     hunk_ids=r.hunk_ids + c.hunk_ids, line=c.line)
                moved.after = {**a, "moved_from": f"{r.file}:{r.line}"}
                out.append(moved)
                continue
        out.append(c)
    return [c for c in out if not (c.status == "removed" and id(c) in consumed)]


def find_callers(cs: ChangeSet, changes: list[EntityChange], changed_paths: set[str]) -> None:
    rev = cs.head_rev
    budget = 60
    for c in changes:
        if budget <= 0:
            break
        if c.kind != "function":
            continue
        need = c.status in ("removed", "added") or c.signature_changed
        if not need:
            continue
        short = c.name.split(".")[-1]
        if short in GENERIC_NAMES or len(short) < 4 or short.startswith("__"):
            continue
        budget -= 1
        hits = grep_word(cs.repo, short, rev=rev, limit=60)
        ent = c.after or c.before or {}
        start, end = ent.get("line", 0), ent.get("end_line", 0)
        callers = []
        for path, ln, text in hits:
            if path == c.file and start <= ln <= end:
                continue
            if re.match(rf"^\s*(async\s+)?(def|function|class)\s+{re.escape(short)}\b", text):
                continue
            if re.match(rf"^\s*(export\s+)?(const|let|var)\s+{re.escape(short)}\b", text):
                continue
            if text.lstrip().startswith(("import ", "from ", "#", "//", "*")) and "(" not in text:
                continue
            callers.append({"file": path, "line": ln, "text": text, "in_diff": path in changed_paths})
        c.callers = callers[:25]


def file_rank(fc: FileChange, ents: list[EntityChange], risks: list[RiskFlag]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    sev = Counter(r.severity for r in risks)
    for s in ("high", "medium"):
        if sev[s]:
            reasons.append(f"{sev[s]} {s}-risk flag{'s' if sev[s] > 1 else ''}")
    score += sum(SEV_WEIGHT[r.severity] for r in risks)
    by_kind = Counter((e.kind, e.status) for e in ents if e.kind != "function")
    for (kind, status), n in by_kind.most_common(3):
        reasons.append(f"{n} {kind}{'s' if n > 1 else ''} {status}")
    score += sum(KIND_WEIGHT.get(e.kind, 1) for e in ents)
    score += min((fc.additions + fc.deletions) / 40.0, 5)
    if not reasons:
        fn = sum(1 for e in ents if e.kind == "function")
        if fn:
            reasons.append(f"{fn} function{'s' if fn > 1 else ''} changed")
        elif fc.category in ("test", "docs", "lockfile", "generated"):
            reasons.append(fc.category)
        else:
            reasons.append(f"+{fc.additions} −{fc.deletions}")
    return score, reasons


def deterministic_summary(cs: ChangeSet, changes: list[EntityChange], risks: list[RiskFlag]) -> dict[str, Any]:
    adds = sum(f.additions for f in cs.files)
    dels = sum(f.deletions for f in cs.files)
    sev = Counter(r.severity for r in risks)
    risk_txt = ", ".join(f"{sev[s]} {s}" for s in ("high", "medium", "low") if sev[s]) or "no risk flags"
    headline = f"+{adds} −{dels} across {len(cs.files)} files · {risk_txt}"
    bullets: list[str] = []
    label = {"migration": "migration", "table": "DB table", "model": "schema/model", "type": "type", "route": "endpoint",
             "task": "task/workflow", "agent": "agent/prompt", "config": "constant", "env": "env var",
             "function": "function", "dependency": "dependency"}
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for c in changes:
        by_kind[c.kind][c.status] += 1
    for kind in ("migration", "table", "model", "route", "task", "agent", "config", "env", "dependency", "type", "function"):
        if kind not in by_kind:
            continue
        parts = [f"{n} {st}" for st, n in by_kind[kind].most_common()]
        plural = label[kind][:-1] + "ies" if label[kind].endswith("y") else label[kind] + "s"
        bullets.append(f"{plural}: " + ", ".join(parts))
    sig = sum(1 for c in changes if c.kind == "function" and c.signature_changed and c.status != "added")
    if sig:
        bullets.append(f"{sig} function signature{'s' if sig > 1 else ''} changed")
    return {"headline": headline, "bullets": bullets}


def trim_hunks_for_report(fc: FileChange) -> list[dict[str, Any]]:
    if fc.category in ("lockfile", "generated") or fc.binary:
        return []
    out = []
    for h in fc.hunks:
        lines = h.lines
        if len(lines) > 600:
            lines = lines[:600] + [[" ", None, None, f"… {len(h.lines) - 600} more lines"]]
        out.append({"id": h.id, "old_start": h.old_start, "old_len": h.old_len, "new_start": h.new_start,
                    "new_len": h.new_len, "header": h.header, "lines": lines})
    return out


def analyze(cs: ChangeSet, with_callers: bool = True, intent: str | None = None) -> dict[str, Any]:
    t0 = time.time()
    errors: list[str] = []
    all_changes: list[EntityChange] = []
    per_file_new: dict[str, list[Entity]] = {}
    per_file_old: dict[str, list[Entity]] = {}
    parse_fail: list[tuple[FileChange, str]] = []
    for fc in cs.files:
        if fc.binary:
            continue
        old_lang = fc.lang
        old_ents: list[Entity] = []
        new_ents: list[Entity] = []
        try:
            old_ents = extract(fc.old_path or fc.path, fc.old_text, old_lang, fc.category)
        except ExtractError:
            pass  # if the old version didn't parse, compare against nothing
        try:
            new_ents = extract(fc.path, fc.new_text, fc.lang, fc.category)
        except ExtractError as e:
            parse_fail.append((fc, str(e)))
            errors.append(str(e))
            continue
        for e in old_ents:
            e.file = fc.path
        per_file_new[fc.path] = new_ents
        per_file_old[fc.path] = old_ents
        all_changes += diff_entities(old_ents, new_ents, fc)

    all_changes = pair_moves(all_changes)
    changed_paths = {f.path for f in cs.files}
    if with_callers:
        try:
            find_callers(cs, all_changes, changed_paths)
        except Exception as e:  # pragma: no cover - never block a review on grep
            errors.append(f"caller search failed: {e}")

    tracked = git(cs.repo, "ls-files", check=False).splitlines()
    ctx = risk.Context(cs=cs, changes=all_changes, new_entities=per_file_new, old_entities=per_file_old,
                       tracked_files=tracked, parse_failures=parse_fail)
    risks = risk.run_all(ctx)

    # attach route→handler body changes so routes show "handler logic changed"
    fn_changes = {(c.file, c.name): c for c in all_changes if c.kind == "function"}
    for c in all_changes:
        if c.kind == "route" and c.after:
            h = (c.after.get("meta") or {}).get("handler")
            fcn = fn_changes.get((c.file, h))
            if fcn and fcn.body_changed:
                c.after["meta"]["handler_body_changed"] = True
                c.hunk_ids = sorted(set(c.hunk_ids) | set(fcn.hunk_ids))
    # surface routes whose only change is in their handler body
    route_names = {(c.file, c.name) for c in all_changes if c.kind == "route"}
    for fc_path, ents in per_file_new.items():
        for e in ents:
            if e.kind != "route" or (fc_path, e.name) in route_names:
                continue
            fcn = fn_changes.get((fc_path, e.meta.get("handler")))
            if fcn and fcn.status == "modified" and fcn.body_changed:
                d = e.to_dict()
                d["meta"]["handler_body_changed"] = True
                all_changes.append(EntityChange("modified", "route", e.name, fc_path, e.lang, e.framework, d, d,
                                                body_changed=True, hunk_ids=list(fcn.hunk_ids), line=e.line))

    files_out = []
    ranked = []
    for fc in cs.files:
        ents = [c for c in all_changes if c.file == fc.path]
        frisks = [r for r in risks if r.file == fc.path]
        score, reasons = file_rank(fc, ents, frisks)
        ranked.append((CATEGORY_ORDER.get(fc.category, 1), -score, fc.path))
        files_out.append({
            "path": fc.path, "old_path": fc.old_path, "status": fc.status, "lang": fc.lang, "category": fc.category,
            "additions": fc.additions, "deletions": fc.deletions, "binary": fc.binary,
            "hunks": trim_hunks_for_report(fc), "entity_ids": [c.id for c in ents], "risk_ids": [r.id for r in frisks],
            "score": round(score, 2), "reasons": reasons[:4],
        })
    ranked.sort()
    order = [p for _, _, p in ranked]
    rank_of = {p: i + 1 for i, p in enumerate(order)}
    for f in files_out:
        f["rank"] = rank_of[f["path"]]
    files_out.sort(key=lambda f: f["rank"])

    langs = Counter(f.lang for f in cs.files if f.lang not in ("other",))
    report: dict[str, Any] = {
        "version": 1,
        "id": short_hash(f"{cs.repo}|{cs.base_rev}|{cs.head_rev or 'wt'}|{time.time()}"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo": cs.repo.name,
        "repo_path": str(cs.repo),
        "base": {"ref": cs.base_ref, "rev": cs.base_rev[:10]},
        "head": {"ref": cs.head_ref, "rev": (cs.head_rev or "")[:10], "worktree": cs.worktree},
        "branch": git(cs.repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip(),
        "commits": cs.commits[:50],
        "intent": intent,
        "stats": {
            "files": len(cs.files),
            "additions": sum(f.additions for f in cs.files),
            "deletions": sum(f.deletions for f in cs.files),
            "languages": dict(langs),
            "categories": dict(Counter(f.category for f in cs.files)),
        },
        "summary": deterministic_summary(cs, all_changes, risks),
        "changes": [dict(c.__dict__, id=c.id) for c in all_changes],
        "risks": [dict(r.__dict__, id=r.id) for r in sorted(risks, key=lambda r: (-SEV_WEIGHT[r.severity], r.file, r.line))],
        "files": files_out,
        "reading_order": order,
        "llm": {"status": "skipped", "reason": "not run"},
        "errors": errors,
        "timing_ms": int((time.time() - t0) * 1000),
    }
    return report
