"""Offline provider backed by local CSV files under `data/manual/`.

This is what makes the tool testable and demoable with zero network access: it does
not depend on DataDragonProvider or any HTTP call. It owns its own small champion
registry (`champions.csv`) rather than borrowing Data Dragon's, precisely so it never
needs the network. The synthetic win/game counts are fabricated for demo and test
purposes -- `SYNTH-1` (returned by `get_patch()`) is not a real League patch.

Missing data is expressed the way the provider protocol requires: a matchup or
synergy pair with no row simply returns `games=0`, which the shrinkage layer collapses
to zero influence automatically.
"""

from __future__ import annotations

import csv
from pathlib import Path

from draftiq.models import (
    Build,
    Champion,
    ChampionStats,
    Matchup,
    RankBracket,
    Role,
    Synergy,
)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "manual"
SYNTHETIC_PATCH = "SYNTH-1"


def _split(value: str) -> list[str]:
    return [part for part in value.split(";") if part]


class ManualCSVProvider:
    """Loads all CSVs eagerly at construction time; the dataset is small enough
    (tens of rows per file) that there is no need for lazy loading or a cache layer
    on top of it."""

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR) -> None:
        self._source = "manual"
        self._data_dir = Path(data_dir)
        self._champions = self._load_champions()
        self._stats = self._load_champion_stats()
        self._matchups = self._load_matchups()
        self._synergies = self._load_synergies()
        self._builds = self._load_builds()

    def get_patch(self) -> str:
        return SYNTHETIC_PATCH

    def get_champions(self) -> list[Champion]:
        return list(self._champions.values())

    def get_champion_stats(self, champion_id: int, role: Role, rank: RankBracket) -> ChampionStats:
        key = (champion_id, role, rank)
        stats = self._stats.get(key)
        if stats is not None:
            return stats
        return ChampionStats(
            champion_id=champion_id,
            role=role,
            rank=rank,
            patch=SYNTHETIC_PATCH,
            wins=0,
            games=0,
            source=self._source,
        )

    def get_matchup(
        self, champion_id: int, opponent_id: int, role: Role, rank: RankBracket
    ) -> Matchup:
        key = (champion_id, opponent_id, role, rank)
        matchup = self._matchups.get(key)
        if matchup is not None:
            return matchup
        return Matchup(
            champion_id=champion_id,
            opponent_id=opponent_id,
            role=role,
            rank=rank,
            patch=SYNTHETIC_PATCH,
            wins=0,
            games=0,
            source=self._source,
        )

    def get_synergy(self, champion_id: int, ally_id: int, rank: RankBracket) -> Synergy:
        key = (champion_id, ally_id, rank)
        synergy = self._synergies.get(key)
        if synergy is not None:
            return synergy
        return Synergy(
            champion_id=champion_id,
            ally_id=ally_id,
            rank=rank,
            patch=SYNTHETIC_PATCH,
            wins=0,
            games=0,
            source=self._source,
        )

    def get_build(
        self,
        champion_id: int,
        role: Role,
        rank: RankBracket,
        opponent_id: int | None = None,
    ) -> Build:
        key = (champion_id, role, rank, opponent_id)
        build = self._builds.get(key)
        if build is not None:
            return build
        # Fall back to the role-default build (no opponent match) if a
        # matchup-specific one wasn't found.
        fallback = self._builds.get((champion_id, role, rank, None))
        if fallback is not None:
            return fallback
        raise KeyError(f"No build data for champion_id={champion_id}, role={role}, rank={rank}")

    def _load_champions(self) -> dict[int, Champion]:
        champions: dict[int, Champion] = {}
        with (self._data_dir / "champions.csv").open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                champion_id = int(row["champion_id"])
                champions[champion_id] = Champion(
                    champion_id=champion_id,
                    name=row["name"],
                    ddragon_id=row["ddragon_id"],
                    tags=_split(row["tags"]),
                )
        return champions

    def _load_champion_stats(self) -> dict[tuple[int, Role, RankBracket], ChampionStats]:
        stats: dict[tuple[int, Role, RankBracket], ChampionStats] = {}
        with (self._data_dir / "champion_stats.csv").open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                champion_id = int(row["champion_id"])
                role = Role(row["role"])
                rank = RankBracket(row["rank"])
                stats[(champion_id, role, rank)] = ChampionStats(
                    champion_id=champion_id,
                    role=role,
                    rank=rank,
                    patch=row["patch"],
                    wins=int(row["wins"]),
                    games=int(row["games"]),
                    pick_count=int(row["pick_count"]),
                    ban_count=int(row["ban_count"]),
                    total_games=int(row["total_games"]),
                    source=self._source,
                )
        return stats

    def _load_matchups(self) -> dict[tuple[int, int, Role, RankBracket], Matchup]:
        matchups: dict[tuple[int, int, Role, RankBracket], Matchup] = {}
        with (self._data_dir / "matchups.csv").open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                champion_id = int(row["champion_id"])
                opponent_id = int(row["opponent_id"])
                role = Role(row["role"])
                rank = RankBracket(row["rank"])
                matchups[(champion_id, opponent_id, role, rank)] = Matchup(
                    champion_id=champion_id,
                    opponent_id=opponent_id,
                    role=role,
                    rank=rank,
                    patch=row["patch"],
                    wins=int(row["wins"]),
                    games=int(row["games"]),
                    source=self._source,
                )
        return matchups

    def _load_synergies(self) -> dict[tuple[int, int, RankBracket], Synergy]:
        synergies: dict[tuple[int, int, RankBracket], Synergy] = {}
        with (self._data_dir / "synergies.csv").open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                champion_id = int(row["champion_id"])
                ally_id = int(row["ally_id"])
                rank = RankBracket(row["rank"])
                synergies[(champion_id, ally_id, rank)] = Synergy(
                    champion_id=champion_id,
                    ally_id=ally_id,
                    rank=rank,
                    patch=row["patch"],
                    wins=int(row["wins"]),
                    games=int(row["games"]),
                    source=self._source,
                )
        return synergies

    def _load_builds(
        self,
    ) -> dict[tuple[int, Role, RankBracket, int | None], Build]:
        builds: dict[tuple[int, Role, RankBracket, int | None], Build] = {}
        with (self._data_dir / "builds.csv").open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                champion_id = int(row["champion_id"])
                role = Role(row["role"])
                rank = RankBracket(row["rank"])
                opponent_id = int(row["opponent_id"]) if row["opponent_id"] else None
                builds[(champion_id, role, rank, opponent_id)] = Build(
                    champion_id=champion_id,
                    role=role,
                    rank=rank,
                    opponent_id=opponent_id,
                    patch=row["patch"],
                    starting_items=_split(row["starting_items"]),
                    items=_split(row["items"]),
                    runes_primary=_split(row["runes_primary"]),
                    runes_secondary=_split(row["runes_secondary"]),
                    rune_shards=_split(row["rune_shards"]),
                    skill_order=_split(row["skill_order"]),
                    summoner_spells=_split(row["summoner_spells"]),
                    source=self._source,
                )
        return builds
