"""Tests for search/priority.py, using a small hand-built fake provider rather than
ManualCSVProvider -- the real manual dataset only has stats for each champion's
single primary role, so there's no genuine multi-role ("flex") champion in it to
exercise the cross-role and flex-bonus logic against.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from draftiq.draft.state import DraftStateMachine
from draftiq.models import (
    Build,
    Champion,
    ChampionStats,
    DraftMode,
    Matchup,
    RankBracket,
    Role,
    Synergy,
)
from draftiq.providers.manual import ManualCSVProvider
from draftiq.search.priority import FLEX_BONUS_PER_ROLE, suggest_priority

FLEX = 101  # strong in both top and jungle
TOP_ONLY = 102
JUNGLE_ONLY = 103
MID_ONLY = 104
BOTTOM_ONLY = 105
SUPPORT_ONLY = 106

_CHAMPIONS = [
    Champion(champion_id=FLEX, name="Flex", ddragon_id="Flex", tags=["Fighter"]),
    Champion(champion_id=TOP_ONLY, name="TopOnly", ddragon_id="TopOnly", tags=["Fighter"]),
    Champion(champion_id=JUNGLE_ONLY, name="JungleOnly", ddragon_id="JungleOnly", tags=["Fighter"]),
    Champion(champion_id=MID_ONLY, name="MidOnly", ddragon_id="MidOnly", tags=["Mage"]),
    Champion(
        champion_id=BOTTOM_ONLY, name="BottomOnly", ddragon_id="BottomOnly", tags=["Marksman"]
    ),
    Champion(
        champion_id=SUPPORT_ONLY, name="SupportOnly", ddragon_id="SupportOnly", tags=["Support"]
    ),
]

# (champion_id, role) -> (wins, games, pick_count). Off-role entries are deliberately
# given real (bad) sample data -- 30% win rate on a small sample -- rather than
# games=0. games=0 shrinks all the way to that role's population baseline (p0), which
# for a role with very few sampled champions (as here) can be pulled arbitrarily high
# or low by whichever champion *is* strong there -- a small-sample artifact of the
# fake data, not something a real ~170-champion roster would exhibit. Giving every
# champion an explicit (bad) off-role sample sidesteps that and more realistically
# models "this champion, forced into a role they don't belong in."
_GOOD_TOP = (750, 1000, 1000)  # Flex's top-lane win rate: 75%
_GOOD_JUNGLE = (730, 1000, 1000)  # Flex's jungle win rate: 73%
_BASELINE = (500, 1000, 1000)  # a specialist's win rate in their own role: 50%
_OFF_ROLE = (90, 300, 50)  # forced off-role performance: 30%, small sample

_STATS: dict[tuple[int, Role], tuple[int, int, int]] = {}
for _champ in _CHAMPIONS:
    for _role in Role:
        _STATS[(_champ.champion_id, _role)] = _OFF_ROLE
_STATS[(FLEX, Role.TOP)] = _GOOD_TOP
_STATS[(FLEX, Role.JUNGLE)] = _GOOD_JUNGLE
_STATS[(TOP_ONLY, Role.TOP)] = _BASELINE
_STATS[(JUNGLE_ONLY, Role.JUNGLE)] = _BASELINE
_STATS[(MID_ONLY, Role.MID)] = _BASELINE
_STATS[(BOTTOM_ONLY, Role.BOTTOM)] = _BASELINE
_STATS[(SUPPORT_ONLY, Role.SUPPORT)] = _BASELINE


@dataclass
class FakeProvider:
    stats: dict[tuple[int, Role], tuple[int, int, int]] = field(
        default_factory=lambda: dict(_STATS)
    )

    def get_patch(self) -> str:
        return "FAKE-1"

    def get_champions(self) -> list[Champion]:
        return list(_CHAMPIONS)

    def get_champion_stats(self, champion_id: int, role: Role, rank: RankBracket) -> ChampionStats:
        wins, games, pick_count = self.stats.get((champion_id, role), (0, 0, 0))
        return ChampionStats(
            champion_id=champion_id,
            role=role,
            rank=rank,
            patch="FAKE-1",
            wins=wins,
            games=games,
            pick_count=pick_count,
            ban_count=0,
            total_games=100_000,
        )

    def get_matchup(
        self, champion_id: int, opponent_id: int, role: Role, rank: RankBracket
    ) -> Matchup:
        return Matchup(
            champion_id=champion_id,
            opponent_id=opponent_id,
            role=role,
            rank=rank,
            patch="FAKE-1",
            wins=0,
            games=0,
        )

    def get_synergy(self, champion_id: int, ally_id: int, rank: RankBracket) -> Synergy:
        return Synergy(
            champion_id=champion_id, ally_id=ally_id, rank=rank, patch="FAKE-1", wins=0, games=0
        )

    def get_build(
        self, champion_id: int, role: Role, rank: RankBracket, opponent_id: int | None = None
    ) -> Build:
        raise NotImplementedError


def _fresh_soloq_first_pick() -> DraftStateMachine:
    """Drive a SOLOQ draft through its 10-ban phase with filler ban ids so the next
    step is blue's first pick, with all 5 roles still open."""
    sm = DraftStateMachine.new(DraftMode.SOLOQ)
    for i in range(10):
        sm.apply_ban(champion_id=900 + i)
    return sm


class TestSuggestPriority:
    def test_returns_at_most_top_n(self) -> None:
        sm = _fresh_soloq_first_pick()
        recs = suggest_priority(sm, FakeProvider(), top_n=2)
        assert len(recs) <= 2

    def test_specialist_is_ranked_in_its_only_viable_role(self) -> None:
        sm = _fresh_soloq_first_pick()
        recs = suggest_priority(sm, FakeProvider(), top_n=20)
        top_only = next(r for r in recs if r.champion_id == TOP_ONLY)
        assert top_only.role is Role.TOP
        assert not any(t.label.startswith("flex") for t in top_only.terms)

    def test_flex_champion_gets_a_flex_bonus_specialist_does_not(self) -> None:
        sm = _fresh_soloq_first_pick()
        recs = suggest_priority(sm, FakeProvider(), top_n=20)
        by_id = {r.champion_id: r for r in recs}

        flex = by_id[FLEX]
        top_only = by_id[TOP_ONLY]

        assert flex.role is Role.TOP  # the stronger of its two good roles
        flex_terms = [t for t in flex.terms if t.label.startswith("flex")]
        assert len(flex_terms) == 1
        assert flex_terms[0].value > 0
        assert "jungle" in flex_terms[0].label

        assert not any(t.label.startswith("flex") for t in top_only.terms)

    def test_never_played_role_does_not_count_as_flex(self) -> None:
        """Regression test: every champion in the real manual dataset only has
        stats for one role (see data/manual/champion_stats.csv). Before the
        n_games>0 gate, an unplayed role's score collapses to that role's baseline
        win rate, which is close enough to any real champion's own shrunk score to
        spuriously pass the flex-viability margin -- every champion in the dataset
        would incorrectly get flagged as a 4-5 role flex pick. None should be."""
        sm = _fresh_soloq_first_pick()
        recs = suggest_priority(sm, ManualCSVProvider(), top_n=20)
        for rec in recs:
            assert not any(t.label.startswith("flex") for t in rec.terms), rec

    def test_never_played_role_cannot_win_best_role_either(self) -> None:
        """Same underlying bug, applied to best-role selection: a jungle specialist
        with zero top-lane games must never be reported as a top-lane recommendation
        just because top's population baseline happens to be higher than the
        jungler's own shrunk jungle score."""
        provider = ManualCSVProvider()
        sm = _fresh_soloq_first_pick()
        recs = suggest_priority(sm, provider, top_n=20)
        by_id = {r.champion_id: r for r in recs}
        # Lee Sin, Vi, Sejuani, Kindred, Xin Zhao only have jungle-role stats.
        for jungler_id in (5, 6, 7, 8, 20):
            if jungler_id in by_id:
                assert by_id[jungler_id].role is Role.JUNGLE

    def test_flex_bonus_matches_the_documented_constant(self) -> None:
        """The flex bonus is exactly `FLEX_BONUS_PER_ROLE` per additional viable
        role -- Flex has exactly one (jungle, beyond its best role of top)."""
        sm = _fresh_soloq_first_pick()
        recs = suggest_priority(sm, FakeProvider(), top_n=20)
        flex = next(r for r in recs if r.champion_id == FLEX)
        flex_term = next(t for t in flex.terms if t.label.startswith("flex"))
        assert flex_term.value == pytest.approx(FLEX_BONUS_PER_ROLE)

    def test_contest_risk_term_present_when_pick_rate_positive(self) -> None:
        sm = _fresh_soloq_first_pick()
        recs = suggest_priority(sm, FakeProvider(), top_n=20)
        for rec in recs:
            contest_terms = [t for t in rec.terms if t.label == "contest_risk"]
            assert len(contest_terms) == 1
            assert contest_terms[0].value >= 0.0

    def test_raises_when_draft_is_complete(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        roles = [Role.TOP, Role.JUNGLE, Role.MID, Role.BOTTOM, Role.SUPPORT]
        role_cursor = {"blue": 0, "red": 0}
        champ_id = 1
        while not sm.is_complete():
            if sm.current_action_type().value == "ban":
                sm.apply_ban(champion_id=champ_id)
            else:
                side_key = sm.current_side().value
                role = roles[role_cursor[side_key]]
                role_cursor[side_key] += 1
                sm.apply_pick(champion_id=champ_id, role=role)
            champ_id += 1
        with pytest.raises(ValueError, match="already complete"):
            suggest_priority(sm, FakeProvider())
