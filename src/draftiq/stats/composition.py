"""Team composition feature vectors and fit scoring.

Every champion gets a small feature vector -- damage-type share, engage, disengage,
poke, waveclear, frontline, and an early/mid/late scaling window -- used only as a
soft tiebreaker on top of the win-rate terms (per spec), never as a primary signal:
penalties here are kept small relative to `base_rate`/matchup/synergy deltas.

Where a champion is hand-curated in `data/composition_features.toml`, that entry is
authoritative. The spec suggested a checked-in YAML file for this; `pyyaml` isn't on
the approved dependency list, so this uses TOML via the standard library's
`tomllib` (read-only, Python 3.11+) instead -- the same "small, human-editable,
checked-in" goal, no new dependency. For any champion not in the table (most of
OP.GG's ~170-champion roster), a crude Data Dragon tag-based heuristic fills in --
tag-based AD/AP heuristics are known to be wrong for plenty of real champions (Kayle,
Gwen, Rumble all defy their primary tag), so this is explicitly a fallback, not a
substitute for hand-curating more entries.

The table is keyed by `Champion.ddragon_id` (a real, provider-independent Data
Dragon string id), NOT `champion_id` -- a real bug caught from a live user report:
numeric `champion_id` collides across providers, since `ManualCSVProvider`'s
synthetic 1-20 ids overlap with real Data Dragon/OP.GG ids 1-20 (which name a
completely different 20 champions). Keying by id used to let, e.g., real OP.GG
Warwick (id 19) silently inherit the manual dataset's id-19 entry (Renekton) --
letting a champion with zero recorded games in a role dodge composition penalties
a real, unrelated champion's kit happened not to have. See
`data/composition_features.toml`'s own header for the full writeup.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from draftiq.models import Champion, TermContribution

DEFAULT_FEATURES_PATH = Path(__file__).resolve().parents[3] / "data" / "composition_features.toml"

DAMAGE_SKEW_THRESHOLD = 0.70
DAMAGE_SKEW_PENALTY = 0.02
NO_FRONTLINE_PENALTY = 0.02
NO_ENGAGE_PENALTY = 0.015
NO_WAVECLEAR_PENALTY = 0.01

Scaling = Literal["early", "mid", "late"]


class CompositionFeatures(BaseModel):
    ad_share: float
    ap_share: float
    true_share: float
    engage: bool
    disengage: bool
    poke: bool
    waveclear: bool
    frontline: bool
    scaling: Scaling


# Coarse fallback per Data Dragon tag, used only when a champion has no hand-curated
# entry. Data Dragon lists a champion's tags with the primary one first, so
# `features_from_tags` just uses the first tag it recognizes.
_TAG_HEURISTIC: dict[str, CompositionFeatures] = {
    "Marksman": CompositionFeatures(
        ad_share=1.0,
        ap_share=0.0,
        true_share=0.0,
        engage=False,
        disengage=False,
        poke=True,
        waveclear=True,
        frontline=False,
        scaling="late",
    ),
    "Assassin": CompositionFeatures(
        ad_share=0.7,
        ap_share=0.3,
        true_share=0.0,
        engage=True,
        disengage=False,
        poke=False,
        waveclear=False,
        frontline=False,
        scaling="mid",
    ),
    "Fighter": CompositionFeatures(
        ad_share=0.8,
        ap_share=0.2,
        true_share=0.0,
        engage=True,
        disengage=False,
        poke=False,
        waveclear=True,
        frontline=True,
        scaling="mid",
    ),
    "Tank": CompositionFeatures(
        ad_share=0.3,
        ap_share=0.7,
        true_share=0.0,
        engage=True,
        disengage=False,
        poke=False,
        waveclear=False,
        frontline=True,
        scaling="mid",
    ),
    "Mage": CompositionFeatures(
        ad_share=0.0,
        ap_share=1.0,
        true_share=0.0,
        engage=False,
        disengage=False,
        poke=True,
        waveclear=True,
        frontline=False,
        scaling="mid",
    ),
    "Support": CompositionFeatures(
        ad_share=0.2,
        ap_share=0.8,
        true_share=0.0,
        engage=False,
        disengage=True,
        poke=False,
        waveclear=False,
        frontline=False,
        scaling="mid",
    ),
}

_UNKNOWN_CHAMPION_FEATURES = CompositionFeatures(
    ad_share=0.5,
    ap_share=0.5,
    true_share=0.0,
    engage=False,
    disengage=False,
    poke=False,
    waveclear=False,
    frontline=False,
    scaling="mid",
)


@lru_cache(maxsize=1)
def load_hand_curated_features(
    path: Path = DEFAULT_FEATURES_PATH,
) -> dict[str, CompositionFeatures]:
    """Loads and caches `data/composition_features.toml`, keyed by `ddragon_id`
    (NOT `champion_id` -- see the file's own header comment for why: numeric
    champion_id collides across providers, since ManualCSVProvider's synthetic
    1-20 ids overlap with real Data Dragon/OP.GG ids 1-20, which name a completely
    different set of champions). Cached because it's read on every `suggest()`
    call -- the file never changes at runtime."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return {ddragon_id: CompositionFeatures(**entry) for ddragon_id, entry in raw.items()}


def features_from_tags(tags: list[str]) -> CompositionFeatures:
    for tag in tags:
        if tag in _TAG_HEURISTIC:
            return _TAG_HEURISTIC[tag]
    return _UNKNOWN_CHAMPION_FEATURES


def get_champion_features(
    champion: Champion, hand_curated: dict[str, CompositionFeatures]
) -> CompositionFeatures:
    if champion.ddragon_id in hand_curated:
        return hand_curated[champion.ddragon_id]
    return features_from_tags(champion.tags)


def comp_fit(
    candidate: CompositionFeatures, ally_features: list[CompositionFeatures]
) -> tuple[float, list[TermContribution]]:
    """Scores the team assembled by adding `candidate` to `ally_features` against
    soft targets: damage skew, frontline, engage, waveclear. Returns the total
    penalty (<= 0) and the individual term contributions that produced it."""
    team = [*ally_features, candidate]
    terms: list[TermContribution] = []
    total = 0.0

    total_ad = sum(f.ad_share for f in team)
    total_ap = sum(f.ap_share for f in team)
    total_damage = total_ad + total_ap
    if total_damage > 0:
        ad_fraction = total_ad / total_damage
        if ad_fraction > DAMAGE_SKEW_THRESHOLD or ad_fraction < (1.0 - DAMAGE_SKEW_THRESHOLD):
            terms.append(TermContribution(label="damage_skew", value=-DAMAGE_SKEW_PENALTY))
            total -= DAMAGE_SKEW_PENALTY

    if not any(f.frontline for f in team):
        terms.append(TermContribution(label="no_frontline", value=-NO_FRONTLINE_PENALTY))
        total -= NO_FRONTLINE_PENALTY

    if not any(f.engage for f in team):
        terms.append(TermContribution(label="no_engage", value=-NO_ENGAGE_PENALTY))
        total -= NO_ENGAGE_PENALTY

    if not any(f.waveclear for f in team):
        terms.append(TermContribution(label="no_waveclear", value=-NO_WAVECLEAR_PENALTY))
        total -= NO_WAVECLEAR_PENALTY

    return total, terms
