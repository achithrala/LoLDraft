# Build Prompt: League of Legends Draft Optimizer

> Paste this into Claude Code at the root of an empty repo. It is written to be executed in
> phases — Claude Code should stop at the end of Phase 1 and wait for review.

---

## Objective

Build a Python tool that assists with League of Legends champion select. The user tells it what
has been picked and banned so far; it returns ranked recommendations for the next pick or ban,
each with a confidence-aware win-rate estimate, matchup tips, and a recommended build.

Correctness of the *statistics* matters more than breadth of features. A tool that recommends
three champions with honest, well-calibrated numbers is far more valuable than one that
recommends thirty with noisy ones.

## Non-goals (do not build these unless I explicitly ask)

- No web UI, no Electron overlay, no Discord bot in Phase 1.
- No automated reading of the game client, no memory reading, no input automation. The user
  types the draft state in manually.
- No user accounts, no auth, no multi-user server.
- No ML model training. The scoring function is explicit and interpretable by design.
- Do not add dependencies beyond the approved list without asking me first.

## Stack

- Python 3.11+
- `uv` for dependency and venv management
- `httpx` for HTTP, `pydantic` v2 for models, `typer` for CLI, `rich` for terminal output
- SQLite (stdlib `sqlite3`) for the response cache — no ORM
- `pytest` for tests
- Full type hints, checked with `mypy --strict`. `ruff` for lint and format.

---

## Architecture

```
src/draftiq/
  __init__.py
  models.py           # Champion, DraftState, Recommendation, ChampionStats, Matchup, Synergy
  providers/
    base.py           # StatsProvider protocol — the key abstraction
    opgg.py            # OP.GG MCP provider (primary)
    ddragon.py          # Data Dragon — static champion metadata, no API key needed
    cache.py           # SQLite-backed caching decorator with TTL
  stats/
    shrinkage.py      # empirical Bayes + credible intervals
    scoring.py         # the value function
    composition.py     # team comp feature vectors and fit scoring
  draft/
    state.py           # draft state machine
    rules.py            # SOLOQ and TOURNAMENT pick/ban orders
  search/
    greedy.py           # Phase 1
    lookahead.py         # Phase 2
  content/
    tips.py              # matchup tip generation + cache
  cli.py
tests/
```

### The provider abstraction (build this first, get it right)

Everything else depends on this. Define in `providers/base.py`:

```python
class StatsProvider(Protocol):
    def get_patch(self) -> str: ...
    def get_champions(self) -> list[Champion]: ...
    def get_champion_stats(
        self, champion_id: int, role: Role, rank: RankBracket
    ) -> ChampionStats: ...          # wins, games, pick rate, ban rate
    def get_matchup(
        self, champion_id: int, opponent_id: int, role: Role, rank: RankBracket
    ) -> Matchup: ...                 # wins, games — RAW COUNTS, not percentages
    def get_synergy(
        self, champion_id: int, ally_id: int, rank: RankBracket
    ) -> Synergy: ...
    def get_build(
        self, champion_id: int, role: Role, rank: RankBracket,
        opponent_id: int | None = None,
    ) -> Build: ...                   # items, runes, skill order, summoners
```

**Critical:** every method returns raw `wins` and `games` counts, never a pre-computed
percentage. The shrinkage layer needs sample sizes. If a source only exposes percentages,
the provider must also obtain the sample size or explicitly return `games=0` so downstream
code knows it cannot trust the number.

Sources, in priority order:

1. **OP.GG MCP server** (primary) — official, at `https://mcp-api.op.gg/mcp`, Streamable HTTP
   transport. Relevant tools include `lol-champion-analysis` (win/pick/ban rates, recommended
   builds, skill order, and counter data under a `weakCounters` field),
   `lol-champion-positions-data` (win and pick rates by position), and
   `lol-champion-leader-board`. Most tools take a `desired_output_fields` parameter to trim
   payload size — use it. **Probe the actual schema at build time and write the models to
   match what comes back; do not trust the field names in this document.** If a field I've
   named doesn't exist, tell me rather than silently guessing.
2. **Data Dragon** (`https://ddragon.leagueoflegends.com`) — champion list, IDs, tags, ability
   text, current patch version. No key required. Use this as the canonical champion registry
   and map every other source's names onto these IDs.
3. Leave a stub `providers/manual.py` that loads a local CSV, so the tool is testable and
   demoable with zero network access.

Cache every provider response in SQLite keyed on `(source, method, args, patch)` with a TTL —
default 24h, but invalidate immediately whenever the detected patch changes. Draft select gives
the user roughly 30 seconds, so a warm cache is a hard requirement, not an optimization.

---

## The scoring model

Implement exactly this. Do not substitute a simpler heuristic.

### 1. Shrunk base rate

For a champion in a role with `w` wins over `n` games, and `p0` the average win rate across all
champions in that role (compute it; don't hardcode 0.50):

```
p_hat = (w + k * p0) / (n + k)          # k = 300 pseudo-games, configurable
```

Report uncertainty from the Beta posterior `Beta(w + k*p0, n - w + k*(1 - p0))`, exposing the
90% credible interval on every recommendation.

### 2. Matchup and synergy deltas

Never use a matchup win rate as an absolute. Convert it to a delta and shrink it toward zero:

```
d_raw   = matchup_wr(A vs B) - p_hat(A)
d_shrunk = d_raw * n_matchup / (n_matchup + k_m)     # k_m = 150, configurable
```

Same treatment for synergy pairs. Small samples collapse to zero influence automatically,
which is the behaviour we want.

### 3. Composition fit

Give every champion a feature vector — AD/AP/true damage share, engage, disengage, poke,
waveclear, frontline, and a scaling curve (early / mid / late). Derive what you can from Data
Dragon tags and stats; hand-curate the rest into a checked-in YAML file and mark clearly that
it is hand-curated.

Score the assembled team against soft targets and apply penalties for: damage skew worse than
roughly 70/30 in either direction, no frontline, no engage tool, no waveclear. Keep these
penalties small relative to the win-rate terms — they are tiebreakers, not the main signal.

### 4. Counterpick exposure

If the enemy still picks after us, a candidate that has severe counters remaining in the pool is
risky. For each enemy pick still to come:

```
exposure(A) = max over unpicked champions C of ( -d_shrunk(A vs C) )
```

Subtract this, weighted by how many enemy picks remain and by how likely those counters are to
actually be picked (weight by pick rate — nobody counterpicks with a 0.3% pick rate champion).

### 5. Total

```
score(A) = p_hat(A)
         + Σ d_shrunk(A vs each enemy champion)
         + Σ s_shrunk(A with each ally champion)
         + comp_fit(A, team state)
         - exposure(A)
```

Every recommendation must be able to explain itself — return the individual term contributions,
not just the total, and render them in the CLI output. If I can't see *why* a champion is
recommended, the tool has failed.

---

## Draft state machine

Support two modes:

- `SOLOQ` — 10 bans (alternating, 5 per side), then picks in order B1 / R1 R2 / B2 B3 / R3 R4 /
  B4 B5 / R5. Roles are assigned before champion select.
- `TOURNAMENT` — 6 bans, 6 picks, 4 bans, 4 picks in the standard competitive order.

The state machine must know whose turn it is, whether this turn is a ban or a pick, which
champions are still legal, and how many enemy picks remain after the current one (the exposure
term needs this). Validate rigorously — reject duplicate champions, out-of-order actions, and
picks for a role that is already filled.

---

## Tips and builds

Builds come straight from the provider: items, runes, skill order, summoner spells, ideally
matchup-specific where available.

Matchup tips use a hybrid approach:

1. Derive structured features from data — who outscales whom (win rate by game length), whether
   the lane is volatile (early kill/death rates), the wave-clear and range differential.
2. Render those features into prose. Start with a **template-based generator** in Phase 1 — no
   LLM. Templates are deterministic, testable, and instant.
3. Leave a clean seam for optional LLM-generated tips in Phase 3, cached on
   `(champ_a, champ_b, role, patch)`. **Never call an LLM synchronously during a live draft.**

---

## Phases

**Phase 1 — stop here and wait for my review.**

- Models, provider protocol, Data Dragon provider, manual CSV provider, SQLite cache
- Shrinkage module with tests
- Greedy scorer using base rate + matchup deltas + synergy deltas
- Draft state machine for SOLOQ
- CLI supporting: `draftiq new`, `draftiq ban <champ>`, `draftiq pick <champ> --side --role`,
  `draftiq suggest`, `draftiq state`
- Output shows top 5 candidates with score, credible interval, sample size, and the term-by-term
  breakdown

**Phase 2** — OP.GG MCP provider, composition fit, counterpick exposure, 2-ply lookahead,
tournament draft mode, ban recommendations, build display.

**Phase 3** — interactive TUI or local web UI, LLM-generated tips, per-player champion pool
weighting (recommendations restricted to champions the user actually plays).

---

## Working agreement

- Write tests as you go, not at the end. Shrinkage math and the draft state machine need
  thorough unit tests — those are where correctness bugs will hide and where they'll be
  invisible at runtime.
- Include at least one end-to-end test that runs a full draft against the manual CSV provider
  with no network access.
- Keep a `CLAUDE.md` at the repo root recording architecture decisions and any provider schema
  quirks you discover, so future sessions have context.
- Commit at meaningful checkpoints with clear messages.
- If a provider's real schema contradicts this spec, **stop and tell me** — don't paper over it.
- If you find yourself wanting a dependency not on the approved list, ask first.
- Prefer boring, readable code. This is a statistics tool; clarity beats cleverness.

Start with Phase 1. Ask me any clarifying questions before you begin writing code.
