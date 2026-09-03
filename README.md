# Chito Bag Console Relay

A Discord bot that runs Minecraft console commands on the Chito Bag server over
RCON, mirrors server events into Discord, and bridges chat both ways.

Separate repo from [The-Chito-Bag-Server-files](https://github.com/FunFighter/The-Chito-Bag-Server-files)
on purpose: changes here must never trigger the server's deploy pipeline.

## How it reaches the server

The relay runs as its own compose project and joins `chitobag_default`, the
network the Minecraft compose project already owns (`external: true`, so this
project never creates or deletes it). It talks to `chitobag-server:25575`.

**No new host ports.** RCON stays unpublished, exactly as it is today. The only
inbound path is Discord's own outbound websocket.

The container runs as `PUID:PGID` (1001, the `minecraft` service account) so it
can read `.rcon_password`, which is mode 600. If you ever change who owns the
data directory, change these to match or the bot cannot authenticate.

`/srv/chitobag/data` is mounted **read-only** — the relay needs the RCON
password and `logs/latest.log`, and must never be able to write the world or
the configs.

## Commands

| Command | Tier | Notes |
|---|---|---|
| `/mc help` | read-only | What you can run, with examples |
| `/mc players` | read-only | Who is online |
| `/mc whoami` | read-only | Your tier and what it permits |
| `/mc say <message>` | moderator | Broadcast via `tellraw` |
| `/mc run <command>` | varies | Allowlisted console commands |

`/mc run` is governed by the allowlist in [`relay/authz.py`](relay/authz.py):

- **Tier 0, read-only** — `list`, `seed`, `whitelist list`, `datapack list`
- **Tier 1, moderator** — `kick`, `weather`, `time set`, `whitelist add|remove`,
  `save-all`, `difficulty`
- **Tier 2, admin** — **any console command**, exactly as if typed into the
  server console.

### Why tiers 0 and 1 are allowlists but tier 2 is not

At the lower tiers a denylist cannot work: `execute run <anything>` wraps every
other command and `data modify` rewrites arbitrary block and entity state, so
blocking `stop` while permitting either of those protects nothing. Those tiers
are therefore allowlist-only, and `execute`, `data`, `fill`, `setblock`,
`give`, `summon`, `gamemode` and `tp` are refused with a message explaining why
rather than a bare "no".

Admins are unrestricted, and that is deliberate. They already hold **op level
4 in game**, so they can run `execute` and `data` from their own chat window
regardless — an allowlist here would obstruct the administration the bot exists
to do without preventing anything. What still applies at tier 2:

- the channel lockdown, so it can only be done from one place
- the audit log, so every command has a name attached
- rate limiting
- button confirmation for `stop`, `op`, `deop`, `ban`, `ban-ip`, `save-off`,
  `kill`, `datapack` and `forceload`
- one command per call — an embedded newline is refused at every tier, so a
  second command cannot ride along unseen by the confirmation prompt

Admin is granted per person (`ADMIN_USERNAMES` or `ADMIN_USER_IDS`) rather than
by role, so tier 2 cannot be handed out by editing Discord roles. The bot
refuses to start with no admin configured.

Usernames are accepted because they are what people actually know, but they
are **mutable** — Discord lets anyone change their handle, and a freed handle
can later be claimed by someone else. User IDs are permanent and are the
stronger grant; move to `ADMIN_USER_IDS` once you have collected them.

## Channel lockdown

The bot acts and speaks in exactly the configured channel, enforced in four
places rather than one:

- **Slash commands are registered to a single guild**, resolved from the
  channel itself at startup. Registered globally they would appear in every
  server the bot joins and in every channel of those servers — the per-call
  check would refuse them, but they would still be listed and still draw a
  reply.
- **Every send goes through one chokepoint** that drops and logs anything
  addressed outside `Config.allowed_channels`. A future code path cannot leak
  a message elsewhere by accident.
- **The game-bound bridge ignores DMs and group chats** outright, and matches
  the channel id exactly — a thread inside the channel has its own id and does
  not count, which is the stricter and intended reading.
- **Interactions outside a guild are refused** before any tier check runs.

If the bot is a member of any other server, it logs a warning on connect: no
commands are registered there and it cannot post there.

## Chat bridge

In-game chat, joins, leaves, deaths, advancements, and `ERROR`/`FATAL` lines
are relayed to `CONSOLE_CHANNEL_ID`. Events are batched every few seconds —
this pack is loud enough that unbatched relay hits Discord's rate limit within
about a minute.

With `BRIDGE_TO_GAME=true`, messages in `BRIDGE_CHANNEL_ID` are relayed into
the game as `tellraw`. This needs the **Message Content** privileged intent
enabled in the Discord Developer Portal, or login fails outright.

Text crossing either way is sanitized in [`relay/sanitize.py`](relay/sanitize.py):
`tellraw` payloads are built with `json.dumps` (never string concatenation),
leading slashes are stripped so a message cannot become a command, Minecraft
`§` formatting codes are removed so nobody can fake a system message, newlines
collapse to spaces, and `@everyone`/`@here` are defanged on the way into
Discord.

## Setup

```bash
git clone https://github.com/FunFighter/The-Chito-Bag-Console-Relay.git
cd The-Chito-Bag-Console-Relay
cp .env.example .env      # fill in token, channel ids, role ids
docker compose up -d --build
docker compose logs -f
```

The Minecraft server must be running first, since the network comes from it.

### Discord application

1. Developer Portal → New Application → Bot → copy the token into `.env`.
2. Enable **Message Content Intent** if you want the game-bound bridge.
3. Invite with scopes `bot` + `applications.commands` and permissions
   *Send Messages*, *Read Message History*, *Add Reactions*.
4. Right-click channels and roles with Developer Mode on to copy their IDs.

## Tests

```bash
python3 -m pytest tests -q
```

83 tests, weighted toward the parts where a mistake matters: the allowlist
(including `execute`/`data` bypasses, newline smuggling, partial matches, and
selector arguments like `@a` in place of a player name), `tellraw` injection,
and log rotation — a tail that holds a file descriptor follows the old inode
into oblivion when the server restarts and goes **silently** dead, so that case
is tested against a real rename.

Parser tests use log lines taken verbatim off the running server rather than
invented ones.

## Deploying updates

Deliberately manual, because this repo has no runner of its own:

```bash
ssh minecraft@192.168.50.10
cd /srv/chitobag/relay && git pull && docker compose up -d --build
```

Runs as the `minecraft` service account (uid 1001), which is in the `docker`
group; nothing here needs root.

To automate it, register a second self-hosted runner instance on `wonton`
against this repo — a runner belongs to one repository, so the Minecraft
server's runner cannot serve this one.
