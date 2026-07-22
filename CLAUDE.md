# draftiq

League of Legends draft pick/ban recommendation tool. See
`lol-draft-tool-prompt.md` at the repo root for the original spec this was built
from (architecture, scoring formulas, and phase plan all live there in full).

## Status

**Phase 1: complete.** Models, provider protocol, Data Dragon provider (champion
registry only), manual CSV provider, SQLite cache, shrinkage math, greedy scorer
(base rate + matchup + synergy deltas), SOLOQ draft state machine, CLI (`new`/`ban`/
`pick`/`suggest`/`state`).

**Phase 2: in progress.** Done so far: OP.GG MCP provider (`providers/opgg.py`) with
its own compact-response parser (`providers/opgg_format.py`), wired into the CLI
behind `draftiq new --provider {manual,opgg}`, `RankBracket` redesigned to match
OP.GG's real tier vocabulary, composition fit (`stats/composition.py` +
`data/composition_features.toml`), counterpick exposure (`stats/exposure.py`) -- all
5 score terms from the spec are now implemented in `score_candidate`. Still to do:
2-ply lookahead, TOURNAMENT draft mode, ban-specific recommendations, build display
in the CLI.

Phase 3 (not started): TUI/web UI, LLM-generated tips, per-player champion pool
weighting.

## OP.GG schema notes (read before touching `providers/opgg.py`)

Everything below was confirmed live against `https://mcp-api.op.gg/mcp` during Phase
2 development, not guessed from its tool descriptions -- several things it documents
about itself turned out to be wrong or incomplete. If OP.GG changes any of this,
`tests/test_opgg_format.py` and `tests/test_opgg_provider.py` are the ones that
should fail first.

- **Every tool requiring `desired_output_fields` does not return JSON.** It returns a
  bespoke compact text format: `class Name: field1,field2` declarations followed by a
  blank line and one positional-constructor-call data expression, e.g.
  `WeakCounter("Singed",927,408,0.56)`. This is every stats tool draftiq needs
  (`lol_get_champion_analysis`, `lol_get_champion_synergies`, `lol_list_champions`,
  etc.). `providers/opgg_format.py` is a small hand-rolled recursive-descent parser
  for it -- there is no MCP SDK or JSON mode that avoids this. Tools that *don't* take
  `desired_output_fields` (e.g. `lol_get_lane_matchup_guide`) return normal JSON.

- **Raw win counts exist for matchups and synergies, but not for a champion's own
  base rate.** `weak_counters`/`strong_counters` (from `lol_get_champion_analysis`)
  and `synergies` (from `lol_get_champion_synergies`) all expose a raw `win` integer
  alongside `play` -- exactly the raw-counts contract `StatsProvider` requires.
  `summary.average_stats` (what `get_champion_stats` needs) only ever exposes
  `win_rate` (rounded to ~2 decimals when fetched via `desired_output_fields`) and
  `play`. `get_champion_stats` reconstructs `wins = round(win_rate * play)`, with up
  to roughly ±0.25% relative error on high-sample champions. This is a deliberate,
  documented tradeoff (confirmed with the user), not an oversight.

- **Matchup coverage is sparse by design.** `weak_counters`/`strong_counters` is a
  small curated top-~3-per-side list per champion/role, not a full pairwise matchup
  matrix. Most candidate-vs-enemy pairs during a real draft will legitimately get
  `games=0` back from `get_matchup` -- this is the "no data available" case the
  protocol is designed to express, not a bug. `lol_get_lane_matchup_guide` (the
  arbitrary-pair tool) has no raw win/game counts at all -- it returns prose tips, a
  qualitative lane-advantage indicator, and win-rate-by-game-length curves. That's the
  right source for Phase 3's tips module, not for `get_matchup`.

- **`get_synergy`'s signature has no role/position parameters** (per the original
  spec), but OP.GG's synergy tool requires both `my_position` and `synergy_position`.
  `OpggProvider` queries both as `"all"` rather than the actual roles being drafted --
  a coarser aggregate than a role-specific number. If this proves too coarse in
  practice, the fix is threading role through the `StatsProvider` protocol, not
  guessing a position here.

- **Champion query parameter** is `Champion.ddragon_id.upper()` (e.g. `"MISS_FORTUNE"`
  or `"MISSFORTUNE"` -- OP.GG's matcher tolerates both, and apostrophes/periods can be
  present or stripped). Confirmed against Kai'Sa, Cho'Gath, Dr. Mundo, Wukong
  (ddragon id `"MonkeyKing"`), Jarvan IV, Xin Zhao, Renata Glasc. OP.GG's own
  `key` field (from `lol_list_champions`) is identical to Data Dragon's `id` field, so
  no separate id-mapping table is needed -- `Champion.ddragon_id` already *is* the
  OP.GG key.

- **`ids_names` on `summoner_spells` and `stat_mod_names` on `runes` both return raw
  numeric ids** despite the field name promising strings -- a narrow, apparent bug in
  OP.GG's field resolver scoped to exactly these two fields (item names and rune
  names resolve correctly everywhere else). Worked around with small hand-curated
  id->name tables in `providers/opgg.py` (`_SUMMONER_SPELL_NAMES`,
  `_STAT_SHARD_NAMES`) -- the same "hand-curated, clearly marked" pattern the spec
  already blesses for composition features.

- **`get_build`'s `opponent_id` is accepted but ignored.** OP.GG has no
  opponent-specific build data via `desired_output_fields` -- only prose tips via
  `lol_get_lane_matchup_guide` are opponent-aware, and that tool doesn't expose
  structured item/rune data.

- **`ChampionStats.pick_count`/`ban_count`/`total_games` are derived, not exact**, for
  the OP.GG provider. `pick_count = play` (OP.GG's `play` already means "games this
  champion was picked in this role/bracket"). OP.GG gives `pick_rate`/`ban_rate`
  directly but no bracket-wide game total, so
  `total_games ~= round(play / pick_rate)` and `ban_count ~= round(ban_rate *
  total_games)`. These only feed the Phase 2 counterpick-exposure weighting (a soft
  tiebreaker), not the core win-rate score.

- **`RankBracket` was redesigned in Phase 2** to match OP.GG's real tier vocabulary
  exactly (`IRON` through `CHALLENGER`, plus `_PLUS` bands, plus `ALL`), replacing
  Phase 1's 5-value placeholder which didn't correspond to anything real except the
  string `"ALL"`. `IBSG` (an OP.GG-defined tier, presumably Iron+Bronze+Silver+Gold)
  is intentionally omitted -- undocumented by OP.GG, not worth guessing at.

- **A cold `suggest()` call against OP.GG is otherwise unacceptably slow.** Scoring
  every legal champion against a ~170-champion live roster means ~170 sequential HTTP
  round-trips if nothing is done about it -- confirmed by timing an actual cold call,
  which took over 2 minutes and had to be killed. `OpggProvider.prefetch_for_suggest`
  fires the same lookups concurrently via a thread pool (warm: 41s -> 0.5s on a
  repeat call). This required making `SQLiteCache` and `_McpClient` thread-safe
  (`check_same_thread=False` + a lock around all cache I/O; a lock around the MCP
  session-init check-then-act and the request-id counter). `search/greedy.py` calls
  it via `hasattr(provider, "prefetch_for_suggest")` duck-typing rather than adding it
  to the `StatsProvider` protocol -- providers with negligible per-call cost (manual
  CSV, a dict lookup) have no reason to implement it.

## Architecture decisions and quirks

- **Provider split.** `DataDragonProvider` only implements `get_patch()` and
  `get_champions()` (real HTTP calls to ddragon.leagueoflegends.com). Its
  `get_champion_stats` / `get_matchup` / `get_synergy` / `get_build` all raise
  `NotImplementedError` rather than returning `games=0` -- Data Dragon has *no* stats
  data at all, which is a different fact from "a stats provider looked and found
  nothing." Collapsing the two would hide a real bug if this provider were ever wired
  into scoring by mistake.

- **`ManualCSVProvider` owns its own champion registry.** It does not borrow Data
  Dragon's `get_champions()` -- it reads `data/manual/champions.csv` directly, so it
  has zero network dependency and can back the CLI (and tests) fully offline. Its
  champion ids (1-20) are a local synthetic numbering, *not* Riot's real Data Dragon
  keys -- there's no cross-provider id mapping for it, since it never talks to another
  provider.

- **All win/game numbers in `data/manual/*.csv` are fabricated** for demo and test
  purposes. `get_patch()` on `ManualCSVProvider` returns `"SYNTH-1"`, which is not a
  real League patch string, specifically so it can never be confused for real data.

- **Draft state persists to `.draftiq/state.json`** in the current directory. Each
  CLI command is a separate process, so this is the only way "whose turn is it" (and
  which provider a draft was started with -- see `DraftState.provider`) survives
  between `draftiq ban ...` and `draftiq pick ...` calls. `draftiq new` overwrites it
  unconditionally.

- **`suggest` always requires `--role`,** even during the ban phase. Roles are
  assigned before champion select (per the spec), so during picks this is just "which
  role am I drafting for." During bans there are no picks on the board yet in SOLOQ,
  so matchup/synergy deltas are moot (though composition fit and exposure still
  compute against the full remaining pool, since neither depends on picks existing
  yet) -- the ban-phase ranking is a reasonable proxy for "worth denying," but not
  true ban-specific reasoning (that's a separate `ban_suggest`, still Phase 2 work).

- **Beta credible intervals are computed without scipy.** The Beta distribution's
  quantile function is implemented from scratch in `stats/shrinkage.py`: the
  regularized incomplete beta function via Lentz's continued-fraction method
  (Numerical Recipes), inverted by bisection. Only the standard library is used, per
  the approved dependency list. Validated in `tests/test_shrinkage.py` against the
  closed-form Beta(1,1) = Uniform(0,1) case.

- **`score_candidate` implements all 5 of the spec's score terms**: base rate,
  matchup deltas, synergy deltas, composition fit, and counterpick exposure.

- **Composition features use TOML, not YAML.** The spec says "checked-in YAML
  file"; `pyyaml` isn't on the approved dependency list, so `stats/composition.py`
  reads `data/composition_features.toml` via the standard library's `tomllib`
  (read-only, Python 3.11+) instead -- same goal (small, human-editable, checked-in,
  hand-curated), no new dependency. All 20 champions in the manual dataset are
  hand-curated; anything else (most of OP.GG's ~170-champion roster) falls back to a
  crude Data-Dragon-tag heuristic, clearly marked as a fallback, not a substitute.

- **Counterpick exposure's exact weighting formula isn't in the spec** -- it says
  "weighted by how many enemy picks remain and by how likely those counters are to
  actually be picked (pick rate)" without a formula. `stats/exposure.py` finds the
  single worst remaining counter (max shrunk-delta loss, same shrinkage as the
  regular matchup term) and weights it by the probability the enemy lands that
  specific counter across their remaining picks: `1 - (1 - pick_rate) **
  remaining_picks`, treating each remaining enemy pick as an independent chance at
  it. This is what makes pick order matter: the exact same candidate is scored as
  more exposed with 5 enemy picks left than with 1, and exactly 0 exposure with 0
  enemy picks left (there is a dedicated test for this in `tests/test_exposure.py`).

- **Cache key includes the patch string** (`providers/cache.py`), so a patch bump is
  a natural cache miss rather than needing an explicit invalidation pass.
  `SQLiteCache.prune_stale_patch` exists purely so the DB doesn't grow unbounded
  across patches over time -- nothing calls it automatically yet.

## Approved dependencies

`httpx`, `pydantic` (v2), `typer`, `rich`, stdlib `sqlite3`/`threading`/
`concurrent.futures`/`tomllib`, `pytest`, `mypy`, `ruff`. Nothing else without asking
first (see the spec doc). Notably: no MCP SDK dependency was needed for the OP.GG
provider -- `httpx` alone is enough, since every response observed from the live
server is plain JSON over HTTP POST (never SSE). No `pyyaml` either -- see the
composition-features bullet above.
