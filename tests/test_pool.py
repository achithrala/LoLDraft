"""Tests for the champion-pool weighting model/persistence layer: `ChampionPool`,
`TeamRoster`, `consolidated_pool_ids`, `add_to_pool_registry` (all in `models.py`),
and `persistence.load_pool_registry`/`save_pool_registry`/
`get_active_or_default_provider`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from draftiq import persistence
from draftiq.draft.state import DraftStateMachine
from draftiq.models import (
    Champion,
    ChampionPool,
    DraftMode,
    ProviderName,
    Role,
    add_to_pool_registry,
    consolidated_pool_ids,
)
from draftiq.providers.manual import ManualCSVProvider
from draftiq.providers.opgg import OpggProvider

AATROX = 1
DARIUS = 2
JAX = 4


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def champions() -> list[Champion]:
    return ManualCSVProvider().get_champions()


class TestChampionPoolResolveIds:
    def test_no_entry_for_role_returns_none(self, champions: list[Champion]) -> None:
        pool = ChampionPool(by_role={Role.TOP: ["Aatrox"]})
        assert pool.resolve_ids(Role.JUNGLE, champions) is None

    def test_entry_resolves_case_insensitively(self, champions: list[Champion]) -> None:
        pool = ChampionPool(by_role={Role.TOP: ["aatrox", "DARIUS"]})
        assert pool.resolve_ids(Role.TOP, champions) == {AATROX, DARIUS}

    def test_entry_with_no_matching_names_returns_empty_set(
        self, champions: list[Champion]
    ) -> None:
        pool = ChampionPool(by_role={Role.TOP: ["NotAChampion"]})
        assert pool.resolve_ids(Role.TOP, champions) == set()

    def test_resolves_by_name_across_differently_numbered_registries(self) -> None:
        """The whole reason pools are stored by name, not champion_id:
        ManualCSVProvider's ids are a local synthetic numbering while
        OpggProvider's are real Data Dragon ids -- the same pool must resolve to
        the *correct, different* id against each registry."""
        pool = ChampionPool(by_role={Role.TOP: ["Aatrox"]})
        manual_registry = [Champion(champion_id=1, name="Aatrox", ddragon_id="Aatrox")]
        opgg_like_registry = [Champion(champion_id=266, name="Aatrox", ddragon_id="Aatrox")]
        assert pool.resolve_ids(Role.TOP, manual_registry) == {1}
        assert pool.resolve_ids(Role.TOP, opgg_like_registry) == {266}


class TestConsolidatedPoolIds:
    def test_no_players_have_data_returns_none(self, champions: list[Champion]) -> None:
        registry: dict[str, ChampionPool] = {}
        assert consolidated_pool_ids(registry, ["Alice", "Bob"], Role.TOP, champions) is None

    def test_unions_multiple_players(self, champions: list[Champion]) -> None:
        registry = {
            "Alice": ChampionPool(by_role={Role.TOP: ["Aatrox"]}),
            "Bob": ChampionPool(by_role={Role.TOP: ["Darius"]}),
        }
        result = consolidated_pool_ids(registry, ["Alice", "Bob"], Role.TOP, champions)
        assert result == {AATROX, DARIUS}

    def test_player_not_in_roster_ignored(self, champions: list[Champion]) -> None:
        registry = {"Alice": ChampionPool(by_role={Role.TOP: ["Aatrox"]})}
        result = consolidated_pool_ids(registry, ["Alice", "Ghost"], Role.TOP, champions)
        assert result == {AATROX}

    def test_defined_but_unresolvable_is_empty_set_not_none(
        self, champions: list[Champion]
    ) -> None:
        registry = {"Alice": ChampionPool(by_role={Role.TOP: ["NotAChampion"]})}
        result = consolidated_pool_ids(registry, ["Alice"], Role.TOP, champions)
        assert result == set()

    def test_one_player_defined_one_not_still_unions(self, champions: list[Champion]) -> None:
        """A player with no pool entry for this role contributes nothing, but
        doesn't turn the overall result into None if another player does have data."""
        registry = {
            "Alice": ChampionPool(by_role={Role.TOP: ["Aatrox"]}),
            "Bob": ChampionPool(by_role={Role.JUNGLE: ["Jax"]}),  # no top entry
        }
        result = consolidated_pool_ids(registry, ["Alice", "Bob"], Role.TOP, champions)
        assert result == {AATROX}


class TestAddToPoolRegistry:
    def test_adds_new_player(self, champions: list[Champion]) -> None:
        registry: dict[str, ChampionPool] = {}
        champ_by_id = {c.champion_id: c for c in champions}
        added = add_to_pool_registry(registry, "Alice", Role.TOP, [champ_by_id[AATROX]])
        assert [c.name for c in added] == ["Aatrox"]
        assert registry["Alice"].by_role[Role.TOP] == ["Aatrox"]

    def test_dedupes_case_insensitively(self, champions: list[Champion]) -> None:
        registry: dict[str, ChampionPool] = {"Alice": ChampionPool(by_role={Role.TOP: ["Aatrox"]})}
        champ_by_id = {c.champion_id: c for c in champions}
        added = add_to_pool_registry(registry, "Alice", Role.TOP, [champ_by_id[AATROX]])
        assert added == []
        assert registry["Alice"].by_role[Role.TOP] == ["Aatrox"]


class TestPoolRegistryPersistence:
    def test_load_missing_file_returns_empty_dict(self) -> None:
        assert persistence.load_pool_registry() == {}

    def test_round_trip(self) -> None:
        registry = {"Alice": ChampionPool(by_role={Role.TOP: ["Aatrox", "Darius"]})}
        persistence.save_pool_registry(registry)
        loaded = persistence.load_pool_registry()
        assert loaded == registry


class TestGetActiveOrDefaultProvider:
    def test_no_draft_falls_back_to_manual(self) -> None:
        provider = persistence.get_active_or_default_provider()
        assert isinstance(provider, ManualCSVProvider)

    def test_active_draft_uses_its_provider(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ, provider=ProviderName.OPGG)
        persistence.save_state_machine(sm)
        provider = persistence.get_active_or_default_provider()
        assert isinstance(provider, OpggProvider)

    def test_corrupt_state_file_falls_back_to_manual(self) -> None:
        persistence.STATE_DIR.mkdir(exist_ok=True)
        persistence.STATE_FILE.write_text("not valid json{{{")
        provider = persistence.get_active_or_default_provider()
        assert isinstance(provider, ManualCSVProvider)
