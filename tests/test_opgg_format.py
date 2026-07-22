"""Tests pinned to real response strings captured live from the OP.GG MCP server
(https://mcp-api.op.gg/mcp) during Phase 2 development. If OP.GG ever changes this
format, these are the tests that should fail first and loudly.
"""

from __future__ import annotations

import pytest

from draftiq.providers.opgg_format import OpggFormatError, parse

# Captured from lol_get_champion_analysis(champion=AATROX, position=top, tier=all)
# with desired_output_fields selecting average_stats + weak/strong counters.
CHAMPION_ANALYSIS_SAMPLE = (
    "class LolGetChampionAnalysis: data\n"
    "class Data: summary,weak_counters,strong_counters\n"
    "class Summary: average_stats\n"
    "class AverageStats: ban_rate,kda,pick_rate,play,win_rate\n"
    "class WeakCounter: champion_name,counter_win_rate,my_win_rate,play,win,win_rate\n"
    "\n"
    "LolGetChampionAnalysis(Data(Summary(AverageStats(0.08,2.01,0.08,98383,0.5)),"
    '[WeakCounter("Singed",0.56,0.44,927,408,0.56),'
    'WeakCounter("Kayle",0.55,0.45,558,253,0.55),'
    'WeakCounter("Quinn",0.55,0.45,231,104,0.55)],'
    '[WeakCounter("Trundle",0.43,0.57,387,220,0.57),'
    'WeakCounter("Vladimir",0.43,0.57,331,188,0.57),'
    'WeakCounter("Ryze",0.45,0.55,163,90,0.55)]))'
)

# Captured from lol_get_champion_synergies(champion=JINX, my_position=adc,
# synergy_position=support).
SYNERGIES_SAMPLE = (
    "class LolGetChampionSynergies: data\n"
    "class Data: synergies\n"
    "class Synergie: synergy_champion_name,play,win,win_rate\n"
    "\n"
    'LolGetChampionSynergies(Data([Synergie("Thresh",2857,1539,0.54),'
    'Synergie("Lulu",2367,1185,0.5),'
    'Synergie("Nautilus",779,432,0.55)]))'
)

# Trimmed subset (3 of ~170 champions) of a real lol_list_champions capture --
# chosen to exercise an apostrophe inside a string literal (Cho'Gath).
LIST_CHAMPIONS_SAMPLE = (
    "class LolListChampions: data\n"
    "class Data: champions\n"
    "class Champion: champion_id,key,name\n"
    "\n"
    'LolListChampions(Data([Champion(1,"Annie","Annie"),'
    'Champion(4,"TwistedFate","Twisted Fate"),'
    'Champion(31,"Chogath","Cho\'Gath")]))'
)


class TestChampionAnalysisSample:
    def test_parses_nested_structure(self) -> None:
        result = parse(CHAMPION_ANALYSIS_SAMPLE)
        data = result["data"]
        assert data["summary"]["average_stats"] == {
            "ban_rate": 0.08,
            "kda": 2.01,
            "pick_rate": 0.08,
            "play": 98383,
            "win_rate": 0.5,
        }
        assert len(data["weak_counters"]) == 3
        assert len(data["strong_counters"]) == 3

    def test_shared_class_reused_with_its_own_declared_field_order(self) -> None:
        # weak_counters and strong_counters both instantiate WeakCounter, but the
        # positional values must be zipped against WeakCounter's *declared* order,
        # not whatever order a caller originally requested for either array.
        result = parse(CHAMPION_ANALYSIS_SAMPLE)
        singed = result["data"]["weak_counters"][0]
        assert singed == {
            "champion_name": "Singed",
            "counter_win_rate": 0.56,
            "my_win_rate": 0.44,
            "play": 927,
            "win": 408,
            "win_rate": 0.56,
        }
        # win/play recovers my_win_rate exactly, confirming `win` is a raw count.
        assert singed["win"] / singed["play"] == pytest.approx(singed["my_win_rate"], abs=0.005)

    def test_types_are_int_and_float_not_strings(self) -> None:
        result = parse(CHAMPION_ANALYSIS_SAMPLE)
        singed = result["data"]["weak_counters"][0]
        assert isinstance(singed["play"], int)
        assert isinstance(singed["win"], int)
        assert isinstance(singed["win_rate"], float)
        assert isinstance(singed["champion_name"], str)


class TestSynergiesSample:
    def test_parses_list_of_class_instances(self) -> None:
        result = parse(SYNERGIES_SAMPLE)
        synergies = result["data"]["synergies"]
        assert synergies == [
            {"synergy_champion_name": "Thresh", "play": 2857, "win": 1539, "win_rate": 0.54},
            {"synergy_champion_name": "Lulu", "play": 2367, "win": 1185, "win_rate": 0.5},
            {"synergy_champion_name": "Nautilus", "play": 779, "win": 432, "win_rate": 0.55},
        ]


class TestListChampionsSample:
    def test_handles_apostrophe_inside_string_literal(self) -> None:
        result = parse(LIST_CHAMPIONS_SAMPLE)
        champions = result["data"]["champions"]
        assert champions[2] == {"champion_id": 31, "key": "Chogath", "name": "Cho'Gath"}

    def test_integer_champion_id_not_parsed_as_float(self) -> None:
        result = parse(LIST_CHAMPIONS_SAMPLE)
        assert isinstance(result["data"]["champions"][0]["champion_id"], int)


class TestMalformedInput:
    def test_undeclared_class_raises(self) -> None:
        with pytest.raises(OpggFormatError, match="undeclared class"):
            parse("class Foo: a,b\n\nBar(1,2)")

    def test_field_count_mismatch_raises(self) -> None:
        with pytest.raises(OpggFormatError, match="declared 2 fields"):
            parse("class Foo: a,b\n\nFoo(1,2,3)")

    def test_unterminated_string_raises(self) -> None:
        with pytest.raises(OpggFormatError, match="unterminated string"):
            parse('class Foo: a\n\nFoo("unterminated')

    def test_trailing_garbage_raises(self) -> None:
        with pytest.raises(OpggFormatError, match="trailing data"):
            parse("class Foo: a\n\nFoo(1) garbage")

    def test_empty_list_parses_to_empty(self) -> None:
        result = parse("class Foo: items\n\nFoo([])")
        assert result == {"items": []}


class TestNoClassDeclarations:
    def test_bare_scalar_with_no_header(self) -> None:
        # Defensive case: not observed live, but the grammar allows a response with
        # no class declarations at all (e.g. a bare list or scalar).
        assert parse("[1, 2, 3]") == [1, 2, 3]

    def test_bare_string_with_no_header(self) -> None:
        assert parse('"hello"') == "hello"
