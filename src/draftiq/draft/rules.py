"""Pick/ban order tables. Each entry is (side, action_type) for one step of the draft,
in order. `DraftStateMachine` (in `state.py`) is generic over this table -- it doesn't
know or care what the order actually is, just what comes next.
"""

from __future__ import annotations

from draftiq.models import ActionType, DraftMode, Side

BAN = ActionType.BAN
PICK = ActionType.PICK
BLUE = Side.BLUE
RED = Side.RED

# 10 bans alternating blue/red, then picks B1 / R1 R2 / B2 B3 / R3 R4 / B4 B5 / R5.
SOLOQ_ORDER: tuple[tuple[Side, ActionType], ...] = (
    (BLUE, BAN),
    (RED, BAN),
    (BLUE, BAN),
    (RED, BAN),
    (BLUE, BAN),
    (RED, BAN),
    (BLUE, BAN),
    (RED, BAN),
    (BLUE, BAN),
    (RED, BAN),
    (BLUE, PICK),
    (RED, PICK),
    (RED, PICK),
    (BLUE, PICK),
    (BLUE, PICK),
    (RED, PICK),
    (RED, PICK),
    (BLUE, PICK),
    (BLUE, PICK),
    (RED, PICK),
)


def order_for(mode: DraftMode) -> tuple[tuple[Side, ActionType], ...]:
    if mode is DraftMode.SOLOQ:
        return SOLOQ_ORDER
    raise NotImplementedError(
        f"{mode.value} draft order is not implemented yet (Phase 2). "
        "Only SOLOQ is supported in Phase 1."
    )
