from __future__ import annotations

import pytest

from draftiq.draft.rules import SOLOQ_ORDER, order_for
from draftiq.draft.state import (
    ChampionUnavailableError,
    DraftCompleteError,
    DraftStateMachine,
    RoleAlreadyFilledError,
    WrongActionTypeError,
    WrongSideError,
)
from draftiq.models import ActionType, DraftMode, Role, Side


class TestSoloqOrder:
    def test_has_twenty_steps(self) -> None:
        assert len(SOLOQ_ORDER) == 20

    def test_ten_bans_alternating_five_each(self) -> None:
        bans = SOLOQ_ORDER[:10]
        assert all(action is ActionType.BAN for _, action in bans)
        assert sum(1 for side, _ in bans if side is Side.BLUE) == 5
        assert sum(1 for side, _ in bans if side is Side.RED) == 5
        assert [side for side, _ in bans] == [
            Side.BLUE,
            Side.RED,
            Side.BLUE,
            Side.RED,
            Side.BLUE,
            Side.RED,
            Side.BLUE,
            Side.RED,
            Side.BLUE,
            Side.RED,
        ]

    def test_pick_order_matches_b1_r1r2_b2b3_r3r4_b4b5_r5(self) -> None:
        picks = SOLOQ_ORDER[10:]
        assert [side for side, _ in picks] == [
            Side.BLUE,
            Side.RED,
            Side.RED,
            Side.BLUE,
            Side.BLUE,
            Side.RED,
            Side.RED,
            Side.BLUE,
            Side.BLUE,
            Side.RED,
        ]
        assert all(action is ActionType.PICK for _, action in picks)

    def test_tournament_order_not_implemented_yet(self) -> None:
        with pytest.raises(NotImplementedError):
            order_for(DraftMode.TOURNAMENT)


class TestDraftStateMachine:
    def test_new_starts_at_step_zero_blue_ban(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        assert sm.step_index == 0
        assert sm.current_side() is Side.BLUE
        assert sm.current_action_type() is ActionType.BAN
        assert not sm.is_complete()

    def test_apply_ban_advances_turn(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        sm.apply_ban(champion_id=1)
        assert sm.step_index == 1
        assert sm.current_side() is Side.RED
        assert sm.banned_champion_ids() == {1}

    def test_rejects_duplicate_champion(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        sm.apply_ban(champion_id=1)
        with pytest.raises(ChampionUnavailableError):
            sm.apply_ban(champion_id=1)

    def test_rejects_pick_during_ban_phase(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        with pytest.raises(WrongActionTypeError):
            sm.apply_pick(champion_id=1, role=Role.TOP)

    def test_rejects_ban_during_pick_phase(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        for champ_id in range(1, 11):
            sm.apply_ban(champion_id=champ_id)
        with pytest.raises(WrongActionTypeError):
            sm.apply_ban(champion_id=11)

    def test_rejects_wrong_side(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        with pytest.raises(WrongSideError):
            sm.apply_ban(champion_id=1, side=Side.RED)

    def test_rejects_role_already_filled_for_side(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        for champ_id in range(1, 11):
            sm.apply_ban(champion_id=champ_id)
        # Step 10: blue's first pick (B1).
        sm.apply_pick(champion_id=11, role=Role.TOP)
        # Step 11: red pick (R1).
        sm.apply_pick(champion_id=12, role=Role.TOP)
        # Step 12: red pick (R2).
        sm.apply_pick(champion_id=13, role=Role.JUNGLE)
        # Step 13: blue's second pick (B2) -- reusing TOP should fail.
        with pytest.raises(RoleAlreadyFilledError):
            sm.apply_pick(champion_id=14, role=Role.TOP)

    def test_legal_champion_ids_excludes_banned_and_picked(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        sm.apply_ban(champion_id=1)
        sm.apply_ban(champion_id=2)
        assert sm.legal_champion_ids({1, 2, 3, 4}) == {3, 4}

    def test_remaining_picks_counts_only_future_picks_for_side(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        assert sm.remaining_picks(Side.BLUE) == 5
        assert sm.remaining_picks(Side.RED) == 5
        for champ_id in range(1, 11):
            sm.apply_ban(champion_id=champ_id)
        # All 10 picks remain right as the pick phase begins.
        assert sm.remaining_picks(Side.BLUE) == 5
        assert sm.remaining_picks(Side.RED) == 5
        sm.apply_pick(champion_id=11, role=Role.TOP)  # B1
        assert sm.remaining_picks(Side.BLUE) == 4
        assert sm.remaining_picks(Side.RED) == 5

    def test_full_draft_completes_after_twenty_actions(self) -> None:
        sm = DraftStateMachine.new(DraftMode.SOLOQ)
        champ_id = 1
        for _ in range(10):
            sm.apply_ban(champion_id=champ_id)
            champ_id += 1
        roles = [Role.TOP, Role.JUNGLE, Role.MID, Role.BOTTOM, Role.SUPPORT]
        role_cursor = {Side.BLUE: 0, Side.RED: 0}
        while not sm.is_complete():
            side = sm.current_side()
            role = roles[role_cursor[side]]
            role_cursor[side] += 1
            sm.apply_pick(champion_id=champ_id, role=role)
            champ_id += 1
        assert sm.is_complete()
        assert len(sm.picked_champion_ids(Side.BLUE)) == 5
        assert len(sm.picked_champion_ids(Side.RED)) == 5
        with pytest.raises(DraftCompleteError):
            sm.current_side()
