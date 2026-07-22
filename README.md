# draftiq

A League of Legends champion-select assistant. You tell it what's been picked and
banned so far; it returns ranked pick/ban recommendations with a confidence-aware
win-rate estimate and a term-by-term explanation of why each champion is recommended.

Supports both SOLOQ and standard competitive TOURNAMENT draft order. Two data
sources: a bundled synthetic dataset (`data/manual/`, fully offline) and live OP.GG
win-rate data. See `CLAUDE.md` for architecture notes and `lol-draft-tool-prompt.md`
for the full spec and phase plan.

## Usage

```sh
uv run draftiq new                          # start a SOLOQ draft (offline synthetic data)
uv run draftiq new --mode tournament        # ...or the standard competitive draft order
uv run draftiq new --provider opgg          # ...or backed by live OP.GG win-rate data
uv run draftiq ban Yasuo                    # ban/pick follow whoever's turn it is
uv run draftiq pick Aatrox --role top
uv run draftiq suggest                      # bans: ranked by how much denying them hurts the opponent
uv run draftiq suggest --role top           # picks: ranked recommendations, with explanations
uv run draftiq suggest --role top --lookahead  # ...also weighing the opponent's likely reply
uv run draftiq state                        # show the full draft so far
```

Draft state is saved to `.draftiq/state.json` in the current directory between runs.

## Project layout

**`src/draftiq/`**

- `__init__.py` -- empty package marker.
- `models.py` -- every pydantic model shared across the codebase:
  - `Champion` -- id, name, Data Dragon id, tags.
  - `ChampionStats` / `Matchup` / `Synergy` -- raw `wins`/`games` counts (never
    pre-computed rates) plus `.win_rate`/`.pick_rate`/`.ban_rate` convenience
    properties that divide them out.
  - `Build` -- items, runes, skill order, summoner spells for a champion/role.
  - `Recommendation` / `TermContribution` -- a scored candidate plus its
    term-by-term breakdown (`base_rate`, `vs <enemy>`, `with <ally>`, ...).
  - `DraftState` / `DraftAction` -- the persisted draft: mode, rank, provider, and
    the ordered list of bans/picks so far.
  - Enums: `Role`, `RankBracket` (OP.GG's real tier vocabulary), `Side`,
    `ActionType`, `DraftMode`, `ProviderName`.
- `cli.py` -- the `draftiq` command line app, built on `typer`:
  - `new` -- starts a draft (`--mode`, `--rank`, `--provider`), writes
    `.draftiq/state.json`.
  - `ban CHAMPION` / `pick CHAMPION --role ROLE [--side SIDE]` -- resolves the
    champion name (exact/fuzzy match against the provider's registry, or a
    "did you mean" hint), applies it via `DraftStateMachine`, saves state.
  - `suggest [--role ROLE] [--top N] [--lookahead]` -- dispatches on the current
    action: picks (`--role` required) go through `search/greedy.suggest` (or
    `search/lookahead.suggest_with_lookahead` with `--lookahead`); bans (`--role`
    unused) go through `search/ban.suggest_bans`. Renders a `rich` table (score,
    90% credible interval, sample size, term breakdown) either way.
  - `state` -- prints mode/rank/provider, all bans, both sides' picks, and whose
    turn is next.
  - Private helpers: `_get_provider` (picks `ManualCSVProvider`/`OpggProvider` from
    the saved state), `_load_state_machine`/`_save_state_machine`
    (`.draftiq/state.json` round-trip), `_resolve_champion` (name matching),
    `_render_recommendations` (the `rich` table).

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
  the protocol's contract). Zero network calls, ever.
- `opgg.py` -- `OpggProvider`, talking to the live OP.GG MCP server:
  - `_McpClient` (private) -- minimal MCP-over-HTTP client: `initialize` handshake,
    `notifications/initialized`, then `tools/call`, reusing one session id.
    Thread-safe (a lock guards the session-init race and the request-id counter).
  - `get_patch`/`get_champions`/`get_champion_stats`/`get_matchup`/`get_synergy`/
    `get_build` -- same contract as every other provider, each mapping draftiq's
    request onto the right OP.GG MCP tool call and parsing its response.
  - `_counters`/`_synergies` (private, cached) -- fetch a champion's whole
    counters/synergies list once, so `get_matchup`/`get_synergy` calls for the same
    champion against different opponents/allies hit the cache instead of the network.
  - `prefetch_for_suggest` -- warms the cache for many champions concurrently via a
    thread pool; without it, a cold `suggest()` call is one sequential HTTP
    round-trip per legal champion (confirmed at 2+ minutes for the full roster).
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
    `data/composition_features.toml`.
  - `features_from_tags(tags)` -- crude fallback for any champion not in that table,
    derived from Data Dragon tags.
  - `get_champion_features(champion, hand_curated)` -- hand-curated entry if one
    exists, else the tag-based fallback.
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

- `greedy.py` -- `suggest(sm, provider, role, top_n)`: gathers the legal candidate
  pool and each side's picks from the state machine, calls the provider's optional
  `prefetch_for_suggest` if it has one, scores every legal champion via
  `score_candidate`, and returns the top N by total score. 1-ply: doesn't consider
  what the opponent might pick next.
- `lookahead.py` -- `suggest_with_lookahead(...)`: 2-ply. Runs `greedy.suggest` for
  a wider candidate pool, then for each candidate simulates picking it and checks
  the opponent's best available reply across each of their still-unfilled roles
  (SOLOQ has no pre-assigned role per pick slot, so there's no single deterministic
  "their next pick"), penalizing candidates that would hand them a strong follow-up.
  Opt-in (`draftiq suggest --lookahead`) since it's several extra scoring passes.
- `ban.py` -- `suggest_bans(sm, provider, top_n)`: a genuinely different question
  from picking -- not "what's good for me" but "how much does denying this hurt the
  opponent." Reuses `score_candidate` with the sides swapped (their picks as allies,
  ours as the matchup threat) to get "how good would this be for them" for free,
  checked across each of their still-unfilled roles (bans aren't role-locked) and
  weighted by pick rate. Automatic whenever `draftiq suggest` runs during a ban.

**`tests/`** -- one file per module above (`test_shrinkage.py`, `test_draft_state.py`,
`test_scoring.py`, `test_composition.py`, `test_exposure.py`, `test_lookahead.py`,
`test_ban.py`, `test_opgg_format.py`, `test_opgg_provider.py`), plus
`test_e2e_cli.py` (full offline SOLOQ and TOURNAMENT drafts driven entirely through
the CLI, no network access). The OP.GG tests use `httpx.MockTransport` with real
captured server responses -- no live network calls in the test suite.

**`data/`**

- `composition_features.toml` -- hand-curated `CompositionFeatures` for all 20
  champions in the manual dataset, keyed by `champion_id`.
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
