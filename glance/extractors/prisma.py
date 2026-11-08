"""schema.prisma parsing: models, enums, views and composite types."""

from __future__ import annotations

import re

from ..model import Entity

BLOCK_RE = re.compile(r"^\s*(model|enum|view|type)\s+(\w+)\s*\{", re.M)


def extract(path: str, text: str, category: str = "source") -> list[Entity]:
    out: list[Entity] = []
    lines = text.splitlines()
    for m in BLOCK_RE.finditer(text):
        kind, name = m.group(1), m.group(2)
        start = text[: m.start()].count("\n") + 1
        # find matching close brace
        end = start
        for i in range(start, len(lines)):
            if lines[i].strip().startswith("}"):
                end = i + 1
                break
        fields: dict[str, str] = {}
        meta: dict = {}
        for raw in lines[start:end - 1]:
            l = raw.split("//")[0].strip()
            if not l:
                continue
            if l.startswith("@@"):
                meta.setdefault("block_attributes", []).append(l)
                mm = re.match(r'@@map\(\s*"([^"]+)"', l)
                if mm:
                    meta["tablename"] = mm.group(1)
                continue
            parts = l.split(None, 1)
            if kind == "enum":
                fields[parts[0]] = parts[1] if len(parts) > 1 else ""
            elif len(parts) == 2:
                fields[parts[0]] = re.sub(r"\s+", " ", parts[1])
        ek = "type" if kind == "enum" else "table"
        fw = "prisma-enum" if kind == "enum" else "prisma"
        meta.setdefault("tablename", name)
        out.append(Entity(kind=ek, name=name, file=path, line=start, end_line=end, lang="prisma", framework=fw,
                          fields=fields, meta=meta, signature=f"{kind} {name}"))
    return out
