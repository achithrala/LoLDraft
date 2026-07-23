"""End-to-end tests: drive the CLI through complete SOLOQ and TOURNAMENT drafts
against ManualCSVProvider (the CLI's default). No network access is possible here --
ManualCSVProvider never imports httpx or touches the network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from draftiq.cli import app

runner = CliRunner()


def _row_count(output: str) -> int:
    """Counts data rows in a rendered recommendations table by counting lines
    that *start* with a `│ <digits> │` row-index cell. Anchored to line start
    (not just "appears somewhere") so it isn't fooled by other columns that also
    contain bare digits (e.g. n games) -- and robust against the Breakdown
    column's word-wrap potentially pulling another champion's name into view via
    a cross-reference term like "exposure to <champ>", which a naive substring
    search for a champion's name would otherwise misread as "still a candidate"."""
    return len(re.findall(r"(?m)^│\s*\d+\s*│", output))


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # draftiq persists draft state to `.draftiq/state.json` under the cwd; run every
    # test in its own scratch directory so they can't see each other's state.
    monkeypatch.chdir(tmp_path)


BANS = [
    "Malphite",
    "Jax",
    "Renekton",
    "Sejuani",
    "Kindred",
    "Xin Zhao",
    "Orianna",
    "Yasuo",
    "Miss Fortune",
    "Nautilus",
]

# (champion, role) in strict B1 / R1 R2 / B2 B3 / R3 R4 / B4 B5 / R5 order.
PICKS = [
    ("Aatrox", "top"),  # B1
    ("Darius", "top"),  # R1
    ("Vi", "jungle"),  # R2
    ("Lee Sin", "jungle"),  # B2
    ("Ahri", "mid"),  # B3
    ("Zed", "mid"),  # R3
    ("Caitlyn", "bottom"),  # R4
    ("Jinx", "bottom"),  # B4
    ("Thresh", "support"),  # B5
    ("Lulu", "support"),  # R5
]


def _run(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_full_soloq_draft_completes_offline() -> None:
    _run("new", "--mode", "soloq")

    for champ in BANS:
        _run("ban", champ)

    suggest_output = _run("suggest", "--role", "top")
    assert "Jax" in suggest_output or "Aatrox" in suggest_output or "Darius" in suggest_output

    build_output = _run("build", "Aatrox", "--role", "top")
    assert "Aatrox build" in build_output
    assert "Items:" in build_output

    for champ, role in PICKS:
        _run("pick", champ, "--role", role)

    state_output = _run("state")
    assert "Draft complete." in state_output
    assert "Aatrox" in state_output
    assert "Lulu" in state_output

    # No more actions should be legal once the draft is complete.
    result = runner.invoke(app, ["ban", "Aatrox"])
    assert result.exit_code != 0


def test_full_tournament_draft_completes_offline() -> None:
    _run("new", "--mode", "tournament")

    # Ban phase 1 (6, B/R/B/R/B/R).
    for champ in ["Malphite", "Jax", "Renekton", "Sejuani", "Kindred", "Xin Zhao"]:
        _run("ban", champ)

    # Pick phase 1 (6, B/R/R/B/B/R).
    for champ, role in [
        ("Aatrox", "top"),
        ("Darius", "top"),
        ("Vi", "jungle"),
        ("Lee Sin", "jungle"),
        ("Ahri", "mid"),
        ("Zed", "mid"),
    ]:
        _run("pick", champ, "--role", role)

    # Ban phase 2 (4, R/B/R/B).
    for champ in ["Orianna", "Yasuo", "Miss Fortune", "Nautilus"]:
        _run("ban", champ)

    # Pick phase 2 (4, R/B/B/R).
    for champ, role in [
        ("Caitlyn", "bottom"),
        ("Jinx", "bottom"),
        ("Thresh", "support"),
        ("Lulu", "support"),
    ]:
        _run("pick", champ, "--role", role)

    state_output = _run("state")
    assert "Draft complete." in state_output
    assert "Aatrox" in state_output
    assert "Lulu" in state_output


def test_rejects_duplicate_ban() -> None:
    _run("new")
    _run("ban", "Aatrox")
    result = runner.invoke(app, ["ban", "Aatrox"])
    assert result.exit_code != 0


def test_rejects_unknown_champion_name() -> None:
    _run("new")
    result = runner.invoke(app, ["ban", "NotAChampion"])
    assert result.exit_code != 0
    assert "Unknown champion" in result.output


def test_suggest_before_new_fails_cleanly() -> None:
    result = runner.invoke(app, ["suggest", "--role", "top"])
    assert result.exit_code != 0
    assert "No draft in progress" in result.output


def test_suggest_without_role_during_pick_phase_fails_cleanly() -> None:
    _run("new")
    for champ in BANS:
        _run("ban", champ)
    result = runner.invoke(app, ["suggest"])
    assert result.exit_code != 0
    assert "--role is required" in result.output


def test_suggest_with_no_role_during_ban_phase_shows_ban_recommendations() -> None:
    _run("new")
    output = _run("suggest")
    assert "Suggesting bans for blue" in output


def test_build_shows_items_runes_skills_summoners() -> None:
    _run("new")
    output = _run("build", "Jinx", "--role", "bottom")
    assert "Jinx build (bottom" in output
    assert "Items:" in output
    assert "Skill order:" in output
    assert "Summoners:" in output


def test_build_rejects_unknown_champion() -> None:
    _run("new")
    result = runner.invoke(app, ["build", "NotAChampion", "--role", "top"])
    assert result.exit_code != 0
    assert "Unknown champion" in result.output


def test_build_accepts_optional_opponent() -> None:
    _run("new")
    output = _run("build", "Aatrox", "--role", "top", "--opponent", "Darius")
    assert "Aatrox build" in output


def test_suggest_any_role_shows_role_column_during_pick_phase() -> None:
    _run("new")
    for champ in BANS:
        _run("ban", champ)
    output = _run("suggest", "--any-role")
    assert "priority picks" in output
    assert "Role" in output


def test_suggest_any_role_rejected_during_ban_phase() -> None:
    _run("new")
    result = runner.invoke(app, ["suggest", "--any-role"])
    assert result.exit_code != 0
    assert "only applies to picks" in result.output


def test_suggest_any_role_and_lookahead_rejected_together() -> None:
    _run("new")
    for champ in BANS:
        _run("ban", champ)
    result = runner.invoke(app, ["suggest", "--any-role", "--lookahead"])
    assert result.exit_code != 0
    assert "can't be combined" in result.output


def test_pool_add_show_remove_clear() -> None:
    output = _run("pool", "add", "Me", "top", "Aatrox", "Darius")
    assert "Aatrox" in output and "Darius" in output

    output = _run("pool", "show", "Me")
    assert "Aatrox" in output and "Darius" in output

    output = _run("pool", "remove", "Me", "top", "Darius")
    assert "Darius" in output
    output = _run("pool", "show", "Me", "top")
    assert "Aatrox" in output
    assert "Darius" not in output

    _run("pool", "clear", "Me", "top")
    output = _run("pool", "show", "Me", "top")
    assert "(empty)" in output


def test_pool_clear_requires_exactly_one_of_role_or_all() -> None:
    _run("pool", "add", "Me", "top", "Aatrox")
    result = runner.invoke(app, ["pool", "clear", "Me"])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_pool_works_before_any_draft_started() -> None:
    """Pool commands bootstrap to ManualCSVProvider when no draft exists yet."""
    output = _run("pool", "add", "Me", "top", "Aatrox")
    assert "Aatrox" in output


def test_roster_add_show_remove_requires_active_draft() -> None:
    result = runner.invoke(app, ["roster", "add", "ally", "Me"])
    assert result.exit_code != 0
    assert "No draft in progress" in result.output

    _run("new")
    _run("roster", "add", "ally", "Me")
    _run("roster", "add", "enemy", "EnemyMid")
    output = _run("roster", "show")
    assert "Me" in output and "EnemyMid" in output

    _run("roster", "remove", "enemy", "EnemyMid")
    output = _run("roster", "show")
    assert "EnemyMid" not in output


def test_suggest_pool_restricts_pick_candidates() -> None:
    _run("pool", "add", "Me", "top", "Aatrox")
    _run("new")
    _run("roster", "add", "ally", "Me")
    for champ in BANS:
        _run("ban", champ)

    unrestricted = _run("suggest", "--role", "top", "--top", "10")
    restricted = _run("suggest", "--role", "top", "--pool", "--top", "10")

    # Aatrox correctly still shows a legitimate "exposure to Darius" term in its
    # own breakdown (Darius remains a real threat the enemy could still draft --
    # see the exposure-preservation fix in greedy.py), so a plain substring check
    # for "Darius" would be misled by that cross-reference. Row count is the
    # actual measure of how many *candidates* were returned.
    assert "Aatrox" in restricted
    assert _row_count(restricted) == 1
    assert _row_count(unrestricted) > 1


def test_suggest_pool_adds_bonus_during_ban_without_shrinking_list() -> None:
    _run("pool", "add", "EnemyMid", "mid", "Ahri")
    _run("new")
    _run("roster", "add", "enemy", "EnemyMid")

    unrestricted = _run("suggest", "--top", "20")
    with_pool = _run("suggest", "--pool", "--top", "20")

    # Normalize: the Breakdown column can word-wrap "in enemy pool" across two
    # rendered lines, with the wrapped continuation line carrying literal "│"
    # column-border characters between the two halves (empty cells for every
    # column before the Breakdown one) -- strip those too, not just whitespace,
    # or the search would break without the actual content having changed.
    unrestricted_flat = " ".join(unrestricted.replace("│", " ").split())
    with_pool_flat = " ".join(with_pool.replace("│", " ").split())

    assert "Ahri" in unrestricted_flat
    assert "Ahri" in with_pool_flat  # still present -- a bonus, not a restriction
    assert "in enemy pool" in with_pool_flat
    assert "in enemy pool" not in unrestricted_flat
    assert _row_count(with_pool) == _row_count(unrestricted)  # list isn't narrowed


def test_suggest_pool_during_ban_is_no_longer_rejected() -> None:
    """Confirms the reversal from the single-pool design: --pool is now valid
    (and meaningful) during a ban, not an error."""
    _run("new")
    result = runner.invoke(app, ["suggest", "--pool"])
    assert result.exit_code == 0
