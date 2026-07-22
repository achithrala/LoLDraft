from __future__ import annotations

import pytest

from draftiq.draft.rules import SOLOQ_ORDER, TOURNAMENT_ORDER, order_for
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

    def test_order_for_soloq(self) -> None:
        assert order_for(DraftMode.SOLOQ) == SOLOQ_ORDER


class TestTournamentOrder:
    def test_has_twenty_steps(self) -> None:
        assert len(TOURNAMENT_ORDER) == 20

    def test_order_for_tournament(self) -> None:
        assert order_for(DraftMode.TOURNAMENT) == TOURNAMENT_ORDER

    def test_ban_phase_one_is_six_alternating_starting_blue(self) -> None:
        ban_phase_1 = TOURNAMENT_ORDER[:6]
        assert all(action is ActionType.BAN for _, action in ban_phase_1)
        assert [side for side, _ in ban_phase_1] == [
            Side.BLUE,
            Side.RED,
            Side.BLUE,
            Side.RED,
            Side.BLUE,
            Side.RED,
        ]

    def test_pick_phase_one_is_six_b_r_r_b_b_r(self) -> None:
        pick_phase_1 = TOURNAMENT_ORDER[6:12]
        assert all(action is ActionType.PICK for _, action in pick_phase_1)
        assert [side for side, _ in pick_phase_1] == [
            Side.BLUE,
            Side.RED,
            Side.RED,
            Side.BLUE,
            Side.BLUE,
            Side.RED,
        ]

    def test_ban_phase_two_is_four_alternating_starting_red(self) -> None:
        ban_phase_2 = TOURNAMENT_ORDER[12:16]
        assert all(action is ActionType.BAN for _, action in ban_phase_2)
        assert [side for side, _ in ban_phase_2] == [Side.RED, Side.BLUE, Side.RED, Side.BLUE]

    def test_pick_phase_two_is_four_r_b_b_r(self) -> None:
        pick_phase_2 = TOURNAMENT_ORDER[16:]
        assert all(action is ActionType.PICK for _, action in pick_phase_2)
        assert [side for side, _ in pick_phase_2] == [Side.RED, Side.BLUE, Side.BLUE, Side.RED]

    def test_five_bans_and_five_picks_per_side(self) -> None:
        for side in (Side.BLUE, Side.RED):
            bans = sum(1 for s, a in TOURNAMENT_ORDER if s is side and a is ActionType.BAN)
            picks = sum(1 for s, a in TOURNAMENT_ORDER if s is side and a is ActionType.PICK)
            assert bans == 5
            assert picks == 5


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

    def test_full_tournament_draft_completes_after_twenty_actions(self) -> None:
        # Unlike SOLOQ, tournament bans and picks interleave (ban phase 2 comes
        # after pick phase 1), so this drives the state machine generically off
        # current_action_type() instead of "ban ten times, then pick."
        sm = DraftStateMachine.new(DraftMode.TOURNAMENT)
        champ_id = 1
        roles = [Role.TOP, Role.JUNGLE, Role.MID, Role.BOTTOM, Role.SUPPORT]
        role_cursor = {Side.BLUE: 0, Side.RED: 0}
        while not sm.is_complete():
            side = sm.current_side()
            if sm.current_action_type() is ActionType.BAN:
                sm.apply_ban(champion_id=champ_id)
            else:
                role = roles[role_cursor[side]]
                role_cursor[side] += 1
                sm.apply_pick(champion_id=champ_id, role=role)
            champ_id += 1
        assert sm.is_complete()
        assert len(sm.banned_champion_ids()) == 10
        assert len(sm.picked_champion_ids(Side.BLUE)) == 5
        assert len(sm.picked_champion_ids(Side.RED)) == 5
        with pytest.raises(DraftCompleteError):
            sm.current_side()
