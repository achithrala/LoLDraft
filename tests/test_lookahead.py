from __future__ import annotations

import pytest

from draftiq.draft.state import DraftStateMachine
from draftiq.models import DraftMode, Recommendation, Role
from draftiq.providers.manual import ManualCSVProvider
from draftiq.search.lookahead import suggest_with_lookahead

DUMMY_BAN_IDS = range(1000, 1010)


def _burn_ban_phase(sm: DraftStateMachine) -> None:
    for champ_id in DUMMY_BAN_IDS:
        sm.apply_ban(champion_id=champ_id)


@pytest.fixture
def provider() -> ManualCSVProvider:
    return ManualCSVProvider()


class TestSuggestWithLookahead:
    def test_does_not_mutate_the_original_state(self, provider: ManualCSVProvider) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        step_before = sm.step_index

        suggest_with_lookahead(sm, provider, role=Role.TOP, top_n=5, lookahead_width=4)

        assert sm.step_index == step_before
        assert not sm.picked_champion_ids()  # nothing was actually picked

    def test_returns_at_most_top_n_recommendations(self, provider: ManualCSVProvider) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)

        recs = suggest_with_lookahead(sm, provider, role=Role.TOP, top_n=3, lookahead_width=6)
        assert len(recs) <= 3

    def test_penalized_candidates_show_a_lookahead_term(self, provider: ManualCSVProvider) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)

        recs = suggest_with_lookahead(sm, provider, role=Role.TOP, top_n=10, lookahead_width=8)
        # At least one candidate should have picked up the "opponent best reply"
        # term -- with a full 20-champion roster and only bans on the board, the
        # opponent always has a strong reply available in at least one open role.
        labelled = [r for r in recs if any(t.label == "opponent best reply" for t in r.terms)]
        assert labelled

    def test_zero_weight_matches_the_plain_greedy_ranking(
        self, provider: ManualCSVProvider
    ) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)

        from draftiq.search.greedy import suggest as greedy_suggest

        plain = greedy_suggest(sm, provider, role=Role.TOP, top_n=5)
        looked_ahead = suggest_with_lookahead(
            sm, provider, role=Role.TOP, top_n=5, lookahead_width=5, lookahead_weight=0.0
        )
        assert [r.champion_id for r in looked_ahead] == [r.champion_id for r in plain]

    def test_works_near_the_end_of_the_draft(self, provider: ManualCSVProvider) -> None:
        """When picking last (no one picks after), ply 2 has nothing to look ahead
        to -- must not error, and must simply return the plain ranking."""
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        # Fast-forward to the final pick (R5): fill every role for both sides except
        # blue's support, then red picks their final champion (R5) last.
        picks = [
            (1, Role.TOP),  # B1
            (2, Role.TOP),  # R1
            (5, Role.JUNGLE),  # R2
            (6, Role.JUNGLE),  # B2
            (9, Role.MID),  # B3
            (10, Role.MID),  # R3
            (13, Role.BOTTOM),  # R4
            (14, Role.BOTTOM),  # B4
            (16, Role.SUPPORT),  # B5
        ]
        for champ_id, role in picks:
            sm.apply_pick(champ_id, role)
        assert sm.step_index == len(sm.state.actions) == 19  # one step left: R5

        recs = suggest_with_lookahead(sm, provider, role=Role.SUPPORT, top_n=5, lookahead_width=5)
        assert recs  # no crash, still returns a ranking
        assert not any(any(t.label == "opponent best reply" for t in r.terms) for r in recs)


class TestSuggestWithLookaheadPool:
    """`pool_ids` must reach ply 1's candidate generation but never ply 2's
    opponent-response simulation -- see lookahead.py's module docstring."""

    def test_pool_ids_restricts_ply1_candidates(self, provider: ManualCSVProvider) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)
        recs = suggest_with_lookahead(
            sm, provider, role=Role.TOP, top_n=20, lookahead_width=8, pool_ids={1, 4}
        )
        assert recs
        assert {r.champion_id for r in recs} <= {1, 4}

    def test_pool_ids_does_not_affect_ply2_opponent_response(
        self, provider: ManualCSVProvider
    ) -> None:
        """Ply 2 simulates the *opponent's* best reply -- restricting our own
        candidate pool must never leak into that simulation. `pool_ids` is set to
        exactly the unrestricted run's own ply-1 candidates (not an arbitrary
        smaller set, which the actual ranking -- shaped by popularity, matchups,
        etc. -- isn't guaranteed to overlap with), isolating the question: does
        merely *passing* `pool_ids` change ply 2's per-candidate penalty for the
        same candidates? It must not."""
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        _burn_ban_phase(sm)

        unrestricted = suggest_with_lookahead(
            sm, provider, role=Role.TOP, top_n=20, lookahead_width=8
        )
        pool_ids = {r.champion_id for r in unrestricted}
        restricted = suggest_with_lookahead(
            sm, provider, role=Role.TOP, top_n=20, lookahead_width=8, pool_ids=pool_ids
        )

        def penalty_for(recs: list[Recommendation], champ_id: int) -> float:
            rec = next(r for r in recs if r.champion_id == champ_id)
            return next((t.value for t in rec.terms if t.label == "opponent best reply"), 0.0)

        assert {r.champion_id for r in restricted} == pool_ids
        for champ_id in pool_ids:
            assert penalty_for(restricted, champ_id) == penalty_for(unrestricted, champ_id)
