"""Configuration, all from the environment.

The RCON password is read from a file on every use rather than cached at
startup: the Minecraft entrypoint regenerates it if the file is ever emptied,
and a cached value would then fail silently until the bot was restarted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .authz import Grants


def _ids(name: str) -> set[int]:
    raw = os.environ.get(name, "")
    out = set()
    for part in raw.replace(",", " ").split():
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def _names(name: str) -> set[str]:
    """Discord usernames, lowercased. Comma or space separated."""
    raw = os.environ.get(name, "")
    return {p.strip().lstrip("@").lower() for p in raw.replace(",", " ").split() if p.strip()}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class ConfigError(Exception):
    pass


@dataclass
class Config:
    token: str
    rcon_host: str
    rcon_port: int
    rcon_password_file: str
    log_path: str

    command_channels: set[int] = field(default_factory=set)
    console_channel: int = 0
    bridge_channel: int = 0
    audit_channel: int = 0

    admin_users: set[int] = field(default_factory=set)
    admin_usernames: set[str] = field(default_factory=set)
    admin_roles: set[int] = field(default_factory=set)
    mod_roles: set[int] = field(default_factory=set)
    mod_usernames: set[str] = field(default_factory=set)

    bridge_to_game: bool = False
    relay_chat: bool = True
    relay_joins: bool = True
    relay_deaths: bool = True
    relay_advancements: bool = True
    relay_severe: bool = True

    rate_per_user: int = 8
    rate_window: int = 60
    flush_seconds: float = 3.0

    @property
    def grants(self) -> Grants:
        return Grants(
            admin_ids=frozenset(self.admin_users),
            admin_usernames=frozenset(self.admin_usernames),
            admin_roles=frozenset(self.admin_roles),
            mod_roles=frozenset(self.mod_roles),
            mod_usernames=frozenset(self.mod_usernames),
        )

    @property
    def rcon_password(self) -> str:
        try:
            with open(self.rcon_password_file) as fh:
                return fh.read().strip()
        except OSError:
            return ""


def load() -> Config:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError("DISCORD_TOKEN is not set")

    cfg = Config(
        token=token,
        rcon_host=os.environ.get("RCON_HOST", "chitobag-server"),
        rcon_port=_int("RCON_PORT", 25575),
        rcon_password_file=os.environ.get("RCON_PASSWORD_FILE", "/data/.rcon_password"),
        log_path=os.environ.get("MC_LOG_PATH", "/data/logs/latest.log"),
        command_channels=_ids("COMMAND_CHANNEL_IDS"),
        console_channel=_int("CONSOLE_CHANNEL_ID", 0),
        bridge_channel=_int("BRIDGE_CHANNEL_ID", 0),
        audit_channel=_int("AUDIT_CHANNEL_ID", 0),
        admin_users=_ids("ADMIN_USER_IDS"),
        admin_usernames=_names("ADMIN_USERNAMES"),
        admin_roles=_ids("ADMIN_ROLE_IDS"),
        mod_roles=_ids("MOD_ROLE_IDS"),
        mod_usernames=_names("MOD_USERNAMES"),
        bridge_to_game=os.environ.get("BRIDGE_TO_GAME", "false").lower() == "true",
        rate_per_user=_int("RATE_PER_USER", 8),
        rate_window=_int("RATE_WINDOW", 60),
    )
    if not cfg.rcon_password:
        raise ConfigError(f"no RCON password readable at {cfg.rcon_password_file}")
    if not (cfg.admin_users or cfg.admin_usernames or cfg.admin_roles):
        raise ConfigError("no admins configured; set ADMIN_USERNAMES or ADMIN_USER_IDS")
    if not cfg.command_channels:
        raise ConfigError("COMMAND_CHANNEL_IDS is empty; refusing to accept "
                          "commands from every channel")
    return cfg
