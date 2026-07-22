# draftiq

A League of Legends champion-select assistant. You tell it what's been picked and
banned so far; it returns ranked pick/ban recommendations with a confidence-aware
win-rate estimate and a term-by-term explanation of why each champion is recommended.

Phase 1 (current): SOLOQ drafts only, scored from a bundled synthetic dataset
(`data/manual/`) so the whole thing runs fully offline. See `CLAUDE.md` for
architecture notes and `lol-draft-tool-prompt.md` for the full spec and phase plan.

## Usage

```sh
uv run draftiq new                      # start a SOLOQ draft
uv run draftiq ban Yasuo                # ban/pick follow whoever's turn it is
uv run draftiq pick Aatrox --role top
uv run draftiq suggest --role top       # ranked recommendations, with explanations
uv run draftiq state                    # show the full draft so far
```

Draft state is saved to `.draftiq/state.json` in the current directory between runs.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format .
uv run mypy --strict
```
