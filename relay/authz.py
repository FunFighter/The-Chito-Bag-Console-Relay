"""Who may run what.

Tiers 0 and 1 are allowlist-only: a fully anchored pattern per command, and
anything not matching a rule at or below the caller's tier is refused. A
denylist would be useless at those tiers, since `execute run` wraps any other
command and `data modify` rewrites arbitrary block and entity state.

Tier 2 (admin) may run **any** console command. That is a deliberate choice,
not an oversight: the admins are already op level 4 in game, so they can run
`execute` and `data` from their own chat window regardless. Restricting them
here would protect nothing they could not already do, while making the bot
useless for the administration it exists to do.

What still applies at tier 2:
  - the channel lockdown, so it can only be done from one place
  - the audit log, so every command has a name attached
  - rate limiting
  - button confirmation for commands that end sessions or grant privileges
  - one command per call: embedded newlines are always refused
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
    usage: str = ""
    confirm: bool = False
    note: str = ""


def _r(tier: int, pattern: str, usage: str = "", confirm: bool = False,
       note: str = "") -> Rule:
    return Rule(tier, re.compile(rf"^{pattern}$", re.IGNORECASE), usage,
                confirm, note)


# Ordered only for readability; matching checks all of them.
RULES: tuple[Rule, ...] = (
    # -- tier 0: cannot change game state ---------------------------------
    _r(TIER_READ, r"list", "list"),
    _r(TIER_READ, r"seed", "seed"),
    _r(TIER_READ, r"whitelist list", "whitelist list"),
    _r(TIER_READ, r"datapack list(\s+(available|enabled))?", "datapack list"),
    # -- tier 1: reversible and visible -----------------------------------
    _r(TIER_MOD, rf"kick\s+{_PLAYER}(\s+.{{1,120}})?", "kick <player> [reason]"),
    _r(TIER_MOD, r"weather\s+(clear|rain|thunder)(\s+\d{1,6})?", "weather clear|rain|thunder [ticks]"),
    _r(TIER_MOD, r"time\s+set\s+(day|night|noon|midnight|\d{1,6})", "time set day|night|noon|midnight|<ticks>"),
    _r(TIER_MOD, rf"whitelist\s+(add|remove)\s+{_PLAYER}", "whitelist add|remove <player>"),
    _r(TIER_MOD, r"whitelist\s+(on|off|reload)", "whitelist on|off|reload"),
    _r(TIER_MOD, r"save-all(\s+flush)?", "save-all [flush]"),
    _r(TIER_MOD, r"difficulty(\s+(peaceful|easy|normal|hard))?", "difficulty [peaceful|easy|normal|hard]"),
    # -- tier 2: privilege changes and outages ----------------------------
    _r(TIER_ADMIN, rf"op\s+{_PLAYER}", "op <player>", confirm=True, note="grants full server admin"),
    _r(TIER_ADMIN, rf"deop\s+{_PLAYER}", "deop <player>", confirm=True),
    _r(TIER_ADMIN, rf"ban\s+{_PLAYER}(\s+.{{1,120}})?", "ban <player> [reason]", confirm=True),
    _r(TIER_ADMIN, rf"pardon\s+{_PLAYER}", "pardon <player>", confirm=True),
    _r(TIER_ADMIN, r"ban-ip\s+\S{1,45}(\s+.{1,120})?", "ban-ip <ip> [reason]", confirm=True),
    _r(TIER_ADMIN, r"gamerule\s+[A-Za-z]{1,40}(\s+\S{1,20})?", "gamerule <rule> [value]"),
    _r(TIER_ADMIN, r"save-off", "save-off", confirm=True, note="disables world saving"),
    _r(TIER_ADMIN, r"save-on", "save-on"),
    _r(TIER_ADMIN, r"stop", "stop", confirm=True, note="ends the session for everyone"),
)

# Verbs a non-admin might reasonably try, with a reason rather than a bare no.
_EXPLAIN = {
    "execute": "wraps arbitrary commands, so it is admin-only",
    "data": "can rewrite arbitrary block and entity state",
    "fill": "can overwrite terrain in bulk",
    "setblock": "can overwrite terrain",
    "give": "breaks a survival economy",
    "summon": "can spawn anything, including lag machines",
    "gamemode": "hands out creative mode",
    "tp": "moves players without consent",
    "teleport": "moves players without consent",
}

# Admins may run anything, but these get a confirmation button: they either end
# everyone's session, change who holds privileges, or risk the world.
_ADMIN_CONFIRM = {
    "stop": "ends the session for everyone",
    "op": "grants full server admin",
    "deop": "removes server admin",
    "ban": "bans a player",
    "ban-ip": "bans an address",
    "save-off": "disables world saving",
    "kill": "can wipe every entity or player at once",
    "datapack": "can disable a datapack the world depends on",
    "forceload": "can pin chunks loaded indefinitely",
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

    # Admins are unrestricted. They already hold op level 4 in game, so an
    # allowlist here would only obstruct, not protect.
    if tier >= TIER_ADMIN:
        for rule in RULES:
            if rule.tier <= TIER_ADMIN and rule.pattern.match(cmd):
                return Decision(True, confirm=rule.confirm, note=rule.note)
        if verb in _ADMIN_CONFIRM:
            return Decision(True, confirm=True, note=_ADMIN_CONFIRM[verb])
        return Decision(True)

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


def usage_at(tier: int) -> list[str]:
    """Commands available at exactly `tier`, as readable usage strings."""
    seen, out = set(), []
    for r in RULES:
        if r.tier == tier and r.usage and r.usage not in seen:
            seen.add(r.usage)
            out.append(r.usage + ("  (confirm)" if r.confirm else ""))
    return out


def allowed_at(tier: int) -> list[str]:
    """Everything a tier can run, cumulative, for a short summary."""
    out = []
    for t in range(0, tier + 1):
        out.extend(usage_at(t))
    return out


def confirm_verbs() -> list[str]:
    return sorted(_ADMIN_CONFIRM)
