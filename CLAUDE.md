# draftiq

League of Legends draft pick/ban recommendation tool. See
`lol-draft-tool-prompt.md` at the repo root for the original spec this was built
from (architecture, scoring formulas, and phase plan all live there in full).

## Status

**Phase 1: complete.** Models, provider protocol, Data Dragon provider (champion
registry only), manual CSV provider, SQLite cache, shrinkage math, greedy scorer
(base rate + matchup + synergy deltas), SOLOQ draft state machine, CLI (`new`/`ban`/
`pick`/`suggest`/`state`).

**Phase 2: complete.** OP.GG MCP provider (`providers/opgg.py`) with its own
compact-response parser (`providers/opgg_format.py`), wired into the CLI behind
`draftiq new --provider {manual,opgg}`; `RankBracket` redesigned to match OP.GG's
real tier vocabulary; composition fit (`stats/composition.py` +
`data/composition_features.toml`) and counterpick exposure (`stats/exposure.py`) --
all 5 score terms from the spec are implemented in `score_candidate`; 2-ply
lookahead (`search/lookahead.py`, opt-in via `draftiq suggest --lookahead`);
TOURNAMENT draft mode (`draft/rules.py`, `draftiq new --mode tournament`);
ban-specific recommendations (`search/ban.py`, automatic when `draftiq suggest` runs
during a ban step); build display (`draftiq build CHAMPION --role ROLE
[--opponent CHAMPION]`).

**Phase 3: complete.** Local web UI (`web/app.py` + `web/static/`, launched via
`draftiq serve`). Champion pool weighting, expanded well beyond the spec's "the
user's own pool" -- named pools for any player (teammates, enemy players in a Clash
draft), team-membership rosters, and OP.GG-summoner import (`draftiq pool`/
`draftiq roster`, `draftiq suggest --pool`; see "Phase 3: champion pool weighting"
below for the full design). Matchup tips (`draftiq tips CHAMPION --role ROLE
--opponent CHAMPION`) -- also descoped from the spec's literal "LLM-generated tips":
confirmed with the user that surfacing OP.GG's own prose tips directly (no LLM call,
no new dependency) satisfies the need; see "Phase 3: matchup tips" below.

**Post-Phase 2 addition: champion-priority / flex-pick suggestions.** Not in the
original spec. `search/priority.py` (`suggest_priority`, wired into the CLI as
`draftiq suggest --any-role`) answers a different question from every other search/
module: not "who's best for the role I've already chosen" (`greedy.suggest`) but
"which champion should I grab right now, whichever role it ends up filling." It
scores each legal champion against every one of the current side's unfilled roles
(same cross-role trick as `search/ban.py`, but for your own roles instead of the
opponent's), then adds two small additive tiebreakers: a flex bonus for champions who
score well in more than one role (drafting them keeps the opponent guessing and keeps
your own options open), and a contest-risk bonus using the same
`1 - (1-pick_rate)**remaining_enemy_picks` shape as counterpick exposure and
`search/ban.py`'s pick-rate weighting (grab popular champions before the opponent
denies them). See `search/priority.py`'s module docstring for the full reasoning,
including a documented bug-and-fix: a naive version let a champion's *unplayed* roles
(games=0, which shrinks all the way to that role's population baseline) count as
"flex viable" just because the baseline happened to be close to their real score --
confirmed live against the manual dataset, where every single champion was getting
flagged as a 4-5 role flex pick before the fix. Both best-role selection and the flex
bonus now require `n_games > 0` in a role before it can count at all.

## Phase 3: local web UI (read before touching `web/`, `persistence.py`, or
`search/dispatch.py`)

- **Local-only by design, confirmed with the user before building it.** No auth, binds
  `127.0.0.1` by default, single shared `.draftiq/state.json` -- the same trust model
  the CLI already has, not a multi-user or LAN-shareable tool. `draftiq serve` is the
  only supported way to run it; it deliberately never exposes a `--workers` flag (see
  the STATE_LOCK bullet below for why that would be actively dangerous, not just
  unnecessary).

- **The CLI and the web UI read/write the exact same `.draftiq/state.json`.** This was
  a deliberate simplicity choice over multi-draft/session support (also confirmed with
  the user) -- `draftiq ban X` from a terminal and a browser tab pointed at
  `draftiq serve` in the same directory drive the identical live draft
  interchangeably. There is no session concept anywhere in `web/app.py`.

- **`persistence.py` and `search/dispatch.py` are new shared modules, extracted from
  what used to be CLI-only private helpers**, specifically so the web API and the CLI
  can never disagree about behavior:
  - `persistence.py` owns `STATE_DIR`/`STATE_FILE`, `get_provider`,
    `load_state_machine`/`save_state_machine` (moved verbatim out of `cli.py`, now
    raising plain exceptions -- `NoDraftInProgressError`, or letting
    `pydantic.ValidationError`/`json.JSONDecodeError` propagate for a corrupt file --
    instead of calling `typer.Exit`/`console.print`, so each front end renders errors
    its own way). `cli.py`'s `_load_state_machine`/`_save_state_machine` are now thin
    wrappers that add back the CLI's presentation; `tests/test_e2e_cli.py` needed zero
    changes.
  - `search/dispatch.py`'s `resolve_suggestion(sm, provider, role, top_n, lookahead,
    any_role)` is `cli.suggest`'s old if/elif chain (ban vs. any-role vs.
    role-locked pick, and the exact three validation error strings), now shared by
    `cli.suggest` and the web `suggest` endpoint. `SuggestRequestError` (a `ValueError`
    subclass) is raised for the three validation cases; the underlying search modules
    can still raise a plain `ValueError` on their own (e.g. "already complete") --
    callers must catch `ValueError` broadly, not just `SuggestRequestError`, since the
    latter *is* the former.

- **`persistence.STATE_LOCK` (a plain `threading.Lock`) is a web-only concern, not
  used by the CLI at all.** Each CLI invocation is a separate, single-threaded OS
  process, so an in-process `Lock` object provides zero cross-process protection --
  it was never the mitigation for CLI-vs-CLI races (those are out of scope by design,
  same as before this feature). The web server is long-lived and *can* receive
  genuinely concurrent requests (two browser tabs, a double-click), where two racing
  mutations would otherwise both read the same on-disk state, compute independently,
  and let the second write silently clobber the first -- the same class of bug already
  fixed once for `SQLiteCache` during OP.GG prefetching (`providers/cache.py`).
  `web/app.py`'s mutating routes hold the lock across the *entire* load-mutate-save
  critical section (not per-call), and read-only routes hold it across their single
  load too, since `write_text(...)` isn't atomic and a concurrent read mid-write could
  see a torn file. This is also exactly why `serve` must never expose `--workers`:
  separate uvicorn worker processes would each get their own independent `Lock`
  object, silently defeating this the same way multiple CLI processes already would.

- **The web API validates `champion_id` against the active provider's roster; the CLI
  never had to.** `DraftStateMachine.apply_ban`/`apply_pick` only check
  `champion_id in taken_champion_ids()` (duplicate/already-taken) -- they have no
  concept of whether an id is a *real* champion, because the CLI is only safe today
  since `_resolve_champion` always resolves through `provider.get_champions()` first.
  The web API accepts a bare `champion_id: int` directly (an exact-match picker UI is
  the natural web equivalent of the CLI's fuzzy name matching, not a reimplementation
  of `difflib` fuzzy matching over HTTP), so `web/schemas.py`'s
  `resolve_champion_id`/`UnknownChampionIdError` fill that gap -- used by the
  ban/pick/build routes, mapped to `400`.

- **Provider instances are memoized on `app.state`, keyed by `ProviderName`,** unlike
  the CLI where `_get_provider` builds a fresh one every invocation (each CLI command
  is a new process anyway). Both `StatsProvider` implementations are stateless/
  read-only after construction, so this just avoids re-parsing the manual CSVs or
  re-opening `OpggProvider`'s SQLite cache connection on every single request.

- **`GET /api/champions` requires an active draft**, 404 otherwise -- it needs
  `sm.state.provider` to know which `StatsProvider` to ask, exactly like every other
  route already needs a loaded draft first. Not a limitation worth working around: the
  frontend's champion picker only ever appears after a draft exists anyway.

- **The frontend (`web/static/`) is plain HTML/CSS/vanilla JS, zero build tooling** --
  no npm, no node, no framework, one `<script type="module">` doing `fetch()` calls
  and DOM updates. This was a deliberate choice to avoid introducing a second package
  ecosystem alongside the project's all-Python one for what is a single page with five
  panels (new-draft form, ban/pick board, champion picker, suggestions table, build
  panel). Every mutating action re-fetches `GET /api/draft/state` and re-renders from
  scratch -- there is no separate client-side draft state machine; the server is
  always the source of truth, matching the "no auth, single local user" trust model.
  The Lookahead/Any-role checkboxes mirror `search/dispatch.py`'s mutual-exclusion
  rules client-side for UX, but the backend's `400`s remain the actual source of
  truth (a user could still hit the API directly).

- **`fastapi`/`uvicorn` are imported lazily inside `cli.py`'s `serve` command**, not at
  module level, so every other CLI command stays fast and independent of the web
  dependencies even if something's wrong with them.

## Post-Phase-3 addition: popularity weighting in `search/greedy.py`

- **Why this exists.** A live `draftiq suggest --provider opgg --role top` call
  surfaced Warwick and Soraka as the top two suggestions -- unusual top-lane picks,
  ranked purely on shrunk win rate with no signal for how commonly a champion is
  actually played there. `score_candidate`'s 5 spec terms have no room for
  popularity, and it isn't one of them by design (see the spec doc) -- this is a
  search-layer tiebreaker added on top, the same architectural pattern as
  `search/ban.py`'s and `search/priority.py`'s own pick-rate weighting, not a
  change to `score_candidate` itself.

- **This is a genuinely different signal from shrinkage's `k`, not a duplicate of
  it.** `pick_rate` is a champion's share of a *huge, stable* bracket-wide
  denominator (OP.GG's `total_games` is often in the millions) -- a champion can be
  rarely picked yet still have a large, reliable `games` sample for the games it
  *did* get (confirmed live: Warwick top had 129,874 games, a large sample by any
  standard, at only a 6% pick rate). Shrinkage only discounts small samples; it has
  no way to express "this performed well in the games it got, but few players
  actually choose it."

- **Calibration false start, corrected before shipping.** The first version used a
  flat `POPULARITY_WEIGHT_SCALE * pick_rate` (scale 0.03, chosen to be safely small
  against the manual dataset's deliberately extreme pick-rate spread, 0.5% to 42%,
  used elsewhere to test shrinkage's small-sample behavior). Verified live: real
  OP.GG pick rates for a single role cluster tightly (4-8% across an entire top lane
  pool in this test), so the same scale that was safe against the manual dataset's
  spread produced a bonus of ~0.001-0.002 against live data -- a rounding error,
  nowhere near enough to compete with even a 1-2 percentage point win-rate gap. A
  single fixed linear scale cannot be simultaneously small enough for one dataset's
  spread and large enough for the other's. Fixed by normalizing against the pool's
  own max: `POPULARITY_WEIGHT_SCALE * (pick_rate / max_pick_rate_among_legal_ids)`
  -- the single most-picked legal champion in a given role/rank query always gets
  the full `POPULARITY_WEIGHT_SCALE` (0.02, on the same scale as
  `stats/composition.py`'s fit penalties), regardless of whether the underlying
  pick-rate distribution happens to be wide or narrow. Re-verified live after the
  fix: `popularity` bonus values moved from ~0.001-0.002 to ~0.003-0.012, a real
  tiebreaker now, without needing a second live data point to recalibrate.

- **Live re-verification also showed the original "problem" was smaller than it
  first looked.** Once actually checked, Warwick's 52% win rate and Senna's 52%
  win rate at a 12% pick rate (more popular than Darius or Garen's 8%) turned out to
  be genuinely strong, reasonably mainstream picks in the current patch's live
  data -- not statistical noise from an obscure pick. Aatrox/Darius/Garen were
  hovering at 50-51% in the same query. The popularity term is working as intended
  either way (it's a tiebreaker among close win rates, not an override), but this is
  a reminder that "feels off-meta" isn't the same as "is a small-sample fluke" --
  always check live numbers before assuming which one it is.

- **Only `greedy.suggest` (and therefore `search/lookahead.py`, which wraps it) has
  this term.** `search/ban.py` and `search/priority.py` keep their own
  independently-calibrated pick-rate weighting (denial value / contest risk are
  different questions from "is this a trustworthy suggestion"), and
  `score_candidate` itself remains untouched -- still exactly the spec's 5 terms.

## Phase 3: champion pool weighting (read before touching `pool`/`roster`
commands, `models.ChampionPool`/`TeamRoster`/`consolidated_pool_ids`, or any
`pool_ids`/`pool_ids_by_role` parameter in `search/`)

- **Scope grew well past the spec during design.** The spec says "per-player
  champion pool weighting (recommendations restricted to champions the user
  actually plays)" -- singular user, singular pool. Two rounds of user
  clarification expanded this to: named pools for *any* player (teammates in a
  premade lobby, enemy players in a Clash draft), and a way to import a pool from
  a real OP.GG summoner. Both are real, confirmed requirements, not scope creep on
  my part -- documented here so the size of what got built doesn't look
  unexplained next to the one-line spec bullet.

- **No player-to-role assignment, by explicit design.** The obvious-seeming design
  (assign "this teammate plays top") was proposed and rejected: pick order/priority
  means who ends up in which slot isn't knowable in advance, especially for the
  enemy team. Instead, `models.TeamRoster` tracks only **team membership** --
  `ally`/`enemy` lists of player names, no roles -- and every suggestion
  **unions** all of that side's players' pools for whatever role is actually
  relevant at the moment (`models.consolidated_pool_ids`), recomputed fresh on
  every `suggest` call rather than fixed at roster-setup time.

- **Pools and rosters live in different files with different lifetimes, on
  purpose.** `ChampionPool`s are reusable across drafts (a teammate's champion
  pool doesn't change between games), keyed by player name in a registry
  (`.draftiq/pools.json`, `persistence.load_pool_registry`/`save_pool_registry`).
  `TeamRoster` is draft-specific (who's actually in *this* game changes every
  draft) and lives inside `DraftState` itself, resetting automatically on every
  `draftiq new`.

- **Storage is by champion name, never `champion_id`.** `ManualCSVProvider` uses a
  local synthetic numbering; `OpggProvider` uses real Data Dragon ids. Only names
  are stable across both, so a pool set up under one provider still resolves
  correctly after switching -- confirmed live: set a pool while on `manual`, then
  started an `opgg` draft and had `suggest --pool` correctly restrict to the same
  champion by name, now under OP.GG's real ids (129874 -> 241008-scale numbers in
  the `n games` column instead of the manual dataset's thousands -- unmistakably
  real data, not a coincidence of small numbers matching).

- **`ChampionPool.resolve_ids`/`consolidated_pool_ids` return `set[int] | None`,
  and the two are never interchangeable.** `None` means "no pool data at all for
  this role -- don't restrict or bonus." An empty `set()` means "pool data
  exists, but nothing in it resolves against this provider's roster -- restrict
  to nothing." Collapsing this distinction anywhere (e.g. treating a missing role
  as an empty set) would silently turn "you haven't set this up yet" into "you
  play nothing here," which is wrong. Every `search/*` function that consumes a
  resolved pool preserves this distinction explicitly; `tests/test_pool.py` has a
  dedicated case for each side of it.

- **`search/greedy.py`'s `legal_ids` vs `candidate_ids` split is the one place
  this bites for real.** `legal_ids` (the true undrafted roster) is also passed
  into every `score_candidate` call as `remaining_enemy_ids`, which feeds
  `stats/exposure.py`'s "what real counter could the enemy still draft"
  calculation -- a question about the whole board, never about my own pool.
  Naively reassigning `legal_ids` to the pool-restricted set (the obvious first
  attempt) would silently make counterpick exposure ask "could something in my
  own pool counter this" instead -- recommendations keep returning, scores look
  plausible, and a real off-pool counter the enemy could still draft just never
  gets flagged. Fixed with a second variable (`candidate_ids`) that drives the
  scoring loop and the popularity term, while `legal_ids` keeps feeding
  `prefetch_for_suggest` and `remaining_enemy_ids` unrestricted. There's a
  dedicated regression test for this in `tests/test_scoring.py`
  (`test_pool_restriction_preserves_exposure_to_a_real_off_pool_counter`) --
  this is exactly the kind of bug that returns a plausible-looking wrong answer
  rather than an error, so it needs a test that would actually have caught it,
  not just a smoke test.

- **Picks restrict; bans only bonus/highlight -- confirmed, not symmetric by
  accident.** For a pick (or `--any-role`), `--pool` restricts the candidate set
  to the union of the *ally* roster's pools -- matches the spec's literal
  "restricted to." For a ban, `--pool` instead adds a bonus/highlight
  (`search/ban.py`'s `POOL_BONUS = 0.05`, labeled `"in enemy pool"`) for
  candidates in the union of the *enemy* roster's pools, but the full ban list is
  **never** narrowed -- confirmed explicitly: *"bonus/highlight only... you're
  never prevented from seeing... any other ban."* This reversed an earlier,
  simpler design (`--pool` rejected outright during bans) once named enemy pools
  made "deny something a specific enemy player actually plays" a real,
  higher-value signal than generic pick-rate popularity.

- **OP.GG has no role/position data for a summoner's champion history --
  confirmed live, not assumed.** `providers/opgg.py`'s `get_summoner_champion_pool`
  (`lol_get_summoner_profile`'s `most_champions.champion_stats`) returns
  `champion_name`/`play`/`win`/`id` per champion, sorted by play count -- nothing
  else. A live check against a real summoner (`Faker#KR1`) showed their top 5 by
  play count spanning mid/jungle/top, exactly as a real player's history would.
  `draftiq pool import-opgg <player> <role> <riot_id>` therefore always requires
  an explicit `role` from the caller -- there's no data-driven way to infer it.

- **`GET /api/pool/champions` exists specifically so the web pool panel works
  before any draft has started** -- `GET /api/champions` requires an active draft
  (a locked-in, tested contract) but setting up your pool before ever clicking
  "New Draft" is the natural first use of this feature. Backed by
  `persistence.get_active_or_default_provider()`, the same bootstrap-to-Manual
  fallback the CLI's `pool` commands use.

## Phase 3: matchup tips (read before touching `draftiq tips`,
`OpggProvider.get_lane_matchup_guide`, or `models.LaneMatchupGuide`)

- **Descoped from the spec's literal "LLM-generated tips," confirmed with the
  user.** `lol_get_lane_matchup_guide` already returns real prose tips and
  qualitative indicators directly from OP.GG -- no LLM call needed to get text,
  and the user confirmed surfacing that prose as-is satisfies the need. No new
  dependency, no API key, no per-call cost.

- **This is the one tool `OpggProvider` calls that returns plain JSON directly,
  confirmed live.** Every other tool this provider needs requires
  `desired_output_fields` and returns OP.GG's bespoke compact text format (see
  the OP.GG schema notes below); `lol_get_lane_matchup_guide` takes no such
  parameter at all and its response is real, parseable JSON -- `get_lane_matchup_
  guide` uses `json.loads` directly, not `opgg_format.parse`. It also takes no
  rank/tier parameter (not in its input schema), so results aren't
  bracket-specific the way every other stats call in this provider is.

- **Champion names for this tool need genuine `UPPER_SNAKE_CASE`, confirmed
  live -- a stricter format than every other tool in this provider.**
  `_opgg_champion_param` (`Champion.ddragon_id.upper()`) is deliberately *not*
  reused here: passing `"MISSFORTUNE"` (which the stats tools tolerate fine) is
  rejected outright by `lol_get_lane_matchup_guide` with "Invalid position or
  champion specified." A dedicated `_lane_guide_champion_param` derives the
  parameter from `Champion.name` instead: strip apostrophes and periods
  entirely (not replace with underscore), replace spaces with underscores, then
  uppercase. Confirmed live: `"Miss Fortune"` -> `"MISS_FORTUNE"`, `"Kai'Sa"` ->
  `"KAISA"` (not `"KAI_SA"`, which fails), `"Dr. Mundo"` -> `"DR_MUNDO"`,
  `"Vel'Koz"` -> `"VELKOZ"`.

- **Only one tip field exists (`opponent_champion_tip`), not a symmetric pair.**
  It's specifically about playing *against* the opponent (positioning around
  their kit, item timings to survive their power spikes) -- there's no
  equivalent "how to play my_champion" tip. `LaneMatchupGuide.tip` is named and
  documented accordingly; don't assume a "my_champion_tip" field will ever
  appear without checking live first.

- **`draftiq tips` and `GET /api/tips` always use a fresh `OpggProvider()`
  directly, regardless of the active draft's provider (if any) -- the same
  pattern as `pool import-opgg`.** No equivalent data exists in the offline
  manual dataset, and neither command requires an active draft to exist at all
  (matches `pool add`'s "usable standalone" precedent). `GET /api/tips/champions`
  exists for the same reason `GET /api/pool/champions` does: `/api/champions`
  resolves through the *active draft's* provider, which could be
  `ManualCSVProvider` (a different id space entirely) -- the tips panel needs
  ids that will actually resolve against live OP.GG.

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
  `summary.positions[].stats` (what `get_champion_stats` needs -- see the next
  bullet for why it's `positions[]` and not `average_stats`) only ever exposes
  `win_rate` (rounded to ~2 decimals when fetched via `desired_output_fields`) and
  `play`. `get_champion_stats` reconstructs `wins = round(win_rate * play)`, with up
  to roughly ±0.25% relative error on high-sample champions. This is a deliberate,
  documented tradeoff (confirmed with the user), not an oversight.

- **A real correctness bug, caught from a live user report: `get_champion_stats`
  originally read `summary.average_stats`, which is NOT role-specific.** It's a
  champion-wide aggregate across every position that champion has ever been
  played in, and the `position` request parameter has zero effect on it --
  confirmed live: a Warwick query returned the exact same 461,688-game, 52%-win
  numbers whether `position` was `"top"`, `"jungle"`, `"bottom"`, or `"support"`.
  Symptom in practice: `draftiq suggest --role bottom`/`--role support` kept
  surfacing the same high-overall-sample champions (Warwick, Mordekaiser, Olaf)
  that were legitimately strong *top-lane* suggestions, because every role was
  silently being scored with that champion's top-lane-dominated aggregate number
  instead of their real (often zero) games in that role. The correct per-role
  breakdown lives one level deeper, in `summary.positions[]` -- a list of
  `{name, stats: {play, win_rate, pick_rate, ban_rate, ...}}` entries, one per
  role the champion actually has recorded games in, confirmed to genuinely vary
  by role (unlike `average_stats`). No entry for a requested role (e.g. Warwick
  has no `"SUPPORT"` entry at all) means zero games there -- the same "no data"
  contract `get_matchup`/`get_synergy` already use, not an error. `_counters`
  (matchup data) and `get_build` were independently verified live to already
  vary correctly by `position`, so this bug was isolated to `get_champion_stats`
  alone. Verified fixed live, end-to-end through the web `suggest` endpoint:
  bottom-lane suggestions now surface Veigar/Seraphine/Swain/Miss Fortune instead
  of Warwick, with Warwick correctly shrunk to the role's population baseline
  (`n_games=0`) rather than ranking on borrowed top-lane data.

- **Matchup coverage is sparse by design.** `weak_counters`/`strong_counters` is a
  small curated top-~3-per-side list per champion/role, not a full pairwise matchup
  matrix. Most candidate-vs-enemy pairs during a real draft will legitimately get
  `games=0` back from `get_matchup` -- this is the "no data available" case the
  protocol is designed to express, not a bug. `lol_get_lane_matchup_guide` (the
  arbitrary-pair tool) has no raw win/game counts at all -- it returns prose tips, a
  qualitative lane-advantage indicator, and win-rate-by-game-length curves. That's the
  right source for Phase 3's tips module (`get_lane_matchup_guide`, wired up as
  `draftiq tips`), not for `get_matchup` -- see "Phase 3: matchup tips" above for the
  full writeup, including why this tool needs a different champion-param format from
  every other one (next bullet).

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
  OP.GG key. **`lol_get_lane_matchup_guide` is the one exception** -- it rejects the
  tolerant `"MISSFORTUNE"` format outright and needs genuine `UPPER_SNAKE_CASE`
  derived from the display name instead (`_lane_guide_champion_param`, not
  `_opgg_champion_param`); see "Phase 3: matchup tips" above.

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

- **`get_champions()` returns champions sorted alphabetically by name, for both
  `OpggProvider` and `ManualCSVProvider`** -- prompted by a live usability report:
  OP.GG's `lol_list_champions` returns its own internal ordering (previously
  re-sorted by `champion_id`, i.e. Data Dragon's numeric key, which isn't
  meaningful to a human either), so the web champion picker and the build/tips/
  pool selects were showing champions in an arbitrary order with no way to
  quickly scan for one by name. Every consumer of the full roster list is a
  selection UI, never the ranked suggestion tables (`search/*` always sorts its
  *own* output by score, independent of the order champions were handed to it
  in), so this was safe to change unconditionally rather than needing a
  per-caller opt-in.

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

- **`prefetch_for_suggest`'s thread pool was raised from 16 to 64 workers**, prompted
  by a live-usage report: a real draft only gives roughly a minute per pick/ban, and a
  cold web-UI suggestion at 16 workers measured 45-86s for a single role/rank query in
  practice (see the "web UI suggestions" bullet below) -- close enough to the clock to
  be a real problem, not just a nice-to-have. These are pure I/O-bound HTTP calls (the
  GIL is released while waiting on the network), so more worker threads is a genuine
  speedup rather than pointless contention -- confirmed live with a fully cleared
  on-disk cache (`~/.cache/draftiq/cache.sqlite3`) *and* a freshly started process each
  time (an already-running process keeps its open sqlite connection alive even after
  the file is deleted out from under it on most filesystems, which silently produced a
  false "already fast" reading the first time this was checked): the same cold query
  dropped from ~45-86s to ~29s for both a top and a jungle role. `OpggProvider.__init__`
  now also passes the underlying `httpx.Client` an explicit `httpx.Limits(max_connections
  =96, max_keepalive_connections=64)` -- httpx's default is `max_connections=100`
  (comfortable headroom over 64 already, so this wasn't strictly required to fix the
  bug), but leaving it implicit would make a future worker-count bump silently
  bottleneck on the connection pool instead of doing what its name says. 64 was chosen
  for headroom under that cap, not because OP.GG documents a rate limit -- it doesn't;
  if OP.GG starts throttling or erroring under this load in practice, lower this before
  raising the connection limit further.

- **The web UI's "suggestions show the wrong role" bug was a frontend race, not a
  backend one -- confirmed live before touching any code.** Switching the role dropdown
  fires a new `/api/draft/suggest` fetch without waiting for the previous one; against
  OP.GG's variable, network-bound latency, an older/slower request could resolve after
  a newer/faster one and silently overwrite the table with the previous role's results
  (e.g. top-lane picks still showing after switching to jungle). Direct API checks
  during the same investigation confirmed the backend itself always returned correct,
  role-scoped results -- this ruled out `search/dispatch.py`/`greedy.py` before any
  fix was written. `web/static/app.js`'s `refreshSuggestions` now tags every call with
  a monotonic `suggestRequestId` and only renders a response if it's still the most
  recent one in flight (aborting the stale fetch via `AbortController` is a courtesy,
  not what makes this correct); it also shows a full-width "Loading suggestions..."
  row while a fetch against OP.GG is outstanding, since even after the 64-worker fix
  above a cold query is still ~29s, not instant.

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

- **`suggest` dispatches on the current action type, not a flag.** `--role` is
  required for picks (roles are assigned before champion select, per the spec, so
  it's just "which role am I drafting for") but optional and unused for bans, since
  bans aren't role-locked. Picks go through `search/greedy.py` (or
  `search/lookahead.py` with `--lookahead`); bans go through `search/ban.py`, which
  asks a genuinely different question -- see `search/ban.py`'s docstring for how it
  reuses `score_candidate` with the sides swapped to get "how good would this be
  for the opponent" without a second scoring formula.

- **`draftiq build` is a thin renderer, not new logic.** Both providers already
  implement `get_build` (Phase 1 for `ManualCSVProvider`, Phase 2 for
  `OpggProvider`) -- the command just resolves champion names, calls it, and prints
  the result. `NotImplementedError`/`KeyError` from the provider (no build data for
  that champion/role) are caught and shown as a clean CLI error rather than a
  traceback. `--opponent` is accepted per the `StatsProvider.get_build` signature
  but currently only ever ignored: neither provider has opponent-specific build
  data (`OpggProvider`'s reason is documented in `providers/opgg.py`).

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

- **2-ply lookahead has to guess which role the opponent's next pick targets.**
  SOLOQ doesn't pre-assign roles to pick slots (role is only known at `draftiq pick
  --role` time), so `search/lookahead.py` can't simulate a single deterministic
  "opponent's next pick." Ply 2 instead checks the opponent's best response across
  *each* of their still-unfilled roles and uses the strongest one -- a proxy for
  "what would a rational opponent value most right now," not a prediction of what
  they'll actually pick. It's `lookahead_width` nested `greedy.suggest()` calls, so
  it's deliberately opt-in (`--lookahead`) rather than the CLI's default path --
  real added latency, especially against a network-bound provider like OP.GG.

- **TOURNAMENT's ban phase 2 happens after pick phase 1**, unlike SOLOQ where all
  bans come first. `DraftStateMachine` is already fully generic over the order
  table (it just asks "what's the next step," never assumes bans precede picks), so
  this needed no state-machine changes -- only `draft/rules.py:TOURNAMENT_ORDER`
  (ban1: 6, B/R alternating; pick1: 6, B/R/R/B/B/R; ban2: 4, R/B alternating --
  starts with red since red picked last in pick1; pick2: 4, R/B/B/R -- starts with
  red since blue banned last in ban2). `search/ban.py` handles this correctly by
  construction: it always uses whatever's actually picked on both sides (which is
  nothing during SOLOQ's single ban phase, and 6 champions by tournament's ban
  phase 2) as the matchup/synergy inputs, no phase-specific branching needed.

- **Cache key includes the patch string** (`providers/cache.py`), so a patch bump is
  a natural cache miss rather than needing an explicit invalidation pass.
  `SQLiteCache.prune_stale_patch` exists purely so the DB doesn't grow unbounded
  across patches over time -- nothing calls it automatically yet.

## Approved dependencies

`httpx`, `pydantic` (v2), `typer`, `rich`, `fastapi`, `uvicorn` (base package, not
`uvicorn[standard]` -- see the Phase 3 section above for why: no websockets, no
`--reload`, no `.env` loading, so none of the `[standard]` extras are needed), stdlib
`sqlite3`/`threading`/`concurrent.futures`/`tomllib`, `pytest`, `mypy`, `ruff`. Nothing
else without asking first (see the spec doc). Notably: no MCP SDK dependency was
needed for the OP.GG provider -- `httpx` alone is enough, since every response
observed from the live server is plain JSON over HTTP POST (never SSE). No `pyyaml`
either -- see the composition-features bullet above. No `jinja2`/frontend framework
either -- the web UI's frontend is static HTML/CSS/vanilla JS with zero build tooling,
see the Phase 3 section.
