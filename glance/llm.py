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

Return ONLY a JSON object, no prose, matching:
{
  "summary": "2-4 sentences: what this change does and why it matters",
  "intent": {"asked": "...", "done": ["..."], "extra": [{"text": "...", "hunks": ["h_..."]}], "missing": ["..."]} | null,
  "business_logic": [{"title": "...", "before": "...", "after": "...", "why_it_matters": "...", "hunks": ["h_..."]}],
  "concerns": [{"severity": "high|medium|low", "title": "...", "detail": "...", "hunks": ["h_..."]}],
  "walkthrough": [{"step": "short sentence on what to read and why", "hunks": ["h_..."]}],
  "story": {"goal": "...", "how_it_works": [{"text": "...", "units": ["u1", "u4"]}],
            "chapters": {"database": "...", "routes": "...", "schemas": "...", "background": "...", "supporting": "..."},
            "units": {"u1": {"what": "...", "why": "..."}}}
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
                parts.append(f"{uid} [{u['status']} {u['kind']}] {u['name']} — {u['detail'][:160]}{extra} ({u['file']}:{u['line']}) hunks: {','.join(u['hunk_ids'][:6])}")
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
    msg = client.messages.create(model=model, max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content)


def ground(result: dict, valid: set[str], units: set[str] | None = None) -> tuple[dict, int]:
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
    result["story"] = ground_story(result.get("story"), units or set())
    return result, dropped


def ground_story(story: Any, units: set[str]) -> dict:
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
    out["units"] = {k: {"what": str(v.get("what") or ""), "why": str(v.get("why") or "")} for k, v in us.items() if k in units and isinstance(v, dict)}
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
            return {**json.loads(cache.read_text()), "cached": True}
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
    data, dropped = ground(data, valid, {u["id"] for u in (report.get("story") or {}).get("units", [])})
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
