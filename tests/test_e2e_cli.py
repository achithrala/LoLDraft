"""End-to-end test: drives the CLI through a complete SOLOQ draft against
ManualCSVProvider only. No network access is possible here -- the CLI hard-codes
ManualCSVProvider in Phase 1, and that provider never imports httpx or touches the
network.
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

    for champ, role in PICKS:
        _run("pick", champ, "--role", role)

    state_output = _run("state")
    assert "Draft complete." in state_output
    assert "Aatrox" in state_output
    assert "Lulu" in state_output

    # No more actions should be legal once the draft is complete.
    result = runner.invoke(app, ["ban", "Aatrox"])
    assert result.exit_code != 0


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
