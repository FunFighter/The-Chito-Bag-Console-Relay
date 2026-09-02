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

`/srv/chitobag/data` is mounted **read-only** — the relay needs the RCON
password and `logs/latest.log`, and must never be able to write the world or
the configs.

## Commands

| Command | Tier | Notes |
|---|---|---|
| `/mc players` | read-only | Who is online |
| `/mc whoami` | read-only | Your tier and what it permits |
| `/mc say <message>` | moderator | Broadcast via `tellraw` |
| `/mc run <command>` | varies | Allowlisted console commands |

`/mc run` is governed by the allowlist in [`relay/authz.py`](relay/authz.py):

- **Tier 0, read-only** — `list`, `seed`, `whitelist list`, `datapack list`
- **Tier 1, moderator** — `kick`, `weather`, `time set`, `whitelist add|remove`,
  `save-all`, `difficulty`
- **Tier 2, admin** — `op`, `deop`, `ban`, `pardon`, `gamerule`, `save-off`,
  `stop`. Privilege changes and outages require button confirmation.

### Why allowlist and not denylist

A denylist cannot work. `execute run <anything>` wraps every other command and
`data modify` rewrites arbitrary block and entity state, so blocking `stop`
while permitting either of those protects nothing. `execute`, `data`, `fill`,
`setblock`, `give`, `summon`, `gamemode` and `tp` are refused at **every**
tier, with a message explaining why rather than a bare "no".

Admin is granted by **user id**, not role, so tier 2 cannot be handed out by
editing Discord roles.

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

56 tests, weighted toward the parts where a mistake matters: the allowlist
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
ssh brandonl@192.168.50.10
cd /srv/chitobag/relay && git pull && docker compose up -d --build
```

To automate it, register a second self-hosted runner instance on `wonton`
against this repo — a runner belongs to one repository, so the Minecraft
server's runner cannot serve this one.
