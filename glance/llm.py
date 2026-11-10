"""Optional model pass: summary, intent check, business-logic changes and concerns.

Everything the model says must cite hunk ids from the diff; ungrounded items are dropped.

Providers (auto-detected in this order):
  claude-cli  -> `claude -p` in the repo, read-only tools, so it sees CLAUDE.md and can look around
  anthropic   -> Anthropic SDK with ANTHROPIC_API_KEY
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_MODEL = os.environ.get("GLANCE_MODEL", "claude-sonnet-5-5")
DIFF_BUDGET = int(os.environ.get("GLANCE_DIFF_BUDGET", "160000"))

INSTRUCTIONS = """You are reviewing a code change (often AI-written) for a busy engineer who wants to understand
it WITHOUT reading every line. Static analysis already extracted the structural facts (schemas, models,
routes, migrations, tasks, signatures) and heuristic risk flags; they're given below. Your job is the part
static analysis can't do: explain intent and business logic, and spot problems.

Rules:
- Every business_logic item and every concern MUST cite one or more hunk ids exactly as written in the diff
  (e.g. "h_1a2b3c4d5e6f"). Items without valid hunk ids are discarded.
- Business logic = behavior a product person would care about: rules, thresholds, calculations, state
  transitions, permissions, what happens on failure. Not refactors, renames or formatting.
- Describe before → after concretely ("fee was 1% flat; now 0.5% under $10k and 1% above").
- Concerns = real bugs or risks you can point to (wrong logic, missing edge case, race, unhandled error,
  mismatch between code and the stated intent). Don't repeat the static risk flags unless you add something.
  Don't pad: zero concerns is a fine answer.
- If an intent/task is given, compare it to what the diff actually does: what was asked, what was done,
  what was done that wasn't asked (scope creep), and what was asked but is missing. If NO intent is given,
  return "intent": null. Never infer the task from commit messages or branch names.
- Be terse. Plain English. No markdown inside strings.

Decisions are the judgement calls in this change: places where a competent engineer could reasonably have gone
another way, and the choice has consequences a reviewer should consciously agree to. Reviewers skim code and
approve it without noticing these, so surface them. Look for: how data is modeled (new table vs. column, JSON
blob vs. columns, denormalized copies, nullable vs. default, enum vs. string), sync vs. background work, where
validation or authorization lives, error handling and retry strategy (fail, swallow, retry, partial writes),
idempotency and concurrency, caching, what is computed on write vs. on read, library or API choices, what was
left out of scope, and behavior at boundaries. For each:
- title: the choice as a short claim ("Margin is checked nightly, not on each trade").
- chose: what the code does, concretely.
- alternative: the most plausible other way to do it (a real option, not a straw man).
- tradeoff: what this choice gains and what it costs or risks, concretely (when does it bite, who notices).
- weight: "high" if getting it wrong costs money, data, security or a painful migration later; "medium" if it
  shapes future work; "low" otherwise.
- hunks: the hunk ids where the choice is made. units: the story unit ids involved (may be empty).
Not decisions: naming, formatting, following an existing pattern in the codebase, anything with only one sane
answer. 0-6 decisions, most consequential first; zero is fine for a mechanical change.

Scenarios show behavior instead of describing it. For the 1-5 most important behavior changes (rules,
thresholds, calculations, state transitions, permissions, failure handling), give a table of concrete inputs
and what happens before and after this change:
- title: the behavior ("Late fee calculation"). subject: what the inputs go into, as code
  ("late_fee(balance, days_late)" or "POST /loans/{id}/repay"). rule: one sentence the table illustrates.
- rows: 3-8 cases. Pick inputs that reveal the rule: a typical case, each boundary where the outcome flips
  (exactly at the threshold and just either side), zero/empty/null, and the failure path. Each row is
  {"case": the input in plain words, "input": the actual argument values as a JSON object (null if it isn't a
  plain function call), "before": the outcome under the old code, "after": the outcome under the new code}.
- Outcomes are concrete: numbers, states, status codes, which rows get written, what error is raised. Write
  "n/a (didn't exist)" for before when the behavior is new and "n/a (removed)" for after when it's gone.
- Work outcomes out by reading the code. Never guess: if the code doesn't determine it, write "unclear: <why>".
- hunks: where the new behavior lives. units: story unit ids involved (may be empty).
Skip scenarios for pure refactors, plumbing and CRUD with no rules.

Questions check that the reviewer actually understands the change. Write 3-8 questions someone must be able
to answer before merging this safely: about behavior, failure modes, data and the decisions above, not trivia
or names ("What happens to an account that drops below 25% between nightly runs?", "If the Stripe call fails
after the row is written, what state is the order left in?"). Each {"q": "...", "answer": "1-3 concrete
sentences, citing the actual behavior", "hunks": ["h_..."]}. Prefer questions whose answer would surprise
someone who skimmed the diff.

The "story" explains the change the way a teammate would walk someone through a feature: first the goal, then
the records we store, then the routes that touch them, then the schemas those routes use, then the background
work. Static analysis already sorted the change into "Story units" (listed below, ids like "u3"); you write
the words. For "story":
- goal: 2-4 sentences a product manager would understand. The user-facing or business outcome, not the code.
- how_it_works: 3-7 steps tracing the flow end to end, in the order it happens (e.g. "When a user signs up,
  they're enrolled in the Base plan (UserSubscription row)"). Each step cites the unit ids it involves.
- chapters: one sentence per chapter id (database, routes, schemas, background, supporting) saying what that
  part contributes to the goal. Omit chapters with no units.
- units: for EVERY unit id in the database, routes, schemas and background chapters (and the supporting ones
  that matter), {"what": what it is or does, one plain sentence; "why": why this change needs it, one sentence}.
  For a removed unit, say what it did and what replaces it (or that nothing does). Only use listed unit ids.
- Triage, so a reviewer only reads what needs a human. Each unit lists a default ("read" or "routine").
  In units, add "attention": "read" | "routine" and, for read, "read_reason": what is custom, subtle or risky
  here. Routine = standard, mechanical or obvious (plumbing, renames, UI copy, simple CRUD, passing a value
  through): the reviewer will see it pre-approved. Read = custom business logic, money math, state
  transitions, concurrency/idempotency, auth, data migrations, anything a wrong line would cost money or data.
  Be decisive: most units in a typical change are routine.
- read_first: the 3-8 spots a human must actually read to approve this change, most important first. Each is
  {"unit": "u..", "hunk": "h_..", "from": N, "to": M, "why": "one sentence"} where from/to are NEW-file line
  numbers inside that hunk, at most 25 lines. Point at the decisive lines, not whole functions.

Return ONLY a JSON object, no prose, matching:
{
  "summary": "2-4 sentences: what this change does and why it matters",
  "intent": {"asked": "...", "done": ["..."], "extra": [{"text": "...", "hunks": ["h_..."]}], "missing": ["..."]} | null,
  "business_logic": [{"title": "...", "before": "...", "after": "...", "why_it_matters": "...", "hunks": ["h_..."]}],
  "concerns": [{"severity": "high|medium|low", "title": "...", "detail": "...", "hunks": ["h_..."]}],
  "walkthrough": [{"step": "short sentence on what to read and why", "hunks": ["h_..."]}],
  "decisions": [{"title": "...", "chose": "...", "alternative": "...", "tradeoff": "...", "weight": "high|medium|low",
                 "hunks": ["h_..."], "units": ["u.."]}],
  "scenarios": [{"title": "...", "subject": "...", "rule": "...", "hunks": ["h_..."], "units": ["u.."],
                 "rows": [{"case": "...", "input": {} | null, "before": "...", "after": "..."}]}],
  "questions": [{"q": "...", "answer": "...", "hunks": ["h_..."]}],
  "story": {"goal": "...", "how_it_works": [{"text": "...", "units": ["u1", "u4"]}],
            "chapters": {"database": "...", "routes": "...", "schemas": "...", "background": "...", "supporting": "..."},
            "units": {"u1": {"what": "...", "why": "...", "attention": "read|routine", "read_reason": "..."}},
            "read_first": [{"unit": "u1", "hunk": "h_...", "from": 10, "to": 22, "why": "..."}]}
}
walkthrough = the 3-8 hunks to read, in order, to understand the change."""


def available_provider(preferred: str = "auto") -> str | None:
    if preferred == "none":
        return None
    if preferred in ("auto", "claude-cli") and shutil.which("claude"):
        return "claude-cli"
    if preferred in ("auto", "anthropic") and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "anthropic"
        except ImportError:
            return None
    return None


def _compact_change(c: dict) -> str:
    bits = [f"[{c['kind']} {c['status']}] {c['name']} ({c['file']}:{c['line']})"]
    if c.get("signature_changed"):
        bits.append(f"sig: {(c.get('before') or {}).get('signature')} → {(c.get('after') or {}).get('signature')}")
    fc = c.get("field_changes") or []
    if fc and c["kind"] != "function":
        bits.append("fields: " + "; ".join(
            f"{'+' if f['change']=='added' else '-' if f['change']=='removed' else '~'}{f['name']}"
            + (f" {f['before']} → {f['after']}" if f['change'] == 'changed' else f" {f.get('after') or f.get('before') or ''}")
            for f in fc[:12]))
    for m in (c.get("meta_changes") or [])[:4]:
        bits.append(f"{m['key']}: {json.dumps(m['before'])[:120]} → {json.dumps(m['after'])[:120]}")
    if c["kind"] == "migration":
        ops = ((c.get("after") or {}).get("meta") or {}).get("upgrade", [])
        bits.append("ops: " + "; ".join(f"{o['op']} {o.get('table') or ''}{'.' + o['column'] if o.get('column') else ''}" for o in ops[:12]))
    if c.get("callers"):
        bits.append(f"callers: {len(c['callers'])}")
    bits.append("hunks: " + ",".join(c.get("hunk_ids") or []))
    return " | ".join(bits)


def build_prompt(report: dict) -> str:
    parts = [INSTRUCTIONS, ""]
    if report.get("intent"):
        parts += ["## Intent / task given for this change", report["intent"].strip()[:8000], ""]
    else:
        parts += ["## Intent / task given for this change", "(none given: return \"intent\": null)", ""]
    if report.get("commits"):
        parts += ["## Commit messages", "\n".join("- " + c.splitlines()[0] for c in report["commits"][:30]), ""]
    parts += ["## Static summary", report["summary"]["headline"], *("- " + b for b in report["summary"]["bullets"]), ""]
    changes = [c for c in report["changes"] if not (c["kind"] == "function" and c["status"] == "modified" and not c.get("body_changed") and not c.get("signature_changed"))]
    parts += ["## Structural changes", *(_compact_change(c) for c in changes[:250]), ""]
    story = report.get("story") or {}
    if story.get("units"):
        by_id = {u["id"]: u for u in story["units"]}
        parts.append("## Story units (write story.units for these ids)")
        for ch in story["chapters"]:
            if not ch["unit_ids"]:
                continue
            parts.append(f"### {ch['id']} — {ch['title']}")
            for uid in ch["unit_ids"][:120]:
                u = by_id[uid]
                extra = " (used by a changed route)" if u.get("used_by_routes") else ""
                parts.append(f"{uid} [{u['status']} {u['kind']}] {u['name']} — {u['detail'][:160]}{extra} ({u['file']}:{u['line']}, {u.get('churn', 0)} changed lines) default: {u.get('attention', 'read')} ({u.get('attention_why', '')}) hunks: {','.join(u['hunk_ids'][:6])}")
        if story.get("test_files"):
            parts.append(f"(plus {len(story['test_files'])} test files, not listed)")
        parts.append("")
    if report["risks"]:
        parts += ["## Static risk flags", *(f"- [{r['severity']}] {r['title']} ({r['file']}:{r['line']})" for r in report["risks"][:60]), ""]
    parts += ["## Diff (hunk ids in brackets; files in suggested reading order)"]
    used = sum(len(p) for p in parts)
    files = sorted(report["files"], key=lambda f: f["rank"])
    skipped = []
    for f in files:
        if f["category"] in ("lockfile", "generated") or not f["hunks"]:
            continue
        block = [f"\n### {f['path']} ({f['status']}, {f['category']})"]
        for h in f["hunks"]:
            block.append(f"[{h['id']}] @@ -{h['old_start']},{h['old_len']} +{h['new_start']},{h['new_len']} @@ {h['header']}")
            for l in h["lines"][:220]:
                block.append(f"{l[0]}{l[3]}")
            if len(h["lines"]) > 220:
                block.append(f"… ({len(h['lines']) - 220} more lines)")
        text = "\n".join(block)
        if used + len(text) > DIFF_BUDGET:
            skipped.append(f["path"])
            continue
        parts.append(text)
        used += len(text)
    if skipped:
        parts.append(f"\n(Diff omitted for budget: {', '.join(skipped[:30])})")
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("model did not return JSON")
    return json.loads(text[start:end + 1])


def _call_claude_cli(prompt: str, repo: str, model: str | None, timeout: int) -> str:
    cmd = ["claude", "-p", "--output-format", "json",
           "--allowedTools", "Read,Grep,Glob", "--disallowedTools", "Edit,Write,Bash,NotebookEdit"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=repo, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "claude exited non-zero").strip()[:500])
    out = proc.stdout.strip()
    try:
        env = json.loads(out)
        if isinstance(env, dict) and env.get("is_error"):
            raise RuntimeError(str(env.get("result"))[:500])
        return env.get("result", out) if isinstance(env, dict) else out
    except json.JSONDecodeError:
        return out


def _call_anthropic(prompt: str, model: str, timeout: int) -> str:
    import anthropic

    client = anthropic.Anthropic(timeout=timeout)
    msg = client.messages.create(model=model, max_tokens=16000, messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content)


def ground(result: dict, valid: set[str], units: set[str] | None = None, hunk_index: dict[str, dict] | None = None) -> tuple[dict, int]:
    dropped = 0

    def keep(items: list | None) -> list:
        nonlocal dropped
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            hs = [h for h in (it.get("hunks") or []) if h in valid]
            if not hs:
                dropped += 1
                continue
            it["hunks"] = hs
            out.append(it)
        return out

    result["business_logic"] = keep(result.get("business_logic"))
    result["concerns"] = keep(result.get("concerns"))
    result["walkthrough"] = keep(result.get("walkthrough"))
    raw_decisions = [d for d in result.get("decisions") or [] if isinstance(d, dict)]
    result["decisions"] = ground_decisions(keep(raw_decisions), units or set())
    result["scenarios"] = ground_scenarios(keep(result.get("scenarios")), units or set())
    result["questions"] = ground_questions(keep(result.get("questions")))
    intent = result.get("intent")
    if isinstance(intent, dict):
        extra = []
        for it in intent.get("extra") or []:
            if isinstance(it, str):
                it = {"text": it, "hunks": []}
            it["hunks"] = [h for h in it.get("hunks") or [] if h in valid]
            extra.append(it)
        intent["extra"] = extra
    for c in result["concerns"]:
        if c.get("severity") not in ("high", "medium", "low"):
            c["severity"] = "medium"
    result["story"] = ground_story(result.get("story"), units or set(), hunk_index)
    return result, dropped


def _decision_id(d: dict) -> str:
    return "d_" + hashlib.sha1(f"{d.get('title')}|{d.get('chose')}".encode()).hexdigest()[:10]


def ground_decisions(items: list, units: set[str]) -> list[dict]:
    """Decisions arrive already filtered to ones citing real hunks; normalise fields and cap the list."""
    out = []
    for d in items:
        if not str(d.get("title") or "").strip():
            continue
        out.append({
            "title": str(d["title"]).strip()[:200],
            "chose": str(d.get("chose") or "").strip()[:600],
            "alternative": str(d.get("alternative") or "").strip()[:600],
            "tradeoff": str(d.get("tradeoff") or "").strip()[:800],
            "weight": d.get("weight") if d.get("weight") in ("high", "medium", "low") else "medium",
            "hunks": d["hunks"],
            "units": [u for u in d.get("units") or [] if u in units],
        })
    return out[:6]


def _same(a: str, b: str) -> bool:
    norm = lambda x: re.sub(r"[\s.]+$", "", re.sub(r"\s+", " ", x.strip().lower()))
    return norm(a) == norm(b)


def ground_scenarios(items: list, units: set[str]) -> list[dict]:
    """Behavior tables: drop empty rows, mark rows whose outcome actually changed, cap sizes."""
    out = []
    for sc in items:
        rows = []
        for r in sc.get("rows") or []:
            if not isinstance(r, dict) or not str(r.get("case") or "").strip():
                continue
            before, after = str(r.get("before") or "").strip()[:300], str(r.get("after") or "").strip()[:300]
            inp = r.get("input") if isinstance(r.get("input"), dict) else None
            rows.append({"case": str(r["case"]).strip()[:300], "input": inp, "before": before or "—", "after": after or "—",
                         "changed": not _same(before, after),
                         "unclear": before.lower().startswith("unclear") or after.lower().startswith("unclear")})
        if not rows or not str(sc.get("title") or "").strip():
            continue
        item = {"title": str(sc["title"]).strip()[:200], "subject": str(sc.get("subject") or "").strip()[:200],
                "rule": str(sc.get("rule") or "").strip()[:400], "rows": rows[:10], "hunks": sc["hunks"],
                "units": [u for u in sc.get("units") or [] if u in units]}
        item["id"] = "s_" + hashlib.sha1(f"{item['title']}|{item['subject']}".encode()).hexdigest()[:10]
        out.append(item)
    return out[:5]


def ground_questions(items: list) -> list[dict]:
    out = []
    for q in items:
        text = str(q.get("q") or "").strip()
        if not text:
            continue
        out.append({"id": "q_" + hashlib.sha1(text.encode()).hexdigest()[:10], "q": text[:400],
                    "answer": str(q.get("answer") or "").strip()[:900], "hunks": q["hunks"]})
    return out[:8]


def attach_evidence(result: dict, report: dict) -> dict:
    """Give each concern a stable id, its files, and the cited code, so it can be re-checked after edits.
    Decisions get a stable id and a fingerprint too, so a reviewer's verdict can be flagged when the code moves."""
    hunks = {h["id"]: (f["path"], h) for f in report["files"] for h in f["hunks"]}
    for d in result.get("decisions") or []:
        d["id"] = _decision_id(d)
    for key in ("decisions", "scenarios", "questions"):
        for d in result.get(key) or []:
            d["fingerprint"] = {hid: _hunk_print(hunks[hid][1]) for hid in d["hunks"] if hid in hunks}
    for c in result.get("concerns") or []:
        c["id"] = "c_" + hashlib.sha1(f"{c.get('title')}|{c.get('detail')}".encode()).hexdigest()[:10]
        c["files"] = sorted({hunks[h][0] for h in c["hunks"] if h in hunks})
        lines = []
        for hid in c["hunks"][:4]:
            path, h = hunks[hid]
            lines.append(f"--- {path} @@ +{h['new_start']} @@")
            lines += [f"{l[0]}{l[3]}" for l in h["lines"][:80]]
        c["evidence"] = "\n".join(lines)[:12000]
        c["fingerprint"] = {hid: _hunk_print(hunks[hid][1]) for hid in c["hunks"] if hid in hunks}
    return result


def _hunk_print(h: dict) -> str:
    """Content fingerprint: hunk ids are positional (stable for citations), so edits inside a hunk keep its id."""
    return hashlib.sha1(json.dumps([[l[0], l[3]] for l in h["lines"]]).encode()).hexdigest()[:12]


def carry_over(old: dict, new: dict) -> dict:
    """After a refresh: keep the previous model pass instead of paying for a new one. Prose still points at
    hunks that exist; concerns whose cited code changed are marked stale so they can be re-checked one by one.
    Story prose is moved onto the new unit ids by (chapter, name)."""
    prev = old.get("llm") or {}
    if prev.get("status") != "ok":
        return {"status": "skipped", "reason": "not run"}
    valid = {h["id"] for f in new["files"] for h in f["hunks"]}
    prints = {h["id"]: _hunk_print(h) for f in new["files"] for h in f["hunks"]}
    out = json.loads(json.dumps(prev))           # deep copy
    for c in out.get("concerns") or []:
        c.setdefault("id", "c_" + hashlib.sha1(f"{c.get('title')}|{c.get('detail')}".encode()).hexdigest()[:10])
        if c.get("verdict") == "fixed":
            continue
        kept = [h for h in c.get("hunks") or [] if h in valid]
        edited = any(prints.get(h) != fp for h, fp in (c.get("fingerprint") or {}).items())
        if len(kept) < len(c.get("hunks") or []) or edited:
            c["stale"] = True
        c["hunks"] = kept
    for key in ("decisions", "scenarios", "questions"):
        if key not in out:             # absent = an AI pass from before this feature existed
            continue
        for d in out[key]:
            if key == "decisions":
                d.setdefault("id", _decision_id(d))
            kept = [h for h in d.get("hunks") or [] if h in valid]
            if len(kept) < len(d.get("hunks") or []) or any(prints.get(h) != fp for h, fp in (d.get("fingerprint") or {}).items()):
                d["stale"] = True      # the code behind it changed since it was written (and since the reviewer saw it)
            d["hunks"] = kept
        out[key] = [d for d in out[key] if d["hunks"]]
    for key in ("business_logic", "walkthrough"):
        out[key] = [dict(it, hunks=[h for h in it.get("hunks") or [] if h in valid]) for it in out.get(key) or []]
        out[key] = [it for it in out[key] if it["hunks"]]
    old_units = {u["id"]: (u["chapter"], u["name"]) for u in (old.get("story") or {}).get("units", [])}
    new_ids = {(u["chapter"], u["name"]): u["id"] for u in (new.get("story") or {}).get("units", [])}
    remap = {oid: new_ids[k] for oid, k in old_units.items() if k in new_ids}
    st = out.get("story") or {}
    if st:
        st["units"] = {remap[k]: v for k, v in (st.get("units") or {}).items() if k in remap}
        st["how_it_works"] = [dict(s2, units=[remap[u] for u in s2.get("units", []) if u in remap]) for s2 in st.get("how_it_works") or []]
        st["how_it_works"] = [s2 for s2 in st["how_it_works"] if s2["units"]]
        st["read_first"] = [dict(sp, unit=remap[sp["unit"]]) for sp in st.get("read_first") or []
                            if sp.get("unit") in remap and sp.get("hunk") in valid]
    if old.get("intent") != new.get("intent"):
        out["intent"] = None
    out["carried"] = True
    return out


def resolved_since(old: dict, new: dict) -> list[dict]:
    """Static risks that were in the previous report and aren't any more (matched on rule, file and title,
    since line numbers move when you edit)."""
    key = lambda r: (r["rule"], r["file"], r["title"])
    now = {key(r) for r in new.get("risks", [])}
    return [{"title": r["title"], "file": r["file"], "severity": r["severity"], "rule": r["rule"]}
            for r in old.get("risks", []) if key(r) not in now]


RECHECK = """A reviewer flagged a problem in a code change. The author has since edited the code. Decide whether the
problem is still there, using the CURRENT code below (you may also Read/Grep files in the repo).

Return ONLY JSON: {"verdict": "fixed" | "still_present" | "partly_fixed", "explanation": "1-2 plain sentences:
what changed and why that does or doesn't resolve it", "hunks": ["h_..."]}
"hunks" = ids from the CURRENT diff that matter for your answer (may be empty if fixed by removing code)."""


def recheck(report: dict, concern: dict, provider: str = "auto", model: str | None = None, timeout: int = 240) -> dict:
    """Targeted model call for one concern against the current diff: much smaller and faster than a full pass."""
    prov = available_provider(provider)
    if prov is None:
        return {"error": "no `claude` CLI on PATH and no ANTHROPIC_API_KEY"}
    files = set(concern.get("files") or [])
    parts = [RECHECK, "", "## The problem as originally reported",
             f"[{concern.get('severity')}] {concern.get('title')}", str(concern.get('detail') or ""), "",
             "## Code it was found in (at the time)", concern.get("evidence") or "(not recorded)", "",
             "## Current diff of those files (hunk ids in brackets)"]
    for f in report["files"]:
        if f["path"] in files or (not files):
            parts.append(f"\n### {f['path']}")
            for h in f["hunks"]:
                parts.append(f"[{h['id']}] @@ -{h['old_start']},{h['old_len']} +{h['new_start']},{h['new_len']} @@ {h['header']}")
                parts += [f"{l[0]}{l[3]}" for l in h["lines"][:300]]
    missing = files - {f["path"] for f in report["files"]}
    if missing:
        parts.append(f"\n(These files no longer differ from the base branch: {', '.join(sorted(missing))})")
    prompt = "\n".join(parts)[:DIFF_BUDGET]
    try:
        raw = _call_claude_cli(prompt, report["repo_path"], model, timeout) if prov == "claude-cli" else _call_anthropic(prompt, model or DEFAULT_MODEL, timeout)
        data = _extract_json(raw)
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)[:300]}
    valid = {h["id"] for f in report["files"] for h in f["hunks"]}
    verdict = data.get("verdict") if data.get("verdict") in ("fixed", "still_present", "partly_fixed") else "still_present"
    return {"verdict": verdict, "explanation": str(data.get("explanation") or "")[:600],
            "hunks": [h for h in data.get("hunks") or [] if h in valid]}


def ground_story(story: Any, units: set[str], hunks: dict[str, dict] | None = None) -> dict:
    """Keep only prose tied to real story units: unknown unit ids and uncited flow steps are dropped."""
    if not isinstance(story, dict):
        return {}
    out: dict[str, Any] = {"goal": str(story.get("goal") or "").strip()}
    steps = []
    for st in story.get("how_it_works") or []:
        if isinstance(st, dict) and st.get("text"):
            ids = [u for u in st.get("units") or [] if u in units]
            if ids:
                steps.append({"text": str(st["text"]), "units": ids})
    out["how_it_works"] = steps
    chapters = story.get("chapters") if isinstance(story.get("chapters"), dict) else {}
    out["chapters"] = {k: str(v) for k, v in chapters.items() if k in ("database", "routes", "schemas", "background", "supporting") and v}
    us = story.get("units") if isinstance(story.get("units"), dict) else {}
    out["units"] = {}
    for k, v in us.items():
        if k in units and isinstance(v, dict):
            out["units"][k] = {"what": str(v.get("what") or ""), "why": str(v.get("why") or "")}
            if v.get("attention") in ("read", "routine"):
                out["units"][k]["attention"] = v["attention"]
                out["units"][k]["read_reason"] = str(v.get("read_reason") or "")[:300]
    spots = []
    for sp in story.get("read_first") or []:
        if not isinstance(sp, dict) or sp.get("unit") not in units or (hunks is not None and sp.get("hunk") not in hunks):
            continue
        try:
            a, b = int(sp.get("from") or 0), int(sp.get("to") or 0)
        except (TypeError, ValueError):
            a = b = 0
        if hunks is not None:
            h = hunks[sp["hunk"]]
            lo, hi = h["new_start"], h["new_start"] + max(h["new_len"], 1) - 1
            a, b = (max(lo, min(a, hi)), max(lo, min(b, hi))) if a and b else (lo, min(hi, lo + 24))
            a, b = min(a, b), max(a, b)
            b = min(b, a + 39)
        spots.append({"unit": sp["unit"], "hunk": sp.get("hunk"), "from": a, "to": b, "why": str(sp.get("why") or "")[:300]})
    out["read_first"] = spots[:8]
    return out


def run(report: dict, provider: str = "auto", model: str | None = None, timeout: int = 420) -> dict[str, Any]:
    prov = available_provider(provider)
    if prov is None:
        reason = "disabled" if provider == "none" else "no `claude` CLI on PATH and no ANTHROPIC_API_KEY"
        return {"status": "skipped", "reason": reason}
    prompt = build_prompt(report)
    valid = {h["id"] for f in report["files"] for h in f["hunks"]}
    # Same diff + same task + same model = same answer: reuse it instead of paying minutes and tokens again.
    cache = Path(report["repo_path"]) / ".glance" / "llm-cache" / (hashlib.sha256(f"{prov}|{model}|{prompt}".encode()).hexdigest()[:24] + ".json")
    if cache.is_file():
        try:
            return attach_evidence({**json.loads(cache.read_text()), "cached": True}, report)
        except (OSError, ValueError):
            pass
    try:
        if prov == "claude-cli":
            raw = _call_claude_cli(prompt, report["repo_path"], model, timeout)
        else:
            raw = _call_anthropic(prompt, model or DEFAULT_MODEL, timeout)
        data = _extract_json(raw)
    except subprocess.TimeoutExpired:
        return {"status": "error", "provider": prov, "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"status": "error", "provider": prov, "error": str(e)[:500]}
    if not report.get("intent"):
        data["intent"] = None          # the scope check only means something against a stated task
    data, dropped = ground(data, valid, {u["id"] for u in (report.get("story") or {}).get("units", [])},
                           {h["id"]: h for f in report["files"] for h in f["hunks"]})
    attach_evidence(data, report)
    result = {"status": "ok", "provider": prov, "model": model or ("default" if prov == "claude-cli" else DEFAULT_MODEL),
              "prompt_chars": len(prompt), "dropped_ungrounded": dropped, **data}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result))
        for old in sorted(cache.parent.glob("*.json"), key=lambda q: q.stat().st_mtime)[:-20]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return result
