"""Request/response models for the web API. `Recommendation`, `Champion`, and `Build`
(from `draftiq.models`) are reused directly as response bodies elsewhere in
`web/app.py` -- these are only the shapes that don't already exist, plus champion-id
validation.
"""

from __future__ import annotations

from pydantic import BaseModel

from draftiq.draft.state import DraftStateMachine
from draftiq.models import ActionType, Champion, DraftMode, ProviderName, RankBracket, Role, Side
from draftiq.providers.base import StatsProvider


class UnknownChampionIdError(ValueError):
    """A `champion_id` in a request doesn't match any champion the active provider
    knows about. The CLI never needs this check -- `_resolve_champion` always returns
    a `Champion` pulled from `provider.get_champions()` in the first place -- but the
    web API accepts a bare `champion_id` directly (for an exact-match picker UI rather
    than the CLI's fuzzy name matching), so it has to validate that id itself before
    handing it to `DraftStateMachine`, which only checks for duplicates/already-taken,
    never realness."""


def resolve_champion_id(champion_id: int, champions: list[Champion]) -> Champion:
    for champ in champions:
        if champ.champion_id == champion_id:
            return champ
    raise UnknownChampionIdError(f"Unknown champion_id {champion_id}.")


class NewDraftRequest(BaseModel):
    mode: DraftMode = DraftMode.SOLOQ
    rank: RankBracket = RankBracket.ALL
    provider: ProviderName = ProviderName.MANUAL


class BanRequest(BaseModel):
    champion_id: int


class PickRequest(BaseModel):
    champion_id: int
    role: Role
    side: Side | None = None


class DraftActionOut(BaseModel):
    side: Side
    action_type: ActionType
    champion_id: int
    champion_name: str
    role: Role | None = None


class DraftStateResponse(BaseModel):
    mode: DraftMode
    rank: RankBracket
    provider: ProviderName
    patch: str
    actions: list[DraftActionOut]
    is_complete: bool
    next_side: Side | None
    next_action: ActionType | None


def build_state_response(sm: DraftStateMachine, provider: StatsProvider) -> DraftStateResponse:
    champion_by_id = {c.champion_id: c for c in provider.get_champions()}

    def name_of(champion_id: int) -> str:
        champ = champion_by_id.get(champion_id)
        return champ.name if champ is not None else f"#{champion_id}"

    actions = [
        DraftActionOut(
            side=a.side,
            action_type=a.action_type,
            champion_id=a.champion_id,
            champion_name=name_of(a.champion_id),
            role=a.role,
        )
        for a in sm.state.actions
    ]

    is_complete = sm.is_complete()
    next_side = None if is_complete else sm.current_side()
    next_action = None if is_complete else sm.current_action_type()

    return DraftStateResponse(
        mode=sm.state.mode,
        rank=sm.state.rank,
        provider=sm.state.provider,
        patch=provider.get_patch(),
        actions=actions,
        is_complete=is_complete,
        next_side=next_side,
        next_action=next_action,
    )
