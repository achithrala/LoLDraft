# draftiq

League of Legends draft pick/ban recommendation tool. See
`lol-draft-tool-prompt.md` at the repo root for the original spec this was built
from (architecture, scoring formulas, and phase plan all live there in full).

## Status

Phase 1 complete: models, provider protocol, Data Dragon provider (champion registry
only), manual CSV provider, SQLite cache, shrinkage math, greedy scorer (base rate +
matchup + synergy deltas), SOLOQ draft state machine, CLI (`new`/`ban`/`pick`/
`suggest`/`state`).

Not yet built (Phase 2): OP.GG MCP provider, composition fit, counterpick exposure,
2-ply lookahead, TOURNAMENT draft mode, ban-specific recommendations, build display in
the CLI. Phase 3: TUI/web UI, LLM-generated tips, per-player champion pool weighting.

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
  keys -- there's no cross-provider id mapping in Phase 1 since only one provider is
  wired up. That mapping work (Data Dragon id <-> OP.GG id) is explicitly Phase 2.

- **All win/game numbers in `data/manual/*.csv` are fabricated** for demo and test
  purposes. `get_patch()` on `ManualCSVProvider` returns `"SYNTH-1"`, which is not a
  real League patch string, specifically so it can never be confused for real data.

- **CLI hard-codes `ManualCSVProvider`.** There's no `--provider` flag yet because
  there's nothing to switch to in Phase 1 (Data Dragon can't answer stats queries).
  Add the flag when the OP.GG provider lands in Phase 2.

- **Draft state persists to `.draftiq/state.json`** in the current directory. Each
  CLI command is a separate process, so this is the only way "whose turn is it"
  survives between `draftiq ban ...` and `draftiq pick ...` calls. `draftiq new`
  overwrites it unconditionally.

- **`suggest` always requires `--role`,** even during the ban phase. Roles are
  assigned before champion select (per the spec), so during picks this is just "which
  role am I drafting for." During bans there are no picks on the board yet in SOLOQ,
  so matchup/synergy terms are moot and it degenerates to ranking champions in that
  role by shrunk base rate -- a reasonable proxy for "worth denying," but not real
  ban-specific reasoning (weighted by what the opponent is likely to pick), which is
  Phase 2 (`counterpick exposure`, ban recommendations).

- **Beta credible intervals are computed without scipy.** The Beta distribution's
  quantile function is implemented from scratch in `stats/shrinkage.py`: the
  regularized incomplete beta function via Lentz's continued-fraction method
  (Numerical Recipes), inverted by bisection. Only the standard library is used, per
  the approved dependency list. Validated in `tests/test_shrinkage.py` against the
  closed-form Beta(1,1) = Uniform(0,1) case.

- **`score_candidate` (Phase 1) implements only 3 of the spec's 5 score terms:**
  base rate, matchup deltas, synergy deltas. Composition fit and counterpick exposure
  need a team composition feature vector and a full pick pool, both Phase 2 work --
  adding placeholder zero terms for them now would just be dead code.

- **Cache key includes the patch string** (`providers/cache.py`), so a patch bump is
  a natural cache miss rather than needing an explicit invalidation pass.
  `SQLiteCache.prune_stale_patch` exists purely so the DB doesn't grow unbounded
  across patches over time -- nothing calls it automatically yet.

## Approved dependencies

`httpx`, `pydantic` (v2), `typer`, `rich`, stdlib `sqlite3`, `pytest`, `mypy`, `ruff`.
Nothing else without asking first (see the spec doc).
