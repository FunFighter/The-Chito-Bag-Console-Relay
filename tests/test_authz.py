import pytest

from relay import authz
from relay.authz import TIER_ADMIN, TIER_MOD, TIER_READ


@pytest.mark.parametrize("cmd", ["list", "seed", "whitelist list", "datapack list"])
def test_read_tier_allows_harmless_queries(cmd):
    assert authz.check(cmd, TIER_READ).allowed


@pytest.mark.parametrize("cmd", ["kick Chito_", "weather clear", "op Chito_", "stop"])
def test_read_tier_refuses_anything_that_changes_state(cmd):
    assert not authz.check(cmd, TIER_READ).allowed


def test_mod_can_kick_but_not_op():
    assert authz.check("kick Chito_ being rude", TIER_MOD).allowed
    d = authz.check("op Chito_", TIER_MOD)
    assert not d.allowed
    assert "admin" in d.reason


def test_admin_privilege_changes_require_confirmation():
    for cmd in ["op Chito_", "ban Keynash", "stop", "save-off"]:
        d = authz.check(cmd, TIER_ADMIN)
        assert d.allowed, cmd
        assert d.confirm, f"{cmd} should require confirmation"


def test_reversible_admin_commands_do_not_require_confirmation():
    assert not authz.check("gamerule keepInventory true", TIER_ADMIN).confirm


# --- the cases that actually matter -------------------------------------

@pytest.mark.parametrize("cmd", [
    "execute as @a run op Chito_",
    "data modify block 0 0 0 Items set value []",
    "fill 0 0 0 100 100 100 tnt",
    "give @a command_block",
    "summon creeper",
    "gamemode creative Chito_",
    "tp Chito_ 0 0 0",
])
def test_escape_hatches_are_refused_at_every_tier(cmd):
    for tier in (TIER_READ, TIER_MOD, TIER_ADMIN):
        assert not authz.check(cmd, tier).allowed, f"{cmd} allowed at tier {tier}"


def test_refusal_explains_why_for_known_dangerous_verbs():
    d = authz.check("execute run stop", TIER_ADMIN)
    assert not d.allowed
    assert "bypass" in d.reason


def test_leading_slash_is_stripped_not_a_bypass():
    assert authz.check("/list", TIER_READ).allowed
    assert not authz.check("//execute run stop", TIER_ADMIN).allowed


def test_newline_smuggling_is_refused():
    # Without the newline check, an approved prefix could carry a second
    # command that the pattern never saw.
    assert not authz.check("list\nop Chito_", TIER_ADMIN).allowed
    assert not authz.check("list\r\nstop", TIER_ADMIN).allowed


def test_partial_match_does_not_pass():
    # 'list' is allowed; 'listen' must not inherit that.
    assert not authz.check("listen", TIER_ADMIN).allowed
    # A trailing payload after an exact-match rule must not slip through.
    assert not authz.check("stop; rm -rf /", TIER_ADMIN).allowed
    assert not authz.check("seed && op Chito_", TIER_ADMIN).allowed


def test_player_argument_is_constrained():
    assert authz.check("kick Chito_", TIER_MOD).allowed
    # Selectors are not player names -- @a would kick the whole server.
    assert not authz.check("kick @a", TIER_MOD).allowed
    assert not authz.check("kick ab", TIER_MOD).allowed          # too short
    assert not authz.check("kick " + "x" * 17, TIER_MOD).allowed  # too long


def test_whitespace_is_normalised_before_matching():
    assert authz.check("  list  ", TIER_READ).allowed
    assert authz.check("kick    Chito_", TIER_MOD).allowed


def test_case_insensitive():
    assert authz.check("LIST", TIER_READ).allowed
    assert authz.check("Op Chito_", TIER_ADMIN).allowed


# --- tier resolution -----------------------------------------------------

def test_tier_resolution_prefers_highest_match():
    assert authz.tier_for(1, set(), {1}, set(), set()) == TIER_ADMIN
    assert authz.tier_for(2, {10}, set(), {10}, set()) == TIER_ADMIN
    assert authz.tier_for(2, {20}, set(), {10}, {20}) == TIER_MOD
    assert authz.tier_for(2, {99}, set(), {10}, {20}) == TIER_READ
    # Holding both roles must resolve to admin, not moderator.
    assert authz.tier_for(2, {10, 20}, set(), {10}, {20}) == TIER_ADMIN


def test_unknown_user_defaults_to_read_only():
    assert authz.tier_for(12345, set(), set(), set(), set()) == TIER_READ
