"""The provider abstraction. Every stats source (Data Dragon, manual CSV, OP.GG in
Phase 2) implements this Protocol so the scoring and CLI layers never know or care
where a number came from.

Critical contract: `get_champion_stats`, `get_matchup`, and `get_synergy` return raw
`wins`/`games` counts, never a pre-computed percentage. The shrinkage layer needs
sample sizes to know how much to trust a number. A provider with no data for a given
query must return `games=0` (or raise `NotImplementedError` if it has no concept of
the query at all, e.g. Data Dragon has no stats) rather than silently guess.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from draftiq.models import Build, Champion, ChampionStats, Matchup, RankBracket, Role, Synergy


@runtime_checkable
class StatsProvider(Protocol):
    def get_patch(self) -> str: ...

    def get_champions(self) -> list[Champion]: ...

    def get_champion_stats(
        self, champion_id: int, role: Role, rank: RankBracket
    ) -> ChampionStats: ...

    def get_matchup(
        self, champion_id: int, opponent_id: int, role: Role, rank: RankBracket
    ) -> Matchup: ...

    def get_synergy(self, champion_id: int, ally_id: int, rank: RankBracket) -> Synergy: ...

    def get_build(
        self,
        champion_id: int,
        role: Role,
        rank: RankBracket,
        opponent_id: int | None = None,
    ) -> Build: ...
