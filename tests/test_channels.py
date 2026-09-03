"""The bot must act and speak in exactly one channel.

These lock down the set of channels the send path will accept, because that is
the guarantee: a future code path should not be able to leak a message into
some other channel or server.
"""
import os

import pytest

from relay.config import Config, ConfigError, load

CHAN = 1544568009505513492
OTHER = 999888777666555444


def _cfg(**kw):
    base = dict(
        token="t", rcon_host="h", rcon_port=25575,
        rcon_password_file="/nonexistent", log_path="/nonexistent",
        command_channels={CHAN},
    )
    base.update(kw)
    return Config(**base)


def test_single_channel_setup_allows_only_that_channel():
    c = _cfg(console_channel=CHAN, bridge_channel=CHAN, audit_channel=0)
    assert c.allowed_channels == frozenset({CHAN})


def test_other_channels_are_not_allowed():
    c = _cfg(console_channel=CHAN, bridge_channel=CHAN)
    assert OTHER not in c.allowed_channels
    # A thread inside the channel has its own id, so it is out too.
    assert (CHAN + 1) not in c.allowed_channels


def test_disabled_channels_do_not_widen_the_allowlist():
    # audit_channel=0 means "off" and must not become a real target.
    c = _cfg(console_channel=CHAN, bridge_channel=CHAN, audit_channel=0)
    assert 0 not in c.allowed_channels
    assert len(c.allowed_channels) == 1


def test_extra_targets_are_included_when_deliberately_set():
    c = _cfg(console_channel=CHAN, bridge_channel=CHAN, audit_channel=OTHER)
    assert c.allowed_channels == frozenset({CHAN, OTHER})


# --- config loader refuses an unsafe setup ------------------------------

def _env(**kw):
    keys = ["DISCORD_TOKEN", "COMMAND_CHANNEL_IDS", "ADMIN_USERNAMES",
            "ADMIN_USER_IDS", "ADMIN_ROLE_IDS", "RCON_PASSWORD_FILE",
            "CONSOLE_CHANNEL_ID", "BRIDGE_CHANNEL_ID", "AUDIT_CHANNEL_ID",
            "MOD_USERNAMES", "MOD_ROLE_IDS", "BRIDGE_TO_GAME"]
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in kw.items()})
    return saved, keys


def _restore(saved, keys):
    for k in keys:
        os.environ.pop(k, None)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


def test_loader_refuses_empty_channel_list(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("secret")
    saved, keys = _env(DISCORD_TOKEN="t", ADMIN_USERNAMES="a",
                       RCON_PASSWORD_FILE=str(pw))
    try:
        with pytest.raises(ConfigError, match="COMMAND_CHANNEL_IDS"):
            load()
    finally:
        _restore(saved, keys)


def test_loader_refuses_no_admins(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("secret")
    saved, keys = _env(DISCORD_TOKEN="t", COMMAND_CHANNEL_IDS=str(CHAN),
                       RCON_PASSWORD_FILE=str(pw))
    try:
        with pytest.raises(ConfigError, match="admins"):
            load()
    finally:
        _restore(saved, keys)


def test_loader_accepts_the_real_single_channel_setup(tmp_path):
    pw = tmp_path / "pw"
    pw.write_text("secret")
    saved, keys = _env(
        DISCORD_TOKEN="t",
        COMMAND_CHANNEL_IDS=str(CHAN),
        CONSOLE_CHANNEL_ID=str(CHAN),
        BRIDGE_CHANNEL_ID=str(CHAN),
        AUDIT_CHANNEL_ID="0",
        ADMIN_USERNAMES="new_metoadbird,saddadchito",
        BRIDGE_TO_GAME="true",
        RCON_PASSWORD_FILE=str(pw),
    )
    try:
        c = load()
        assert c.allowed_channels == frozenset({CHAN})
        assert c.bridge_to_game is True
        assert c.admin_usernames == {"new_metoadbird", "saddadchito"}
    finally:
        _restore(saved, keys)
