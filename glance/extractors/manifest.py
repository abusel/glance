"""Dependency manifests: package.json, pyproject.toml, requirements*.txt."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from ..model import Entity

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


def _line_of(text: str, needle: str) -> int:
    i = text.find(needle)
    return text[:i].count("\n") + 1 if i >= 0 else 1


def _dep(path: str, text: str, name: str, version: str, framework: str) -> Entity:
    ln = _line_of(text, f'"{name}"') if path.endswith(".json") else _line_of(text, name)
    return Entity(kind="dependency", name=name, file=path, line=ln, end_line=ln, lang="manifest",
                  framework=framework, signature=version or "*", fields={"version": version or "*"})


PEP508 = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def _py_req(path: str, text: str, spec: str, framework: str) -> Entity | None:
    spec = spec.split("#")[0].strip()
    if not spec or spec.startswith(("-", "git+", "http://", "https://", "file:")):
        return None
    m = PEP508.match(spec)
    if not m:
        return None
    name = m.group(1).lower().replace("_", "-")
    return _dep(path, text, m.group(1), (m.group(3) or "").strip(), framework)


def extract(path: str, text: str, category: str = "config") -> list[Entity]:
    name = PurePosixPath(path).name
    out: list[Entity] = []
    if name == "package.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        for section, fw in (("dependencies", "npm"), ("devDependencies", "npm-dev"), ("peerDependencies", "npm-peer"),
                            ("optionalDependencies", "npm-optional")):
            for dep, ver in (data.get(section) or {}).items():
                out.append(_dep(path, text, dep, str(ver), fw))
        for script, cmd in (data.get("scripts") or {}).items():
            ln = _line_of(text, f'"{script}"')
            out.append(Entity(kind="config", name=f"script:{script}", file=path, line=ln, end_line=ln, lang="manifest",
                              framework="npm-script", signature=str(cmd)))
    elif name == "pyproject.toml" and tomllib:
        try:
            data = tomllib.loads(text)
        except Exception:
            return []
        for spec in (data.get("project", {}).get("dependencies") or []):
            e = _py_req(path, text, spec, "pip")
            if e:
                out.append(e)
        for group, specs in (data.get("project", {}).get("optional-dependencies") or {}).items():
            for spec in specs:
                e = _py_req(path, text, spec, f"pip[{group}]")
                if e:
                    out.append(e)
        for group, specs in (data.get("dependency-groups") or {}).items():
            for spec in specs:
                if isinstance(spec, str):
                    e = _py_req(path, text, spec, f"pip[{group}]")
                    if e:
                        out.append(e)
        poetry = data.get("tool", {}).get("poetry", {})
        for dep, ver in (poetry.get("dependencies") or {}).items():
            if dep != "python":
                out.append(_dep(path, text, dep, ver if isinstance(ver, str) else json.dumps(ver), "poetry"))
    elif re.match(r"requirements.*\.txt$", name):
        for raw in text.splitlines():
            e = _py_req(path, text, raw, "pip")
            if e:
                out.append(e)
    return out
