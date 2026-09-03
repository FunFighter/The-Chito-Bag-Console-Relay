"""Turn raw server log lines into the handful of events worth relaying.

This pack is loud -- a few hundred lines a second during load, and mods warn
constantly at runtime. Relaying everything would hit Discord's rate limit
within a minute, so only these categories pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# [02Sep2026 01:58:10.783] [Server thread/INFO] [minecraft/MinecraftServer]: <msg>
_LINE_RE = re.compile(
    r"^\[[^\]]+\]\s*\[(?P<thread>[^/\]]+)/(?P<level>[A-Z]+)\]\s*"
    r"(?:\[(?P<source>[^\]]*)\]:?\s*)?(?P<msg>.*)$"
)

_PLAYER = r"[A-Za-z0-9_]{3,16}"

CHAT = "chat"
JOIN = "join"
LEAVE = "leave"
DEATH = "death"
ADVANCEMENT = "advancement"
SEVERE = "severe"

_CHAT_RE = re.compile(rf"^<(?P<who>{_PLAYER})>\s(?P<text>.+)$")
_JOIN_RE = re.compile(rf"^(?P<who>{_PLAYER}) joined the game$")
_LEAVE_RE = re.compile(rf"^(?P<who>{_PLAYER}) left the game$")
_ADV_RE = re.compile(
    rf"^(?P<who>{_PLAYER}) has (?:made the advancement|completed the challenge|"
    rf"reached the goal) \[(?P<what>[^\]]+)\]$"
)
# Deaths have no single format -- there are dozens of messages. Match on a
# known player name followed by anything, and rely on the negative checks
# above having already claimed join/leave/advancement lines.
_DEATH_VERBS = (
    "was ", "died", "drowned", "blew up", "fell", "burned", "went up in flames",
    "walked into", "suffocated", "starved", "froze", "was squashed",
    "hit the ground", "experienced kinetic energy", "withered away",
    "was impaled", "discovered the floor", "tried to swim in lava",
)
_DEATH_RE = re.compile(rf"^(?P<who>{_PLAYER}) (?P<rest>.+)$")


@dataclass(frozen=True)
class Event:
    kind: str
    who: str = ""
    text: str = ""
    raw: str = ""


def parse(line: str) -> Event | None:
    """Return an Event for a line worth relaying, else None."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    level = m.group("level")
    msg = m.group("msg").strip()
    if not msg:
        return None

    if (c := _CHAT_RE.match(msg)):
        return Event(CHAT, c.group("who"), c.group("text"), line)
    if (j := _JOIN_RE.match(msg)):
        return Event(JOIN, j.group("who"), raw=line)
    if (l := _LEAVE_RE.match(msg)):
        return Event(LEAVE, l.group("who"), raw=line)
    if (a := _ADV_RE.match(msg)):
        return Event(ADVANCEMENT, a.group("who"), a.group("what"), line)

    if level in ("ERROR", "FATAL"):
        return Event(SEVERE, text=msg, raw=line)

    if level == "INFO" and (d := _DEATH_RE.match(msg)):
        rest = d.group("rest")
        if any(rest.startswith(v) or f" {v}" in f" {rest}" for v in _DEATH_VERBS):
            return Event(DEATH, d.group("who"), rest, line)
    return None


# Numbers, coordinates, uuids, hex ids and paths vary between otherwise
# identical errors. Blanking them lets repeats of the same fault collapse.
_SIG_SUBS = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"-?\d+\.\d+"), "<f>"),
    (re.compile(r"-?\d+"), "<n>"),
    (re.compile(r"/[\w./-]{6,}"), "<path>"),
)


def signature(text: str) -> str:
    """Collapse an error message to a stable signature for deduplication."""
    out = text.strip()
    for pat, rep in _SIG_SUBS:
        out = pat.sub(rep, out)
    return re.sub(r"\s+", " ", out)[:300]
