# draftiq

A League of Legends champion-select assistant. You tell it what's been picked and
banned so far; it returns ranked pick/ban recommendations with a confidence-aware
win-rate estimate and a term-by-term explanation of why each champion is recommended.

Supports both SOLOQ and standard competitive TOURNAMENT draft order. Two data
sources: a bundled synthetic dataset (`data/manual/`, fully offline) and live OP.GG
win-rate data. See `CLAUDE.md` for architecture notes and `lol-draft-tool-prompt.md`
for the full spec and phase plan.

## Getting started

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/achithrala/LoLDraft.git
cd LoLDraft
uv sync              # creates .venv/ and installs draftiq + its dependencies
```

There's no separate build/install step beyond `uv sync` -- `draftiq` is a normal
`uv`-managed project, and `pyproject.toml` registers a `draftiq` console-script entry
point (`draftiq = "draftiq.cli:app"`) pointing at the `typer` app in `src/draftiq/cli.py`.
Two ways to run it, both launch the exact same executable:

```sh
uv run draftiq new          # (1) recommended -- uv resolves the venv for you every time
```

```sh
source .venv/bin/activate   # (2) or activate the venv once per shell session...
draftiq new                 #     ...then drop the `uv run` prefix for the rest of it
```

Every command below assumes option (1) (`uv run draftiq ...`); substitute `draftiq ...`
if you've activated the venv instead. Nothing else needs to be started or launched for
the CLI itself -- `draftiq new`/`ban`/`pick`/`suggest`/etc. run and exit like any other
CLI tool, persisting state to `.draftiq/state.json` between invocations (see below).

The one thing that *does* stay running is the optional local web UI:

```sh
uv run draftiq serve                        # binds 127.0.0.1:8765 by default
uv run draftiq serve --host 0.0.0.0 --port 9000  # override host/port if needed
```

then open `http://127.0.0.1:8765` (or whatever host/port you chose) in a browser.
`Ctrl-C` in the terminal stops it. It's local-only by design -- no auth, no
`--workers` flag (see `CLAUDE.md`'s Phase 3 section for why) -- so don't expose it
past your own machine without understanding that tradeoff first.

A minimal first session, offline (no OP.GG account or network access needed):

```sh
uv run draftiq new                          # start a fresh SOLOQ draft
uv run draftiq ban Yasuo                    # ban/pick follow whoever's turn it is --
uv run draftiq state                        # ...check `state` if you're unsure whose turn is next
uv run draftiq suggest                      # see ranked ban suggestions
uv run draftiq pick Aatrox --role top       # once it's a pick phase
uv run draftiq suggest --role top           # ranked pick suggestions with explanations
```

Add `--provider opgg` to `draftiq new` once you want live win-rate data instead of the
bundled offline dataset (no API key needed -- it talks to OP.GG's public MCP server
directly). The full command reference follows below.

## Usage

```sh
uv run draftiq new                          # start a SOLOQ draft (offline synthetic data)
uv run draftiq new --mode tournament        # ...or the standard competitive draft order
uv run draftiq new --provider opgg          # ...or backed by live OP.GG win-rate data
uv run draftiq ban Yasuo                    # ban/pick follow whoever's turn it is
uv run draftiq pick Aatrox --role top
uv run draftiq build Aatrox --role top      # items, runes, skill order, summoners
uv run draftiq suggest                      # bans: ranked by how much denying them hurts the opponent
uv run draftiq suggest --role top           # picks: ranked recommendations, with explanations
uv run draftiq suggest --role top --lookahead  # ...also weighing the opponent's likely reply
uv run draftiq suggest --any-role           # picks: ranked across all your unfilled roles at once
uv run draftiq state                        # show the full draft so far
uv run draftiq serve                        # local web UI at http://127.0.0.1:8765

uv run draftiq pool add Me top Aatrox Darius        # champions a named player actually plays, per role
uv run draftiq pool import-opgg Me top "Faker#KR1" --region KR  # ...or import from a real summoner
uv run draftiq roster add ally Me                   # team membership for this draft (no role assignment)
uv run draftiq roster add enemy EnemyMidLaner
uv run draftiq suggest --role top --pool            # picks: restricted to your roster's ally pools
uv run draftiq suggest --pool                       # bans: bonus/highlight for enemy roster's pools

uv run draftiq tips Aatrox --role top --opponent Darius  # OP.GG's own matchup tips -- no LLM involved
```

Draft state is saved to `.draftiq/state.json` in the current directory between runs --
`draftiq serve`'s web UI reads/writes the exact same file, so the CLI and a browser
tab can drive one live draft interchangeably. The web UI is local-only (no auth,
binds `127.0.0.1` by default) and single-draft, matching the CLI's own trust model.

Champion pools (`.draftiq/pools.json`) are separate from draft state and persist
across drafts -- a teammate's pool doesn't change between games. Team rosters reset
on every `draftiq new` (who's actually in a given draft does change every game) and
deliberately don't assign players to specific roles: pick order/priority means who
ends up where isn't knowable in advance, so `--pool` consults the *union* of a
side's roster's pools for whichever role is relevant. `--pool` restricts candidates
for picks, but only adds a visible bonus for bans -- the full ban list is never
narrowed to what the enemy roster is known to play.

`draftiq tips` surfaces OP.GG's own prose matchup guide directly -- no LLM call, no
new dependency. Like `pool import-opgg`, it always uses live OP.GG data regardless
of the active draft's provider, and doesn't require an active draft at all.

## Project layout

**`src/draftiq/`**

- `__init__.py` -- empty package marker.
- `models.py` -- every pydantic model shared across the codebase:
  - `Champion` -- id, name, Data Dragon id, tags.
  - `ChampionStats` / `Matchup` / `Synergy` -- raw `wins`/`games` counts (never
    pre-computed rates) plus `.win_rate`/`.pick_rate`/`.ban_rate` convenience
    properties that divide them out.
  - `Build` -- items, runes, skill order, summoner spells for a champion/role.
  - `Recommendation` / `TermContribution` -- a scored candidate (including which
    `role` it was scored for) plus its term-by-term breakdown (`base_rate`,
    `vs <enemy>`, `with <ally>`, ...).
  - `DraftState` / `DraftAction` -- the persisted draft: mode, rank, provider,
    `roster` (this draft's ally/enemy team membership), and the ordered list of
    bans/picks so far.
  - `ChampionPool` -- one named player's per-role champion pool (stored by
    champion *name*, not id -- ids aren't stable across providers), with
    `resolve_ids(role, champions) -> set[int] | None` (`None` = no pool data for
    this role, don't restrict; `set()` = pool data exists but nothing resolves).
  - `TeamRoster` -- a draft's `ally`/`enemy` player-name lists. Membership only,
    no role assignment -- pick order/priority means who ends up in which slot
    isn't knowable in advance, so suggestions consult the *union* of a side's
    players' pools for whatever role is relevant instead of a fixed mapping.
  - `consolidated_pool_ids(registry, player_names, role, champions)` -- unions
    every named player's resolved pool for one role; same `None`-vs-`set()`
    contract as `ChampionPool.resolve_ids`.
  - `add_to_pool_registry(registry, player, role, champions)` -- appends already-
    resolved `Champion`s into a player's pool, deduped case-insensitively; shared
    by both `cli.py` (fuzzy-resolved names) and `web/app.py` (exact `champion_id`s).
  - `GameLengthWinRate` / `LaneMatchupGuide` -- OP.GG-only matchup tips (prose
    tip on playing against the opponent, qualitative lane/solo-kill advantage,
    recommended play style, win rate by game length). No equivalent in the
    offline manual dataset.
  - Enums: `Role`, `RankBracket` (OP.GG's real tier vocabulary), `Side`,
    `ActionType`, `DraftMode`, `ProviderName`, `RosterSide` (`ally`/`enemy` --
    relative to the user, not `Side.BLUE`/`RED`, which flips every draft).
- `cli.py` -- the `draftiq` command line app, built on `typer`:
  - `new` -- starts a draft (`--mode`, `--rank`, `--provider`), writes
    `.draftiq/state.json`.
  - `ban CHAMPION` / `pick CHAMPION --role ROLE [--side SIDE]` -- resolves the
    champion name (exact/fuzzy match against the provider's registry, or a
    "did you mean" hint), applies it via `DraftStateMachine`, saves state.
  - `build CHAMPION --role ROLE [--opponent CHAMPION]` -- resolves the champion
    (and opponent, if given), calls the provider's `get_build`, and prints items,
    runes, skill order, and summoners. A thin renderer -- both providers already
    implement `get_build`.
  - `tips CHAMPION --role ROLE --opponent CHAMPION` -- OP.GG's own prose lane
    matchup guide: a tip on playing against the opponent, which champion has
    the lane/solo-kill advantage, recommended play style, and win rate by game
    length. Always uses a fresh `OpggProvider()` directly (like
    `pool import-opgg`) regardless of the active draft's provider -- no
    equivalent exists in the offline manual dataset, and no active draft is
    required at all.
  - `suggest [--role ROLE] [--top N] [--lookahead] [--any-role] [--pool]` --
    dispatches on the current action: picks (`--role` required, unless
    `--any-role`) go through `search/greedy.suggest` (or
    `search/lookahead.suggest_with_lookahead` with `--lookahead`, or
    `search/priority.suggest_priority` with `--any-role`, which ranks champions
    across every unfilled role for your side instead of one and can't be
    combined with `--lookahead`); bans (`--role`/`--any-role` unused) go through
    `search/ban.suggest_bans`. `--pool` means something different depending on
    the action: for picks/`--any-role` it *restricts* candidates to the union of
    `draftiq roster`'s ally players' pools for the relevant role; for bans it
    only adds a bonus/highlight for candidates in the enemy roster's pools --
    the full ban list is never narrowed. Renders a `rich` table (score, 90%
    credible interval, sample size, term breakdown -- plus a Role column for
    `--any-role`) either way.
  - `pool add/remove/show/clear/import-opgg` -- manage named players' champion
    pools (`.draftiq/pools.json`, independent of any one draft). `add`/`remove`
    fuzzy-resolve champion names the same way `ban`/`pick` do, against
    `persistence.get_active_or_default_provider()` (the active draft's provider,
    or `ManualCSVProvider` as a bootstrap default before any draft exists).
    `import-opgg <player> <role> <riot_id> --region R [--top N]` pulls a real
    summoner's most-played champions via live OP.GG data (always uses
    `OpggProvider` directly, regardless of the active draft's provider) --
    requires an explicit `role` since OP.GG exposes no role/position data for a
    summoner's champion history.
  - `roster add/remove/show` -- manage the *current draft's* ally/enemy team
    membership (`sm.state.roster`, requires an active draft, resets on every
    `new`). Names only, no role assignment (see `models.TeamRoster`).
  - `state` -- prints mode/rank/provider, all bans, both sides' picks (in the order
    they were actually picked), and whose turn is next. Once the draft is complete,
    also prints a "Final teams" section (`_render_final_teams`, shared with
    `_print_next_turn` so it appears immediately after the pick that finishes the
    draft too) -- both sides' picks re-sorted into canonical role order
    (top/jungle/mid/bottom/support, not pick order, since role is chosen freely at
    `pick` time) with each entry annotated `(pick N)`, its 1-indexed position in the
    overall draft-wide pick sequence -- the one piece of information the role
    re-sort would otherwise lose.
  - `serve [--host] [--port]` -- launches the local web UI (`web/app.py`) via
    `uvicorn`, binding `127.0.0.1` by default. Reads/writes the exact same
    `.draftiq/state.json`, so a browser tab and the CLI can drive the same live
    draft. `fastapi`/`uvicorn` are imported lazily inside this command only, so
    every other command stays independent of the web dependencies.
  - Private helpers: `_get_provider`/`_load_state_machine`/`_save_state_machine`
    (thin wrappers around `persistence.py`, adding `rich`/`typer.Exit` presentation
    on top), `_resolve_champion` (fuzzy name matching, CLI-only -- the web UI uses
    exact `champion_id` selection instead), `_render_recommendations` /
    `_render_build` (the `rich` output for each).
- `persistence.py` -- draft-state file I/O and provider resolution, shared by
  `cli.py` and `web/app.py` so both front ends drive the same `.draftiq/state.json`
  identically:
  - `STATE_DIR`/`STATE_FILE`, `get_provider(name)` (`ManualCSVProvider`/
    `OpggProvider`), `load_state_machine()`/`save_state_machine(sm)` -- raise plain
    exceptions (`NoDraftInProgressError`, or a propagated `ValidationError`/
    `JSONDecodeError` for a corrupt file) rather than doing any CLI/HTTP-specific
    presentation.
  - `STATE_LOCK` -- a `threading.Lock`, used only by `web/app.py` (the CLI is a
    fresh single-threaded process per invocation, so a lock provides no benefit
    there). Serializes each mutating web request's load-mutate-save cycle so two
    concurrent browser requests can't race and silently clobber one another's write.
  - `load_pool_registry()`/`save_pool_registry(registry)` -- same shape as the
    state functions, for `.draftiq/pools.json` (`dict[str, ChampionPool]`,
    reusable across drafts, separate from `state.json` since a player's pool
    doesn't reset when a new draft starts).
  - `get_active_or_default_provider()` -- the active draft's provider if
    `state.json` loads cleanly, else `ManualCSVProvider()`. Lets `pool`/`roster`
    commands validate/resolve champion names even before any draft has started.

**`src/draftiq/providers/`** -- each data source implements the same `StatsProvider`
Protocol, so nothing else in the codebase knows or cares which one it's talking to.

- `base.py` -- defines `StatsProvider`: `get_patch`, `get_champions`,
  `get_champion_stats`, `get_matchup`, `get_synergy`, `get_build`. The contract every
  implementation below follows: raw counts, never a bare percentage.
- `cache.py` -- `SQLiteCache` (thread-safe get/set/prune against a local SQLite file,
  keyed on `cache_key`, with TTL expiry) and the `@cached(...)` decorator that
  provider methods wrap themselves in -- it builds the cache key from
  `(source, method name, args, current patch)`, so a patch bump is a natural cache
  miss with no explicit invalidation step needed.
- `ddragon.py` -- `DataDragonProvider`:
  - `get_patch` -- current patch version from Data Dragon's `/api/versions.json`.
  - `get_champions` -- the full champion list from `/cdn/{patch}/.../champion.json`.
  - `get_champion_stats`/`get_matchup`/`get_synergy`/`get_build` -- all raise
    `NotImplementedError`; Data Dragon has no win-rate data at all.
- `manual.py` -- `ManualCSVProvider`: loads all five `data/manual/*.csv` files into
  memory at construction, then implements every `StatsProvider` method as a plain
  dict lookup (falling back to `games=0` for an unlisted matchup/synergy pair, per
  the protocol's contract). Zero network calls, ever. `get_champions` returns the
  list sorted alphabetically by name (both this provider and `OpggProvider` do --
  see the note below `opgg.py`'s `get_champions`), since every consumer of the
  full roster is a selection UI (the web champion picker, and the build/tips/pool
  selects) where insertion/id order is just noise; suggestion ranking itself is
  unaffected since `search/*` always sorts its own output by score, independent
  of the input list's order.
- `opgg.py` -- `OpggProvider`, talking to the live OP.GG MCP server:
  - `_McpClient` (private) -- minimal MCP-over-HTTP client: `initialize` handshake,
    `notifications/initialized`, then `tools/call`, reusing one session id.
    Thread-safe (a lock guards the session-init race and the request-id counter).
  - `get_patch`/`get_champions`/`get_champion_stats`/`get_matchup`/`get_synergy`/
    `get_build` -- same contract as every other provider, each mapping draftiq's
    request onto the right OP.GG MCP tool call and parsing its response.
    `get_champion_stats` reads `data.summary.positions[].stats` (one
    `{name, stats: {play, win_rate, pick_rate, ban_rate}}` entry per role the
    champion has recorded games in), matched case-insensitively against the
    requested role -- **not** `data.summary.average_stats`, which looks like the
    obvious field but is a champion-wide aggregate the `position` request
    parameter has no effect on at all (confirmed live: querying it at
    `position=top` vs `position=support` for the same champion returned
    byte-identical numbers). A role with no matching `positions[]` entry (the
    champion genuinely has zero recorded games there) falls through to the same
    zero-games `ChampionStats` `get_matchup`/`get_synergy` already return for an
    unlisted pair. See `CLAUDE.md`'s "OP.GG schema notes" for the live
    investigation and the suggestion-quality bug this was caught from.
  - `_counters`/`_synergies` (private, cached) -- fetch a champion's whole
    counters/synergies list once, so `get_matchup`/`get_synergy` calls for the same
    champion against different opponents/allies hit the cache instead of the network.
  - `prefetch_for_suggest` -- warms the cache for many champions concurrently via a
    thread pool; without it, a cold `suggest()` call is one sequential HTTP
    round-trip per legal champion (confirmed at 2+ minutes for the full roster).
  - `get_summoner_champion_pool(game_name, tag_line, region, limit)` -- a real
    summoner's most-played champion names, sorted by play count. Not part of
    `StatsProvider` (`ManualCSVProvider` has no concept of a real summoner, same
    reasoning as `prefetch_for_suggest`); used only by `draftiq pool import-opgg`.
    OP.GG exposes no role/position data for this -- confirmed live -- so callers
    must supply the target role themselves.
  - `get_lane_matchup_guide(my_champion_id, opponent_id, role)` -- prose tips +
    qualitative lane-advantage indicators for one matchup, via
    `lol_get_lane_matchup_guide`. Not part of `StatsProvider`; used only by
    `draftiq tips`/`GET /api/tips`. The one tool this provider calls that takes
    no `desired_output_fields` and returns plain JSON directly (`json.loads`,
    not `opgg_format.parse`) and no rank/tier parameter at all. Uses
    `_lane_guide_champion_param` (not `_opgg_champion_param`) -- this tool needs
    genuine `UPPER_SNAKE_CASE` derived from the display name and rejects the
    other tools' more tolerant format outright.
  - `OpggApiError` -- raised when OP.GG's MCP server returns a JSON-RPC error.
  - See `CLAUDE.md` for the full list of schema quirks each method works around.
- `opgg_format.py` -- `parse(text)`: turns OP.GG's bespoke compact response format
  (`class Name: fields` declarations + one positional-constructor data expression)
  into plain nested dicts/lists/scalars. `OpggFormatError` on anything that doesn't
  match the expected grammar.

**`src/draftiq/stats/`**

- `shrinkage.py` -- the statistics core:
  - `shrink_win_rate(wins, games, p0, k)` -- empirical-Bayes shrinkage of a raw win
    rate toward the role average `p0`, returning a `ShrinkageResult` (point estimate
    plus a 90% Beta credible interval).
  - `shrink_delta(d_raw, n, k_m)` -- shrinks a matchup/synergy delta toward zero by
    sample size; `n=0` always collapses to exactly `0`.
  - `compute_role_average(stats)` -- the games-weighted `p0` across a set of
    champions (never hardcoded to 0.5).
  - `regularized_incomplete_beta` / `beta_ppf` -- the Beta distribution's CDF and
    inverse-CDF, implemented from scratch (continued-fraction method + bisection) so
    the project never needs scipy.
- `scoring.py` -- `score_candidate(...)`: combines all 5 score terms from the spec --
  shrunk base rate, a shrunk delta per enemy matchup, a shrunk delta per ally
  synergy, composition fit, and counterpick exposure -- into one `Recommendation`
  whose `.terms` list shows exactly where the total score came from.
- `composition.py` -- team composition as a soft tiebreaker:
  - `CompositionFeatures` -- a champion's damage-type split, engage/disengage/poke/
    waveclear/frontline flags, and early/mid/late scaling window.
  - `load_hand_curated_features()` -- loads (and caches) the hand-curated table from
    `data/composition_features.toml`, keyed by `Champion.ddragon_id` (a real,
    provider-independent Data Dragon string id) -- NOT `champion_id`, which
    collides across providers (see `CLAUDE.md`'s composition-features bullet for
    the live bug this was caught from: `ManualCSVProvider`'s synthetic ids 1-20
    overlap with real Data Dragon/OP.GG ids 1-20, a completely different 20
    champions).
  - `features_from_tags(tags)` -- crude fallback for any champion not in that table,
    derived from Data Dragon tags.
  - `get_champion_features(champion, hand_curated)` -- hand-curated entry (matched
    by `ddragon_id`) if one exists, else the tag-based fallback.
  - `comp_fit(candidate, ally_features)` -- penalizes a team (soft targets only, per
    spec: small relative to the win-rate terms) for damage skew, no frontline, no
    engage, or no waveclear.
- `exposure.py` -- `compute_exposure(...)`: the counterpick-exposure term. Finds the
  single worst remaining counter for a candidate among the still-unpicked pool and
  weights it by the probability the enemy actually lands it across their remaining
  picks -- this is what makes pick order matter (the same candidate is riskier to
  pick with 5 enemy picks left than with 1, and risk-free with 0 left).

**`src/draftiq/draft/`**

- `rules.py` -- the two 20-step turn order tables and `order_for(mode)`:
  `SOLOQ_ORDER` (10 bans, then picks B1/R1R2/B2B3/R3R4/B4B5/R5) and
  `TOURNAMENT_ORDER` (the standard competitive order: ban1 x6, pick1 x6, ban2 x4,
  pick2 x4 -- ban2 interleaves *after* pick1, unlike SOLOQ where all bans come
  first).
- `state.py` -- `DraftStateMachine`, wrapping a `DraftState` and validating every
  mutation:
  - `new(mode, rank, provider)` -- classmethod, starts a fresh draft.
  - `current_side`/`current_action_type`/`is_complete` -- whose turn, ban or pick,
    and whether the draft is done.
  - `apply_ban`/`apply_pick` -- the only way to mutate state; reject duplicate
    champions, wrong-phase actions, wrong-side actions, and already-filled roles by
    raising one of `DraftCompleteError`/`WrongActionTypeError`/`WrongSideError`/
    `ChampionUnavailableError`/`RoleAlreadyFilledError` (all subclasses of
    `DraftError`).
  - `banned_champion_ids`/`picked_champion_ids`/`taken_champion_ids`/
    `legal_champion_ids`/`filled_roles`/`remaining_picks` -- read-only queries used
    by the scorer and the CLI.

**`src/draftiq/search/`**

- `greedy.py` -- `suggest(sm, provider, role, top_n, pool_ids=None)`: gathers the
  legal candidate pool and each side's picks from the state machine, calls the
  provider's optional `prefetch_for_suggest` if it has one, scores every legal
  champion via `score_candidate`, and returns the top N by total score. 1-ply:
  doesn't consider what the opponent might pick next. Also adds a `popularity`
  tiebreaker term -- `POPULARITY_WEIGHT_SCALE * (pick_rate /
  max_pick_rate_among_legal_ids)`, relative to the most-picked legal candidate in
  this exact role/rank query rather than a flat `pick_rate` scale, so it behaves
  consistently whether pick rates are spread wide (the manual dataset) or
  clustered tight (real OP.GG data) -- added after live suggestions surfaced
  legitimately-strong-but-rarely-played picks (e.g. a top-lane Warwick with a
  large sample and a genuinely good win rate) ahead of standard picks with a
  similar win rate; see the module's docstring for the full reasoning and a
  documented first-attempt-too-weak calibration finding. `pool_ids` (an
  already-resolved id set, see `models.consolidated_pool_ids`) restricts the
  candidate set when given -- via a *separate* `candidate_ids` variable, not by
  reassigning `legal_ids`, since `legal_ids` also feeds `remaining_enemy_ids` into
  `score_candidate` for counterpick exposure, which must stay unrestricted (the
  enemy isn't limited to my pool) -- see `CLAUDE.md` for the bug this guards
  against and its regression test.
- `lookahead.py` -- `suggest_with_lookahead(..., pool_ids=None)`: 2-ply. Runs
  `greedy.suggest` for a wider candidate pool, then for each candidate simulates
  picking it and checks the opponent's best available reply across each of their
  still-unfilled roles (SOLOQ has no pre-assigned role per pick slot, so there's
  no single deterministic "their next pick"), penalizing candidates that would
  hand them a strong follow-up. Opt-in (`draftiq suggest --lookahead`) since it's
  several extra scoring passes. `pool_ids` reaches only ply 1's candidate
  generation, deliberately never ply 2's opponent-response simulation (the
  opponent doesn't share your pool).
- `ban.py` -- `suggest_bans(sm, provider, top_n, pool_ids_by_role=None)`: a
  genuinely different question from picking -- not "what's good for me" but "how
  much does denying this hurt the opponent." Reuses `score_candidate` with the
  sides swapped (their picks as allies, ours as the matchup threat) to get "how
  good would this be for them" for free, checked across each of their
  still-unfilled roles (bans aren't role-locked) and weighted by pick rate.
  Automatic whenever `draftiq suggest` runs during a ban. `pool_ids_by_role` (the
  union of the *enemy* roster's pools per role) adds a `POOL_BONUS = 0.05`
  `"in enemy pool"` term for a known match -- unlike every other `pool_ids*`
  parameter in this package, this is a bonus/highlight, **never** a restriction:
  the full ban list always stays the same length.
- `priority.py` -- `suggest_priority(sm, provider, top_n, pool_ids_by_role=None)`:
  a third question, not in the original spec -- "which champion should I grab
  right now, whichever role it fills," for flex/contested picks rather than a
  role you've already committed to. Scores every legal champion against each of
  *your* still-unfilled roles (like `ban.py`, but for your own roles) and keeps
  the best; adds a small flex-value bonus when more than one role scores close to
  the best (only counting roles the champion actually has games in -- a role with
  zero recorded games shrinks to that role's population baseline, which is "no
  evidence," not "proven competence," and must not count), and a contest-risk
  bonus (same `1 - (1-pick_rate)**remaining_enemy_picks` shape as counterpick
  exposure) for champions likely to get taken if you wait. Opt-in via
  `draftiq suggest --any-role`; picks only, and not combinable with `--lookahead`
  yet. `pool_ids_by_role` (per-role, unlike `greedy.py`'s single set -- a
  candidate can be pooled for one role and not another) restricts which of a
  candidate's unfilled roles are even scored; a candidate eligible in none of
  them is dropped entirely rather than crashing on an empty per-candidate role set.
- `dispatch.py` -- `resolve_suggestion(sm, provider, role, top_n, lookahead,
  any_role, pool=False) -> (recommendations, show_role_column)`: the CLI's
  original `suggest` if/elif chain (ban vs. any-role vs. role-locked pick, and the
  exact three validation-error strings), extracted so `cli.suggest` and the web
  `suggest` endpoint share one implementation and can never disagree about what's
  valid. `SuggestRequestError` (a `ValueError` subclass) covers the three
  validation cases; a plain `ValueError` from the underlying search module (e.g.
  "already complete") can still surface too -- callers catch `ValueError`
  broadly. Also the one place that resolves the `pool: bool` flag into actual
  champion-id sets (loading `persistence.load_pool_registry()` and reading
  `sm.state.roster`), since it already knows the current side and action type --
  restricting via the *ally* roster for picks, bonusing via the *enemy* roster
  for bans (see `ban.py` above).

**`src/draftiq/web/`** -- the local web UI: a FastAPI app re-exposing the same
`DraftStateMachine`/`StatsProvider`/`search/*` logic over HTTP, plus a static
frontend. Local-only (no auth, `127.0.0.1` by default), single shared
`.draftiq/state.json` -- see CLAUDE.md's "Phase 3" section for the full reasoning
(concurrency locking, why `--workers` is never exposed, champion-id validation the
CLI never needed, provider memoization).

- `app.py` -- `create_app() -> FastAPI`. Routes: `POST /api/draft/new`,
  `POST /api/draft/ban`, `POST /api/draft/pick`, `GET /api/draft/state`,
  `GET /api/draft/build`, `GET /api/draft/suggest`, `GET /api/champions`,
  `GET /api/health`, plus `GET /` serving `static/index.html`. Global exception
  handlers map `persistence.NoDraftInProgressError` -> 404, `ValueError` from a
  corrupt `state.json` -> 500, and `draft.state.DraftError` subclasses -> 409;
  per-route `try`/`except` handles unknown `champion_id`s and `suggest`/`build`'s
  own errors -> 400/404. Every mutating route holds `persistence.STATE_LOCK`
  across its whole load-mutate-save critical section. Pool/roster routes:
  `GET /api/pool` (whole registry), `POST /api/pool/{add,remove,clear,import-opgg}`,
  `GET /api/pool/champions` (like `/api/champions` but doesn't require an active
  draft -- backed by `get_active_or_default_provider()`, so the pool panel works
  before you've ever clicked "New Draft"), `GET /api/roster`,
  `POST /api/roster/{add,remove}` (requires an active draft -- roster lives in
  `DraftState`). `suggest` takes a `pool: bool` query param with the same
  restrict-for-picks/bonus-for-bans asymmetry as the CLI's `--pool`.
  `GET /api/tips` and `GET /api/tips/champions` always construct a fresh
  `OpggProvider()` directly (like `pool_import_opgg`), regardless of the active
  draft's provider -- `/api/tips/champions` exists because `/api/champions`/
  `/api/pool/champions` could resolve through `ManualCSVProvider`'s different id
  space instead. `OpggApiError` -> 502 (an upstream failure, not a client input
  problem).
- `schemas.py` -- request/response pydantic models not already covered by
  `models.py` (`NewDraftRequest`, `BanRequest`, `PickRequest`,
  `DraftStateResponse`/`DraftActionOut` + `build_state_response(sm, provider)`,
  `PoolAddRequest`/`PoolRemoveRequest`/`PoolClearRequest`/
  `PoolImportOpggRequest`/`PoolResponse`, `RosterAddRequest`/
  `RosterRemoveRequest`/`RosterResponse`), plus
  `resolve_champion_id(champion_id, champions)`/`UnknownChampionIdError` -- the
  exact-id equivalent of `cli._resolve_champion`'s fuzzy name matching. Pool
  requests are id-based (matching `ban`/`pick`'s web convention -- resolved
  through the champion picker), except `PoolImportOpggRequest`, which is
  name-based since the whole point is importing names the web UI doesn't know
  about yet. `Recommendation`, `Champion`, `Build`, and `LaneMatchupGuide` from
  `models.py` are reused directly as response bodies elsewhere.
- `static/index.html` / `app.js` / `style.css` -- one page, zero build tooling
  (no npm/node/framework): a new-draft form, ban/pick board, champion picker
  (search + click, backed by `GET /api/champions`), a suggestions table with
  Lookahead/Any-role/"Use my pool" toggles that mirror `dispatch.py`'s
  mutual-exclusion rules client-side, a roster panel (ally/enemy player-name
  lists), a champion-pool panel (per-player, per-role, plus an OP.GG-import
  sub-form), a build panel, and a matchup-tips panel (champion/role/opponent
  selects backed by `GET /api/tips/champions`, its own OP.GG-specific champion
  list). Every mutating action re-fetches
  `GET /api/draft/state` and re-renders from scratch -- the server is always the
  source of truth, no separate client-side draft logic. The board always shows each
  side's 5 picks sorted into canonical role order rather than pick order (unlike
  the CLI's separate running `state` log, this is the board's only view), each
  annotated `(pick N)` with its overall draft-wide pick number -- the same
  role-sort-loses-pick-order tradeoff `cli._render_final_teams` handles, computed
  client-side from the same `state.actions` order the server returns. A ban-phase
  candidate with the `"in enemy pool"` bonus term gets a visible badge next to its
  name, not just buried breakdown text -- the "Use my pool" checkbox is enabled
  during bans now too (unlike Lookahead/Any-role, which stay pick-only), since the
  bonus (unlike a restriction) is meaningful there.

**`tests/`** -- one file per module above (`test_shrinkage.py`, `test_draft_state.py`,
`test_scoring.py`, `test_composition.py`, `test_exposure.py`, `test_lookahead.py`,
`test_ban.py`, `test_priority.py`, `test_pool.py`, `test_opgg_format.py`,
`test_opgg_provider.py`), plus `test_e2e_cli.py` (full offline SOLOQ and TOURNAMENT
drafts driven entirely through the CLI, no network access) and `test_e2e_web.py` (the
same, driven through the web API via `fastapi.testclient.TestClient`, itself built on
the already-approved `httpx`). The OP.GG tests use `httpx.MockTransport` with real
captured server responses -- no live network calls anywhere in the test suite.
`test_pool.py` covers `models.ChampionPool`/`TeamRoster`/`consolidated_pool_ids`/
`add_to_pool_registry` and `persistence`'s pool-registry functions in isolation;
each `pool_ids`/`pool_ids_by_role`-consuming `search/*` module has its own extended
test class instead of a shared one, since what "correct" means differs per module
(hard restriction for `greedy.py`/`priority.py`/`lookahead.py`, bonus-only for
`ban.py`) -- most notably `test_scoring.py`'s
`test_pool_restriction_preserves_exposure_to_a_real_off_pool_counter`, a regression
test for the `legal_ids`-vs-`candidate_ids` bug described above. `test_opgg_provider.py`'s
`TestGetLaneMatchupGuide` covers `get_lane_matchup_guide` against a canned real plain-JSON
response (not `opgg_format`'s grammar) and `_lane_guide_champion_param`'s stricter
`UPPER_SNAKE_CASE` formatting directly, since the mock transport itself doesn't
validate the champion-param format the way live OP.GG does. `draftiq tips`/
`GET /api/tips` have no offline CLI/web e2e coverage -- like `pool import-opgg`,
they always require live OP.GG data with no manual-dataset equivalent to mock a
full command flow against meaningfully offline; verified live instead.

**`data/`**

- `composition_features.toml` -- hand-curated `CompositionFeatures` for all 20
  champions in the manual dataset, keyed by `ddragon_id` (e.g. `"MissFortune"`) --
  not `champion_id`, which collides across providers.
- `manual/` -- the synthetic dataset `ManualCSVProvider` reads: `champions.csv`,
  `champion_stats.csv`, `matchups.csv`, `synergies.csv`, `builds.csv`. All
  fabricated, never mistakable for real data (`get_patch()` returns `"SYNTH-1"`).

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy --strict
```
