"""glance CLI.

  glance                          review working tree (incl. uncommitted) against main, open the UI
  glance feature/x                review a branch against main
  glance --base develop HEAD~3    explicit base / head
  glance --intent "Add margin call emails when equity < 25%"
  glance --intent-file task.md    compare the change to a task / plan (e.g. a Ranger plan JSON)
  glance --format md              print markdown (for Claude Code, PR descriptions)
  glance --format json            print the report path
  glance serve                    reopen the last report
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path

from . import __version__, llm
from .analyze import analyze
from .git import GitError, collect, git, list_branches, ref_exists, repo_root
from .render_md import render
from .server import State, serve


def _ensure_excluded(repo: Path) -> None:
    """Keep .glance/ out of git without touching the user's .gitignore."""
    gd = git(repo, "rev-parse", "--git-common-dir", check=False).strip()
    if not gd:
        return
    excl = (repo / gd / "info" / "exclude") if not Path(gd).is_absolute() else Path(gd) / "info" / "exclude"
    try:
        excl.parent.mkdir(parents=True, exist_ok=True)
        cur = excl.read_text() if excl.exists() else ""
        if ".glance/" not in cur:
            excl.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + ".glance/\n")
    except OSError:
        pass


def _write(repo: Path, report: dict) -> Path:
    out = repo / ".glance"
    out.mkdir(exist_ok=True)
    p = out / "latest.json"
    p.write_text(json.dumps(report, indent=1))
    hist = out / "history"
    hist.mkdir(exist_ok=True)
    (hist / f"{time.strftime('%Y%m%d-%H%M%S')}-{report['id']}.json").write_text(json.dumps(report))
    for old in sorted(hist.glob("*.json"))[:-30]:
        old.unlink(missing_ok=True)
    return p


def export_html(report: dict) -> str:
    """Self-contained review page with the report embedded (no server needed)."""
    from importlib import resources

    html = resources.files("glance").joinpath("ui/index.html").read_text()
    data = json.dumps(report).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return html.replace("<script>\n(() => {", f"<script>window.__GLANCE_REPORT__ = {data};</script>\n<script>\n(() => {{", 1)


def intent_path(repo: Path, args) -> Path:
    """Where the UI saves "what was this change supposed to do?" — one file per branch."""
    ref = (args.head or git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip()      # works before the first commit
           or git(repo, "rev-parse", "--short", "HEAD", check=False).strip() or "HEAD")              # detached: the commit
    return repo / ".glance" / "intent" / (re.sub(r"[^A-Za-z0-9._-]+", "_", ref) + ".md")


def _read_intent(args, repo: Path) -> str | None:
    parts = []
    if args.intent:
        parts.append(args.intent)
    if args.intent_file:
        parts.append(Path(args.intent_file).read_text(errors="replace"))
    if parts:
        return "\n\n".join(parts)
    saved = intent_path(repo, args)
    return (saved.read_text(errors="replace").strip() or None) if saved.is_file() else None


def build(args, repo: Path) -> dict:
    cs = collect(repo, base=args.base, head=args.head, include_untracked=not args.no_untracked)
    return analyze(cs, with_callers=not args.no_callers, intent=_read_intent(args, repo))


def cmd_review(args) -> int:
    try:
        repo = repo_root(args.repo)
    except GitError:
        print(f"glance: {args.repo} is not inside a git repository", file=sys.stderr)
        return 2
    _ensure_excluded(repo)
    t0 = time.time()
    try:
        report = build(args, repo)
    except GitError as e:
        print(f"glance: {e}", file=sys.stderr)
        return 2
    if not report["files"]:
        print(f"glance: no changes between {report['base']['ref']} and {report['head']['ref']}.")
        return 0
    if args.format != "ui":
        print(f"glance: analyzed {report['stats']['files']} files in {time.time() - t0:.1f}s", file=sys.stderr)

    run_llm = llm.available_provider(args.llm) is not None
    if args.format in ("md", "json", "html"):
        if run_llm:
            print(f"glance: asking {llm.available_provider(args.llm)} to read the diff…", file=sys.stderr)
            report["llm"] = llm.run(report, provider=args.llm, model=args.model)
        else:
            report["llm"] = llm.run(report, provider="none" if args.llm == "none" else args.llm)
        path = _write(repo, report)
        if args.format == "md":
            print(render(report))
        elif args.format == "html":
            out = repo / ".glance" / "report.html"
            out.write_text(export_html(report))
            print(out)
        else:
            print(path if not args.stdout else json.dumps(report, indent=1))
        return 0

    # UI: show the static report immediately, fill in the model pass when it lands.
    state = State(report)

    def model_pass(rep: dict) -> None:
        rep["llm"] = {"status": "running", "provider": llm.available_provider(args.llm)}
        state.set(rep)
        result = llm.run(rep, provider=args.llm, model=args.model)
        cur = dict(state.get())
        if cur.get("id") != rep["id"]:
            return                                  # a refresh replaced the report; its own pass will run
        if cur.get("intent") and cur.get("intent") != rep.get("intent"):
            return model_pass(cur)                  # the task was set mid-run: answer against the new one
        if not cur.get("intent") and isinstance(result.get("intent"), dict):
            result = {**result, "intent": None}     # the task was removed mid-run
        cur["llm"] = result                         # merge into the live report, never overwrite newer state
        state.set(cur)
        _write(repo, cur)

    def rebuild() -> None:
        """Re-analyze (seconds). Static risks are recomputed; the AI pass is carried forward, not re-run:
        concerns whose code changed are marked stale for a one-by-one re-check."""
        old = state.get()
        rep = build(args, repo)
        rep["resolved"] = llm.resolved_since(old, rep)
        rep["llm"] = llm.carry_over(old, rep)
        state.set(rep)
        _write(repo, rep)
        if run_llm and rep["llm"].get("status") != "ok":
            model_pass(rep)

    state.rebuild = rebuild

    # The UI's "Generate AI summary" button: works even when started with --llm none.
    ui_provider = args.llm if args.llm != "none" else "auto"

    def run_model_now() -> bool:
        nonlocal run_llm
        if llm.available_provider(ui_provider) is None or (state.get().get("llm") or {}).get("status") == "running":
            return False
        args.llm = ui_provider
        run_llm = True          # later refreshes keep the model pass on
        rep = dict(state.get())
        rep["llm"] = {"status": "running", "provider": llm.available_provider(ui_provider)}
        state.set(rep)          # mark running before the thread starts, so a second click gets 409
        threading.Thread(target=model_pass, args=(rep,), daemon=True).start()
        return True

    state.run_model = run_model_now

    def set_intent(text: str) -> str:
        """Save the task for this branch; re-run the model pass against it (or clear the old intent check)."""
        text = text.strip()
        p = intent_path(repo, args)
        p.parent.mkdir(parents=True, exist_ok=True)
        if text:
            p.write_text(text + "\n")
        else:
            p.unlink(missing_ok=True)
        args.intent, args.intent_file = None, None      # the saved file is now the source of truth
        rep = dict(state.get())
        rep["intent"] = text or None
        if not text:
            rep["llm"] = {**(rep.get("llm") or {}), "intent": None}
            state.set(rep)
            return "cleared"
        state.set(rep)
        if (rep.get("llm") or {}).get("status") == "running":
            return "running"                        # the pass in flight re-runs against the new task
        return "running" if run_model_now() else "saved"

    state.set_intent = set_intent

    def recheck(concern_id: str) -> bool:
        """Re-check one AI concern against the current code, in the background."""
        cur = state.get()
        concerns = (cur.get("llm") or {}).get("concerns") or []
        target = next((c for c in concerns if c.get("id") == concern_id), None)
        if target is None or target.get("rechecking") or llm.available_provider(ui_provider) is None:
            return False

        def mutate(fn) -> None:
            rep = dict(state.get())
            lm = dict(rep.get("llm") or {})
            lm["concerns"] = [fn(dict(c)) if c.get("id") == concern_id else c for c in lm.get("concerns") or []]
            lm["rechecking"] = [c["id"] for c in lm["concerns"] if c.get("rechecking")]
            rep["llm"] = lm
            state.set(rep)

        mutate(lambda c: {**c, "rechecking": True})

        def work() -> None:
            res = llm.recheck(state.get(), target, provider=ui_provider, model=args.model)

            def apply(c: dict) -> dict:
                c.pop("rechecking", None)
                if res.get("error"):
                    c["recheck_error"] = res["error"]
                    return c
                c.pop("recheck_error", None)
                c.update(verdict=res["verdict"], recheck_note=res["explanation"], stale=False,
                         checked_at=time.strftime("%H:%M"))
                if res["hunks"]:
                    c["hunks"] = res["hunks"]
                cur_files = {f["path"]: f for f in state.get()["files"]}
                live = {h["id"]: h for f in cur_files.values() for h in f["hunks"]}
                c["fingerprint"] = {h: llm._hunk_print(live[h]) for h in c.get("hunks") or [] if h in live}  # baseline for the next edit
                return c

            mutate(apply)
            _write(repo, state.get())

        threading.Thread(target=work, daemon=True).start()
        return True

    state.recheck = recheck

    def branches() -> dict:
        cur = state.get()
        return {"branches": list_branches(repo), "base": cur["base"]["ref"],
                "head": None if cur["head"]["worktree"] else cur["head"]["ref"]}

    state.branches = branches

    def switch(head: str | None, base: str | None) -> str | None:
        """Review a different branch (None = working tree) without restarting; returns an error or None."""
        if state.rebuilding:
            return "already loading a report"
        for ref in (head, base):
            if ref and not ref_exists(repo, ref):
                return f"unknown ref: {ref}"
        state.rebuilding, state.switch_error = True, None

        def work() -> None:
            saved = (args.head, args.base, args.intent, args.intent_file)
            # --intent described the branch glance started on; other branches use their saved intent file.
            args.head, args.base, args.intent, args.intent_file = head, base or args.base, None, None
            try:
                rep = build(args, repo)
                if not rep["files"]:
                    raise GitError(f"no changes between {rep['base']['ref']} and {rep['head']['ref']}")
            except Exception as e:  # noqa: BLE001 — any failure keeps the current report and is shown in the page
                args.head, args.base, args.intent, args.intent_file = saved
                state.switch_error = str(e)
                state.rebuilding = False
                return
            if run_llm:
                rep["llm"] = {"status": "running", "provider": llm.available_provider(args.llm)}
            else:
                rep["llm"] = llm.run(rep, provider="none" if args.llm == "none" else args.llm)
            state.set(rep)
            _write(repo, rep)
            state.rebuilding = False
            if run_llm:
                model_pass(rep)

        threading.Thread(target=work, daemon=True).start()
        return None

    state.switch = switch
    _write(repo, report)
    if run_llm:
        report["llm"] = {"status": "running", "provider": llm.available_provider(args.llm)}
        threading.Thread(target=model_pass, args=(report,), daemon=True).start()
    else:
        report["llm"] = llm.run(report, provider="none" if args.llm == "none" else args.llm)
    print(f"glance: {report['summary']['headline']}")
    serve(state, port=args.port, open_browser=not args.no_open)
    return 0


def cmd_serve(args) -> int:
    repo = repo_root(args.repo)
    p = repo / ".glance" / "latest.json"
    if not p.exists():
        print("glance: no report yet — run `glance` first.", file=sys.stderr)
        return 2
    serve(State(json.loads(p.read_text())), port=args.port, open_browser=not args.no_open)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("review", "serve", "-h", "--help", "--version"):
        argv.insert(0, "review")
    ap = argparse.ArgumentParser(prog="glance", description="Structured local review for AI-written changes.")
    ap.add_argument("--version", action="version", version=f"glance {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    rv = sub.add_parser("review", help="analyze a change (default)")
    rv.add_argument("head", nargs="?", default=None, help="branch/commit to review (default: working tree incl. uncommitted)")
    rv.add_argument("--base", default=None, help="base ref (default: merge-base with origin/main or main)")
    rv.add_argument("--repo", default=".", help="path inside the repo or worktree")
    rv.add_argument("--intent", default=None, help="what the change was supposed to do")
    rv.add_argument("--intent-file", default=None, help="file with the task/plan (md, txt, json)")
    rv.add_argument("--llm", default="auto", choices=["auto", "claude-cli", "anthropic", "none"])
    rv.add_argument("--model", default=None, help="model for the model pass")
    rv.add_argument("--format", default="ui", choices=["ui", "md", "json", "html"],
                    help="ui: local review page (default); md: markdown to stdout; json: report file; html: self-contained page to share")
    rv.add_argument("--stdout", action="store_true", help="with --format json, print the report instead of its path")
    rv.add_argument("--no-callers", action="store_true", help="skip the git-grep blast-radius search")
    rv.add_argument("--no-untracked", action="store_true", help="ignore untracked files in the working tree")
    rv.add_argument("--port", type=int, default=7878)
    rv.add_argument("--no-open", action="store_true", help="don't open a browser")

    sv = sub.add_parser("serve", help="reopen the last report")
    sv.add_argument("--repo", default=".")
    sv.add_argument("--port", type=int, default=7878)
    sv.add_argument("--no-open", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return cmd_serve(args)
    return cmd_review(args)


if __name__ == "__main__":
    sys.exit(main())
