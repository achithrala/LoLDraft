"""Core data models shared across providers, stats, draft, and search modules.

Every stats-bearing model (ChampionStats, Matchup, Synergy) carries raw `wins`/`games`
counts rather than pre-computed percentages -- the shrinkage layer needs sample sizes,
and a provider that can't supply them must return `games=0` rather than fabricate one.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Role(StrEnum):
    TOP = "top"
    JUNGLE = "jungle"
    MID = "mid"
    BOTTOM = "bottom"
    SUPPORT = "support"


class RankBracket(StrEnum):
    """Matches OP.GG's real `tier` vocabulary exactly (confirmed live against
    `lol_get_champion_analysis` in Phase 2) rather than the coarser 5-value
    placeholder Phase 1 shipped with. `IBSG` (an OP.GG-defined aggregate band,
    presumably Iron+Bronze+Silver+Gold) is intentionally omitted -- OP.GG doesn't
    document what it covers, and guessing would be exactly the kind of silent
    schema-guessing the project spec forbids."""

    IRON = "iron"
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    GOLD_PLUS = "gold_plus"
    PLATINUM = "platinum"
    PLATINUM_PLUS = "platinum_plus"
    EMERALD = "emerald"
    EMERALD_PLUS = "emerald_plus"
    DIAMOND = "diamond"
    DIAMOND_PLUS = "diamond_plus"
    MASTER = "master"
    MASTER_PLUS = "master_plus"
    GRANDMASTER = "grandmaster"
    CHALLENGER = "challenger"
    ALL = "all"


class Side(StrEnum):
    BLUE = "blue"
    RED = "red"

    def other(self) -> Side:
        return Side.RED if self is Side.BLUE else Side.BLUE


class ActionType(StrEnum):
    BAN = "ban"
    PICK = "pick"


class DraftMode(StrEnum):
    SOLOQ = "soloq"
    TOURNAMENT = "tournament"


class ProviderName(StrEnum):
    MANUAL = "manual"
    OPGG = "opgg"


class Champion(BaseModel):
    """Canonical champion identity. `champion_id` is the numeric key every other
    provider's data is mapped onto; `ddragon_id` is Data Dragon's string id
    (e.g. "Aatrox"), kept around for asset/URL construction."""

    champion_id: int
    name: str
    ddragon_id: str
    tags: list[str] = Field(default_factory=list)


class ChampionStats(BaseModel):
    champion_id: int
    role: Role
    rank: RankBracket
    patch: str
    wins: int
    games: int
    pick_count: int = 0
    ban_count: int = 0
    total_games: int = 0  # bracket-wide denominator for pick_rate / ban_rate
    source: str = "unknown"

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games if self.games > 0 else None

    @property
    def pick_rate(self) -> float | None:
        return self.pick_count / self.total_games if self.total_games > 0 else None

    @property
    def ban_rate(self) -> float | None:
        return self.ban_count / self.total_games if self.total_games > 0 else None


class Matchup(BaseModel):
    champion_id: int
    opponent_id: int
    role: Role
    rank: RankBracket
    patch: str
    wins: int
    games: int
    source: str = "unknown"

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games if self.games > 0 else None


class Synergy(BaseModel):
    champion_id: int
    ally_id: int
    rank: RankBracket
    patch: str
    wins: int
    games: int
    source: str = "unknown"

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.games if self.games > 0 else None


class Build(BaseModel):
    champion_id: int
    role: Role
    rank: RankBracket
    opponent_id: int | None
    patch: str
    starting_items: list[str] = Field(default_factory=list)
    items: list[str] = Field(default_factory=list)
    runes_primary: list[str] = Field(default_factory=list)
    runes_secondary: list[str] = Field(default_factory=list)
    rune_shards: list[str] = Field(default_factory=list)
    skill_order: list[str] = Field(default_factory=list)
    summoner_spells: list[str] = Field(default_factory=list)
    source: str = "unknown"


class TermContribution(BaseModel):
    """One labeled term in a score's breakdown, e.g. ("base_rate", 0.512)."""

    label: str
    value: float


class Recommendation(BaseModel):
    champion_id: int
    champion_name: str
    total_score: float
    p_hat: float
    ci_low: float
    ci_high: float
    n_games: int
    terms: list[TermContribution]


class DraftAction(BaseModel):
    side: Side
    action_type: ActionType
    champion_id: int
    role: Role | None = None  # required for picks, absent for bans


class DraftState(BaseModel):
    mode: DraftMode
    rank: RankBracket = RankBracket.ALL
    provider: ProviderName = ProviderName.MANUAL
    actions: list[DraftAction] = Field(default_factory=list)
