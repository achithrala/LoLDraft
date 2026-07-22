"""The `draftiq` CLI: `new`, `ban`, `pick`, `build`, `suggest`, `state`, `serve`.

Draft state is persisted between invocations as JSON at `.draftiq/state.json` in the
current directory -- each command is a separate process, so there is nowhere else for
"whose turn is it" (or which provider a draft was started with) to live in between.
`new --provider` picks the data source once and it's remembered from then on; every
other command just reads it back out of the saved state. `persistence.py` owns this
file I/O and provider resolution now, shared with the web UI (`web/app.py`) so both
can drive the same live draft; `search/dispatch.py` owns `suggest`'s ban/any-role/
role-locked-pick branching for the same reason.

`manual` (the default) uses the offline synthetic dataset and needs no network.
`opgg` talks to the live OP.GG MCP server -- see providers/opgg.py for the schema
caveats (reconstructed win counts, sparse matchup coverage, position="all" synergy).

`serve` launches a local-only web UI (see web/app.py) on top of the same state file --
`fastapi`/`uvicorn` are imported lazily inside that command so every other command
stays independent of the web dependencies.
"""

from __future__ import annotations

import difflib
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from draftiq import persistence
from draftiq.draft.state import DraftError, DraftStateMachine
from draftiq.models import (
    ActionType,
    Build,
    Champion,
    DraftMode,
    ProviderName,
    RankBracket,
    Recommendation,
    Role,
    Side,
)
from draftiq.providers.base import StatsProvider
from draftiq.search.dispatch import SuggestRequestError, resolve_suggestion

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


def _get_provider(provider_name: ProviderName) -> StatsProvider:
    return persistence.get_provider(provider_name)


def _load_state_machine() -> DraftStateMachine:
    try:
        return persistence.load_state_machine()
    except persistence.NoDraftInProgressError as e:
        console.print("[red]No draft in progress.[/red] Run [bold]draftiq new[/bold] first.")
        raise typer.Exit(1) from e
    except ValueError as e:
        console.print(f"[red]Could not read {persistence.STATE_FILE}:[/red] {e}")
        raise typer.Exit(1) from e


def _save_state_machine(sm: DraftStateMachine) -> None:
    persistence.save_state_machine(sm)


def _resolve_champion(name: str, champions: list[Champion]) -> Champion:
    needle = name.strip().lower()
    for champ in champions:
        if champ.name.lower() == needle or champ.ddragon_id.lower() == needle:
            return champ
    matches = [champ for champ in champions if needle in champ.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        options = ", ".join(sorted(c.name for c in matches))
        console.print(f"[red]'{name}' is ambiguous.[/red] Matches: {options}")
        raise typer.Exit(1)
    close = difflib.get_close_matches(name, [c.name for c in champions], n=5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    console.print(f"[red]Unknown champion '{name}'.[/red]{hint}")
    raise typer.Exit(1)


def _render_recommendations(recs: list[Recommendation], show_role: bool = False) -> None:
    if not recs:
        console.print("[yellow]No legal candidates remain.[/yellow]")
        return
    table = Table(title="Recommendations")
    table.add_column("#", justify="right")
    table.add_column("Champion")
    if show_role:
        table.add_column("Role")
    table.add_column("Score", justify="right")
    table.add_column("90% CI", justify="right")
    table.add_column("n games", justify="right")
    table.add_column("Breakdown")
    for i, rec in enumerate(recs, start=1):
        breakdown = ", ".join(f"{t.label}: {t.value:+.4f}" for t in rec.terms)
        row = [str(i), rec.champion_name]
        if show_role:
            row.append(rec.role.value)
        row.extend(
            [
                f"{rec.total_score:.4f}",
                f"[{rec.ci_low:.3f}, {rec.ci_high:.3f}]",
                str(rec.n_games),
                breakdown,
            ]
        )
        table.add_row(*row)
    console.print(table)


def _render_build(champion_name: str, build: Build) -> None:
    console.print(
        f"\n[bold]{champion_name}[/bold] build ({build.role.value}, patch {build.patch}):"
    )
    comma_rows = [
        ("Starting", build.starting_items),
        ("Items", build.items),
        ("Primary runes", build.runes_primary),
        ("Secondary runes", build.runes_secondary),
        ("Shards", build.rune_shards),
        ("Summoners", build.summoner_spells),
    ]
    for label, values in comma_rows:
        if values:
            console.print(f"  {label}: {', '.join(values)}")
    if build.skill_order:
        console.print(f"  Skill order: {' > '.join(build.skill_order)}")


@app.command()
def new(
    mode: Annotated[DraftMode, typer.Option("--mode", help="Draft format.")] = DraftMode.SOLOQ,
    rank: Annotated[
        RankBracket, typer.Option("--rank", help="Rank bracket for stats.")
    ] = RankBracket.ALL,
    provider: Annotated[
        ProviderName, typer.Option("--provider", help="Stats data source for this draft.")
    ] = ProviderName.MANUAL,
) -> None:
    """Start a new draft, discarding any previous one."""
    sm = DraftStateMachine.new(mode, rank, provider)
    _save_state_machine(sm)
    console.print(
        f"Started a new [bold]{mode.value}[/bold] draft "
        f"(rank: {rank.value}, provider: {provider.value})."
    )
    first_side, first_action = sm.current_side().value, sm.current_action_type().value
    console.print(f"First turn: [bold]{first_side}[/bold] {first_action}.")


@app.command()
def ban(champion: Annotated[str, typer.Argument(help="Champion name to ban.")]) -> None:
    """Record a ban for whoever's turn it currently is."""
    sm = _load_state_machine()
    provider = _get_provider(sm.state.provider)
    champ = _resolve_champion(champion, provider.get_champions())
    try:
        side = sm.current_side()
        sm.apply_ban(champ.champion_id)
    except DraftError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    _save_state_machine(sm)
    console.print(f"{side.value} banned [bold]{champ.name}[/bold].")
    _print_next_turn(sm)


@app.command()
def pick(
    champion: Annotated[str, typer.Argument(help="Champion name to pick.")],
    role: Annotated[Role, typer.Option("--role", help="Role this pick fills.")],
    side: Annotated[
        Side | None,
        typer.Option("--side", help="Expected side, validated against whose turn it actually is."),
    ] = None,
) -> None:
    """Record a pick for whoever's turn it currently is."""
    sm = _load_state_machine()
    provider = _get_provider(sm.state.provider)
    champ = _resolve_champion(champion, provider.get_champions())
    try:
        acting_side = sm.current_side()
        sm.apply_pick(champ.champion_id, role, side=side)
    except DraftError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    _save_state_machine(sm)
    console.print(f"{acting_side.value} picked [bold]{champ.name}[/bold] ({role.value}).")
    _print_next_turn(sm)


@app.command()
def build(
    champion: Annotated[str, typer.Argument(help="Champion name.")],
    role: Annotated[Role, typer.Option("--role", help="Role to show the build for.")],
    opponent: Annotated[
        str | None,
        typer.Option("--opponent", help="Matchup-specific build, if the provider supports it."),
    ] = None,
) -> None:
    """Show recommended items, runes, skill order, and summoners for a champion."""
    sm = _load_state_machine()
    provider = _get_provider(sm.state.provider)
    champions = provider.get_champions()
    champ = _resolve_champion(champion, champions)
    opponent_id = (
        _resolve_champion(opponent, champions).champion_id if opponent is not None else None
    )
    try:
        result = provider.get_build(champ.champion_id, role, sm.state.rank, opponent_id=opponent_id)
    except (NotImplementedError, KeyError) as e:
        console.print(f"[red]No build data available for {champ.name} ({role.value}):[/red] {e}")
        raise typer.Exit(1) from e
    _render_build(champ.name, result)


@app.command()
def suggest(
    role: Annotated[
        Role | None,
        typer.Option("--role", help="Role to suggest for. Required for picks; ignored for bans."),
    ] = None,
    top: Annotated[int, typer.Option("--top", "-n", help="How many candidates to show.")] = 5,
    lookahead: Annotated[
        bool,
        typer.Option(
            "--lookahead",
            help=(
                "Picks only, 2-ply: also penalize candidates that would hand the "
                "opponent a strong reply. Slower -- runs several extra scoring passes."
            ),
        ),
    ] = False,
    any_role: Annotated[
        bool,
        typer.Option(
            "--any-role",
            help=(
                "Picks only: ignore --role and rank champions across all of your "
                "unfilled roles, factoring in flex value and contest risk. Use this "
                "to decide who to grab, not what to put them at."
            ),
        ),
    ] = False,
) -> None:
    """Rank legal champions for the current turn: picks by win-rate value, bans by
    how much they deny the opponent."""
    sm = _load_state_machine()
    provider = _get_provider(sm.state.provider)
    if sm.is_complete():
        console.print("[yellow]The draft is complete -- nothing left to suggest.[/yellow]")
        raise typer.Exit(1)
    side = sm.current_side()
    action = sm.current_action_type()

    try:
        recs, show_role = resolve_suggestion(sm, provider, role, top, lookahead, any_role)
    except SuggestRequestError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    except ValueError as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(1) from e

    if action is ActionType.BAN:
        console.print(f"Suggesting bans for {side.value}:")
    elif any_role:
        console.print(f"Suggesting priority picks for {side.value} (any role):")
    else:
        # resolve_suggestion() would have raised SuggestRequestError above if role
        # were None here (BAN handled, any_role handled -- only the role-locked pick
        # path remains, which requires a role).
        assert role is not None
        console.print(f"Suggesting for {side.value}'s {action.value} ({role.value}):")
    _render_recommendations(recs, show_role=show_role)


@app.command(name="state")
def show_state() -> None:
    """Print the current draft state: bans, picks, and whose turn is next."""
    sm = _load_state_machine()
    provider = _get_provider(sm.state.provider)
    champion_by_id = {c.champion_id: c for c in provider.get_champions()}

    def name_of(champion_id: int) -> str:
        champ = champion_by_id.get(champion_id)
        return champ.name if champ is not None else f"#{champion_id}"

    console.print(
        f"Mode: [bold]{sm.state.mode.value}[/bold]  Rank: {sm.state.rank.value}  "
        f"Provider: {sm.state.provider.value}"
    )

    bans = [a for a in sm.state.actions if a.action_type.value == "ban"]
    console.print(f"\nBans ({len(bans)}):")
    for a in bans:
        console.print(f"  {a.side.value}: {name_of(a.champion_id)}")

    for side in (Side.BLUE, Side.RED):
        console.print(f"\n{side.value.capitalize()} picks:")
        picks = [a for a in sm.state.actions if a.action_type.value == "pick" and a.side is side]
        if not picks:
            console.print("  (none yet)")
        for a in picks:
            role_label = a.role.value if a.role is not None else "?"
            console.print(f"  {role_label}: {name_of(a.champion_id)}")

    if sm.is_complete():
        console.print("\n[bold]Draft complete.[/bold]")
    else:
        next_side, next_action = sm.current_side().value, sm.current_action_type().value
        console.print(f"\nNext: [bold]{next_side} {next_action}[/bold]")


def _print_next_turn(sm: DraftStateMachine) -> None:
    if sm.is_complete():
        console.print("[bold]Draft complete.[/bold]")
    else:
        next_side, next_action = sm.current_side().value, sm.current_action_type().value
        console.print(f"Next: [bold]{next_side} {next_action}[/bold]")


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option("--host", help="Bind address. Keep 127.0.0.1 for local-only use."),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8765,
) -> None:
    """Launch the local web UI. Reads/writes the same .draftiq/state.json as the CLI,
    so the browser and other `draftiq` commands can drive the same draft."""
    import uvicorn

    from draftiq.web.app import create_app

    console.print(f"Serving draftiq at [bold]http://{host}:{port}[/bold] (Ctrl+C to stop).")
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    app()
