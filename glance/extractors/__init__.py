"""Extractor registry: pick a parser by language and return Entities."""

from __future__ import annotations

from ..model import Entity
from . import manifest, prisma, python, sql, typescript


class ExtractError(Exception):
    pass


def extract(path: str, text: str | None, lang: str, category: str) -> list[Entity]:
    if not text:
        return []
    try:
        if lang == "python":
            return python.extract(path, text)
        if lang == "typescript":
            if path.endswith(".d.ts"):
                return [e for e in typescript.extract(path, text, category) if e.kind in ("type", "model")]
            return typescript.extract(path, text, category)
        if lang == "prisma":
            return prisma.extract(path, text, category)
        if lang == "sql":
            return sql.extract(path, text, category)
        if lang == "manifest":
            return manifest.extract(path, text, category)
    except SyntaxError as e:
        raise ExtractError(f"{path}: could not parse ({e})") from e
    except RecursionError as e:  # pragma: no cover
        raise ExtractError(f"{path}: file too deeply nested to parse") from e
    return []
