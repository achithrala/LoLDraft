"""Data Dragon provider -- the canonical champion registry.

Data Dragon (https://ddragon.leagueoflegends.com) has no win-rate, matchup, synergy,
or build data at all, so those methods raise NotImplementedError with a pointer to a
real stats provider rather than fabricating a `games=0` result. `games=0` means "a
stats provider looked and found nothing"; NotImplementedError means "this source was
never capable of answering this question" -- collapsing the two would hide a real bug
if this provider were ever wired into scoring by mistake.
"""

from __future__ import annotations

import httpx

from draftiq.models import Build, Champion, ChampionStats, Matchup, RankBracket, Role, Synergy
from draftiq.providers.cache import SQLiteCache, cached

BASE_URL = "https://ddragon.leagueoflegends.com"
_NO_STATS_MSG = (
    "Data Dragon has no {kind} data; use a stats provider (e.g. ManualCSVProvider "
    "or, in Phase 2, the OP.GG provider)."
)


class DataDragonProvider:
    def __init__(
        self,
        cache: SQLiteCache | None = None,
        client: httpx.Client | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self._source = "ddragon"
        self._cache = cache or SQLiteCache()
        self._client = client or httpx.Client(base_url=base_url, timeout=10.0)

    @cached(ttl_seconds=3600.0, keyed_by_patch=False)
    def get_patch(self) -> str:
        response = self._client.get("/api/versions.json")
        response.raise_for_status()
        versions: list[str] = response.json()
        if not versions:
            raise RuntimeError("Data Dragon returned an empty versions list")
        return versions[0]

    @cached(ttl_seconds=86400.0)
    def get_champions(self) -> list[Champion]:
        patch = self.get_patch()
        response = self._client.get(f"/cdn/{patch}/data/en_US/champion.json")
        response.raise_for_status()
        payload = response.json()
        champions = []
        for entry in payload["data"].values():
            champions.append(
                Champion(
                    champion_id=int(entry["key"]),
                    name=entry["name"],
                    ddragon_id=entry["id"],
                    tags=list(entry.get("tags", [])),
                )
            )
        return sorted(champions, key=lambda c: c.champion_id)

    def get_champion_stats(self, champion_id: int, role: Role, rank: RankBracket) -> ChampionStats:
        raise NotImplementedError(_NO_STATS_MSG.format(kind="champion win-rate"))

    def get_matchup(
        self, champion_id: int, opponent_id: int, role: Role, rank: RankBracket
    ) -> Matchup:
        raise NotImplementedError(_NO_STATS_MSG.format(kind="matchup"))

    def get_synergy(self, champion_id: int, ally_id: int, rank: RankBracket) -> Synergy:
        raise NotImplementedError(_NO_STATS_MSG.format(kind="synergy"))

    def get_build(
        self,
        champion_id: int,
        role: Role,
        rank: RankBracket,
        opponent_id: int | None = None,
    ) -> Build:
        raise NotImplementedError(_NO_STATS_MSG.format(kind="build"))

    def close(self) -> None:
        self._client.close()
