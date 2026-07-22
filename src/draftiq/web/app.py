"""FastAPI app exposing draftiq's draft state machine, providers, and search modules
over HTTP for the local web UI. Every route re-exposes existing business logic
unchanged (`DraftStateMachine`, `StatsProvider` implementations, `search/*`) -- nothing
here reimplements scoring or draft rules, matching the same reuse the CLI already does.

Local-only by design: no auth, a single shared `.draftiq/state.json` (see
`persistence.py`), matching the CLI's trust model exactly -- `cli.py`'s `serve`
command is the only supported way to run this, and binds `127.0.0.1` by default. It
also never exposes a `--workers` option: multiple uvicorn worker processes would each
get their own independent `persistence.STATE_LOCK`, silently defeating the one
mitigation every mutating route here relies on for same-process concurrent-request
safety (two browser tabs racing a ban/pick against the same file).

Route handlers are plain `def`, not `async def` -- FastAPI runs sync handlers in a
worker thread pool, which is exactly the concurrency model `persistence.STATE_LOCK` is
designed to serialize against.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from draftiq import persistence
from draftiq.draft.state import DraftError, DraftStateMachine
from draftiq.models import Build, Champion, ProviderName, Recommendation, Role
from draftiq.providers.base import StatsProvider
from draftiq.search.dispatch import resolve_suggestion
from draftiq.web.schemas import (
    BanRequest,
    DraftStateResponse,
    NewDraftRequest,
    PickRequest,
    UnknownChampionIdError,
    build_state_response,
    resolve_champion_id,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _cached_provider(app: FastAPI, provider_name: ProviderName) -> StatsProvider:
    """Both `StatsProvider` implementations are stateless/read-only after
    construction (never carry draft state), so memoizing them on `app.state` avoids
    re-parsing CSVs / re-opening the OP.GG SQLite cache connection on every single
    request -- the CLI doesn't need this since it's a fresh process per invocation."""
    providers: dict[ProviderName, StatsProvider] = app.state.providers
    if provider_name not in providers:
        providers[provider_name] = persistence.get_provider(provider_name)
    return providers[provider_name]


def create_app() -> FastAPI:
    app = FastAPI(title="draftiq")
    app.state.providers = {}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(persistence.NoDraftInProgressError)
    def _handle_no_draft(request: Request, exc: persistence.NoDraftInProgressError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    def _handle_corrupt_state(request: Request, exc: ValueError) -> JSONResponse:
        # Only reachable for a ValueError raised by persistence.load_state_machine()
        # itself (pydantic ValidationError or json.JSONDecodeError from a corrupt
        # state.json) -- every route that can raise a *request-input* ValueError
        # (suggest's role/lookahead/any-role validation, unknown champion ids) catches
        # it locally into an HTTPException(400) before it would ever reach here.
        return JSONResponse(
            status_code=500, content={"detail": f"Saved draft state is corrupt: {exc}"}
        )

    @app.exception_handler(DraftError)
    def _handle_draft_error(request: Request, exc: DraftError) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "error_type": type(exc).__name__}
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/draft/new", response_model=DraftStateResponse)
    def new_draft(body: NewDraftRequest) -> DraftStateResponse:
        with persistence.STATE_LOCK:
            sm = DraftStateMachine.new(body.mode, body.rank, body.provider)
            persistence.save_state_machine(sm)
        provider = _cached_provider(app, sm.state.provider)
        return build_state_response(sm, provider)

    @app.post("/api/draft/ban", response_model=DraftStateResponse)
    def ban(body: BanRequest) -> DraftStateResponse:
        with persistence.STATE_LOCK:
            sm = persistence.load_state_machine()
            provider = _cached_provider(app, sm.state.provider)
            try:
                champ = resolve_champion_id(body.champion_id, provider.get_champions())
            except UnknownChampionIdError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            sm.apply_ban(champ.champion_id)
            persistence.save_state_machine(sm)
        return build_state_response(sm, provider)

    @app.post("/api/draft/pick", response_model=DraftStateResponse)
    def pick(body: PickRequest) -> DraftStateResponse:
        with persistence.STATE_LOCK:
            sm = persistence.load_state_machine()
            provider = _cached_provider(app, sm.state.provider)
            try:
                champ = resolve_champion_id(body.champion_id, provider.get_champions())
            except UnknownChampionIdError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            sm.apply_pick(champ.champion_id, body.role, side=body.side)
            persistence.save_state_machine(sm)
        return build_state_response(sm, provider)

    @app.get("/api/draft/state", response_model=DraftStateResponse)
    def get_state() -> DraftStateResponse:
        with persistence.STATE_LOCK:
            sm = persistence.load_state_machine()
        provider = _cached_provider(app, sm.state.provider)
        return build_state_response(sm, provider)

    @app.get("/api/draft/build", response_model=Build)
    def get_build(champion_id: int, role: Role, opponent_id: int | None = None) -> Build:
        with persistence.STATE_LOCK:
            sm = persistence.load_state_machine()
        provider = _cached_provider(app, sm.state.provider)
        champions = provider.get_champions()
        try:
            champ = resolve_champion_id(champion_id, champions)
            resolved_opponent_id = (
                resolve_champion_id(opponent_id, champions).champion_id
                if opponent_id is not None
                else None
            )
        except UnknownChampionIdError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            return provider.get_build(
                champ.champion_id, role, sm.state.rank, opponent_id=resolved_opponent_id
            )
        except (NotImplementedError, KeyError) as e:
            raise HTTPException(
                status_code=404,
                detail=f"No build data available for {champ.name} ({role.value}): {e}",
            ) from e

    @app.get("/api/draft/suggest", response_model=list[Recommendation])
    def suggest(
        role: Role | None = None,
        top: int = 5,
        lookahead: bool = False,
        any_role: bool = False,
    ) -> list[Recommendation]:
        with persistence.STATE_LOCK:
            sm = persistence.load_state_machine()
        provider = _cached_provider(app, sm.state.provider)
        if sm.is_complete():
            raise HTTPException(
                status_code=400, detail="The draft is complete -- nothing left to suggest."
            )
        try:
            recs, _show_role = resolve_suggestion(sm, provider, role, top, lookahead, any_role)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return recs

    @app.get("/api/champions", response_model=list[Champion])
    def champions() -> list[Champion]:
        with persistence.STATE_LOCK:
            sm = persistence.load_state_machine()
        provider = _cached_provider(app, sm.state.provider)
        return provider.get_champions()

    return app
