"""Who may run what.

Authorisation is allowlist-only. A denylist cannot work here: `execute run`
wraps any other command and `data modify` rewrites arbitrary block and entity
state, so blocking `stop` while permitting either of those protects nothing.

Every rule is a fully anchored pattern for a complete command. If a command
does not match a rule at or below the caller's tier, it is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

TIER_READ = 0
TIER_MOD = 1
TIER_ADMIN = 2

TIER_NAMES = {TIER_READ: "read-only", TIER_MOD: "moderator", TIER_ADMIN: "admin"}

_PLAYER = r"[A-Za-z0-9_]{3,16}"


@dataclass(frozen=True)
class Rule:
    tier: int
    pattern: re.Pattern
    confirm: bool = False
    note: str = ""


def _r(tier: int, pattern: str, confirm: bool = False, note: str = "") -> Rule:
    return Rule(tier, re.compile(rf"^{pattern}$", re.IGNORECASE), confirm, note)


# Ordered only for readability; matching checks all of them.
RULES: tuple[Rule, ...] = (
    # -- tier 0: cannot change game state ---------------------------------
    _r(TIER_READ, r"list"),
    _r(TIER_READ, r"seed"),
    _r(TIER_READ, r"whitelist list"),
    _r(TIER_READ, r"datapack list(\s+(available|enabled))?"),
    # -- tier 1: reversible and visible -----------------------------------
    _r(TIER_MOD, rf"kick\s+{_PLAYER}(\s+.{{1,120}})?"),
    _r(TIER_MOD, r"weather\s+(clear|rain|thunder)(\s+\d{1,6})?"),
    _r(TIER_MOD, r"time\s+set\s+(day|night|noon|midnight|\d{1,6})"),
    _r(TIER_MOD, rf"whitelist\s+(add|remove)\s+{_PLAYER}"),
    _r(TIER_MOD, r"whitelist\s+(on|off|reload)"),
    _r(TIER_MOD, r"save-all(\s+flush)?"),
    _r(TIER_MOD, r"difficulty(\s+(peaceful|easy|normal|hard))?"),
    # -- tier 2: privilege changes and outages ----------------------------
    _r(TIER_ADMIN, rf"op\s+{_PLAYER}", confirm=True, note="grants full server admin"),
    _r(TIER_ADMIN, rf"deop\s+{_PLAYER}", confirm=True),
    _r(TIER_ADMIN, rf"ban\s+{_PLAYER}(\s+.{{1,120}})?", confirm=True),
    _r(TIER_ADMIN, rf"pardon\s+{_PLAYER}", confirm=True),
    _r(TIER_ADMIN, r"ban-ip\s+\S{1,45}(\s+.{1,120})?", confirm=True),
    _r(TIER_ADMIN, r"gamerule\s+[A-Za-z]{1,40}(\s+\S{1,20})?"),
    _r(TIER_ADMIN, r"save-off", confirm=True, note="disables world saving"),
    _r(TIER_ADMIN, r"save-on"),
    _r(TIER_ADMIN, r"stop", confirm=True, note="ends the session for everyone"),
)

# Named so a refusal can explain itself instead of just saying "no".
_EXPLAIN = {
    "execute": "wraps arbitrary commands, so allowing it would bypass every other rule",
    "data": "can rewrite arbitrary block and entity state",
    "fill": "can overwrite terrain in bulk",
    "setblock": "can overwrite terrain",
    "give": "breaks a survival economy",
    "summon": "can spawn anything, including lag machines",
    "gamemode": "hands out creative mode",
    "tp": "moves players without consent",
    "teleport": "moves players without consent",
}


@dataclass(frozen=True)
class Grants:
    """Who holds which tier.

    Usernames are supported because they are what people actually know, but
    they are mutable -- Discord lets anyone change their handle, and a freed
    handle can be claimed by someone else. User IDs are permanent and are the
    stronger grant; prefer them once you have collected them.
    """
    admin_ids: frozenset[int] = frozenset()
    admin_usernames: frozenset[str] = frozenset()
    admin_roles: frozenset[int] = frozenset()
    mod_roles: frozenset[int] = frozenset()
    mod_usernames: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    confirm: bool = False
    note: str = ""


def tier_for(user_id: int, username: str, role_ids: set[int], grants: Grants) -> int:
    """Resolve a caller to a tier. Highest match wins."""
    name = (username or "").lower()
    if (user_id in grants.admin_ids
            or name in grants.admin_usernames
            or (set(role_ids) & grants.admin_roles)):
        return TIER_ADMIN
    if name in grants.mod_usernames or (set(role_ids) & grants.mod_roles):
        return TIER_MOD
    return TIER_READ


def normalise(command: str) -> str:
    """Strip a leading slash and collapse whitespace before matching."""
    command = command.strip()
    while command.startswith("/"):
        command = command[1:].lstrip()
    return re.sub(r"\s+", " ", command)


def check(command: str, tier: int) -> Decision:
    """Decide whether `command` may run at `tier`."""
    cmd = normalise(command)
    if not cmd:
        return Decision(False, "empty command")
    # Newlines would let one approved command smuggle a second one behind it.
    if "\n" in command or "\r" in command:
        return Decision(False, "command contains a newline")

    verb = cmd.split(" ", 1)[0].lower()

    best: Rule | None = None
    for rule in RULES:
        if rule.pattern.match(cmd):
            if best is None or rule.tier < best.tier:
                best = rule

    if best is None:
        if verb in _EXPLAIN:
            return Decision(False, f"`{verb}` is never exposed: it {_EXPLAIN[verb]}")
        return Decision(False, f"`{verb}` is not on the allowlist")

    if tier < best.tier:
        return Decision(
            False,
            f"`{verb}` needs {TIER_NAMES[best.tier]}; you are {TIER_NAMES[tier]}",
        )
    return Decision(True, confirm=best.confirm, note=best.note)


def allowed_at(tier: int) -> list[str]:
    """Human-readable summary of what a tier can run, for a help command."""
    return [r.pattern.pattern.strip("^$") for r in RULES if r.tier <= tier]
