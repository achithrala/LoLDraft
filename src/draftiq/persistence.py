"""Draft-state persistence and provider resolution, shared by the CLI (`cli.py`) and
the local web UI (`web/app.py`) so both can drive the exact same `.draftiq/state.json`
interchangeably.

Deliberately raises plain exceptions rather than doing any presentation (no
`typer.Exit`/`console.print`, no HTTP status codes) -- each front end maps these to its
own error format. `load_state_machine`/`save_state_machine` do *not* lock internally;
`STATE_LOCK` is exposed for callers to hold across an entire load-mutate-save critical
section. The CLI has no use for it (each invocation is a separate, single-threaded
process -- a `threading.Lock` provides no cross-process protection, and cross-process
file locking is deliberately out of scope, see `web/app.py`). The web server is
long-lived and can receive genuinely concurrent requests (e.g. two browser tabs), where
two racing mutations would otherwise both read the same on-disk state, compute
independently, and let the second write silently clobber the first -- the same class of
bug already fixed once for `SQLiteCache` during OP.GG prefetching
(`providers/cache.py`).

`load_pool_registry`/`save_pool_registry` follow the exact same shape for
`.draftiq/pools.json` (named players' champion pools -- see `models.ChampionPool`),
stored separately from `state.json` because pools are reusable across many drafts
(unlike a draft's `roster`, which lives inside `DraftState` itself and resets on every
`new`, since who's actually on your team/the enemy team changes every game).
"""

from __future__ import annotations

import threading
from pathlib import Path

from pydantic import TypeAdapter

from draftiq.draft.state import DraftStateMachine
from draftiq.models import ChampionPool, DraftState, ProviderName
from draftiq.providers.base import StatsProvider
from draftiq.providers.manual import ManualCSVProvider
from draftiq.providers.opgg import OpggProvider

STATE_DIR = Path(".draftiq")
STATE_FILE = STATE_DIR / "state.json"
POOLS_FILE = STATE_DIR / "pools.json"

_POOL_REGISTRY_ADAPTER = TypeAdapter(dict[str, ChampionPool])

STATE_LOCK = threading.Lock()


class NoDraftInProgressError(Exception):
    """No `.draftiq/state.json` exists yet -- `new` hasn't been run."""


def get_provider(provider_name: ProviderName) -> StatsProvider:
    if provider_name is ProviderName.OPGG:
        return OpggProvider()
    return ManualCSVProvider()


def get_active_or_default_provider() -> StatsProvider:
    """The active draft's provider if `state.json` exists and loads cleanly, else
    `ManualCSVProvider()` as a bootstrap default. Pool/roster management (`pool
    add`/`import-opgg`, etc.) needs *some* provider to validate/resolve champion
    names against even before any draft has ever been started, or if `state.json`
    is corrupt -- both providers use real champion names for real champions, so
    falling back to Manual is safe and consistent, not a guess."""
    try:
        sm = load_state_machine()
    except (NoDraftInProgressError, ValueError):
        return ManualCSVProvider()
    return get_provider(sm.state.provider)


def load_state_machine() -> DraftStateMachine:
    """Raises `NoDraftInProgressError` if no draft has been started, or lets
    `pydantic.ValidationError`/`json.JSONDecodeError` propagate if `STATE_FILE` exists
    but is corrupt -- callers decide how each of those should be rendered."""
    if not STATE_FILE.exists():
        raise NoDraftInProgressError("No draft in progress. Run `draftiq new` first.")
    state = DraftState.model_validate_json(STATE_FILE.read_text())
    return DraftStateMachine(state)


def save_state_machine(sm: DraftStateMachine) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(sm.state.model_dump_json(indent=2))


def load_pool_registry() -> dict[str, ChampionPool]:
    """`{}` if `pools.json` doesn't exist yet -- no named player has any pool data,
    which is a valid, non-error state (unlike a missing draft)."""
    if not POOLS_FILE.exists():
        return {}
    return _POOL_REGISTRY_ADAPTER.validate_json(POOLS_FILE.read_text())


def save_pool_registry(registry: dict[str, ChampionPool]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    POOLS_FILE.write_bytes(_POOL_REGISTRY_ADAPTER.dump_json(registry, indent=2))
