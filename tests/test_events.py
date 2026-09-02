"""Parser tests built from real lines taken off the running server."""
from relay import events
from relay.events import ADVANCEMENT, CHAT, DEATH, JOIN, LEAVE, SEVERE

PRE = "[02Sep2026 03:21:13.664] [Server thread/INFO] [net.minecraft.server.MinecraftServer/]: "


def test_real_advancement_lines():
    for line, who, what in [
        (PRE + "Keynash has made the advancement [Diamonds!]", "Keynash", "Diamonds!"),
        (PRE + "Chito_ has made the advancement [Pokémon Jockey!]", "Chito_", "Pokémon Jockey!"),
        (PRE + "Chito_ has made the advancement ['X' Marks the Spot]", "Chito_", "'X' Marks the Spot"),
    ]:
        e = events.parse(line)
        assert e is not None and e.kind == ADVANCEMENT, line
        assert e.who == who and e.text == what


def test_real_leave_line():
    e = events.parse(PRE + "Chito_ left the game")
    assert e is not None and e.kind == LEAVE and e.who == "Chito_"


def test_join_line():
    e = events.parse(PRE + "Keynash joined the game")
    assert e is not None and e.kind == JOIN and e.who == "Keynash"


def test_chat_line():
    e = events.parse(PRE + "<Chito_> hello there")
    assert e is not None and e.kind == CHAT
    assert e.who == "Chito_" and e.text == "hello there"


def test_real_error_lines_are_severe():
    e = events.parse(
        "[02Sep2026 02:50:31.403] [main/ERROR] [net.minecraft.CrashReport/]: "
        "Negative index in crash report handler (0/18)"
    )
    assert e is not None and e.kind == SEVERE

    e = events.parse(
        "[02Sep2026 02:50:31.409] [main/ERROR] [net.minecraft.server.Main/FATAL]: "
        "Failed to start the minecraft server"
    )
    assert e is not None and e.kind == SEVERE


def test_death_line():
    e = events.parse(PRE + "Chito_ was slain by Zombie")
    assert e is not None and e.kind == DEATH
    assert e.who == "Chito_"


# --- the noise that must NOT be relayed ---------------------------------

def test_debug_noise_is_dropped():
    assert events.parse(
        "[02Sep2026 02:52:16.884] [main/DEBUG] [io.netty.util.internal.PlatformDependent0/]: "
        "java.nio.DirectByteBuffer.<init>(long, {int,long}): unavailable"
    ) is None


def test_ordinary_info_is_dropped():
    assert events.parse(PRE + "Done (2.305s)! For help, type \"help\"") is None
    assert events.parse(PRE + "Preparing spawn area: 18%") is None
    assert events.parse(
        "[02Sep2026 03:21:13.664] [Server thread/WARN] [minecraft/MinecraftServer]: "
        "Can't keep up! Is the server overloaded?"
    ) is None


def test_non_log_lines_are_dropped():
    assert events.parse("") is None
    assert events.parse("not a log line at all") is None
    assert events.parse(">....progress bar noise") is None


def test_entity_id_line_is_not_a_death():
    # These appear on every join and would otherwise trip the death matcher.
    e = events.parse(PRE + "Chito_[/192.168.50.224:51234] logged in with entity id 271")
    assert e is None or e.kind != DEATH
