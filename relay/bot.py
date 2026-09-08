"""Discord side of the relay: slash commands out, log events back."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

import discord
from discord import app_commands

from . import authz, events, rcon, sanitize
from .config import Config
from .logtail import LogTail

log = logging.getLogger("relay")

# Discord hard-caps a message at 2000 characters.
DISCORD_LIMIT = 1900
# Ceiling on queued console lines while the sender is unavailable.
MAX_OUTBOX = 500

_ICON = {
    events.JOIN: "\N{LARGE GREEN CIRCLE}",
    events.LEAVE: "\N{MEDIUM WHITE CIRCLE}",
    events.DEATH: "\N{SKULL}",
    events.ADVANCEMENT: "\N{TROPHY}",
    events.SEVERE: "\N{WARNING SIGN}",
}


class RateLimiter:
    def __init__(self, limit: int, window: int):
        self.limit = limit
        self.window = window
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        q = self._hits[user_id]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class Relay(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        # Required to read Discord messages for the game-bound half of the
        # bridge. This is a privileged intent and must also be enabled in the
        # Developer Portal, or login fails outright.
        intents.message_content = bool(cfg.bridge_to_game)
        super().__init__(intents=intents)
        self.cfg = cfg
        self._guild_id: int | None = None
        self.tree = app_commands.CommandTree(self)
        self.limiter = RateLimiter(cfg.rate_per_user, cfg.rate_window)
        self._allowed = cfg.allowed_channels
        self._outbox: list[str] = []
        self._dropped = 0
        self._errbox: dict[str, int] = {}
        self._err_seen: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------

    async def _supervise(self, name: str, factory) -> None:
        """Keep a background loop alive across transient failures.

        These loops are the whole point of the bot, and a bare create_task
        makes them fragile: one unhandled exception ends the task silently and
        forever. A single DNS blip took the send loops down for four days,
        with the bot still showing as connected the entire time.
        """
        delay = 5.0
        while not self.is_closed():
            try:
                await factory()
                return                      # completed normally
            except asyncio.CancelledError:
                raise                       # shutdown, not a failure
            except Exception:
                log.exception("%s died; restarting in %.0fs", name, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300.0)
            else:
                delay = 5.0

    async def setup_hook(self) -> None:
        # Resolve the guild from the channel itself, so /mc is registered only
        # there. Registered globally, the commands would appear in every
        # server the bot is in and in every channel of those servers -- the
        # per-call channel check would refuse them, but they would still be
        # listed and still draw a reply.
        guild = None
        primary = self.cfg.console_channel or next(iter(self.cfg.command_channels))
        try:
            chan = await self.fetch_channel(primary)
            gid = getattr(getattr(chan, "guild", None), "id", None)
            if gid:
                guild = discord.Object(id=gid)
                self._guild_id = gid
        except discord.HTTPException as exc:
            log.error("could not resolve the guild for channel %s: %s", primary, exc)

        register(self, guild)
        if guild is not None:
            await self.tree.sync(guild=guild)
            # Push an empty global set, clearing any previously global copy.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            log.info("commands registered to guild %s only", self._guild_id)
        else:
            log.error("no guild resolved; commands NOT registered (channel "
                      "%s unreachable -- check the bot can see it)", primary)

        log.info("may post only in: %s", sorted(self._allowed))
        self.loop.create_task(self._supervise("log tail", self._tail_log))
        self.loop.create_task(self._supervise("console flush", self._flush_loop))
        if self.cfg.error_channel:
            self.loop.create_task(self._supervise("error flush", self._error_loop))

    async def on_ready(self) -> None:
        log.info("connected as %s", self.user)
        for g in self.guilds:
            if self._guild_id and g.id != self._guild_id:
                log.warning("also a member of guild %s (%s) -- no commands are "
                            "registered there and it cannot post there", g.id, g.name)

    # -- RCON -------------------------------------------------------------

    async def run_command(self, command: str) -> str:
        """Execute off the event loop so a slow server cannot stall the bot."""
        return await asyncio.to_thread(
            rcon.execute, command, self.cfg.rcon_host, self.cfg.rcon_port,
            self.cfg.rcon_password,
        )

    # -- log -> Discord ---------------------------------------------------

    async def _tail_log(self) -> None:
        await self.wait_until_ready()
        tail = LogTail(self.cfg.log_path)
        c = self.cfg
        wanted = {
            events.CHAT: c.relay_chat,
            events.JOIN: c.relay_joins,
            events.LEAVE: c.relay_joins,
            events.DEATH: c.relay_deaths,
            events.ADVANCEMENT: c.relay_advancements,
            events.SEVERE: c.relay_severe,
        }
        async for line in tail.lines():
            ev = events.parse(line)
            if ev is None or not wanted.get(ev.kind):
                continue
            if ev.kind == events.SEVERE and self.cfg.error_channel:
                self._note_error(ev.text)
            else:
                self._outbox.append(self._render(ev))
                # If the sender is stalled, drop the oldest rather than grow
                # forever -- an unbounded queue turns an outage into an OOM.
                if len(self._outbox) > MAX_OUTBOX:
                    dropped = len(self._outbox) - MAX_OUTBOX
                    del self._outbox[:dropped]
                    self._dropped += dropped

    def _render(self, ev: events.Event) -> str:
        who = sanitize.defang_for_discord(ev.who)
        text = sanitize.defang_for_discord(ev.text)
        if ev.kind == events.CHAT:
            return f"**{who}**: {text}"
        if ev.kind == events.JOIN:
            return f"{_ICON[ev.kind]} **{who}** joined"
        if ev.kind == events.LEAVE:
            return f"{_ICON[ev.kind]} **{who}** left"
        if ev.kind == events.DEATH:
            return f"{_ICON[ev.kind]} **{who}** {text}"
        if ev.kind == events.ADVANCEMENT:
            return f"{_ICON[ev.kind]} **{who}** earned *{text}*"
        return f"{_ICON[events.SEVERE]} `{text[:300]}`"

    def _note_error(self, text: str) -> None:
        """Coalesce errors by signature.

        This pack emits ~690 ERROR lines per boot from only 11 distinct
        causes -- measured. Relaying each one would bury the channel and tell
        you nothing you could not learn from the first. So identical errors
        are counted, and each signature is reported at most once per window.
        """
        sig = events.signature(text)
        self._errbox[sig] = self._errbox.get(sig, 0) + 1

    async def _error_loop(self) -> None:
        await self.wait_until_ready()
        window = self.cfg.error_repeat_seconds
        while not self.is_closed():
            await asyncio.sleep(self.cfg.error_flush_seconds)
            if not self._errbox:
                continue
            async with self._lock:
                batch, self._errbox = self._errbox, {}
            now = time.monotonic()
            chan = self.get_channel(self.cfg.error_channel)
            if chan is None:
                continue
            lines = []
            for sig, count in sorted(batch.items(), key=lambda kv: -kv[1]):
                last = self._err_seen.get(sig, 0)
                if now - last < window:
                    continue          # already reported recently
                self._err_seen[sig] = now
                suffix = f"  *(x{count})*" if count > 1 else ""
                lines.append(f"{_ICON[events.SEVERE]} `{sig[:280]}`{suffix}")
            if not lines:
                continue
            body = ""
            for line in lines[:15]:
                if len(body) + len(line) + 1 > DISCORD_LIMIT:
                    await self._send(chan, body); body = ""
                body += line + "\n"
            if len(lines) > 15:
                body += f"*… {len(lines) - 15} more distinct errors suppressed*\n"
            if body:
                await self._send(chan, body)

    async def _flush_loop(self) -> None:
        """Batch log events. Unbatched relay hits Discord's rate limit fast."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(self.cfg.flush_seconds)
            if not self._outbox:
                continue
            async with self._lock:
                batch, self._outbox = self._outbox, []
                dropped, self._dropped = self._dropped, 0
            if dropped:
                batch.insert(0, f"*… {dropped} lines dropped while disconnected …*")
            # Drop the middle rather than let the buffer grow without bound.
            if len(batch) > 40:
                dropped = len(batch) - 40
                batch = batch[:20] + [f"*… {dropped} lines omitted …*"] + batch[-20:]
            chan = self.get_channel(self.cfg.console_channel or self.cfg.bridge_channel)
            if chan is None:
                continue
            body = ""
            for line in batch:
                if len(body) + len(line) + 1 > DISCORD_LIMIT:
                    await self._send(chan, body)
                    body = ""
                body += line + "\n"
            if body:
                await self._send(chan, body)

    async def _send(self, chan, body: str) -> None:
        # Single chokepoint for everything the bot says. Anything outside the
        # configured channels is dropped and logged rather than posted.
        cid = getattr(chan, "id", None)
        if cid not in self._allowed:
            log.error("refusing to post in channel %s (not in %s)", cid,
                      sorted(self._allowed))
            return
        try:
            await chan.send(body, allowed_mentions=discord.AllowedMentions.none())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Deliberately broad. discord.py wraps aiohttp, which raises its
            # own errors (ClientConnectorDNSError, ConnectionTimeoutError)
            # that are not discord.HTTPException -- catching only the latter
            # is what let a DNS failure escape and kill the caller's loop.
            log.warning("send to %s failed (%s): %s",
                        getattr(chan, "id", "?"), type(exc).__name__, exc)

    # -- Discord -> game --------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if not self.cfg.bridge_to_game or message.author.bot:
            return
        # No DMs, no group chats: a bridge into the game must come from the
        # one channel people can be held accountable in.
        if message.guild is None:
            return
        # Exact match: a thread inside the channel has its own id and does not
        # count, which is the stricter and intended reading.
        if message.channel.id != self.cfg.bridge_channel:
            return
        text = message.clean_content  # resolves mentions to readable names
        if not text.strip():
            return
        if not self.limiter.allow(message.author.id):
            return
        author = message.author.display_name
        try:
            await self.run_command(sanitize.tellraw_bridge(author, text))
        except rcon.RconError as exc:
            log.warning("bridge to game failed: %s", exc)
            try:
                await message.add_reaction("\N{WARNING SIGN}")
            except discord.HTTPException:
                pass

    # -- audit ------------------------------------------------------------

    async def audit(self, user: discord.abc.User, command: str, outcome: str) -> None:
        line = f"`{user}` ({user.id}) → `{command}` → {outcome}"
        log.info("AUDIT %s", line)
        if self.cfg.audit_channel:
            chan = self.get_channel(self.cfg.audit_channel)
            if chan is not None:
                await self._send(chan, line)


class ConfirmView(discord.ui.View):
    """Second deliberate action for privilege changes and outages."""

    def __init__(self, author_id: int, note: str):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.note = note
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "That confirmation is not yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Run it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


def register(client: Relay, guild: discord.Object | None = None) -> None:
    cfg = client.cfg
    group = app_commands.Group(name="mc", description="Minecraft server console")

    def caller_tier(inter: discord.Interaction) -> int:
        roles = {r.id for r in getattr(inter.user, "roles", [])}
        return authz.tier_for(inter.user.id, inter.user.name, roles, cfg.grants)

    async def guard(inter: discord.Interaction) -> bool:
        if inter.guild_id is None:
            await inter.response.send_message(
                "This bot only works in its server channel.", ephemeral=True)
            return False
        if inter.channel_id not in cfg.command_channels:
            await inter.response.send_message(
                "Not a command channel for this server.", ephemeral=True)
            return False
        if not client.limiter.allow(inter.user.id):
            await inter.response.send_message(
                "You are sending commands too quickly. Try again shortly.",
                ephemeral=True)
            return False
        return True

    async def execute(inter: discord.Interaction, command: str, ephemeral: bool):
        try:
            out = await client.run_command(command)
        except rcon.RconAuthError:
            await client.audit(inter.user, command, "RCON auth failed")
            await inter.followup.send(
                "RCON rejected the password. The server may have rotated it.",
                ephemeral=True)
            return
        except rcon.RconError as exc:
            await client.audit(inter.user, command, f"unreachable: {exc}")
            await inter.followup.send(
                "The server is not answering — it may be restarting.",
                ephemeral=True)
            return
        await client.audit(inter.user, command, "ok")
        body = sanitize.defang_for_discord(out.strip()) or "*(no output)*"
        if len(body) > DISCORD_LIMIT:
            import io
            await inter.followup.send(
                "Output was too long for a message; attached instead.",
                file=discord.File(io.BytesIO(body.encode()), "output.txt"),
                ephemeral=ephemeral)
        else:
            await inter.followup.send(
                f"```\n{body}\n```", ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="players", description="Who is online")
    async def players(inter: discord.Interaction):
        if not await guard(inter):
            return
        await inter.response.defer()
        await execute(inter, "list", ephemeral=False)

    @group.command(name="say", description="Send a message to everyone in game")
    @app_commands.describe(message="Text to broadcast")
    async def say(inter: discord.Interaction, message: str):
        if not await guard(inter):
            return
        if caller_tier(inter) < authz.TIER_MOD:
            await inter.response.send_message(
                "`say` needs the moderator role.", ephemeral=True)
            return
        text = sanitize.clean_text(message)
        if not text:
            await inter.response.send_message("Nothing to say.", ephemeral=True)
            return
        await inter.response.defer()
        await execute(inter, sanitize.tellraw_bridge(inter.user.display_name, text),
                      ephemeral=False)

    @group.command(name="run", description="Run a console command (allowlisted)")
    @app_commands.describe(command="Console command, without the leading slash")
    async def run(inter: discord.Interaction, command: str):
        if not await guard(inter):
            return
        tier = caller_tier(inter)
        decision = authz.check(command, tier)
        if not decision.allowed:
            await client.audit(inter.user, command, f"refused: {decision.reason}")
            await inter.response.send_message(
                f"Refused — {decision.reason}", ephemeral=True)
            return

        normalised = authz.normalise(command)
        if decision.confirm:
            note = f"\n{decision.note}." if decision.note else ""
            view = ConfirmView(inter.user.id, decision.note)
            await inter.response.send_message(
                f"Run `{normalised}`?{note}", view=view, ephemeral=True)
            await view.wait()
            if view.value is not True:
                await client.audit(inter.user, normalised, "cancelled")
                await inter.followup.send("Cancelled.", ephemeral=True)
                return
        else:
            await inter.response.defer()
        await execute(inter, normalised, ephemeral=decision.confirm)

    @group.command(name="help", description="What you can run, and how")
    async def help_cmd(inter: discord.Interaction):
        if not await guard(inter):
            return
        tier = caller_tier(inter)
        lines = [
            "**Slash commands**",
            "`/mc help` — this message",
            "`/mc players` — who is online",
            "`/mc whoami` — your permission tier",
            "`/mc say <message>` — broadcast in game *(moderator)*",
            "`/mc run <command>` — run a console command",
            "",
            f"**Your tier: {authz.TIER_NAMES[tier]}**",
        ]

        if tier >= authz.TIER_ADMIN:
            lines += [
                "You can run **any** console command through `/mc run`, "
                "exactly as if you typed it into the server console.",
                "",
                "These ask for a confirmation button first:",
                "`" + "`, `".join(authz.confirm_verbs()) + "`",
                "",
                "*Examples*",
                "`/mc run time set day`",
                "`/mc run gamerule keepInventory true`",
                "`/mc run execute as @a run effect give @s minecraft:glowing 10`",
                "`/mc run data get entity @p Pos`",
            ]
        else:
            for t in range(0, tier + 1):
                cmds = authz.usage_at(t)
                if cmds:
                    lines += ["", f"*{authz.TIER_NAMES[t]}*",
                              "`" + "`\n`".join(cmds) + "`"]
            higher = [authz.TIER_NAMES[t] for t in range(tier + 1, 3)]
            if higher:
                lines += ["", "Higher tiers (" + ", ".join(higher) +
                          ") can run more. Ask an admin."]

        lines += [
            "",
            "**Chat bridge** — anything you type in this channel goes into the "
            "game, and in-game chat, joins, deaths and advancements come back "
            "here. Drop the leading slash: `/mc run list`, not `/list`.",
        ]
        body = "\n".join(lines)
        if len(body) > DISCORD_LIMIT:
            body = body[:DISCORD_LIMIT] + "\n*(truncated)*"
        await inter.response.send_message(
            body, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="whoami", description="Show your permission tier")
    async def whoami(inter: discord.Interaction):
        if not await guard(inter):
            return
        tier = caller_tier(inter)
        if tier >= authz.TIER_ADMIN:
            detail = "You can run any console command. See `/mc help`."
        else:
            allowed = authz.allowed_at(tier)
            detail = ("Allowed: `" + "`, `".join(allowed) + "`"
                      if allowed else "No commands available.")
        await inter.response.send_message(
            f"You are **{authz.TIER_NAMES[tier]}** (tier {tier}).\n{detail}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    if guild is not None:
        client.tree.add_command(group, guild=guild)
    else:
        client.tree.add_command(group)
