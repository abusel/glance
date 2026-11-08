"""Collect a change set from git: which files changed, their before/after text and hunks."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .model import FileChange, Hunk, short_hash

MAX_TEXT_BYTES = 1_500_000

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True, binary: bool = False):
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=repo,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    if binary:
        return proc.stdout if proc.returncode == 0 else None
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else ""


def repo_root(path: str | os.PathLike) -> Path:
    out = git(Path(path), "rev-parse", "--show-toplevel").strip()
    return Path(out)


def ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=repo, capture_output=True
    ).returncode == 0


def default_base(repo: Path) -> str:
    for cand in ("origin/main", "main", "origin/master", "master", "origin/develop", "develop"):
        if ref_exists(repo, cand):
            return cand
    raise GitError("Couldn't find main/master; pass --base explicitly.")


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip() or "HEAD"


# ---------------------------------------------------------------- classification

TS_EXT = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Pipfile.lock", "bun.lockb", "bun.lock"}


def detect_lang(path: str) -> str:
    p = Path(path)
    name, ext = p.name, p.suffix.lower()
    if ext == ".py":
        return "python"
    if ext in TS_EXT:
        return "typescript"
    if ext == ".prisma":
        return "prisma"
    if ext == ".sql":
        return "sql"
    if name == "package.json" or name == "pyproject.toml" or re.match(r"requirements.*\.txt$", name):
        return "manifest"
    return "other"


def categorize(path: str) -> str:
    p = path.lower()
    name = Path(p).name
    parts = set(Path(p).parts)
    if name in {n.lower() for n in LOCKFILES}:
        return "lockfile"
    if parts & {"dist", "build", "__generated__", "generated", ".next", "node_modules"} or name.endswith((".min.js", ".map")):
        return "generated"
    if (
        parts & {"tests", "test", "__tests__", "spec", "e2e"}
        or re.search(r"(^test_.*\.py$|_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$|conftest\.py$)", name)
    ):
        return "test"
    if "versions" in parts and ("alembic" in parts or "migrations" in parts) or "migrations" in parts or "migration" in parts:
        return "migration"
    if name.endswith((".md", ".mdx", ".rst", ".txt")) and detect_lang(p) != "manifest" or "docs" in parts:
        return "docs"
    if name.endswith((".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env.example")) or name in {"dockerfile", ".env.example", "makefile"} or ".github" in parts:
        return "config"
    if detect_lang(p) in {"python", "typescript", "prisma", "sql"}:
        return "source"
    return "other"


# ---------------------------------------------------------------- diff parsing

def parse_hunks(diff_text: str, path: str) -> tuple[list[Hunk], bool]:
    hunks: list[Hunk] = []
    binary = False
    cur: Hunk | None = None
    old_no = new_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            binary = True
            continue
        m = HUNK_RE.match(raw)
        if m:
            o, ol, n, nl, header = m.groups()
            cur = Hunk(
                id="",
                file=path,
                old_start=int(o),
                old_len=int(ol) if ol is not None else 1,
                new_start=int(n),
                new_len=int(nl) if nl is not None else 1,
                header=header.strip(),
            )
            cur.id = "h_" + short_hash(f"{path}|{o}|{n}")
            hunks.append(cur)
            old_no, new_no = int(o), int(n)
            continue
        if cur is None:
            continue
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        tag, text = (raw[:1] or " "), raw[1:]
        if tag == "+":
            cur.lines.append(["+", None, new_no, text])
            new_no += 1
        elif tag == "-":
            cur.lines.append(["-", old_no, None, text])
            old_no += 1
        else:
            cur.lines.append([" ", old_no, new_no, text])
            old_no += 1
            new_no += 1
    return hunks, binary


def synthetic_added_hunk(path: str, text: str) -> Hunk:
    lines = text.splitlines()
    h = Hunk(id="h_" + short_hash(f"{path}|0|1"), file=path, old_start=0, old_len=0,
             new_start=1, new_len=len(lines), header="new file")
    h.lines = [["+", None, i + 1, l] for i, l in enumerate(lines)]
    return h


# ---------------------------------------------------------------- change set

@dataclass
class ChangeSet:
    repo: Path
    base_ref: str
    base_rev: str
    head_ref: str           # "WORKTREE" when diffing the working tree
    head_rev: str | None
    files: list[FileChange] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)

    @property
    def worktree(self) -> bool:
        return self.head_rev is None


def _decode(blob: bytes | None) -> tuple[str | None, bool]:
    if blob is None:
        return None, False
    if len(blob) > MAX_TEXT_BYTES or b"\x00" in blob[:8000]:
        return None, True
    return blob.decode("utf-8", "replace"), False


def collect(repo: Path, base: str | None = None, head: str | None = None, include_untracked: bool = True) -> ChangeSet:
    base_ref = base or default_base(repo)
    if not ref_exists(repo, base_ref):
        raise GitError(f"Unknown base ref: {base_ref}")
    head_for_mb = head or "HEAD"
    if head and not ref_exists(repo, head):
        raise GitError(f"Unknown head ref: {head}")
    base_rev = git(repo, "merge-base", base_ref, head_for_mb, check=False).strip() or git(repo, "rev-parse", base_ref).strip()
    head_rev = git(repo, "rev-parse", head).strip() if head else None

    range_args = [base_rev] + ([head_rev] if head_rev else [])
    status_out = git(repo, "diff", "--name-status", "-M", "-z", *range_args)
    tokens = status_out.split("\0")
    entries: list[tuple[str, str | None, str]] = []  # (status, old_path, path)
    i = 0
    while i < len(tokens):
        st = tokens[i]
        if not st:
            i += 1
            continue
        code = st[0]
        if code in "RC":
            entries.append(("renamed" if code == "R" else "added", tokens[i + 1], tokens[i + 2]))
            i += 3
        else:
            path = tokens[i + 1]
            status = {"A": "added", "D": "removed", "M": "modified", "T": "modified"}.get(code, "modified")
            entries.append((status, path if status != "added" else None, path))
            i += 2

    if head_rev is None and include_untracked:
        untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        for p in untracked:
            if p and not p.startswith(".glance/"):
                entries.append(("added", None, p))

    files: list[FileChange] = []
    for status, old_path, path in entries:
        fc = FileChange(path=path, status=status, old_path=old_path if status == "renamed" else (old_path if status != "added" else None),
                        lang=detect_lang(path), category=categorize(path))
        if status != "added":
            fc.old_text, b1 = _decode(git(repo, "show", f"{base_rev}:{old_path or path}", check=False, binary=True))
            fc.binary |= b1
        if status != "removed":
            if head_rev:
                blob = git(repo, "show", f"{head_rev}:{path}", check=False, binary=True)
            else:
                fp = repo / path
                blob = fp.read_bytes() if fp.is_file() else None
            fc.new_text, b2 = _decode(blob)
            fc.binary |= b2

        tracked = not (status == "added" and head_rev is None and old_path is None and _is_untracked(repo, path))
        if tracked:
            paths = [p for p in {old_path, path} if p]
            diff = git(repo, "diff", "-U3", "--no-color", "--no-ext-diff", "-M", *range_args, "--", *paths, check=False)
            fc.hunks, b3 = parse_hunks(diff, path)
            fc.binary |= b3
        elif fc.new_text is not None:
            fc.hunks = [synthetic_added_hunk(path, fc.new_text)]
        for h in fc.hunks:
            for l in h.lines:
                if l[0] == "+":
                    fc.additions += 1
                elif l[0] == "-":
                    fc.deletions += 1
        files.append(fc)

    log_range = f"{base_rev}..{head_rev or 'HEAD'}"
    commits = [c for c in git(repo, "log", "--format=%s%n%b%x1e", log_range, check=False).split("\x1e") if c.strip()]
    return ChangeSet(repo=repo, base_ref=base_ref, base_rev=base_rev, head_ref=head or "WORKTREE",
                     head_rev=head_rev, files=files, commits=[c.strip() for c in commits])


def _is_untracked(repo: Path, path: str) -> bool:
    return subprocess.run(["git", "ls-files", "--error-unmatch", "--", path], cwd=repo, capture_output=True).returncode != 0


def grep_word(repo: Path, word: str, rev: str | None = None, limit: int = 40) -> list[tuple[str, int, str]]:
    """Find whole-word occurrences in the head tree (working tree when rev is None)."""
    args = ["grep", "-n", "-w", "-I", "--no-color", "-e", word]
    if rev:
        args.append(rev)
    args += ["--", ".", ":!*.lock", ":!package-lock.json", ":!*.min.js", ":!dist", ":!build"]
    if rev is None:
        args.insert(1, "--untracked")
    out = git(repo, *args, check=False)
    hits = []
    for line in out.splitlines():
        if rev and line.startswith(rev + ":"):
            line = line[len(rev) + 1:]
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            hits.append((parts[0], int(parts[1]), parts[2].strip()[:200]))
        if len(hits) >= limit:
            break
    return hits
