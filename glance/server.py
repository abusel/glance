"""Tiny local server for the review UI (stdlib only, binds to 127.0.0.1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .git import git


class State:
    def __init__(self, report: dict[str, Any], rebuild: Callable[[], dict[str, Any]] | None = None):
        self.report = report
        self.rebuild = rebuild
        self.lock = threading.Lock()
        self.rebuilding = False
        self.run_model: Callable[[], bool] | None = None
        self.set_intent: Callable[[str], str] | None = None
        self.recheck: Callable[[str], bool] | None = None

    def get(self) -> dict[str, Any]:
        with self.lock:
            return self.report

    def set(self, report: dict[str, Any]) -> None:
        with self.lock:
            self.report = report


def _ui_html() -> bytes:
    return resources.files("glance").joinpath("ui/index.html").read_bytes()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._send(200, _ui_html(), "text/html; charset=utf-8")
            if u.path == "/api/report":
                body = dict(state.get())
                body["_rebuilding"] = state.rebuilding
                return self._send(200, json.dumps(body).encode())
            if u.path == "/api/file":
                q = parse_qs(u.query)
                path = (q.get("path") or [""])[0]
                side = (q.get("side") or ["new"])[0]
                r = state.get()
                repo = Path(r["repo_path"])
                if not path or ".." in Path(path).parts:
                    return self._send(400, b'{"error":"bad path"}')
                if side == "old":
                    text = git(repo, "show", f"{r['base']['rev']}:{path}", check=False)
                elif r["head"]["worktree"]:
                    fp = (repo / path).resolve()
                    text = fp.read_text(errors="replace") if fp.is_file() and repo.resolve() in fp.parents else ""
                else:
                    text = git(repo, "show", f"{r['head']['rev']}:{path}", check=False)
                return self._send(200, json.dumps({"path": path, "side": side, "text": text}).encode())
            return self._send(404, b'{"error":"not found"}')

        def _same_origin(self) -> bool:
            # Browsers send Origin on cross-site POSTs; refuse anything that isn't this page.
            origin = self.headers.get("Origin")
            return origin is None or origin == f"http://{self.headers.get('Host', '')}"

        def _open_in_editor(self):
            if not self._same_origin():
                return self._send(403, b'{"error":"cross-origin"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
                rel, line = str(req.get("path") or ""), max(1, int(req.get("line") or 1))
            except (ValueError, TypeError):
                return self._send(400, b'{"error":"bad request"}')
            repo = Path(state.get()["repo_path"]).resolve()
            fp = (repo / rel).resolve()
            if not rel or repo not in fp.parents:
                return self._send(400, b'{"error":"bad path"}')
            if not fp.is_file():
                return self._send(404, b'{"error":"file not in working tree"}')
            editor = shutil.which(os.environ.get("GLANCE_EDITOR", "code"))
            if not editor:
                return self._send(501, b'{"error":"editor CLI not found"}')
            try:
                subprocess.Popen([editor, "-g", f"{fp}:{line}"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                return self._send(500, json.dumps({"error": str(e)}).encode())
            return self._send(200, b'{"ok":true}')

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/open":
                return self._open_in_editor()
            if u.path == "/api/intent":
                if not self._same_origin():
                    return self._send(403, b'{"error":"cross-origin"}')
                if not state.set_intent:
                    return self._send(404, b'{"error":"not available"}')
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    text = str(json.loads(self.rfile.read(min(n, 100_000)) or b"{}").get("intent") or "")
                except (ValueError, TypeError):
                    return self._send(400, b'{"error":"bad request"}')
                return self._send(200, json.dumps({"status": state.set_intent(text)}).encode())
            if u.path == "/api/recheck":
                if not self._same_origin():
                    return self._send(403, b'{"error":"cross-origin"}')
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    cid = str(json.loads(self.rfile.read(min(n, 10_000)) or b"{}").get("id") or "")
                except (ValueError, TypeError):
                    return self._send(400, b'{"error":"bad request"}')
                if not state.recheck or not state.recheck(cid):
                    return self._send(409, b'{"error":"unknown concern, already re-checking, or no AI available"}')
                return self._send(202, b'{"ok":true}')
            if u.path == "/api/probe":
                if not self._same_origin():
                    return self._send(403, b'{"error":"cross-origin"}')
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    req = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
                    sid, cases = str(req.get("id") or ""), list(req.get("cases") or [])
                    sides = [x for x in req.get("sides") or ["before", "after"] if x in ("before", "after")]
                except (ValueError, TypeError):
                    return self._send(400, b'{"error":"bad request"}')
                rep = state.get()
                sc = next((x for x in (rep.get("llm") or {}).get("scenarios") or [] if x.get("id") == sid), None)
                if not sc or not sc.get("target"):
                    return self._send(404, b'{"error":"no runnable target for this table"}')
                from . import probe
                return self._send(200, json.dumps(probe.run(rep, sc["target"], cases, sides)).encode())
            if u.path == "/api/llm":
                if not self._same_origin():
                    return self._send(403, b'{"error":"cross-origin"}')
                if not state.run_model or not state.run_model():
                    return self._send(409, b'{"error":"model pass unavailable or already running"}')
                return self._send(202, b'{"ok":true}')
            if u.path == "/api/refresh" and state.rebuild:
                if state.rebuilding:
                    return self._send(409, b'{"error":"already refreshing"}')

                def work():
                    state.rebuilding = True
                    try:
                        state.rebuild()
                    finally:
                        state.rebuilding = False

                threading.Thread(target=work, daemon=True).start()
                return self._send(202, b'{"ok":true}')
            return self._send(404, b'{"error":"not found"}')

    return Handler


def serve(state: State, port: int = 7878, open_browser: bool = True) -> None:
    srv = None
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), make_handler(state))
            port = p
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit(f"No free port in {port}-{port + 19}")
    url = f"http://127.0.0.1:{port}/"
    print(f"glance → {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        srv.server_close()
