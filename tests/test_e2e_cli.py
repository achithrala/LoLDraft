"""End-to-end tests: drive the CLI through complete SOLOQ and TOURNAMENT drafts
against ManualCSVProvider (the CLI's default). No network access is possible here --
ManualCSVProvider never imports httpx or touches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from draftiq.cli import app

runner = CliRunner()


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
