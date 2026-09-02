import json

import pytest

from relay import sanitize


def test_leading_slash_cannot_become_a_command():
    assert sanitize.clean_text("/stop") == "stop"
    assert sanitize.clean_text("//////op me") == "op me"
    assert sanitize.clean_text("  /  /say hi") == "say hi"


def test_formatting_codes_are_stripped():
    # Left in, these let anyone fake system messages or hide text.
    assert "§" not in sanitize.clean_text("§4§lFAKE ADMIN§r message")
    assert sanitize.clean_text("§ahello") == "hello"


def test_newlines_collapse_to_a_space():
    assert sanitize.clean_text("one\ntwo") == "one two"
    assert sanitize.clean_text("one\r\n\r\ntwo") == "one two"
    # A newline must never survive into a console command.
    assert "\n" not in sanitize.clean_text("a\nb\nc")


def test_control_characters_removed():
    assert sanitize.clean_text("a\x00b\x07c") == "abc"


def test_length_is_capped_with_an_ellipsis():
    out = sanitize.clean_text("x" * 500, limit=32)
    assert len(out) == 32
    assert out.endswith("…")


def test_empty_author_falls_back():
    assert sanitize.clean_author("") == "unknown"
    assert sanitize.clean_author("///") == "unknown"


def test_mass_pings_are_defanged_on_the_way_into_discord():
    out = sanitize.defang_for_discord("hey @everyone and @here")
    assert "@everyone" not in out
    assert "@here" not in out
    assert "everyone" in out  # text preserved, ping broken


def test_player_name_validation():
    assert sanitize.is_player_name("Chito_")
    assert sanitize.is_player_name("Keynash")
    assert not sanitize.is_player_name("@a")
    assert not sanitize.is_player_name("ab")
    assert not sanitize.is_player_name("has space")
    assert not sanitize.is_player_name("x" * 17)


# --- tellraw is the injection surface -----------------------------------

def test_tellraw_is_valid_json_for_hostile_input():
    hostile = '"}]},{"text":"pwned","color":"red"}]'
    cmd = sanitize.tellraw_bridge("attacker", hostile)
    assert cmd.startswith("tellraw @a ")
    payload = json.loads(cmd[len("tellraw @a "):])
    # The hostile string must land inside one text node, not restructure it.
    assert len(payload) == 4
    assert payload[3]["text"] == hostile


def test_tellraw_escapes_quotes_and_backslashes():
    cmd = sanitize.tellraw_bridge('a"b', "back\\slash")
    payload = json.loads(cmd[len("tellraw @a "):])
    assert payload[1]["text"] == 'a"b'
    assert payload[3]["text"] == "back\\slash"


def test_tellraw_never_contains_a_raw_newline():
    cmd = sanitize.tellraw_bridge("someone\nelse", "line1\nline2")
    assert "\n" not in cmd
    assert "\r" not in cmd


@pytest.mark.parametrize("payload", [
    "§cred text",
    "/stop",
    "\x00\x1b[31m",
])
def test_tellraw_sanitises_before_encoding(payload):
    cmd = sanitize.tellraw_bridge("user", payload)
    body = json.loads(cmd[len("tellraw @a "):])[3]["text"]
    assert "§" not in body
    assert not body.startswith("/")
    assert "\x00" not in body
