"""Error coalescing, tested against real lines from this pack.

The measured pack emits ~690 ERROR lines per boot from 11 distinct causes.
Without collapsing them the error channel is unreadable, so the signature has
to fold genuine repeats together while keeping different faults apart.
"""
from relay import events
from relay.events import SEVERE

PRE = "[02Sep2026 03:21:13.664] [Server thread/ERROR] [minecraft/LootDataType/]: "


def sig(text):
    return events.signature(text)


def test_identical_errors_collapse():
    a = sig("Negative index in crash report handler (0/18)")
    b = sig("Negative index in crash report handler (0/34)")
    assert a == b, "same fault with a different index should collapse"


def test_coordinates_are_folded():
    a = sig("Couldn't parse element blocks/x at 128 64 -512")
    b = sig("Couldn't parse element blocks/x at -960 12 384")
    assert a == b


def test_uuids_are_folded():
    a = sig("player eb114e6a-26e8-4660-ac70-e2a95f56415e failed")
    b = sig("player 9fbd3454-b732-438b-b021-c410e702d973 failed")
    assert a == b
    assert "<uuid>" in a


def test_different_faults_stay_apart():
    a = sig("Unknown registry key in ResourceKey[minecraft:item]: create:foo")
    b = sig("Encountered unknown or non-serializable data attachment apotheosis:tier")
    assert a != b


def test_same_fault_different_item_collapses():
    """Verbatim from the running server's log.

    These name different items, but they are one fault: this mod's loot
    tables reference registry keys that do not exist. Reporting 612 separate
    lines naming each item is the noise this exists to remove -- one line with
    a count is the actionable message.
    """
    lines = [
        "Couldn't parse element ResourceKey[minecraft:root / minecraft:loot_table]"
        ":create_enchantment_industry:blocks/brass_bookshelf - Unknown registry key",
        "Couldn't parse element ResourceKey[minecraft:root / minecraft:loot_table]"
        ":create_enchantment_industry:blocks/creative_bookshelf - Unknown registry key",
    ]
    assert sig(lines[0]) == sig(lines[1])

    repeats = [
        "Encountered unknown or non-serializable data attachment apotheosis:tier_augments_ap",
        "Encountered unknown or non-serializable data attachment apotheosis:tier_augments_ap",
    ]
    assert sig(repeats[0]) == sig(repeats[1])


def test_different_mods_do_not_collapse_together():
    # Grouping by fault class must not go so far as to merge unrelated mods.
    a = sig("Couldn't parse element ResourceKey[x]:create_dragons_plus:blocks/tank - Unknown registry key")
    b = sig("Couldn't parse element ResourceKey[x]:create_enchantment_industry:blocks/shelf - Unknown registry key")
    assert a != b


def test_signature_is_bounded():
    assert len(sig("x" * 5000)) <= 300


def test_signature_is_single_line():
    s = sig("first line\nsecond line\tthird")
    assert "\n" not in s and "\t" not in s


def test_parse_still_classifies_errors_as_severe():
    e = events.parse(PRE + "Couldn't parse element foo - Unknown registry key")
    assert e is not None and e.kind == SEVERE


def test_dedup_ratio_on_a_realistic_burst():
    """690 lines from 11 causes should collapse to roughly 11 signatures."""
    causes = [
        "Couldn't parse element ResourceKey[a]:mymod:blocks/{i} - Unknown registry key",
        "Encountered unknown or non-serializable data attachment apotheosis:tier",
        "Negative index in crash report handler (0/18)",
        "Couldn't load tag aether_villages:collections/x as it is missing references",
        "Invalid path in pack: cobblemon:dex_additions/README.md, ignoring",
        "Attempted to load class net/minecraft/client/gui/screens/Screen for invalid dist",
        "Access transformer file META-INF/accesstransformer.cfg provided by mod wdutils",
        "Parsing error loading recipe endersdelight:chorus_pie_slice",
        "Couldn't parse data file minecraft:beetroot_soup from minecraft:recipe/x.json",
        "Fabric API detected! This is not a Fabric mod",
        "Unable to apply dex addition cobblemon:mega_z as the sub-dex does not exist",
    ]
    items = ["brass_bookshelf", "creative_bookshelf", "affix_augmentor",
             "fragile_fluid_tank", "levitite_tank", "chorus_pie", "ender_noodle"]
    burst = []
    for i in range(90):                     # 90 * 11 = 990 lines
        for c in causes:
            burst.append(c.replace("{i}", items[i % len(items)]))
    sigs = {sig(line) for line in burst}
    assert len(burst) > 600
    # 990 lines from 11 causes must collapse to about 11 signatures, or the
    # channel is unreadable.
    assert len(sigs) <= len(causes) + 1, f"expected ~{len(causes)}, got {len(sigs)}"
