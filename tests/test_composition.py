from __future__ import annotations

import pytest

from draftiq.models import Champion
from draftiq.providers.manual import ManualCSVProvider
from draftiq.stats.composition import (
    CompositionFeatures,
    comp_fit,
    features_from_tags,
    get_champion_features,
    load_hand_curated_features,
)


@pytest.fixture(scope="module")
def hand_curated() -> dict[str, CompositionFeatures]:
    return load_hand_curated_features()


@pytest.fixture(scope="module")
def champion_by_ddragon_id() -> dict[str, Champion]:
    return {c.ddragon_id: c for c in ManualCSVProvider().get_champions()}


class TestLoadHandCuratedFeatures:
    def test_covers_every_manual_csv_champion(
        self,
        hand_curated: dict[str, CompositionFeatures],
        champion_by_ddragon_id: dict[str, Champion],
    ) -> None:
        # The whole point of hand-curating is that our own demo roster is fully
        # covered, not falling back to the crude tag heuristic.
        assert set(champion_by_ddragon_id) <= set(hand_curated)

    def test_damage_shares_are_plausible(
        self, hand_curated: dict[str, CompositionFeatures]
    ) -> None:
        for features in hand_curated.values():
            total = features.ad_share + features.ap_share + features.true_share
            assert total == pytest.approx(1.0, abs=0.01)

    def test_cached_across_calls(self) -> None:
        assert load_hand_curated_features() is load_hand_curated_features()


class TestFeaturesFromTags:
    def test_uses_first_recognized_tag(self) -> None:
        features = features_from_tags(["Marksman", "Mage"])
        assert features.poke is True
        assert features.ad_share == 1.0

    def test_unknown_tag_falls_back_to_default(self) -> None:
        features = features_from_tags(["SomeBrandNewTagOpggInvents"])
        assert features.ad_share == pytest.approx(0.5)
        assert features.ap_share == pytest.approx(0.5)

    def test_empty_tags_falls_back_to_default(self) -> None:
        assert features_from_tags([]).scaling == "mid"


class TestGetChampionFeatures:
    def test_prefers_hand_curated_over_tags(
        self,
        hand_curated: dict[str, CompositionFeatures],
        champion_by_ddragon_id: dict[str, Champion],
    ) -> None:
        # Malphite is tagged Tank;Fighter but hand-curated as AP (0.8), not the
        # tag heuristic's Tank value (0.7) or Fighter value (0.2).
        malphite = champion_by_ddragon_id["Malphite"]
        features = get_champion_features(malphite, hand_curated)
        assert features == hand_curated["Malphite"]
        assert features.ap_share == 0.8

    def test_falls_back_for_unlisted_champion(
        self, hand_curated: dict[str, CompositionFeatures]
    ) -> None:
        ghost_champion = Champion(
            champion_id=999999, name="Nobody", ddragon_id="Nobody", tags=["Mage"]
        )
        features = get_champion_features(ghost_champion, hand_curated)
        assert features == features_from_tags(["Mage"])

    def test_matches_by_ddragon_id_not_champion_id(
        self, hand_curated: dict[str, CompositionFeatures]
    ) -> None:
        # Regression test: a champion whose numeric champion_id collides with a
        # *different* manual-dataset champion's id (e.g. real OP.GG Warwick is
        # champion_id=19, same as manual-dataset id 19, which is Renekton) must
        # not silently inherit that unrelated champion's hand-curated entry.
        # Warwick has no ddragon_id entry in the table at all, so this must fall
        # through to the tag-based fallback, not Renekton's curated features.
        fake_opgg_warwick = Champion(champion_id=19, name="Warwick", ddragon_id="Warwick", tags=[])
        features = get_champion_features(fake_opgg_warwick, hand_curated)
        assert features == features_from_tags([])
        assert features != hand_curated["Renekton"]


class TestCompFit:
    def test_balanced_team_has_no_penalty(
        self, hand_curated: dict[str, CompositionFeatures]
    ) -> None:
        renekton = hand_curated["Renekton"]  # AD, engage, frontline, waveclear
        orianna = hand_curated["Orianna"]  # AP, engage, waveclear, no frontline
        total, terms = comp_fit(renekton, [orianna])
        assert total == 0.0
        assert terms == []

    def test_ap_only_team_penalized_for_damage_skew(
        self, hand_curated: dict[str, CompositionFeatures]
    ) -> None:
        malphite = hand_curated["Malphite"]  # ap_share=0.8 alone -> skewed AP
        total, terms = comp_fit(malphite, [])
        labels = {t.label for t in terms}
        assert "damage_skew" in labels
        assert total < 0.0

    def test_no_frontline_penalized(self, hand_curated: dict[str, CompositionFeatures]) -> None:
        zed = hand_curated["Zed"]  # no frontline
        ahri = hand_curated["Ahri"]  # no frontline
        total, terms = comp_fit(zed, [ahri])
        labels = {t.label for t in terms}
        assert "no_frontline" in labels
        assert total < 0.0

    def test_no_engage_penalized(self, hand_curated: dict[str, CompositionFeatures]) -> None:
        jinx = hand_curated["Jinx"]  # no engage
        caitlyn = hand_curated["Caitlyn"]  # no engage
        total, terms = comp_fit(jinx, [caitlyn])
        labels = {t.label for t in terms}
        assert "no_engage" in labels

    def test_no_waveclear_penalized(self, hand_curated: dict[str, CompositionFeatures]) -> None:
        lee_sin = hand_curated["LeeSin"]  # no waveclear
        total, terms = comp_fit(lee_sin, [])
        labels = {t.label for t in terms}
        assert "no_waveclear" in labels

    def test_penalties_are_small_relative_to_win_rate_scale(
        self, hand_curated: dict[str, CompositionFeatures]
    ) -> None:
        # Worst case: every soft target missed at once.
        worst = CompositionFeatures(
            ad_share=1.0,
            ap_share=0.0,
            true_share=0.0,
            engage=False,
            disengage=False,
            poke=False,
            waveclear=False,
            frontline=False,
            scaling="mid",
        )
        total, _ = comp_fit(worst, [])
        # Win rates live around 0.45-0.60; comp fit must stay a tiebreaker, not
        # something that can flip a ranking on its own.
        assert abs(total) < 0.10
