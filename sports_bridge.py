"""
sports_bridge.py
================
Optional enrichment helper for the Cache API.

Fetches player / team statistics from the internal Stats API
(realtime_data_fetch/stats_api.py) and caches results in Redis.

Design principles
-----------------
* Zero-overhead when STATS_API_URL is not configured — the coroutine
  returns None immediately without any network activity.
* Hard timeout (default 1 s) so a slow stats service never drags down
  the production Cache API.
* All errors (timeout, connection refused, JSON parse, Redis) are caught
  and logged; the function always returns None on failure.
* Redis cache key is prefixed with ``stats_bridge:`` to guarantee it
  never collides with the main ``cache:*`` keys.

Environment variables
---------------------
STATS_API_URL      Base URL of stats_api.py  (e.g. http://localhost:8001).
                   Leave blank / unset to disable enrichment entirely.
STATS_API_TOKEN    Bearer token required by stats_api.py (optional).
STATS_API_TIMEOUT  Max seconds to wait for a stats response (default 1.0).
STATS_CACHE_TTL    Redis TTL for cached stat payloads in seconds (def 300).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (read once at import; never raises)
# ---------------------------------------------------------------------------

_STATS_URL  = os.getenv("STATS_API_URL", "").rstrip("/")
_TOKEN      = os.getenv("STATS_API_TOKEN", "").strip()
_TIMEOUT    = float(os.getenv("STATS_API_TIMEOUT", "1.0"))
_CACHE_TTL  = int(os.getenv("STATS_CACHE_TTL", "300"))

# ---------------------------------------------------------------------------
# Redis helper (re-uses the existing redis_cache module)
# ---------------------------------------------------------------------------

def _redis_get(key: str) -> Optional[dict]:
    try:
        from redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return None
        raw = client.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:
        log.debug("stats_bridge redis get error: %s", exc)
        return None


def _redis_set(key: str, data: dict, ttl: int) -> None:
    try:
        from redis_cache import get_redis_client
        client = get_redis_client()
        if client is None:
            return
        client.setex(key, ttl, json.dumps(data, default=str))
    except Exception as exc:
        log.debug("stats_bridge redis set error: %s", exc)


def _cache_key(player: str | None, team: str | None, sport: str | None) -> str:
    """Deterministic Redis key for a (player, team, sport) triple."""
    raw = f"{(player or '').lower()}|{(team or '').lower()}|{(sport or '').lower()}"
    h   = hashlib.md5(raw.encode()).hexdigest()[:12]
    return f"stats_bridge:{raw[:60]}:{h}"


# ---------------------------------------------------------------------------
# HTTP call helpers
# ---------------------------------------------------------------------------

def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Accept": "application/json"}
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    return h


async def _get_json(url: str) -> dict[str, Any] | None:
    """Async GET with hard timeout; returns None on any error."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(url, headers=_headers())
            if r.status_code == 200:
                return r.json()
            log.debug("stats_bridge: %s → HTTP %s", url, r.status_code)
            return None
    except Exception as exc:
        log.debug("stats_bridge fetch error for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def enrich(
    player: str | None,
    team:   str | None,
    sport:  str | None,
) -> Optional[dict[str, Any]]:
    """
    Fetch and return stats for the given player / team / sport triple.

    Returns None when:
    - STATS_API_URL is not configured (zero overhead)
    - The stats service is unreachable or times out
    - No matching data is found
    - Any other error occurs

    Never raises.
    """
    if not _STATS_URL:
        return None  # feature disabled — early exit, no network call

    if not player and not team:
        return None  # nothing useful to look up

    cache_key = _cache_key(player, team, sport)
    cached    = _redis_get(cache_key)
    if cached is not None:
        return cached

    result: dict[str, Any] = {"source": "sports_stats_api"}

    # --- Player stats ---
    if player:
        params = f"name={player}"
        if sport:
            params += f"&sport={sport}"
        data = await _get_json(f"{_STATS_URL}/stats/player?{params}&limit=5")
        if data and data.get("found"):
            result["player"] = {
                "name":  player,
                "games": data.get("games", []),
            }

    # --- Team stats ---
    if team:
        params = f"name={team}"
        if sport:
            params += f"&sport={sport}"
        data = await _get_json(f"{_STATS_URL}/stats/team?{params}&limit=5")
        if data and data.get("found"):
            result["team"] = {
                "name":        team,
                "record":      data.get("record", {}),
                "recent":      data.get("recent", []),
                "top_scorers": data.get("top_scorers", []),
            }

    # --- Live state ---
    live_params = []
    if team:
        live_params.append(f"team={team}")
    if player:
        live_params.append(f"player={player}")
    live_data = await _get_json(f"{_STATS_URL}/stats/live?{'&'.join(live_params)}")
    if live_data and live_data.get("found"):
        result["live"] = {
            "live":    live_data.get("live", []),
            "pregame": live_data.get("pregame", []),
        }
    else:
        result["live"] = None

    # Only cache & return if we actually got something useful
    if len(result) > 1:  # has at least one key beyond "source"
        _redis_set(cache_key, result, _CACHE_TTL)
        return result

    return None
