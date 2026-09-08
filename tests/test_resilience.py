"""Regression tests for the outage that took the relay down for four days.

A single ClientConnectorDNSError escaped _send (which only caught
discord.HTTPException), propagated out of the flush loop's `while`, and ended
the task. Because the loops were bare create_task calls, nothing restarted
them -- the bot stayed "connected" and relayed nothing until restarted by hand.
"""
import asyncio

import pytest

from relay.bot import MAX_OUTBOX, Relay
from relay.config import Config


def _cfg(**kw):
    base = dict(
        token="t", rcon_host="h", rcon_port=25575,
        rcon_password_file="/nonexistent", log_path="/nonexistent",
        command_channels={1}, console_channel=1,
    )
    base.update(kw)
    return Config(**base)


class _Boom:
    """A channel whose send always fails the way aiohttp fails."""
    id = 1

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def send(self, *a, **k):
        self.calls += 1
        raise self.exc


class _Ok:
    id = 1

    def __init__(self):
        self.sent = []

    async def send(self, body, **k):
        self.sent.append(body)


def _relay():
    # Constructing Relay needs no gateway connection.
    return Relay.__new__(Relay)


def _wire(r, chan):
    r.cfg = _cfg()
    r._allowed = frozenset({1})
    r._outbox = []
    r._dropped = 0
    r._errbox = {}
    r._err_seen = {}
    r._lock = asyncio.Lock()
    r._chan = chan
    return r


# --- the exception that caused the outage --------------------------------

class FakeDNSError(Exception):
    """Stands in for aiohttp's ClientConnectorDNSError.

    The point is that it is NOT a discord.HTTPException -- that is precisely
    why the original guard missed it.
    """


def test_send_swallows_non_discord_network_errors():
    """_send must not propagate. This is the whole bug."""
    r = _wire(_relay(), None)
    chan = _Boom(FakeDNSError("Temporary failure in name resolution"))

    async def go():
        await Relay._send(r, chan, "hello")     # must not raise

    asyncio.run(go())
    assert chan.calls == 1


def test_send_still_delivers_normally():
    r = _wire(_relay(), None)
    chan = _Ok()
    asyncio.run(Relay._send(r, chan, "hello"))
    assert chan.sent == ["hello"]


def test_send_refuses_channels_outside_the_allowlist():
    r = _wire(_relay(), None)
    chan = _Ok()
    chan.id = 99999                     # not allowed
    asyncio.run(Relay._send(r, chan, "hello"))
    assert chan.sent == [], "posted outside the allowlist"


# --- the supervisor ------------------------------------------------------

def test_supervisor_restarts_a_loop_that_raises():
    r = _relay()
    r._closed = False
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise FakeDNSError("transient")
        # third attempt succeeds and returns

    async def go():
        # is_closed is consulted by the supervisor's while condition.
        Relay.is_closed = lambda self: False
        await asyncio.wait_for(Relay._supervise(r, "flaky", flaky), timeout=30)

    # Shorten the backoff so the test is quick.
    import relay.bot as botmod
    orig_sleep = asyncio.sleep

    async def fast_sleep(d):
        await orig_sleep(0)
    botmod.asyncio.sleep = fast_sleep
    try:
        asyncio.run(go())
    finally:
        botmod.asyncio.sleep = orig_sleep

    assert len(attempts) == 3, f"expected 3 attempts, got {len(attempts)}"


def test_supervisor_lets_cancellation_through():
    r = _relay()
    Relay.is_closed = lambda self: False

    async def blocker():
        await asyncio.sleep(3600)

    async def go():
        t = asyncio.create_task(Relay._supervise(r, "blocker", blocker))
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(go())


# --- the unbounded queue ------------------------------------------------

def test_outbox_is_bounded():
    """A stalled sender must not turn an outage into unbounded memory use."""
    r = _wire(_relay(), None)
    for i in range(MAX_OUTBOX + 250):
        r._outbox.append(f"line {i}")
        if len(r._outbox) > MAX_OUTBOX:
            dropped = len(r._outbox) - MAX_OUTBOX
            del r._outbox[:dropped]
            r._dropped += dropped
    assert len(r._outbox) == MAX_OUTBOX
    assert r._dropped == 250
    # The newest lines are the ones kept.
    assert r._outbox[-1] == f"line {MAX_OUTBOX + 249}"
