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
        self.tree = app_commands.CommandTree(self)
        self.limiter = RateLimiter(cfg.rate_per_user, cfg.rate_window)
        self._outbox: list[str] = []
        self._lock = asyncio.Lock()

    # -- lifecycle --------------------------------------------------------

    async def setup_hook(self) -> None:
        register(self)
        await self.tree.sync()
        self.loop.create_task(self._tail_log())
        self.loop.create_task(self._flush_loop())

    async def on_ready(self) -> None:
        log.info("connected as %s", self.user)

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
            self._outbox.append(self._render(ev))

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

    async def _flush_loop(self) -> None:
        """Batch log events. Unbatched relay hits Discord's rate limit fast."""
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(self.cfg.flush_seconds)
            if not self._outbox:
                continue
            async with self._lock:
                batch, self._outbox = self._outbox, []
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
        try:
            await chan.send(body, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.warning("send failed: %s", exc)

    # -- Discord -> game --------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if not self.cfg.bridge_to_game or message.author.bot:
            return
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


def register(client: Relay) -> None:
    cfg = client.cfg
    group = app_commands.Group(name="mc", description="Minecraft server console")

    def caller_tier(inter: discord.Interaction) -> int:
        roles = {r.id for r in getattr(inter.user, "roles", [])}
        return authz.tier_for(inter.user.id, inter.user.name, roles, cfg.grants)

    async def guard(inter: discord.Interaction) -> bool:
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

    @group.command(name="whoami", description="Show your permission tier")
    async def whoami(inter: discord.Interaction):
        tier = caller_tier(inter)
        await inter.response.send_message(
            f"You are **{authz.TIER_NAMES[tier]}** (tier {tier}).\n"
            f"Allowed: `" + "`, `".join(authz.allowed_at(tier)[:14]) + "`",
            ephemeral=True)

    client.tree.add_command(group)
