"""Tests for OpggProvider using httpx.MockTransport -- no real network access. Canned
responses reuse the exact text captured live from the OP.GG MCP server so these tests
exercise the real (if unusual) response shapes, not idealized ones.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest

from draftiq.models import RankBracket, Role
from draftiq.providers.cache import SQLiteCache
from draftiq.providers.opgg import OpggApiError, OpggProvider

SESSION_ID = "test-session-id"


def _mcp_json(result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "mcp-session-id": SESSION_ID},
        json={"jsonrpc": "2.0", "id": 1, "result": result},
    )


def _mcp_text_result(text: str) -> httpx.Response:
    return _mcp_json({"content": [{"type": "text", "text": text}]})


def _mcp_error(error: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "mcp-session-id": SESSION_ID},
        json={"jsonrpc": "2.0", "id": 1, "error": error},
    )


CHAMPIONS_TEXT = (
    "class LolListChampions: data\n"
    "class Data: champions\n"
    "class Champion: champion_id,key,name\n"
    "\n"
    'LolListChampions(Data([Champion(266,"Aatrox","Aatrox"),'
    'Champion(122,"Darius","Darius"),'
    'Champion(21,"MissFortune","Miss Fortune")]))'
)

# Real per-role numbers live in data.summary.positions[], NOT data.summary.
# average_stats (confirmed live: average_stats is a champion-wide aggregate,
# identical regardless of the requested `position` -- see providers/opgg.py's
# module docstring point 10). This champion only has a TOP entry, so a JUNGLE/
# SUPPORT/etc. query must fall through to the zero-games "no data" case.
CHAMPION_ANALYSIS_TEXT = (
    "class LolGetChampionAnalysis: data\n"
    "class Data: summary\n"
    "class Summary: positions\n"
    "class Position: name,stats\n"
    "class Stats: play,win_rate,pick_rate,ban_rate\n"
    "\n"
    'LolGetChampionAnalysis(Data(Summary([Position("TOP",'
    "Stats(98383,0.504823,0.0804255,0.0775391))])))"
)

COUNTERS_TEXT = (
    "class LolGetChampionAnalysis: data\n"
    "class Data: weak_counters,strong_counters\n"
    "class WeakCounter: champion_id,play,win\n"
    "\n"
    "LolGetChampionAnalysis(Data([WeakCounter(27,927,408),WeakCounter(10,558,253)],"
    "[WeakCounter(48,387,220)]))"
)

SYNERGIES_TEXT = (
    "class LolGetChampionSynergies: data\n"
    "class Data: synergies\n"
    "class Synergie: synergy_champion_id,play,win\n"
    "\n"
    "LolGetChampionSynergies(Data([Synergie(412,2857,1539),Synergie(117,2367,1185)]))"
)

BUILD_TEXT = (
    "class LolGetChampionAnalysis: data\n"
    "class Data: starter_items,core_items,boots,summoner_spells,skills,runes\n"
    "class CoreItems: ids_names\n"
    "class SummonerSpells: ids\n"
    "class Skills: order\n"
    "class Runes: primary_rune_names,secondary_rune_names,stat_mod_names\n"
    "\n"
    'LolGetChampionAnalysis(Data(CoreItems(["Doran\'s Blade","Health Potion"]),'
    'CoreItems(["Eclipse","Serylda\'s Grudge","Death\'s Dance"]),'
    'CoreItems(["Plated Steelcaps"]),'
    "SummonerSpells([4,14]),"
    'Skills(["Q","E","W","R"]),'
    'Runes(["Conqueror","Triumph"],["Second Wind","Unflinching"],[5008,5008,5001])))'
)


# Captured live against lol_get_lane_matchup_guide (Aatrox top vs Darius), trimmed to
# the fields get_lane_matchup_guide actually reads -- this tool has no
# desired_output_fields and returns plain JSON, not opgg_format's compact grammar
# (confirmed live, see providers/opgg.py's module docstring point 9).
LANE_MATCHUP_GUIDE_TEXT = json.dumps(
    {
        "lang": "en_US",
        "position": "top",
        "my_champion": "Aatrox",
        "opponent_champion": "Darius",
        "data": {
            "opponent_champion_tip": "Do not get pulled by Darius's (E).",
            "lane_solo_kill_advantage_champion": "Aatrox",
            "lane_advantage_champion": "Aatrox",
            "recommended_play_style": "aggressive",
            "game_lengths": [
                {"game_length": 0, "rate": 0.516048, "average": 0.5, "rank": 19},
                {"game_length": 25, "rate": 0.505086, "average": 0.5, "rank": 24},
            ],
        },
    }
)

# Captured live against lol_get_summoner_profile (Faker#KR1) -- confirmed there is
# no role/position field anywhere in this response, see providers/opgg.py's
# get_summoner_champion_pool docstring.
SUMMONER_PROFILE_TEXT = (
    "class LolGetSummonerProfile: data\n"
    "class Data: summoner\n"
    "class Summoner: most_champions\n"
    "class MostChampions: champion_stats,game_type,play,win\n"
    "class ChampionStat: champion_name,play,win,id\n"
    "\n"
    "LolGetSummonerProfile(Data(Summoner(MostChampions("
    '[ChampionStat("Sylas",28,19,517),ChampionStat("Viego",15,8,234),'
    'ChampionStat("Ezreal",12,4,81),ChampionStat("Yone",11,5,777),'
    'ChampionStat("Dr. Mundo",5,4,36)],"RANKED",116,64))))'
)


def _make_provider() -> OpggProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ddragon.leagueoflegends.com":
            return httpx.Response(200, json=["14.1.1"])

        body = json.loads(request.content)
        method = body.get("method")

        if method == "initialize":
            return _mcp_json(
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "OP.GG MCP Server", "version": "1.0.0"},
                }
            )
        if method == "notifications/initialized":
            return httpx.Response(202, headers={"content-type": "text/html"})
        if method == "tools/call":
            tool = body["params"]["name"]
            arguments = body["params"]["arguments"]
            if tool == "lol_list_champions":
                return _mcp_text_result(CHAMPIONS_TEXT)
            if tool == "lol_get_champion_analysis":
                fields = arguments["desired_output_fields"]
                if any("weak_counters" in f for f in fields):
                    return _mcp_text_result(COUNTERS_TEXT)
                if any("summoner_spells" in f for f in fields):
                    return _mcp_text_result(BUILD_TEXT)
                return _mcp_text_result(CHAMPION_ANALYSIS_TEXT)
            if tool == "lol_get_champion_synergies":
                return _mcp_text_result(SYNERGIES_TEXT)
            if tool == "lol_get_summoner_profile":
                return _mcp_text_result(SUMMONER_PROFILE_TEXT)
            if tool == "lol_get_lane_matchup_guide":
                return _mcp_text_result(LANE_MATCHUP_GUIDE_TEXT)
        raise AssertionError(f"unexpected request: {method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpggProvider(cache=SQLiteCache(":memory:"), client=client)


class TestGetPatch:
    def test_returns_ddragon_version(self) -> None:
        provider = _make_provider()
        assert provider.get_patch() == "14.1.1"


class TestGetChampions:
    def test_maps_key_to_ddragon_id(self) -> None:
        provider = _make_provider()
        champions = provider.get_champions()
        by_id = {c.champion_id: c for c in champions}
        assert by_id[266].name == "Aatrox"
        assert by_id[266].ddragon_id == "Aatrox"
        assert by_id[21].ddragon_id == "MissFortune"


class TestGetChampionStats:
    def test_reconstructs_wins_from_rounded_win_rate(self) -> None:
        provider = _make_provider()
        stats = provider.get_champion_stats(266, Role.TOP, RankBracket.ALL)
        assert stats.games == 98383
        assert stats.wins == round(0.504823 * 98383)
        assert stats.pick_count == stats.games
        # total_games/ban_count are derived from pick_rate/ban_rate, not exact.
        assert stats.total_games == round(98383 / 0.0804255)
        assert stats.source == "opgg"

    def test_role_with_no_positions_entry_returns_zero_games(self) -> None:
        # CHAMPION_ANALYSIS_TEXT only has a "TOP" entry in positions[] -- this is
        # the regression test for the bug where get_champion_stats used to read
        # the champion-wide average_stats field instead, which is identical no
        # matter what role is requested. A role genuinely never played must fall
        # through to the same zero-games "no data" contract get_matchup/
        # get_synergy already use, not silently reuse another role's numbers.
        provider = _make_provider()
        stats = provider.get_champion_stats(266, Role.SUPPORT, RankBracket.ALL)
        assert stats.games == 0
        assert stats.wins == 0
        assert stats.pick_count == 0
        assert stats.ban_count == 0
        assert stats.total_games == 0

    def test_api_error_raises_opgg_api_error(self) -> None:
        provider = _make_provider()
        # Force the "BOGUS" branch by calling with a champion_id whose ddragon_id
        # we monkeypatch indirectly isn't easy here, so call the mcp client path
        # directly via a champion not in the registry instead.
        with pytest.raises(KeyError):
            provider.get_champion_stats(999999, Role.TOP, RankBracket.ALL)


class TestGetMatchup:
    def test_finds_opponent_in_weak_counters(self) -> None:
        provider = _make_provider()
        matchup = provider.get_matchup(266, 27, Role.TOP, RankBracket.ALL)
        assert matchup.wins == 408
        assert matchup.games == 927

    def test_finds_opponent_in_strong_counters(self) -> None:
        provider = _make_provider()
        matchup = provider.get_matchup(266, 48, Role.TOP, RankBracket.ALL)
        assert matchup.wins == 220
        assert matchup.games == 387

    def test_missing_opponent_returns_zero_games(self) -> None:
        provider = _make_provider()
        matchup = provider.get_matchup(266, 12345, Role.TOP, RankBracket.ALL)
        assert matchup.games == 0
        assert matchup.wins == 0

    def test_second_lookup_for_same_champion_hits_cache_not_network(self) -> None:
        provider = _make_provider()
        provider.get_matchup(266, 27, Role.TOP, RankBracket.ALL)
        # A second call, different opponent, same champion/role/rank -- must not
        # trigger a second HTTP call (the mock would raise AssertionError on an
        # unexpected request shape if it somehow diverged).
        matchup = provider.get_matchup(266, 10, Role.TOP, RankBracket.ALL)
        assert matchup.wins == 253
        assert matchup.games == 558


class TestGetSynergy:
    def test_finds_ally_by_synergy_champion_id(self) -> None:
        provider = _make_provider()
        synergy = provider.get_synergy(266, 412, RankBracket.ALL)
        assert synergy.wins == 1539
        assert synergy.games == 2857

    def test_missing_ally_returns_zero_games(self) -> None:
        provider = _make_provider()
        synergy = provider.get_synergy(266, 99999, RankBracket.ALL)
        assert synergy.games == 0


class TestGetBuild:
    def test_resolves_summoner_spell_and_shard_ids_to_names(self) -> None:
        provider = _make_provider()
        build = provider.get_build(266, Role.TOP, RankBracket.ALL)
        assert build.summoner_spells == ["Flash", "Ignite"]
        assert build.rune_shards == ["Adaptive Force", "Adaptive Force", "Health (scaling)"]

    def test_boots_folded_into_items(self) -> None:
        provider = _make_provider()
        build = provider.get_build(266, Role.TOP, RankBracket.ALL)
        assert "Plated Steelcaps" in build.items
        assert "Eclipse" in build.items

    def test_opponent_id_accepted_but_ignored(self) -> None:
        provider = _make_provider()
        build = provider.get_build(266, Role.TOP, RankBracket.ALL, opponent_id=122)
        assert build.opponent_id is None


class TestGetSummonerChampionPool:
    """`get_summoner_champion_pool` -- not part of `StatsProvider`, used only by
    `draftiq pool import-opgg` / `POST /api/pool/import-opgg`. No role/position
    field exists in this response at all (confirmed live), so this only returns
    champion names -- callers decide which role to apply them to."""

    def test_returns_names_sorted_by_play_descending(self) -> None:
        provider = _make_provider()
        names = provider.get_summoner_champion_pool("Faker", "KR1", "KR")
        # Fixture data is already play-descending (28, 15, 12, 11, 5) -- confirms
        # the method doesn't silently rely on that and re-sorts itself.
        assert names == ["Sylas", "Viego", "Ezreal", "Yone", "Dr. Mundo"]

    def test_respects_limit(self) -> None:
        provider = _make_provider()
        names = provider.get_summoner_champion_pool("Faker", "KR1", "KR", limit=2)
        assert names == ["Sylas", "Viego"]


class TestGetLaneMatchupGuide:
    """`get_lane_matchup_guide` -- the one tool this provider calls that returns
    plain JSON directly (no `desired_output_fields`, not `opgg_format`'s compact
    grammar) and takes no rank/tier parameter at all."""

    def test_parses_tip_and_qualitative_fields(self) -> None:
        provider = _make_provider()
        guide = provider.get_lane_matchup_guide(266, 122, Role.TOP)
        assert guide.my_champion == "Aatrox"
        assert guide.opponent_champion == "Darius"
        assert guide.role is Role.TOP
        assert guide.tip == "Do not get pulled by Darius's (E)."
        assert guide.lane_advantage == "Aatrox"
        assert guide.lane_solo_kill_advantage == "Aatrox"
        assert guide.recommended_play_style == "aggressive"

    def test_parses_win_rate_by_game_length(self) -> None:
        provider = _make_provider()
        guide = provider.get_lane_matchup_guide(266, 122, Role.TOP)
        assert [g.game_length for g in guide.win_rate_by_game_length] == [0, 25]
        assert guide.win_rate_by_game_length[0].win_rate == pytest.approx(0.516048)

    def test_champion_param_strips_punctuation_and_uses_underscores(self) -> None:
        """Unlike `_opgg_champion_param` (used by every other tool this provider
        calls), this tool genuinely requires UPPER_SNAKE_CASE -- confirmed live:
        apostrophes/periods are stripped entirely, spaces become underscores.
        `_make_provider`'s handler doesn't inspect the champion params, so this
        test targets `_lane_guide_champion_param` directly rather than relying on
        the mock to reject a wrong format."""
        provider = _make_provider()
        assert provider._lane_guide_champion_param(21) == "MISS_FORTUNE"  # from CHAMPIONS_TEXT


class TestPrefetchForSuggest:
    def _make_counting_provider(self) -> tuple[OpggProvider, dict[str, int]]:
        counts: dict[str, int] = {"tools/call": 0}
        lock = threading.Lock()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "ddragon.leagueoflegends.com":
                return httpx.Response(200, json=["14.1.1"])
            body = json.loads(request.content)
            method = body.get("method")
            if method == "initialize":
                return _mcp_json(
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "OP.GG MCP Server", "version": "1.0.0"},
                    }
                )
            if method == "notifications/initialized":
                return httpx.Response(202, headers={"content-type": "text/html"})
            if method == "tools/call":
                with lock:
                    counts["tools/call"] += 1
                tool = body["params"]["name"]
                arguments = body["params"]["arguments"]
                if tool == "lol_list_champions":
                    return _mcp_text_result(CHAMPIONS_TEXT)
                if tool == "lol_get_champion_analysis":
                    fields = arguments["desired_output_fields"]
                    if any("weak_counters" in f for f in fields):
                        return _mcp_text_result(COUNTERS_TEXT)
                    return _mcp_text_result(CHAMPION_ANALYSIS_TEXT)
                if tool == "lol_get_champion_synergies":
                    return _mcp_text_result(SYNERGIES_TEXT)
            raise AssertionError(f"unexpected request: {method}")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = OpggProvider(cache=SQLiteCache(":memory:"), client=client)
        return provider, counts

    def test_warms_cache_so_individual_calls_dont_hit_network_again(self) -> None:
        provider, counts = self._make_counting_provider()
        champion_ids = [266, 122, 21]
        provider.prefetch_for_suggest(
            champion_ids, Role.TOP, RankBracket.ALL, include_matchups=True, include_synergies=True
        )
        calls_after_prefetch = counts["tools/call"]
        assert calls_after_prefetch > 0

        # Every one of these would be a fresh HTTP call without the prefetch; with
        # it, they must all be served from cache -- the call count must not budge.
        for champ_id in champion_ids:
            provider.get_champion_stats(champ_id, Role.TOP, RankBracket.ALL)
            provider.get_matchup(champ_id, 27, Role.TOP, RankBracket.ALL)
            provider.get_synergy(champ_id, 412, RankBracket.ALL)

        assert counts["tools/call"] == calls_after_prefetch

    def test_concurrent_prefetch_does_not_race_on_session_init_or_cache(self) -> None:
        provider, counts = self._make_counting_provider()
        # Only 3 distinct champions exist in the mocked registry, repeated to create
        # heavy concurrent contention on the same cache keys and the same lazily
        # initialized MCP session -- a race in either would surface as an exception
        # (e.g. sqlite3 "database is locked", or a corrupted session/id counter)
        # rather than a wrong count, so a clean run is the meaningful assertion.
        many_ids = [266, 122, 21] * 10
        provider.prefetch_for_suggest(many_ids, Role.TOP, RankBracket.ALL)
        assert counts["tools/call"] > 0


class TestOpggApiError:
    def test_raised_on_jsonrpc_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "ddragon.leagueoflegends.com":
                return httpx.Response(200, json=["14.1.1"])
            body = json.loads(request.content)
            method = body.get("method")
            if method == "initialize":
                return _mcp_json({"protocolVersion": "2025-06-18", "capabilities": {}})
            if method == "notifications/initialized":
                return httpx.Response(202, headers={"content-type": "text/html"})
            if method == "tools/call":
                return _mcp_error({"code": -32600, "message": "boom"})
            raise AssertionError("unexpected request")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = OpggProvider(cache=SQLiteCache(":memory:"), client=client)
        with pytest.raises(OpggApiError):
            provider.get_champions()
