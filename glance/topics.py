"""Group changes into topics ("subscription", "loan", ...) so a reviewer can work one feature at a time.

Deterministic and cheap: every change gets candidate words from its name, its changed field names and its
file path. A word becomes a topic when several changes share it; each change takes the shared word that best
describes it (its own name beats its fields, which beat its path). Topics with a single member fold into the
best shared topic they have a word for, else "other".
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

OTHER = "other"
MIN_TOPIC_SIZE = 3   # smaller groups fold into a bigger topic they share a word with
MAX_TOPICS = 10

# Words that say what kind of code something is, not which feature it belongs to.
STOP = set("""
a an and as at by for from in into is of on or the to with via per out off up
src app apps api lib libs pkg package packages index util utils helper helpers common shared core internal
test tests spec specs mock mocks fixture fixtures e2e
controller controllers service services model models schema schemas prisma database db orm entity entities
migration migrations route routes router routers handler handlers middleware view views page pages layout
component components hook hooks provider context store frontend backend server client worker job jobs
workflow workflows step steps task tasks queue queues type types interface dto config configs constant constants
const env settings setting option options enabled enable disabled disable flag flags
get set create update delete add remove list find fetch load save make build run start stop init impl main
new old is has can should do does with without default base data info item items value values result results
response request req res body params param query args arg input output error errors status state
id ids uuid key keys name names date dates time timestamp created updated at count total amount number
ts tsx js jsx mjs cjs py sql json md yml yaml toml txt generated dist
user users account accounts
pending current next prev previous first last max min
""".split())

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

WEIGHTS = {"name": 3.0, "field": 2.0, "file": 1.5, "dir": 1.0}


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def words(text: str) -> list[str]:
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", text or ""):
        for w in _CAMEL.findall(part):
            w = _singular(w.lower())
            if len(w) >= 3 and not w.isdigit() and w not in STOP:
                out.append(w)
    return out


def _path_words(path: str) -> tuple[list[str], list[str]]:
    parts = path.split("/")
    base = re.sub(r"\.[^.]+$", "", parts[-1]) if parts else ""
    return words(base), [w for d in parts[:-1] for w in words(d)]


def _candidates(change: dict[str, Any]) -> dict[str, float]:
    """word -> best weight for one change."""
    best: dict[str, float] = {}

    def add(ws: list[str], weight: float) -> None:
        for w in ws:
            best[w] = max(best.get(w, 0.0), weight)

    add(words(change.get("name", "")), WEIGHTS["name"])
    add([w for fc in change.get("field_changes") or [] for w in words(fc.get("name", ""))], WEIGHTS["field"])
    base, dirs = _path_words(change.get("file", ""))
    add(base, WEIGHTS["file"])
    add(dirs, WEIGHTS["dir"])
    return best


def _pick(cands: dict[str, float], freq: Counter, allowed=None) -> str | None:
    best, best_score = None, 0.0
    for w, weight in cands.items():
        if freq[w] < 2 or (allowed is not None and w not in allowed):
            continue
        score = weight * math.sqrt(freq[w])   # a widely shared word wins, but a name word still beats a path word
        if score > best_score:
            best, best_score = w, score
    return best


def assign_topics(changes: list[dict[str, Any]], files: list[dict[str, Any]], risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set `topic` on every change, file and risk (in place). Returns the topic list, biggest first."""
    cands = {c["id"]: _candidates(c) for c in changes}
    freq: Counter = Counter(w for cs in cands.values() for w in cs)

    first = {cid: _pick(cs, freq) for cid, cs in cands.items()}
    size = Counter(t for t in first.values() if t)
    kept = {t for t, n in size.most_common(MAX_TOPICS) if n >= MIN_TOPIC_SIZE}
    for c in changes:
        t = first[c["id"]]
        if t not in kept:
            t = _pick(cands[c["id"]], freq, kept)
        c["topic"] = t or OTHER

    by_file: dict[str, Counter] = defaultdict(Counter)
    for c in changes:
        by_file[c["file"]][c["topic"]] += 1
    for f in files:
        counts = by_file.get(f["path"])
        topic = counts.most_common(1)[0][0] if counts else None
        if not topic or topic == OTHER:
            base, dirs = _path_words(f["path"])
            path_cands = {w: WEIGHTS["file"] for w in base} | {w: WEIGHTS["dir"] for w in dirs if w not in base}
            topic = _pick(path_cands, Counter({t: 99 for t in kept}), kept) or topic or OTHER
        f["topic"] = topic

    file_topic = {f["path"]: f["topic"] for f in files}
    for r in risks:
        r["topic"] = file_topic.get(r.get("file") or "", OTHER)

    totals = Counter(c["topic"] for c in changes) + Counter(f["topic"] for f in files)
    order = sorted(totals, key=lambda t: (t == OTHER, -totals[t], t))
    return [{"id": t, "label": "Other" if t == OTHER else t.capitalize(),
             "changes": sum(1 for c in changes if c["topic"] == t),
             "files": sum(1 for f in files if f["topic"] == t)} for t in order]
