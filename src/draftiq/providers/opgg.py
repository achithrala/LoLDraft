"""OP.GG MCP provider (https://mcp-api.op.gg/mcp, Streamable HTTP transport).

This talks to the live MCP server with plain `httpx` -- no MCP SDK dependency needed.
Every call we've observed uses a single JSON POST/response (never SSE), so the client
here is intentionally minimal: an `initialize` handshake, a `notifications/initialized`
fire-and-forget, then `tools/call` requests reusing the session id from then on.

Several real behaviors here contradict the original spec or its own tool schemas.
All are confirmed against the live server (see CLAUDE.md, "OP.GG schema notes" for
the full writeup and captured sample strings) rather than guessed:

1. Every tool requiring `desired_output_fields` (all the stats tools we need) returns
   a bespoke compact text format, not JSON -- see `opgg_format.py`.
2. Raw win counts exist for matchups (`weak_counters`/`strong_counters`) and
   synergies, but NOT for a champion's own base rate: `average_stats` only exposes
   `win_rate` (rounded to ~2 decimals) and `play`. `get_champion_stats` reconstructs
   `wins = round(win_rate * play)`, which carries up to roughly ±0.25% relative
   error on high-sample champions. This is a deliberate, documented compromise, not
   an oversight -- OP.GG simply doesn't expose anything more precise here.
3. `weak_counters`/`strong_counters` are a small curated top-~3-per-side list, not a
   full pairwise matchup matrix. Most candidate-vs-enemy pairs during a real draft
   will legitimately get `games=0` back -- exactly the "no data available" case the
   StatsProvider protocol is designed to express, not a bug.
4. The synergy tool (`lol_get_champion_synergies`) requires a `my_position` and
   `synergy_position`, but `StatsProvider.get_synergy`'s signature (per the original
   spec) carries no role information at all for either champion. This provider
   queries with `position="all"` for both sides rather than the actual roles being
   drafted -- a coarser aggregate than a role-specific synergy number would be. If
   this proves too coarse in practice, the fix is threading role through the
   protocol, not guessing a position here.
5. `champion` is queried as `Champion.ddragon_id.upper()` (e.g. "MISS_FORTUNE" or
   "MISSFORTUNE" -- OP.GG's matcher tolerates both, and apostrophes/periods can be
   present or stripped). Confirmed against Kai'Sa, Cho'Gath, Dr. Mundo, Wukong
   (ddragon id "MonkeyKing"), Jarvan IV, Xin Zhao, Renata Glasc.
6. `ids_names` on `summoner_spells` and `stat_mod_names` on `runes` both return raw
   numeric ids despite the field name promising strings -- a narrow, apparent bug in
   OP.GG's field resolver scoped to exactly these two fields (item names and rune
   names resolve correctly). Worked around with small hand-curated id->name tables
   below, the same "hand-curated, clearly marked" pattern the spec already blesses
   for composition features.
7. `get_build`'s `opponent_id` parameter is accepted for StatsProvider compatibility
   but ignored: OP.GG has no opponent-specific build data via `desired_output_fields`
   (only prose tips via `lol_get_lane_matchup_guide` are opponent-aware).
8. `ChampionStats.pick_count`/`ban_count`/`total_games` are derived, not exact:
   `pick_count = play` (play already means "games this champion was picked in this
   role/bracket"). OP.GG gives `pick_rate` and `ban_rate` directly but no bracket-wide
   game total, so `total_games ~= round(play / pick_rate)` and
   `ban_count ~= round(ban_rate * total_games)`. These only feed the Phase 2
   counterpick-exposure weighting (a soft tiebreaker), not the core win-rate score,
   so the compounded estimation error is an acceptable tradeoff.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from draftiq.models import Build, Champion, ChampionStats, Matchup, RankBracket, Role, Synergy
from draftiq.providers import opgg_format
from draftiq.providers.cache import SQLiteCache, cached

MCP_URL = "https://mcp-api.op.gg/mcp"
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
MCP_PROTOCOL_VERSION = "2025-06-18"

_ROLE_TO_POSITION: dict[Role, str] = {
    Role.TOP: "top",
    Role.JUNGLE: "jungle",
    Role.MID: "mid",
    Role.BOTTOM: "adc",
    Role.SUPPORT: "support",
}

# Standard Summoner's Rift spells only -- ARAM/Nexus Blitz specials omitted since
# draftiq only models ranked SOLOQ/TOURNAMENT drafts.
_SUMMONER_SPELL_NAMES: dict[int, str] = {
    1: "Cleanse",
    3: "Exhaust",
    4: "Flash",
    6: "Ghost",
    7: "Heal",
    11: "Smite",
    12: "Teleport",
    13: "Clarity",
    14: "Ignite",
    21: "Barrier",
}

_STAT_SHARD_NAMES: dict[int, str] = {
    5001: "Health (scaling)",
    5002: "Armor",
    5003: "Magic Resist",
    5005: "Attack Speed",
    5007: "Ability Haste",
    5008: "Adaptive Force",
    5011: "Health",
    5013: "Tenacity and Slow Resist",
}


class OpggApiError(RuntimeError):
    """Raised when the OP.GG MCP server returns a JSON-RPC error for a tool call."""

    def __init__(self, tool: str, arguments: dict[str, Any], error: dict[str, Any]) -> None:
        self.tool = tool
        self.arguments = arguments
        self.error = error
        super().__init__(f"OP.GG MCP tool {tool!r} failed: {error}")


class _McpClient:
    """Minimal MCP Streamable HTTP client: initialize handshake + tools/call.
    Every response observed from the live server is plain JSON (never SSE), so
    this deliberately doesn't implement SSE parsing.

    Thread-safe: `OpggProvider.prefetch_for_suggest` calls `call_tool` from a thread
    pool, so session initialization (a check-then-act on `_session_id`) and the
    request-id counter both need locking to avoid two threads racing into two
    separate `initialize` handshakes or reusing the same JSON-RPC id."""

    def __init__(self, client: httpx.Client, url: str = MCP_URL) -> None:
        self._client = client
        self._url = url
        self._session_id: str | None = None
        self._next_id = 1
        self._init_lock = threading.Lock()
        self._id_lock = threading.Lock()

    def _request_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        response = self._client.post(self._url, json=payload, headers=headers)
        response.raise_for_status()
        session_id = response.headers.get("mcp-session-id")
        if session_id is not None:
            self._session_id = session_id
        return response

    def _ensure_initialized(self) -> None:
        if self._session_id is not None:
            return
        with self._init_lock:
            if self._session_id is not None:  # double-checked: another thread won the race
                return
            response = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "draftiq", "version": "0.1.0"},
                    },
                }
            )
            result = response.json()
            if "error" in result:
                raise RuntimeError(f"OP.GG MCP initialize failed: {result['error']}")
            # Fire-and-forget notification -- no JSON-RPC id, no response body to parse.
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self._ensure_initialized()
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._request_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        result = response.json()
        if "error" in result:
            raise OpggApiError(name, arguments, result["error"])
        content: str = result["result"]["content"][0]["text"]
        return content


class OpggProvider:
    def __init__(
        self,
        cache: SQLiteCache | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._source = "opgg"
        self._cache = cache or SQLiteCache()
        self._http = client or httpx.Client(timeout=15.0)
        self._mcp = _McpClient(self._http)

    @cached(ttl_seconds=3600.0, keyed_by_patch=False)
    def get_patch(self) -> str:
        # OP.GG's MCP tools expose no dedicated "current patch" endpoint, so this
        # reuses Data Dragon's version feed as the shared cache-invalidation signal
        # across providers, same as DataDragonProvider.get_patch.
        response = self._http.get(DDRAGON_VERSIONS_URL)
        response.raise_for_status()
        versions: list[str] = response.json()
        if not versions:
            raise RuntimeError("Data Dragon returned an empty versions list")
        return versions[0]

    @cached(ttl_seconds=86400.0)
    def get_champions(self) -> list[Champion]:
        text = self._mcp.call_tool(
            "lol_list_champions",
            {"desired_output_fields": ["data.champions[].{champion_id,key,name}"]},
        )
        parsed = opgg_format.parse(text)
        champions = [
            Champion(
                champion_id=row["champion_id"], name=row["name"], ddragon_id=row["key"], tags=[]
            )
            for row in parsed["data"]["champions"]
        ]
        return sorted(champions, key=lambda c: c.champion_id)

    def _opgg_champion_param(self, champion_id: int) -> str:
        for champion in self.get_champions():
            if champion.champion_id == champion_id:
                return champion.ddragon_id.upper()
        raise KeyError(f"Unknown champion_id {champion_id!r} (not in OP.GG's champion list)")

    @cached(ttl_seconds=86400.0)
    def get_champion_stats(self, champion_id: int, role: Role, rank: RankBracket) -> ChampionStats:
        champion_param = self._opgg_champion_param(champion_id)
        text = self._mcp.call_tool(
            "lol_get_champion_analysis",
            {
                "game_mode": "ranked",
                "champion": champion_param,
                "position": _ROLE_TO_POSITION[role],
                "tier": rank.value,
                "desired_output_fields": [
                    "data.summary.average_stats.{play,win_rate,pick_rate,ban_rate}"
                ],
            },
        )
        parsed = opgg_format.parse(text)
        stats = parsed["data"]["summary"]["average_stats"]
        play: int = stats["play"]
        win_rate: float = stats["win_rate"]
        pick_rate: float = stats["pick_rate"]
        ban_rate: float = stats["ban_rate"]
        total_games_estimate = round(play / pick_rate) if pick_rate > 0 else 0
        return ChampionStats(
            champion_id=champion_id,
            role=role,
            rank=rank,
            patch=self.get_patch(),
            wins=round(win_rate * play),
            games=play,
            pick_count=play,
            ban_count=round(ban_rate * total_games_estimate),
            total_games=total_games_estimate,
            source=self._source,
        )

    @cached(ttl_seconds=86400.0)
    def _counters(self, champion_id: int, role: Role, rank: RankBracket) -> list[dict[str, int]]:
        champion_param = self._opgg_champion_param(champion_id)
        text = self._mcp.call_tool(
            "lol_get_champion_analysis",
            {
                "game_mode": "ranked",
                "champion": champion_param,
                "position": _ROLE_TO_POSITION[role],
                "tier": rank.value,
                "desired_output_fields": [
                    "data.weak_counters[].{champion_id,play,win}",
                    "data.strong_counters[].{champion_id,play,win}",
                ],
            },
        )
        parsed = opgg_format.parse(text)
        data = parsed["data"]
        rows = list(data.get("weak_counters", [])) + list(data.get("strong_counters", []))
        return [{"champion_id": r["champion_id"], "win": r["win"], "play": r["play"]} for r in rows]

    def get_matchup(
        self, champion_id: int, opponent_id: int, role: Role, rank: RankBracket
    ) -> Matchup:
        for row in self._counters(champion_id, role, rank):
            if row["champion_id"] == opponent_id:
                return Matchup(
                    champion_id=champion_id,
                    opponent_id=opponent_id,
                    role=role,
                    rank=rank,
                    patch=self.get_patch(),
                    wins=row["win"],
                    games=row["play"],
                    source=self._source,
                )
        return Matchup(
            champion_id=champion_id,
            opponent_id=opponent_id,
            role=role,
            rank=rank,
            patch=self.get_patch(),
            wins=0,
            games=0,
            source=self._source,
        )

    @cached(ttl_seconds=86400.0)
    def _synergies(self, champion_id: int, rank: RankBracket) -> list[dict[str, int]]:
        champion_param = self._opgg_champion_param(champion_id)
        text = self._mcp.call_tool(
            "lol_get_champion_synergies",
            {
                "champion": champion_param,
                "my_position": "all",
                "synergy_position": "all",
                "desired_output_fields": ["data.synergies[].{synergy_champion_id,play,win}"],
            },
        )
        parsed = opgg_format.parse(text)
        return [
            {"champion_id": row["synergy_champion_id"], "win": row["win"], "play": row["play"]}
            for row in parsed["data"]["synergies"]
        ]

    def get_synergy(self, champion_id: int, ally_id: int, rank: RankBracket) -> Synergy:
        for row in self._synergies(champion_id, rank):
            if row["champion_id"] == ally_id:
                return Synergy(
                    champion_id=champion_id,
                    ally_id=ally_id,
                    rank=rank,
                    patch=self.get_patch(),
                    wins=row["win"],
                    games=row["play"],
                    source=self._source,
                )
        return Synergy(
            champion_id=champion_id,
            ally_id=ally_id,
            rank=rank,
            patch=self.get_patch(),
            wins=0,
            games=0,
            source=self._source,
        )

    @cached(ttl_seconds=86400.0)
    def get_build(
        self,
        champion_id: int,
        role: Role,
        rank: RankBracket,
        opponent_id: int | None = None,
    ) -> Build:
        champion_param = self._opgg_champion_param(champion_id)
        text = self._mcp.call_tool(
            "lol_get_champion_analysis",
            {
                "game_mode": "ranked",
                "champion": champion_param,
                "position": _ROLE_TO_POSITION[role],
                "tier": rank.value,
                "desired_output_fields": [
                    "data.starter_items.ids_names[]",
                    "data.core_items.ids_names[]",
                    "data.boots.ids_names[]",
                    "data.summoner_spells.ids[]",
                    "data.skills.order[]",
                    "data.runes.{primary_rune_names[],secondary_rune_names[],stat_mod_names[]}",
                ],
            },
        )
        parsed = opgg_format.parse(text)
        data = parsed["data"]
        summoner_ids: list[int] = data["summoner_spells"]["ids"]
        shard_ids: list[int] = data["runes"]["stat_mod_names"]
        return Build(
            champion_id=champion_id,
            role=role,
            rank=rank,
            opponent_id=None,  # not supported by OP.GG via desired_output_fields; see docstring
            patch=self.get_patch(),
            starting_items=data["starter_items"]["ids_names"],
            items=data["boots"]["ids_names"] + data["core_items"]["ids_names"],
            runes_primary=data["runes"]["primary_rune_names"],
            runes_secondary=data["runes"]["secondary_rune_names"],
            rune_shards=[_STAT_SHARD_NAMES.get(i, str(i)) for i in shard_ids],
            skill_order=data["skills"]["order"],
            summoner_spells=[_SUMMONER_SPELL_NAMES.get(i, str(i)) for i in summoner_ids],
            source=self._source,
        )

    def prefetch_for_suggest(
        self,
        champion_ids: Iterable[int],
        role: Role,
        rank: RankBracket,
        include_matchups: bool = False,
        include_synergies: bool = False,
    ) -> None:
        """Warms the cache for many champions' base-rate stats (and, on request,
        their counters/synergies lists) concurrently.

        Not part of the StatsProvider protocol -- `search/greedy.py` duck-types this
        (`hasattr(provider, "prefetch_for_suggest")`) rather than every provider
        needing a no-op implementation. Without it, a cold `suggest()` call against
        OP.GG's ~170-champion live roster is ~170 sequential HTTP round-trips, which
        in practice takes minutes -- unacceptable for a live draft (confirmed by
        timing an actual cold call against the production server). Concurrency is
        safe: `httpx.Client` and `SQLiteCache` are both thread-safe, and `_McpClient`
        guards its session-init race with a lock.

        `include_matchups` warms `_counters` -- needed both for matchup deltas
        against already-picked enemies and for counterpick exposure against the
        whole remaining pool, so callers should pass it unconditionally once any
        picks remain for either side. `include_synergies` only matters once there
        are allies to check synergy against.
        """
        ids = list(champion_ids)
        tasks: list[Any] = [lambda cid=cid: self.get_champion_stats(cid, role, rank) for cid in ids]
        if include_matchups:
            tasks += [lambda cid=cid: self._counters(cid, role, rank) for cid in ids]
        if include_synergies:
            tasks += [lambda cid=cid: self._synergies(cid, rank) for cid in ids]
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(task) for task in tasks]
            for future in futures:
                future.result()

    def close(self) -> None:
        self._http.close()
