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
    role: Role
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


class ChampionPool(BaseModel):
    """One named player's per-role champion pool -- "champions this person actually
    plays." Stored by champion name (`Champion.name`), never `champion_id`:
    `ManualCSVProvider`'s ids are a local synthetic numbering while `OpggProvider`'s
    are real Data Dragon ids, so only names are stable across providers. A pool must
    survive switching `--provider manual`/`--provider opgg`."""

    by_role: dict[Role, list[str]] = Field(default_factory=dict)

    def resolve_ids(self, role: Role, champions: list[Champion]) -> set[int] | None:
        """`None` if this player has no pool entry for `role` at all -- callers must
        treat that as "no data, don't restrict or bonus," never as an empty
        restriction. An empty `set()` is a different, real answer: a pool was
        defined for this role, but none of its names resolve against `champions`
        (e.g. a stale name, or `champions` came from a provider whose roster
        doesn't include it)."""
        names = self.by_role.get(role)
        if not names:
            return None
        lowered = {n.lower() for n in names}
        return {c.champion_id for c in champions if c.name.lower() in lowered}


class RosterSide(StrEnum):
    """Which team a named player is on for a given draft -- relative to the user
    (`ally`/`enemy`), not `Side.BLUE`/`Side.RED` (which side is blue/red is a
    per-draft coin flip, not a stable fact about who's on your team)."""

    ALLY = "ally"
    ENEMY = "enemy"


class TeamRoster(BaseModel):
    """Team membership for one draft -- which named players (see `ChampionPool`,
    keyed the same way in the pool registry) are on which side. Names only, not
    role assignments: real pick order/priority means who ends up playing which
    role isn't knowable in advance, especially for the enemy team. Suggestions
    consult the *union* of a side's players' pools for the relevant role instead
    (see `consolidated_pool_ids`) rather than trying to guess a specific
    player-to-role mapping."""

    ally: list[str] = Field(default_factory=list)
    enemy: list[str] = Field(default_factory=list)


def consolidated_pool_ids(
    registry: dict[str, ChampionPool],
    player_names: list[str],
    role: Role,
    champions: list[Champion],
) -> set[int] | None:
    """Union of every named player's resolved pool for `role`. `None` only if
    *none* of `player_names` have any pool data for this role at all -- the
    caller must not restrict or bonus in that case. An empty `set()` is a real,
    different answer: at least one player has a pool entry for this role, but
    none of the names resolve against `champions`."""
    ids: set[int] = set()
    any_defined = False
    for name in player_names:
        pool = registry.get(name)
        if pool is None:
            continue
        resolved = pool.resolve_ids(role, champions)
        if resolved is not None:
            any_defined = True
            ids |= resolved
    return ids if any_defined else None


def add_to_pool_registry(
    registry: dict[str, ChampionPool],
    player: str,
    role: Role,
    champions: list[Champion],
) -> list[Champion]:
    """Appends `champions` (already resolved -- by fuzzy CLI name match or exact web
    `champion_id`, callers differ, this doesn't care) into `registry[player]`'s pool
    for `role`, deduped case-insensitively by name. Returns the ones actually added
    (a champion already present contributes nothing). Mutates `registry` in place;
    the caller still owns persisting it via `persistence.save_pool_registry`."""
    pool = registry.setdefault(player, ChampionPool())
    existing = {n.lower() for n in pool.by_role.get(role, [])}
    added = []
    for champ in champions:
        if champ.name.lower() not in existing:
            pool.by_role.setdefault(role, []).append(champ.name)
            existing.add(champ.name.lower())
            added.append(champ)
    return added


class DraftState(BaseModel):
    mode: DraftMode
    rank: RankBracket = RankBracket.ALL
    provider: ProviderName = ProviderName.MANUAL
    actions: list[DraftAction] = Field(default_factory=list)
    roster: TeamRoster = Field(default_factory=TeamRoster)
