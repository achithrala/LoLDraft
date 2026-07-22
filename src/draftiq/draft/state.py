"""The draft state machine: whose turn it is, ban vs. pick, which champions are still
legal, and how many enemy picks remain (needed later by the exposure term). All
mutation goes through `apply_ban` / `apply_pick`, which validate rigorously --
duplicate champions, out-of-order actions, and picks for an already-filled role are
all rejected with a specific exception rather than silently accepted or fixed up.
"""

from __future__ import annotations

from collections.abc import Iterable

from draftiq.draft.rules import order_for
from draftiq.models import (
    ActionType,
    DraftAction,
    DraftMode,
    DraftState,
    ProviderName,
    RankBracket,
    Role,
    Side,
)


class DraftError(Exception):
    """Base class for all draft state machine validation errors."""


class DraftCompleteError(DraftError):
    pass


class WrongActionTypeError(DraftError):
    pass


class WrongSideError(DraftError):
    pass


class ChampionUnavailableError(DraftError):
    pass


class RoleAlreadyFilledError(DraftError):
    pass


class DraftStateMachine:
    def __init__(self, state: DraftState) -> None:
        self.state = state
        self._order = order_for(state.mode)

    @classmethod
    def new(
        cls,
        mode: DraftMode,
        rank: RankBracket = RankBracket.ALL,
        provider: ProviderName = ProviderName.MANUAL,
    ) -> DraftStateMachine:
        return cls(DraftState(mode=mode, rank=rank, provider=provider))

    @property
    def step_index(self) -> int:
        return len(self.state.actions)

    def is_complete(self) -> bool:
        return self.step_index >= len(self._order)

    def _current_step(self) -> tuple[Side, ActionType]:
        if self.is_complete():
            raise DraftCompleteError("The draft is already complete; no actions remain.")
        return self._order[self.step_index]

    def current_side(self) -> Side:
        return self._current_step()[0]

    def current_action_type(self) -> ActionType:
        return self._current_step()[1]

    def banned_champion_ids(self) -> set[int]:
        return {a.champion_id for a in self.state.actions if a.action_type is ActionType.BAN}

    def picked_champion_ids(self, side: Side | None = None) -> set[int]:
        return {
            a.champion_id
            for a in self.state.actions
            if a.action_type is ActionType.PICK and (side is None or a.side is side)
        }

    def taken_champion_ids(self) -> set[int]:
        return self.banned_champion_ids() | self.picked_champion_ids()

    def legal_champion_ids(self, all_champion_ids: Iterable[int]) -> set[int]:
        return set(all_champion_ids) - self.taken_champion_ids()

    def filled_roles(self, side: Side) -> set[Role]:
        return {
            a.role
            for a in self.state.actions
            if a.action_type is ActionType.PICK and a.side is side and a.role is not None
        }

    def remaining_picks(self, side: Side) -> int:
        """How many PICK steps for `side` occur after the current step. The
        counterpick-exposure term (Phase 2) needs this to know how many more enemy
        picks a candidate might still be punished by."""
        return sum(
            1
            for step_side, action_type in self._order[self.step_index :]
            if step_side is side and action_type is ActionType.PICK
        )

    def apply_ban(self, champion_id: int, side: Side | None = None) -> None:
        expected_side, action_type = self._current_step()
        if action_type is not ActionType.BAN:
            raise WrongActionTypeError(
                f"Step {self.step_index} is a {action_type.value}, not a ban."
            )
        if side is not None and side is not expected_side:
            raise WrongSideError(f"It is {expected_side.value}'s turn to ban, not {side.value}'s.")
        if champion_id in self.taken_champion_ids():
            raise ChampionUnavailableError(
                f"Champion {champion_id} has already been banned or picked."
            )
        self.state.actions.append(
            DraftAction(side=expected_side, action_type=ActionType.BAN, champion_id=champion_id)
        )

    def apply_pick(self, champion_id: int, role: Role, side: Side | None = None) -> None:
        expected_side, action_type = self._current_step()
        if action_type is not ActionType.PICK:
            raise WrongActionTypeError(
                f"Step {self.step_index} is a {action_type.value}, not a pick."
            )
        if side is not None and side is not expected_side:
            raise WrongSideError(f"It is {expected_side.value}'s turn to pick, not {side.value}'s.")
        if champion_id in self.taken_champion_ids():
            raise ChampionUnavailableError(
                f"Champion {champion_id} has already been banned or picked."
            )
        if role in self.filled_roles(expected_side):
            raise RoleAlreadyFilledError(
                f"{expected_side.value} has already picked a {role.value}."
            )
        self.state.actions.append(
            DraftAction(
                side=expected_side,
                action_type=ActionType.PICK,
                champion_id=champion_id,
                role=role,
            )
        )
