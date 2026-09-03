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


# --- escape hatches: refused below admin, permitted at admin -----------

ESCAPE_HATCHES = [
    "execute as @a run op Chito_",
    "data modify block 0 0 0 Items set value []",
    "fill 0 0 0 100 100 100 tnt",
    "give @a command_block",
    "summon creeper",
    "gamemode creative Chito_",
    "tp Chito_ 0 0 0",
]


@pytest.mark.parametrize("cmd", ESCAPE_HATCHES)
def test_escape_hatches_refused_below_admin(cmd):
    for tier in (TIER_READ, TIER_MOD):
        assert not authz.check(cmd, tier).allowed, f"{cmd} allowed at tier {tier}"


@pytest.mark.parametrize("cmd", ESCAPE_HATCHES)
def test_admin_may_run_anything(cmd):
    # Admins hold op level 4 in game and can run these from their own chat,
    # so blocking them here would obstruct without protecting.
    assert authz.check(cmd, TIER_ADMIN).allowed, cmd


def test_admin_may_run_commands_with_no_rule_at_all():
    for cmd in ["effect give @a minecraft:glowing 10", "locate structure village",
                "scoreboard objectives list", "tellraw @a {\"text\":\"hi\"}"]:
        assert authz.check(cmd, TIER_ADMIN).allowed, cmd


def test_admin_destructive_commands_still_confirm():
    for cmd in ["stop", "op Chito_", "ban Keynash", "save-off",
                "kill @a", "datapack disable \"file/x.zip\"", "forceload add 0 0"]:
        d = authz.check(cmd, TIER_ADMIN)
        assert d.allowed, cmd
        assert d.confirm, f"{cmd} should require confirmation"


def test_admin_ordinary_commands_do_not_confirm():
    for cmd in ["list", "time set day", "gamerule keepInventory true",
                "effect clear @a"]:
        assert not authz.check(cmd, TIER_ADMIN).confirm, cmd


def test_refusal_below_admin_explains_why():
    d = authz.check("execute run stop", TIER_MOD)
    assert not d.allowed
    assert "admin-only" in d.reason


def test_leading_slash_is_stripped_not_a_bypass():
    assert authz.check("/list", TIER_READ).allowed
    # Slashes are stripped, so this is just `execute` -- refused below admin.
    assert not authz.check("//execute run stop", TIER_MOD).allowed


def test_partial_match_does_not_pass_below_admin():
    # 'list' is allowed at tier 0; 'listen' must not inherit that.
    assert not authz.check("listen", TIER_MOD).allowed
    # A trailing payload after an exact-match rule must not slip through.
    assert not authz.check("stop; rm -rf /", TIER_MOD).allowed
    assert not authz.check("seed && op Chito_", TIER_MOD).allowed


def test_newlines_refused_even_for_admins():
    # One command per call, at every tier. This is a correctness rule, not a
    # policy one: a second line would run unseen by the confirmation prompt.
    assert not authz.check("list\nop Chito_", TIER_ADMIN).allowed
    assert not authz.check("say hi\r\nstop", TIER_ADMIN).allowed


def test_empty_command_refused_for_admins():
    assert not authz.check("", TIER_ADMIN).allowed
    assert not authz.check("   ", TIER_ADMIN).allowed
    assert not authz.check("///", TIER_ADMIN).allowed


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

def _g(**kw):
    return authz.Grants(**{k: frozenset(v) for k, v in kw.items()})


def test_admin_by_user_id():
    assert authz.tier_for(1, "someone", set(), _g(admin_ids={1})) == TIER_ADMIN


def test_admin_by_username():
    g = _g(admin_usernames={"new_metoadbird", "saddadchito"})
    assert authz.tier_for(999, "new_metoadbird", set(), g) == TIER_ADMIN
    assert authz.tier_for(998, "saddadchito", set(), g) == TIER_ADMIN
    assert authz.tier_for(997, "someone_else", set(), g) == TIER_READ


def test_username_match_is_case_insensitive():
    g = _g(admin_usernames={"new_metoadbird"})
    assert authz.tier_for(1, "NEW_MetoadBird", set(), g) == TIER_ADMIN


def test_username_match_is_exact_not_substring():
    # A near-miss handle must not inherit admin.
    g = _g(admin_usernames={"saddadchito"})
    assert authz.tier_for(1, "saddadchito2", set(), g) == TIER_READ
    assert authz.tier_for(1, "addadchito", set(), g) == TIER_READ
    assert authz.tier_for(1, "xsaddadchitox", set(), g) == TIER_READ


def test_admin_by_role():
    assert authz.tier_for(2, "u", {10}, _g(admin_roles={10})) == TIER_ADMIN


def test_mod_by_role_and_username():
    g = _g(admin_roles={10}, mod_roles={20}, mod_usernames={"helper"})
    assert authz.tier_for(2, "u", {20}, g) == TIER_MOD
    assert authz.tier_for(3, "helper", set(), g) == TIER_MOD


def test_highest_grant_wins():
    g = _g(admin_roles={10}, mod_roles={20})
    assert authz.tier_for(2, "u", {10, 20}, g) == TIER_ADMIN
    g2 = _g(admin_usernames={"boss"}, mod_roles={20})
    assert authz.tier_for(2, "boss", {20}, g2) == TIER_ADMIN


def test_unknown_user_defaults_to_read_only():
    assert authz.tier_for(12345, "nobody", set(), _g()) == TIER_READ
    # Empty/missing username must not match an empty grant set by accident.
    assert authz.tier_for(12345, "", set(), _g(admin_usernames=set())) == TIER_READ


# --- help output ---------------------------------------------------------

def test_usage_strings_are_readable_not_regex():
    for tier in (TIER_READ, TIER_MOD, TIER_ADMIN):
        for u in authz.usage_at(tier):
            assert "\\s" not in u and "[A-Za-z" not in u, u
            assert not u.startswith("^"), u


def test_allowed_at_is_cumulative():
    r, m = authz.allowed_at(TIER_READ), authz.allowed_at(TIER_MOD)
    assert set(r).issubset(set(m))
    assert len(m) > len(r)


def test_confirm_verbs_cover_the_dangerous_ones():
    v = authz.confirm_verbs()
    for expected in ("stop", "op", "ban", "save-off", "kill"):
        assert expected in v
