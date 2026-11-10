"""Probe runner: call the function behind a behavior table with real inputs, on both sides of the change.

"after"  = the code under review (the working tree, or a snapshot of the head commit)
"before" = a snapshot of the base commit, extracted with `git archive` into a cache dir outside the repo

Both run with the project's own interpreter (Python) or node (JS; TS through the project's `tsx`), in a
subprocess with a timeout. Nothing here is sandboxed: it runs the reviewer's code with their environment,
which is why the UI only does it on an explicit click and asks first for anything not marked pure.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .git import git

MARK = "@@GLANCE_PROBE@@"
TIMEOUT = int(os.environ.get("GLANCE_PROBE_TIMEOUT", "30"))
CACHE = Path(os.environ.get("GLANCE_CACHE", Path.home() / ".cache" / "glance")) / "snapshots"
PY_EXT = (".py",)
JS_EXT = (".js", ".mjs", ".cjs")
TS_EXT = (".ts", ".tsx", ".mts", ".cts")

PY_RUNNER = r'''
import asyncio, contextlib, dataclasses, importlib, inspect, io, json, os, sys, time, traceback
from decimal import Decimal
cfg = json.loads(sys.stdin.read())
root, rel, name = cfg["root"], cfg["file"], cfg["function"]
os.chdir(root)
parts = rel[:-3].split("/")
if parts[-1] == "__init__":
    parts = parts[:-1]
mod, modname, err = None, None, None
for k in range(len(parts)):                       # app/x.py -> app.x ; src/pkg/x.py -> pkg.x with src on the path
    base = os.path.join(root, *parts[:k])
    sys.path.insert(0, base)
    try:
        modname = ".".join(parts[k:])
        mod = importlib.import_module(modname)
        break
    except ModuleNotFoundError as e:
        err = e
        if not e.name or not (modname == e.name or modname.startswith(e.name + ".")):
            break                                     # a real missing dependency, not a wrong import root
        sys.path.remove(base)
    except BaseException as e:
        err = e
        break
def out(o):
    sys.stdout.write("\n" + MARK + json.dumps(o, default=str) + "\n")
    sys.stdout.flush()
if mod is None:
    out({"error": "import failed: " + "".join(traceback.format_exception_only(type(err), err)).strip()[-600:]})
    sys.exit(0)
fn = mod
for p in name.split("."):
    fn = getattr(fn, p, None)
    if fn is None:
        out({"missing": True, "error": f"{name} is not defined in {modname}", "module": modname})
        sys.exit(0)
def show(v):
    if hasattr(v, "model_dump"):
        v = v.model_dump()
    elif dataclasses.is_dataclass(v) and not isinstance(v, type):
        v = dataclasses.asdict(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dict, list, tuple)):
        try:
            return json.dumps(v, default=str)
        except Exception:
            pass
    return repr(v)
results = []
for case in cfg["cases"]:
    inp = case.get("input")
    buf = io.StringIO()
    t0 = time.time()
    r = {}
    try:
        with contextlib.redirect_stdout(buf):
            res = fn(**inp) if isinstance(inp, dict) else fn()
            if inspect.isawaitable(res):
                async def _wait(a):
                    return await a
                res = asyncio.run(_wait(res))
        r = {"ok": True, "value": show(res), "repr": repr(res)}
    except BaseException as e:
        r = {"ok": False, "raised": type(e).__name__, "value": f"{type(e).__name__}: {e}"[:400]}
    r["ms"] = round((time.time() - t0) * 1000, 1)
    if buf.getvalue():
        r["stdout"] = buf.getvalue()[-400:]
    results.append(r)
out({"module": modname, "results": results})
'''.replace("MARK", repr(MARK))

JS_RUNNER = r'''
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
const cfg = JSON.parse(readFileSync(0, "utf8"));
const out = o => process.stdout.write("\nMARK" + JSON.stringify(o) + "\n");
const show = v => { if (v === undefined) return "undefined"; if (typeof v === "string") return JSON.stringify(v);
  if (typeof v === "bigint") return v + "n"; if (v instanceof Date) return v.toISOString();
  try { return JSON.stringify(v); } catch { return String(v); } };
let mod;
try { mod = await import(pathToFileURL(cfg.root + "/" + cfg.file).href); }
catch (e) { out({ error: "import failed: " + String(e && e.message || e).slice(0, 600) }); process.exit(0); }
let fn = mod;
for (const p of cfg.function.split(".")) fn = fn?.[p] ?? fn?.default?.[p];
if (typeof fn !== "function") { out({ missing: true, error: `${cfg.function} is not exported from ${cfg.file}` }); process.exit(0); }
const results = [];
for (const c of cfg.cases) {
  const inp = c.input || {};
  const args = cfg.params && cfg.params.length ? cfg.params.map(p => inp[p]) : [inp];
  const t0 = Date.now();
  let r;
  try { const v = await fn(...args); r = { ok: true, value: show(v), repr: show(v) }; }
  catch (e) { r = { ok: false, raised: e?.name || "Error", value: `${e?.name || "Error"}: ${e?.message || e}`.slice(0, 400) }; }
  r.ms = Date.now() - t0;
  results.push(r);
}
out({ module: cfg.file, results });
'''.replace("MARK", MARK)


def python_for(repo: Path) -> str:
    """The project's interpreter, not glance's own (glance usually lives in a pipx venv)."""
    if os.environ.get("GLANCE_PYTHON"):
        return os.environ["GLANCE_PYTHON"]
    for cand in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
        if (repo / cand).exists():
            return str(repo / cand)
    if os.environ.get("VIRTUAL_ENV") and Path(os.environ["VIRTUAL_ENV"], "bin", "python").exists():
        return str(Path(os.environ["VIRTUAL_ENV"], "bin", "python"))
    return shutil.which("python3") or shutil.which("python") or "python3"


def runnable(path: str, repo: Path) -> str | None:
    """Why a file can't be probed, or None if it can."""
    if path.endswith(PY_EXT):
        return None
    if path.endswith(JS_EXT):
        return None if shutil.which("node") else "node is not on PATH"
    if path.endswith(TS_EXT):
        if not shutil.which("node"):
            return "node is not on PATH"
        return None if (repo / "node_modules" / "tsx").is_dir() else "TypeScript needs `tsx` in the project (npm i -D tsx)"
    return "only Python, JavaScript and TypeScript functions can be run"


def snapshot(repo: Path, rev: str) -> Path:
    """The tree at `rev`, extracted once into a cache dir outside the repo (so editors and tools never see it).
    JS projects get a symlink to the repo's node_modules so imports resolve."""
    full = git(repo, "rev-parse", rev).strip()
    d = CACHE / f"{hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:10]}-{full[:12]}"
    if not (d / ".glance-complete").exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=CACHE))
        data = git(repo, "archive", "--format=tar", full, binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            try:
                tf.extractall(tmp, filter="data")
            except TypeError:          # Python without extraction filters
                tf.extractall(tmp)
        shutil.rmtree(d, ignore_errors=True)
        shutil.move(str(tmp), str(d))
        (d / ".glance-complete").write_text(full)
        _prune(keep=d)
    nm = repo / "node_modules"
    if nm.is_dir() and not (d / "node_modules").exists():
        try:
            (d / "node_modules").symlink_to(nm, target_is_directory=True)
        except OSError:
            pass
    return d


def _prune(keep: Path, limit: int = 6) -> None:
    snaps = sorted((p for p in CACHE.iterdir() if p.is_dir() and p != keep and not p.name.startswith(".tmp-")), key=lambda p: p.stat().st_mtime)
    for p in snaps[:max(0, len(snaps) - (limit - 1))]:
        shutil.rmtree(p, ignore_errors=True)


def roots(report: dict) -> dict[str, Path]:
    repo = Path(report["repo_path"])
    after = repo if report["head"].get("worktree") else snapshot(repo, report["head"]["rev"])
    return {"after": after, "before": snapshot(repo, report["base"]["rev"])}


def _run_side(root: Path, target: dict, cases: list[dict], repo: Path) -> dict[str, Any]:
    f = target["file"]
    if not (root / f).is_file():
        return {"missing": True, "error": f"{f} doesn't exist on this side"}
    cfg = json.dumps({"root": str(root), "file": f, "function": target["function"], "params": target.get("params") or [], "cases": cases})
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    if f.endswith(PY_EXT):
        cmd = [python_for(repo), "-c", PY_RUNNER]
    else:
        script = CACHE.parent / "probe-runner.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(JS_RUNNER)
        cmd = ["node", *(["--import", "tsx"] if f.endswith(TS_EXT) else []), str(script)]
    try:
        p = subprocess.run(cmd, input=cfg, capture_output=True, text=True, cwd=root, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {TIMEOUT}s (does it wait on a database or the network?)"}
    except OSError as e:
        return {"error": str(e)[:300]}
    line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith(MARK)), None)
    if line is None:
        return {"error": ((p.stderr or p.stdout).strip()[-600:] or f"exited {p.returncode} without a result")}
    return json.loads(line[len(MARK):])


def run(report: dict, target: dict, cases: list[dict], sides: list[str] | None = None) -> dict[str, Any]:
    repo = Path(report["repo_path"])
    why = runnable(target.get("file") or "", repo)
    if why:
        return {"error": why}
    if not re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)?", target.get("function") or ""):
        return {"error": "not a plain function name"}
    cases = [{"input": c.get("input") if isinstance(c.get("input"), dict) else None} for c in cases][:20]
    try:
        rs = roots(report)
    except Exception as e:  # git archive failures, disk, permissions
        return {"error": f"couldn't prepare the code to run: {e}"[:300]}
    out: dict[str, Any] = {"interpreter": python_for(repo) if target["file"].endswith(PY_EXT) else "node"}
    for side in sides or ["before", "after"]:
        if side == "before" and target.get("existed_before") is False:
            out[side] = {"missing": True, "error": "didn't exist before this change"}
            continue
        out[side] = _run_side(rs[side], target, cases, repo)
    return out


def params_from_signature(sig: str) -> list[str]:
    """Parameter names, in order, from "def f(a: int, b=2, *, c)" or "function f(a: number, b?: string)"."""
    start = sig.find("(")
    if start < 0:
        return []
    depth, cur, parts = 0, "", []
    for ch in sig[start + 1:]:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for p in parts:
        m = re.match(r"\s*(?:\.\.\.)?\**\s*([A-Za-z_$][\w$]*)", p)
        if m and m.group(1) not in ("self", "cls") and p.strip() not in ("*", "/"):
            out.append(m.group(1))
    return out


def resolve_target(sc: dict, report: dict) -> dict | None:
    """Which function a behavior table exercises: the model's `target` if it names a real file in the change,
    else the one changed function whose name matches the table's subject."""
    files = {f["path"] for f in report["files"]}
    fns = [c for c in report["changes"] if c["kind"] == "function"]
    t = sc.get("target") if isinstance(sc.get("target"), dict) else None
    name = file = None
    if t and t.get("file") in files and t.get("function"):
        file, name = t["file"], str(t["function"]).strip()
    else:
        m = re.match(r"\s*([A-Za-z_$][\w$.]*)\s*\(", sc.get("subject") or "")
        if m:
            hits = [c for c in fns if c["name"] == m.group(1) or c["name"].split(".")[-1] == m.group(1)]
            if len({(c["file"], c["name"]) for c in hits}) == 1:
                file, name = hits[0]["file"], hits[0]["name"]
    if not file or not name or not re.fullmatch(r"[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)?", name):
        return None
    if not file.endswith(PY_EXT + JS_EXT + TS_EXT):
        return None
    if "." in name:
        return None                    # methods need an instance; not something a table row can build
    ch = next((c for c in fns if c["file"] == file and c["name"] == name), None)
    ent = (ch or {}).get("after") or (ch or {}).get("before") or {}
    params = list((ent.get("fields") or {}).keys()) or params_from_signature(ent.get("signature") or "")
    return {"file": file, "function": name, "params": params,
            "pure": bool(t.get("pure")) if t else False,
            "existed_before": None if ch is None else ch["status"] != "added"}
