"""Turn untrusted text into something safe to hand to the Minecraft console.

Everything here exists because text from Discord reaches a console that
interprets some of it. Two characters matter most:

  '/'  at the start of a `say` argument turns a message into a command.
  '§' is Minecraft's formatting escape -- left in, a player can recolour
       chat, fake system messages, or hide text against the background.
"""
from __future__ import annotations

import json
import re
import unicodedata

MAX_CHAT = 256
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

# A Minecraft formatting code is '§' plus one selector character, so both go
# together -- dropping only the '§' would leave a stray letter in the text.
_FORMAT_CODE_RE = re.compile("§[0-9A-Fa-fK-Ok-oRr]")
# Any remaining '§', plus the C0/C1 control ranges. Newlines are handled
# separately so they collapse to a space rather than joining words together.
_CONTROL_RE = re.compile("[§\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NEWLINE_RE = re.compile("[\r\n\x09  ]+")
_WS_RE = re.compile(r"\s{2,}")
# @everyone / @here must never survive a round trip into Discord.
_MASS_PING_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
# Zero-width joiner used to defang a ping without mangling the visible text.
_ZWSP = "​"


def clean_text(text: str, limit: int = MAX_CHAT) -> str:
    """Normalise arbitrary text down to a single safe line."""
    text = unicodedata.normalize("NFC", text)
    text = _NEWLINE_RE.sub(" ", text)
    text = _FORMAT_CODE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    # A leading slash would make `say` execute a command instead of speaking.
    while text.startswith("/"):
        text = text[1:].lstrip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def clean_author(name: str, limit: int = 32) -> str:
    """Discord display names are user-controlled; treat them as hostile too."""
    return clean_text(name, limit) or "unknown"


def defang_for_discord(text: str) -> str:
    """Strip mass pings from text on its way *into* Discord."""
    return _MASS_PING_RE.sub(lambda m: "@" + _ZWSP + m.group(1), text)


def is_player_name(name: str) -> bool:
    return bool(PLAYER_RE.match(name))


def tellraw_bridge(author: str, message: str) -> str:
    """Build a `tellraw` that shows a Discord message in game.

    tellraw takes JSON, so json.dumps does the escaping -- string
    concatenation here would be an injection hole. clean_* still runs first,
    because escaping does not remove formatting codes or newlines.
    """
    payload = [
        {"text": "[Discord] ", "color": "blue"},
        {"text": clean_author(author), "color": "aqua"},
        {"text": ": ", "color": "gray"},
        {"text": clean_text(message), "color": "white"},
    ]
    return "tellraw @a " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
