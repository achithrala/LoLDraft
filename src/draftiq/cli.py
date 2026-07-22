"""The `draftiq` CLI: `new`, `ban`, `pick`, `suggest`, `state`.

Draft state is persisted between invocations as JSON at `.draftiq/state.json` in the
current directory -- each command is a separate process, so there is nowhere else for
"whose turn is it" (or which provider a draft was started with) to live in between.
`new --provider` picks the data source once and it's remembered from then on; every
other command just reads it back out of the saved state.

`manual` (the default) uses the offline synthetic dataset and needs no network.
`opgg` talks to the live OP.GG MCP server -- see providers/opgg.py for the schema
caveats (reconstructed win counts, sparse matchup coverage, position="all" synergy).
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from draftiq.draft.state import DraftError, DraftStateMachine
from draftiq.models import (
    Champion,
    DraftMode,
    DraftState,
    ProviderName,
    RankBracket,
    Recommendation,
    Role,
    Side,
)
from draftiq.providers.base import StatsProvider
from draftiq.providers.manual import ManualCSVProvider
from draftiq.providers.opgg import OpggProvider
from draftiq.search.greedy import suggest as greedy_suggest

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()

STATE_DIR = Path(".draftiq")
STATE_FILE = STATE_DIR / "state.json"


def _get_provider(provider_name: ProviderName) -> StatsProvider:
    if provider_name is ProviderName.OPGG:
        return OpggProvider()
    return ManualCSVProvider()


def _load_state_machine() -> DraftStateMachine:
    if not STATE_FILE.exists():
        console.print("[red]No draft in progress.[/red] Run [bold]draftiq new[/bold] first.")
        raise typer.Exit(1)
    try:
        state = DraftState.model_validate_json(STATE_FILE.read_text())
    except (ValidationError, json.JSONDecodeError) as e:
        console.print(f"[red]Could not read {STATE_FILE}:[/red] {e}")
        raise typer.Exit(1) from e
    return DraftStateMachine(state)


def _save_state_machine(sm: DraftStateMachine) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(sm.state.model_dump_json(indent=2))


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


def _render_recommendations(recs: list[Recommendation]) -> None:
    if not recs:
        console.print("[yellow]No legal candidates remain.[/yellow]")
        return
    table = Table(title="Recommendations")
    table.add_column("#", justify="right")
    table.add_column("Champion")
    table.add_column("Score", justify="right")
    table.add_column("90% CI", justify="right")
    table.add_column("n games", justify="right")
    table.add_column("Breakdown")
    for i, rec in enumerate(recs, start=1):
        breakdown = ", ".join(f"{t.label}: {t.value:+.4f}" for t in rec.terms)
        table.add_row(
            str(i),
            rec.champion_name,
            f"{rec.total_score:.4f}",
            f"[{rec.ci_low:.3f}, {rec.ci_high:.3f}]",
            str(rec.n_games),
            breakdown,
        )
    console.print(table)


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
    if mode is not DraftMode.SOLOQ:
        console.print(f"[red]{mode.value} is not supported yet (Phase 2).[/red] Use --mode soloq.")
        raise typer.Exit(1)
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
def suggest(
    role: Annotated[Role, typer.Option("--role", help="Role to suggest for.")],
    top: Annotated[int, typer.Option("--top", "-n", help="How many candidates to show.")] = 5,
) -> None:
    """Rank legal champions for the current turn's role."""
    sm = _load_state_machine()
    provider = _get_provider(sm.state.provider)
    if sm.is_complete():
        console.print("[yellow]The draft is complete -- nothing left to suggest.[/yellow]")
        raise typer.Exit(1)
    side = sm.current_side()
    action = sm.current_action_type()
    console.print(f"Suggesting for {side.value}'s {action.value} ({role.value}):")
    recs = greedy_suggest(sm, provider, role, top_n=top)
    _render_recommendations(recs)


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


if __name__ == "__main__":
    app()
