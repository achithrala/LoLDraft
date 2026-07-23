"""The `draftiq` CLI: `new`, `ban`, `pick`, `build`, `tips`, `suggest`, `state`,
`serve`, `pool`, `roster`.

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

`tips` (like `pool import-opgg`) always talks to live OP.GG data directly, regardless
of the active draft's provider -- no equivalent exists in the offline manual dataset.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
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
    ChampionPool,
    DraftMode,
    LaneMatchupGuide,
    ProviderName,
    RankBracket,
    Recommendation,
    Role,
    RosterSide,
    Side,
    add_to_pool_registry,
)
from draftiq.providers.base import StatsProvider
from draftiq.providers.opgg import OpggApiError, OpggProvider
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


def _render_tips(champion_name: str, opponent_name: str, guide: LaneMatchupGuide) -> None:
    console.print(
        f"\n[bold]{champion_name}[/bold] vs [bold]{opponent_name}[/bold] ({guide.role.value}):"
    )
    console.print(f"  Lane advantage: {guide.lane_advantage}")
    console.print(f"  Solo-kill advantage: {guide.lane_solo_kill_advantage}")
    console.print(f"  Recommended play style: {guide.recommended_play_style}")
    console.print(f"  Tip: {guide.tip}")
    if guide.win_rate_by_game_length:
        curve = ", ".join(
            f"{g.game_length}: {g.win_rate:.1%}" for g in guide.win_rate_by_game_length
        )
        console.print(f"  Win rate by game length: {curve}")


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
    _print_next_turn(sm, provider)


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
    _print_next_turn(sm, provider)


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
def tips(
    champion: Annotated[str, typer.Argument(help="Your champion.")],
    role: Annotated[Role, typer.Option("--role", help="Lane/role for this matchup.")],
    opponent: Annotated[str, typer.Option("--opponent", help="Opponent champion.")],
) -> None:
    """Show OP.GG's lane matchup tips for CHAMPION vs --opponent in --role: a
    prose tip on playing against the opponent, which champion has the lane/
    solo-kill advantage, and win rate by game length. Always uses live OP.GG
    data -- no equivalent exists in the offline synthetic dataset, so this
    doesn't require an active draft and ignores the active draft's provider if
    one is set."""
    provider = OpggProvider()
    champions = provider.get_champions()
    champ = _resolve_champion(champion, champions)
    opp = _resolve_champion(opponent, champions)
    try:
        guide = provider.get_lane_matchup_guide(champ.champion_id, opp.champion_id, role)
    except OpggApiError as e:
        console.print(
            f"[red]Could not fetch matchup tips for {champ.name} vs {opp.name}:[/red] {e}"
        )
        raise typer.Exit(1) from e
    _render_tips(champ.name, opp.name, guide)


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
    pool: Annotated[
        bool,
        typer.Option(
            "--pool",
            help=(
                "Picks: restrict to the union of `draftiq roster`'s ally players' "
                "champion pools for this role. Bans: add a bonus/highlight for "
                "candidates in the enemy roster's pools instead -- the full ban "
                "list is always shown, never narrowed. See `draftiq pool`/"
                "`draftiq roster` to set these up."
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
        recs, show_role = resolve_suggestion(sm, provider, role, top, lookahead, any_role, pool)
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


def _name_lookup(provider: StatsProvider) -> Callable[[int], str]:
    champion_by_id = {c.champion_id: c for c in provider.get_champions()}

    def name_of(champion_id: int) -> str:
        champ = champion_by_id.get(champion_id)
        return champ.name if champ is not None else f"#{champion_id}"

    return name_of


def _render_final_teams(sm: DraftStateMachine, name_of: Callable[[int], str]) -> None:
    """Both sides' picks, sorted by canonical role order (top/jungle/mid/bottom/
    support) rather than the order they were actually picked in -- pick order need
    not match role order, since role is chosen freely at `pick` time (see
    `draft/state.py`), so a running pick-order log isn't the right shape for "here
    are the two final teams." Each entry also shows its overall pick number (1-10,
    across both sides) so the role-sorted view doesn't lose that information."""
    picks = [a for a in sm.state.actions if a.action_type.value == "pick"]
    pick_number = {(a.side, a.champion_id): i + 1 for i, a in enumerate(picks)}

    console.print("\n[bold]Final teams:[/bold]")
    for side in (Side.BLUE, Side.RED):
        console.print(f"\n{side.value.capitalize()} team:")
        picks_by_role = {a.role: a for a in picks if a.side is side}
        for role in Role:
            pick = picks_by_role.get(role)
            if pick is None:
                console.print(f"  {role.value}: -")
            else:
                n = pick_number[(pick.side, pick.champion_id)]
                console.print(f"  {role.value}: {name_of(pick.champion_id)} (pick {n})")


@app.command(name="state")
def show_state() -> None:
    """Print the current draft state: bans, picks, and whose turn is next."""
    sm = _load_state_machine()
    provider = _get_provider(sm.state.provider)
    name_of = _name_lookup(provider)

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
        _render_final_teams(sm, name_of)
    else:
        next_side, next_action = sm.current_side().value, sm.current_action_type().value
        console.print(f"\nNext: [bold]{next_side} {next_action}[/bold]")


def _print_next_turn(sm: DraftStateMachine, provider: StatsProvider) -> None:
    if sm.is_complete():
        console.print("[bold]Draft complete.[/bold]")
        _render_final_teams(sm, _name_lookup(provider))
    else:
        next_side, next_action = sm.current_side().value, sm.current_action_type().value
        console.print(f"Next: [bold]{next_side} {next_action}[/bold]")


pool_app = typer.Typer(help="Manage named players' champion pools (used by `suggest --pool`).")
app.add_typer(pool_app, name="pool")

roster_app = typer.Typer(
    help="Manage this draft's ally/enemy team membership (used by `suggest --pool`)."
)
app.add_typer(roster_app, name="roster")


def _pool_add_names(
    registry: dict[str, ChampionPool],
    player: str,
    role: Role,
    names: list[str],
    registry_champions: list[Champion],
) -> list[str]:
    """Fuzzy-resolves `names` (same matching as `ban`/`pick`) then delegates to
    `models.add_to_pool_registry` for the actual append/dedupe. Returns the
    canonical names actually added."""
    champs = [_resolve_champion(name, registry_champions) for name in names]
    added = add_to_pool_registry(registry, player, role, champs)
    return [c.name for c in added]


@pool_app.command("add")
def pool_add(
    player: Annotated[str, typer.Argument(help="Player name (yourself or someone else).")],
    role: Annotated[Role, typer.Argument(help="Role this pool applies to.")],
    champions: Annotated[list[str], typer.Argument(help="Champion name(s) to add.")],
) -> None:
    """Add champions to a named player's pool for ROLE."""
    provider = persistence.get_active_or_default_provider()
    registry = persistence.load_pool_registry()
    added = _pool_add_names(registry, player, role, champions, provider.get_champions())
    persistence.save_pool_registry(registry)
    if added:
        console.print(f"Added to {player}'s {role.value} pool: {', '.join(added)}")
    else:
        console.print("[yellow]No new champions added (already in the pool).[/yellow]")


@pool_app.command("remove")
def pool_remove(
    player: Annotated[str, typer.Argument(help="Player name.")],
    role: Annotated[Role, typer.Argument(help="Role to remove champions from.")],
    champions: Annotated[list[str], typer.Argument(help="Champion name(s) to remove.")],
) -> None:
    """Remove champions from a named player's pool for ROLE."""
    provider = persistence.get_active_or_default_provider()
    registry_champions = provider.get_champions()
    registry = persistence.load_pool_registry()
    pool = registry.get(player)
    removed = []
    for name in champions:
        champ = _resolve_champion(name, registry_champions)
        if pool is not None and role in pool.by_role:
            before = pool.by_role[role]
            after = [n for n in before if n.lower() != champ.name.lower()]
            if len(after) != len(before):
                removed.append(champ.name)
            pool.by_role[role] = after
    if removed:
        persistence.save_pool_registry(registry)
        console.print(f"Removed from {player}'s {role.value} pool: {', '.join(removed)}")
    else:
        console.print("[yellow]None of those champions were in the pool.[/yellow]")


@pool_app.command("show")
def pool_show(
    player: Annotated[
        str | None, typer.Argument(help="Player name. Omit to show every known player.")
    ] = None,
    role: Annotated[Role | None, typer.Argument(help="Role. Omit to show every role.")] = None,
) -> None:
    """Print one or every named player's champion pool."""
    registry = persistence.load_pool_registry()
    if not registry:
        console.print("[yellow]No pools defined yet.[/yellow]")
        return
    players = [player] if player is not None else sorted(registry.keys())
    for p in players:
        pool = registry.get(p)
        if pool is None:
            console.print(f"[yellow]No pool for {p}.[/yellow]")
            continue
        console.print(f"\n[bold]{p}[/bold]:")
        for r in [role] if role is not None else list(Role):
            names = pool.by_role.get(r, [])
            console.print(f"  {r.value}: {', '.join(names) if names else '(empty)'}")


@pool_app.command("clear")
def pool_clear(
    player: Annotated[str, typer.Argument(help="Player name.")],
    role: Annotated[Role | None, typer.Argument(help="Role to clear.")] = None,
    all_roles: Annotated[
        bool, typer.Option("--all", help="Clear every role for this player.")
    ] = False,
) -> None:
    """Clear a named player's pool for one role, or entirely with --all."""
    if (role is None) == (not all_roles):
        console.print("[red]Specify exactly one of ROLE or --all.[/red]")
        raise typer.Exit(1)
    registry = persistence.load_pool_registry()
    if player not in registry:
        console.print(f"[yellow]No pool for {player}.[/yellow]")
        raise typer.Exit(1)
    if all_roles:
        registry[player] = ChampionPool()
        persistence.save_pool_registry(registry)
        console.print(f"Cleared {player}'s pool.")
    else:
        assert role is not None
        registry[player].by_role.pop(role, None)
        persistence.save_pool_registry(registry)
        console.print(f"Cleared {player}'s {role.value} pool.")


@pool_app.command("import-opgg")
def pool_import_opgg(
    player: Annotated[str, typer.Argument(help="Player name to import into.")],
    role: Annotated[Role, typer.Argument(help="Role to import these champions into.")],
    riot_id: Annotated[str, typer.Argument(help='Riot ID, e.g. "Faker#KR1".')],
    region: Annotated[str, typer.Option("--region", help="Server region code, e.g. KR, NA, EUW.")],
    top: Annotated[
        int, typer.Option("--top", help="How many most-played champions to import.")
    ] = 10,
) -> None:
    """Import a real summoner's most-played champions (via live OP.GG data) into
    PLAYER's pool for ROLE. OP.GG exposes no per-champion role/position data for a
    summoner's champion history, so you tell it which role these go into -- always
    uses OP.GG regardless of the active draft's provider, since this is inherently
    real-data-only."""
    if "#" not in riot_id:
        console.print('[red]Riot ID must be in the form Name#Tag, e.g. "Faker#KR1".[/red]')
        raise typer.Exit(1)
    game_name, tag_line = riot_id.rsplit("#", 1)
    provider = OpggProvider()
    try:
        names = provider.get_summoner_champion_pool(game_name, tag_line, region, limit=top)
    except OpggApiError as e:
        console.print(f"[red]Could not fetch {riot_id}'s champion pool:[/red] {e}")
        raise typer.Exit(1) from e
    if not names:
        console.print(f"[yellow]No champion history found for {riot_id}.[/yellow]")
        raise typer.Exit(1)

    registry = persistence.load_pool_registry()
    added = _pool_add_names(registry, player, role, names, provider.get_champions())
    persistence.save_pool_registry(registry)
    console.print(
        f"Imported into {player}'s {role.value} pool from {riot_id}: "
        f"{', '.join(added) if added else '(no new champions)'}"
    )


def _roster_list(sm: DraftStateMachine, side: RosterSide) -> list[str]:
    return sm.state.roster.ally if side is RosterSide.ALLY else sm.state.roster.enemy


@roster_app.command("add")
def roster_add(
    side: Annotated[RosterSide, typer.Argument(help="Which team this player is on.")],
    player: Annotated[str, typer.Argument(help="Player name (matches a pool name, if any).")],
) -> None:
    """Add a named player to this draft's ally or enemy roster -- team membership
    only, no role assignment: pick order/priority means who ends up playing which
    role isn't knowable in advance. `suggest --pool` consults the union of a side's
    players' pools for whichever role is relevant instead of a fixed mapping."""
    sm = _load_state_machine()
    names = _roster_list(sm, side)
    if player in names:
        console.print(f"[yellow]{player} is already on {side.value}.[/yellow]")
        return
    names.append(player)
    _save_state_machine(sm)
    console.print(f"Added {player} to {side.value}.")


@roster_app.command("remove")
def roster_remove(
    side: Annotated[RosterSide, typer.Argument(help="Which team this player is on.")],
    player: Annotated[str, typer.Argument(help="Player name to remove.")],
) -> None:
    """Remove a named player from this draft's ally or enemy roster."""
    sm = _load_state_machine()
    names = _roster_list(sm, side)
    if player not in names:
        console.print(f"[yellow]{player} is not on {side.value}.[/yellow]")
        return
    names.remove(player)
    _save_state_machine(sm)
    console.print(f"Removed {player} from {side.value}.")


@roster_app.command("show")
def roster_show() -> None:
    """Print this draft's ally/enemy team membership."""
    sm = _load_state_machine()
    console.print(f"Ally: {', '.join(sm.state.roster.ally) or '(none)'}")
    console.print(f"Enemy: {', '.join(sm.state.roster.enemy) or '(none)'}")


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
