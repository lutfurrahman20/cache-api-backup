import argparse
import copy
import itertools
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import requests


PROD_BASE = "https://cache-api.eternitylabs.co"
LOCAL_BASE = "http://127.0.0.1:5000"
TIMEOUT = 20


@dataclass
class CaseResult:
    name: str
    env: str
    method: str
    path: str
    status: int
    ok: bool
    expected: str
    elapsed_ms: float
    detail: str = ""


def auth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"_non_json": resp.text}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def normalize_for_compare(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, list) and item and all(isinstance(x, dict) for x in item):
                if all("id" in x for x in item):
                    out[key] = sorted(
                        [normalize_for_compare(x) for x in item],
                        key=lambda x: str(x.get("id")),
                    )
                else:
                    out[key] = [normalize_for_compare(x) for x in item]
            else:
                out[key] = normalize_for_compare(item)
        return out
    if isinstance(value, list):
        return [normalize_for_compare(x) for x in value]
    return value


def request_json(
    method: str,
    base_url: str,
    path: str,
    token: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> Tuple[int, Any, float]:
    start = time.perf_counter()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers.update(auth_headers(token))

    try:
        resp = requests.request(
            method=method,
            url=f"{base_url}{path}",
            headers=headers,
            params=params,
            json=body,
            timeout=TIMEOUT,
            cookies=cookies,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        return resp.status_code, safe_json(resp), elapsed_ms
    except requests.RequestException as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return 0, {"_request_error": str(exc), "url": f"{base_url}{path}"}, elapsed_ms


def summarize_request(path: str, params: Optional[Dict[str, Any]], body: Optional[Dict[str, Any]]) -> str:
    if path == "/cache/batch" and isinstance(body, dict):
        t = len(body.get("team") or [])
        p = len(body.get("player") or [])
        m = len(body.get("market") or [])
        l = len(body.get("league") or [])
        return f"batch(team={t}, player={p}, market={m}, league={l}, sport={'yes' if body.get('sport') else 'no'})"
    if path == "/cache/batch/precision" and isinstance(body, dict):
        q = body.get("queries") or []
        return f"precision(queries={len(q)})"
    if path == "/cache" and isinstance(params, dict):
        return f"cache(params={','.join(sorted(params.keys()))})"
    if path == "/leagues" and isinstance(params, dict):
        return f"leagues(params={','.join(sorted(params.keys())) or 'none'})"
    return path


def summarize_response(path: str, data: Any) -> str:
    if not isinstance(data, dict):
        return "non-json"

    if path == "/cache":
        return f"found={data.get('found')}"

    if path == "/cache/batch":
        out = []
        for key in ("team", "player", "market", "league"):
            section = data.get(key)
            if isinstance(section, dict):
                total = len(section)
                found = sum(1 for value in section.values() if value is not None)
                out.append(f"{key}:{found}/{total}")
        return " ".join(out) if out else "batch"

    if path == "/cache/batch/precision":
        total = data.get("total_queries")
        ok = data.get("successful")
        bad = data.get("failed")
        if total is not None:
            return f"results:{ok}/{total} failed:{bad}"

    if path == "/leagues":
        leagues = data.get("leagues")
        if isinstance(leagues, list):
            return f"leagues={len(leagues)}"

    return f"keys={len(data.keys())}"


def run_case(
    env_name: str,
    base_url: str,
    name: str,
    method: str,
    path: str,
    expected_statuses: Set[int],
    token: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    cookies: Optional[Dict[str, str]] = None,
) -> CaseResult:
    status, payload, elapsed = request_json(
        method=method,
        base_url=base_url,
        path=path,
        token=token,
        params=params,
        body=body,
        cookies=cookies,
    )
    ok = status in expected_statuses
    exp_label = "/".join(str(x) for x in sorted(expected_statuses))
    detail = summarize_response(path, payload)

    print(
        f"[{env_name}] {name} | {method} {path} | "
        f"status={status} expected={exp_label} | time_ms={elapsed:.1f} | ok={ok} | {detail}"
    )

    return CaseResult(
        name=name,
        env=env_name,
        method=method,
        path=path,
        status=status,
        ok=ok,
        expected=exp_label,
        elapsed_ms=elapsed,
        detail=detail,
    )


PAYLOADS = [
    {
        "team": [
            "hanwha life challengers",
            "shifters",
            "Joint",
            "mythic",
            "Bronzet",
            "Fossa",
            "Navarrette",
            "ksu",
            "faze clan",
            "alt f4",
            "Kudermet",
            "Basilashvi",
            "Pagani",
            "Grabher",
            "Humbert",
        ],
        "player": [
            "Riccardo Orsolini",
            "Tiffany Hayes",
            "Yanni Gourde",
            "Cassandre Prosper",
            "Sonic",
            "Nicolas Bruna",
            "Maynter",
            "Jake Guentzel",
            "Erik Cernak",
            "Vaishnavi Adkar",
            "Adrien Rabiot",
            "Axel Tuanzebe",
            "Matt Scharff",
            "Darja Semenistaja",
            "Stephon Castle",
        ],
        "market": [
            "Avg Yards Per Punt",
            "50+ Rush Yards In Each Half",
            "1H Pass Tds",
            "Game High Rec Yards",
            "Rush + Rec Yards",
            "Double-Doubles",
            "Highest Checkout",
            "10+ Rush Yards In Each Quarter",
            "Rush + Rec Tds",
            "Holes 6-10 Strokes",
            "Points In First 5 Min.",
            "1St Sack Taken",
            "Receiving Tds",
            "Sacks Taken",
            "1Q Pts + Rebs + Asts",
        ],
    },
    {
        "team": ["sea", "ne"],
        "player": [
            "Cooper Kupp",
            "Kyle Williams",
            "Nick Emmanwori",
            "Harold Landry III",
            "George Holani",
            "Cory Durden",
            "Khyiris Tonga",
            "Andy Borregales",
            "Robert Spillane",
            "Sam Darnold",
        ],
        "market": [
            "Completion Percentage",
            "Game Longest Punt Return",
            "1H Targets",
            "1St To 20+ Rush Yards",
            "1+ Fg Or Xp Made In Each Quarter",
            "Rush + Rec Yards",
            "1St 20+ Yard Reception Of Game",
            "10+ Rush Yards In Each Quarter",
            "Sacks Taken",
            "1St Sack Taken",
        ],
    },
]


SEED = {
    "market": "Pass Yards",
    "team": "Seattle Seahawks",
    "player": "Sam Darnold",
    "sport": "Football",
    "league": "NFL",
    "region": "Europe",
    "search": "premier",
}


def collect_pool_values() -> Dict[str, List[str]]:
    markets: List[str] = [SEED["market"]]
    teams: List[str] = [SEED["team"]]
    players: List[str] = [SEED["player"]]

    for payload in PAYLOADS:
        markets.extend(payload.get("market") or [])
        teams.extend(payload.get("team") or [])
        players.extend(payload.get("player") or [])

    def uniq(values: List[str]) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        for value in values:
            key = value.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(value)
        return out

    return {
        "markets": uniq(markets),
        "teams": uniq(teams),
        "players": uniq(players),
        "sports": [SEED["sport"], "Basketball", "Soccer"],
        "leagues": [SEED["league"], "NBA", "Premier League"],
        "regions": [SEED["region"], "North America"],
        "searches": [SEED["search"], "nba", "league"],
    }


def merge_league_discovery(
    pool: Dict[str, List[str]],
    token: str,
    targets: List[Tuple[str, str]],
) -> Dict[str, List[str]]:
    sports: List[str] = copy.deepcopy(pool["sports"])
    leagues: List[str] = copy.deepcopy(pool["leagues"])
    regions: List[str] = copy.deepcopy(pool["regions"])

    for env_name, base_url in targets:
        status, payload, _ = request_json("GET", base_url, "/leagues", token=token)
        if status != 200:
            continue

        entries: List[Any] = []
        if isinstance(payload, dict):
            maybe_leagues = payload.get("leagues")
            if isinstance(maybe_leagues, list):
                entries = maybe_leagues
        elif isinstance(payload, list):
            entries = payload

        for item in entries:
            if not isinstance(item, dict):
                continue
            league = item.get("league") or item.get("name")
            sport = item.get("sport")
            region = item.get("region")
            if isinstance(league, str) and league.strip():
                leagues.append(league.strip())
            if isinstance(sport, str) and sport.strip():
                sports.append(sport.strip())
            if isinstance(region, str) and region.strip():
                regions.append(region.strip())

    def uniq(values: List[str], max_items: int = 12) -> List[str]:
        seen: Set[str] = set()
        out: List[str] = []
        for value in values:
            key = value.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(value)
            if len(out) >= max_items:
                break
        return out

    pool["sports"] = uniq(sports)
    pool["leagues"] = uniq(leagues)
    pool["regions"] = uniq(regions)
    return pool


def quick_run(token: str) -> None:
    print("--- production: POST /cache/batch ---")
    for i, body in enumerate(PAYLOADS, start=1):
        status, payload, elapsed = request_json("POST", PROD_BASE, "/cache/batch", token=token, body=body)
        print(
            f"case={i} request={summarize_request('/cache/batch', None, body)} "
            f"status={status} time_ms={elapsed:.1f} response={summarize_response('/cache/batch', payload)}"
        )

    print("--- local: POST /cache/batch ---")
    for i, body in enumerate(PAYLOADS, start=1):
        status, payload, elapsed = request_json("POST", LOCAL_BASE, "/cache/batch", token=token, body=body)
        print(
            f"case={i} request={summarize_request('/cache/batch', None, body)} "
            f"status={status} time_ms={elapsed:.1f} response={summarize_response('/cache/batch', payload)}"
        )


def compare_batch_truth(token: str, max_show: int = 25) -> None:
    print("=== compare mode: /cache/batch payloads ===")
    all_equal = True

    for i, body in enumerate(PAYLOADS, start=1):
        ps, pj, pt = request_json("POST", PROD_BASE, "/cache/batch", token=token, body=body)
        ls, lj, lt = request_json("POST", LOCAL_BASE, "/cache/batch", token=token, body=body)

        same_status = ps == ls
        same_raw = canonical_json(pj) == canonical_json(lj)
        same_normalized = canonical_json(normalize_for_compare(pj)) == canonical_json(normalize_for_compare(lj))
        overall = same_status and same_raw
        all_equal = all_equal and overall

        print(
            f"case={i} status(prod/local)={ps}/{ls} "
            f"time_ms(prod/local)={pt:.1f}/{lt:.1f} "
            f"same_raw={same_raw} same_order_insensitive={same_normalized}"
        )

        if same_raw:
            continue

        diffs = deep_diff(pj, lj)
        print(f"  raw_diffs={len(diffs)}")
        for idx, (path, pv, lv) in enumerate(diffs[:max_show], start=1):
            pvs = str(pv)
            lvs = str(lv)
            if len(pvs) > 100:
                pvs = pvs[:97] + "..."
            if len(lvs) > 100:
                lvs = lvs[:97] + "..."
            print(f"  {idx}. {path}")
            print(f"     prod={pvs}")
            print(f"     local={lvs}")

        if len(diffs) > max_show:
            print(f"  ... and {len(diffs) - max_show} more diffs")

    print(f"ALL_IDENTICAL={all_equal}")


def deep_diff(a: Any, b: Any, path: str = "") -> List[Tuple[str, Any, Any]]:
    diffs: List[Tuple[str, Any, Any]] = []
    if type(a) != type(b):
        diffs.append((path, f"type:{type(a).__name__}", f"type:{type(b).__name__}"))
        return diffs

    if isinstance(a, dict):
        ka, kb = set(a.keys()), set(b.keys())
        for key in sorted(ka - kb):
            p = f"{path}.{key}" if path else key
            diffs.append((p, "present", "missing"))
        for key in sorted(kb - ka):
            p = f"{path}.{key}" if path else key
            diffs.append((p, "missing", "present"))
        for key in sorted(ka & kb):
            p = f"{path}.{key}" if path else key
            diffs.extend(deep_diff(a[key], b[key], p))
        return diffs

    if isinstance(a, list):
        if len(a) != len(b):
            diffs.append((path, f"len={len(a)}", f"len={len(b)}"))
        n = min(len(a), len(b))
        for idx in range(n):
            diffs.extend(deep_diff(a[idx], b[idx], f"{path}[{idx}]"))
        return diffs

    if a != b:
        diffs.append((path, a, b))

    return diffs


def _all_nonempty_subsets(keys: List[str]) -> List[Tuple[str, ...]]:
    out: List[Tuple[str, ...]] = []
    for r in range(1, len(keys) + 1):
        out.extend(itertools.combinations(keys, r))
    return out


def build_cache_cases(pool: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    market = pool["markets"][0]
    team = pool["teams"][0]
    player = pool["players"][0]
    sport = pool["sports"][0]
    league = pool["leagues"][0]

    cases: List[Dict[str, Any]] = [
        {"market": market},
        {"player": player},
        {"team": team, "sport": sport},
        {"league": league, "sport": sport},
        {"team": team, "player": player},
        {"market": market, "team": team, "sport": sport},
        {"market": market, "team": team, "player": player, "league": league, "sport": sport},
        {"team": team},
        {"league": league},
    ]

    extra_markets = pool["markets"][1:4]
    for item in extra_markets:
        cases.append({"market": item})

    extra_players = pool["players"][1:4]
    for item in extra_players:
        cases.append({"player": item})

    return cases


def build_batch_cases(pool: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    sport = pool["sports"][0]
    cases = [
        {
            "team": pool["teams"][:4],
            "player": pool["players"][:4],
            "market": pool["markets"][:4],
            "league": pool["leagues"][:3],
            "sport": sport,
        },
        {
            "team": pool["teams"][:3],
            "sport": sport,
        },
        {
            "league": pool["leagues"][:3],
            "sport": sport,
        },
        {
            "player": pool["players"][:5],
            "market": pool["markets"][:5],
        },
        {
            "team": [],
            "player": [],
            "market": [],
            "league": [],
        },
    ]
    return cases


def build_precision_cases(pool: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    sport = pool["sports"][0]
    league = pool["leagues"][0]

    return [
        {
            "queries": [
                {"team": pool["teams"][0], "sport": sport},
                {"player": pool["players"][0]},
                {"market": pool["markets"][0]},
                {"league": league, "sport": sport},
                {
                    "team": pool["teams"][0],
                    "player": pool["players"][0],
                    "market": pool["markets"][0],
                    "league": league,
                    "sport": sport,
                },
            ]
        },
        {
            "queries": [
                {"team": team, "sport": sport}
                for team in pool["teams"][:4]
            ]
        },
        {
            "queries": [
                {"market": market}
                for market in pool["markets"][:4]
            ]
        },
    ]


def build_leagues_cases(pool: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    sport = pool["sports"][0]
    region = pool["regions"][0]
    search = pool["searches"][0]

    keys = ["sport", "search", "region"]
    values = {
        "sport": sport,
        "search": search,
        "region": region,
    }

    cases: List[Dict[str, Any]] = [{}]
    for subset in _all_nonempty_subsets(keys):
        cases.append({k: values[k] for k in subset})
    return cases


def run_full_suite(
    env_name: str,
    base_url: str,
    user_token: str,
    admin_token: Optional[str],
    pool: Dict[str, List[str]],
    include_destructive: bool,
) -> List[CaseResult]:
    results: List[CaseResult] = []

    print(f"\n=== {env_name.upper()} | Public endpoints ===")
    results.append(run_case(env_name, base_url, "root", "GET", "/", {200}))
    results.append(run_case(env_name, base_url, "docs", "GET", "/docs", {200}))
    results.append(run_case(env_name, base_url, "openapi (no cookie)", "GET", "/openapi.json", {200}))

    print(f"\n=== {env_name.upper()} | Auth behavior ===")
    results.append(run_case(env_name, base_url, "cache without token", "GET", "/cache", {401, 403}, params={"market": pool["markets"][0]}))
    results.append(run_case(env_name, base_url, "cache invalid token", "GET", "/cache", {401}, token="definitely-invalid-token", params={"market": pool["markets"][0]}))
    results.append(run_case(env_name, base_url, "admin health with user token", "GET", "/health", {403}, token=user_token))
    results.append(run_case(env_name, base_url, "admin dashboard without token", "GET", "/admin/dashboard", {401, 403}))

    print(f"\n=== {env_name.upper()} | User endpoints ===")
    for idx, params in enumerate(build_cache_cases(pool), start=1):
        expected = {200, 404}
        if "team" in params and "sport" not in params and "player" not in params:
            expected = {400}
        if "league" in params and "sport" not in params:
            expected = {400}
        results.append(
            run_case(
                env_name,
                base_url,
                f"cache case {idx}",
                "GET",
                "/cache",
                expected,
                token=user_token,
                params=params,
            )
        )

    for idx, body in enumerate(build_batch_cases(pool), start=1):
        results.append(
            run_case(
                env_name,
                base_url,
                f"batch case {idx}",
                "POST",
                "/cache/batch",
                {200},
                token=user_token,
                body=body,
            )
        )

    for idx, body in enumerate(build_precision_cases(pool), start=1):
        results.append(
            run_case(
                env_name,
                base_url,
                f"precision case {idx}",
                "POST",
                "/cache/batch/precision",
                {200},
                token=user_token,
                body=body,
            )
        )

    for idx, params in enumerate(build_leagues_cases(pool), start=1):
        results.append(
            run_case(
                env_name,
                base_url,
                f"leagues case {idx}",
                "GET",
                "/leagues",
                {200},
                token=user_token,
                params=params,
            )
        )

    if admin_token:
        print(f"\n=== {env_name.upper()} | Admin endpoints ===")
        results.append(run_case(env_name, base_url, "health", "GET", "/health", {200}, token=admin_token))
        results.append(run_case(env_name, base_url, "cache stats", "GET", "/cache/stats", {200}, token=admin_token))
        results.append(run_case(env_name, base_url, "admin sessions", "GET", "/admin/sessions", {200}, token=admin_token))
        results.append(run_case(env_name, base_url, "admin logs", "GET", "/admin/logs", {200}, token=admin_token, params={"limit": 10, "offset": 0}))
        results.append(run_case(env_name, base_url, "admin cache stats", "GET", "/admin/stats/cache", {200}, token=admin_token))
        results.append(
            run_case(
                env_name,
                base_url,
                "admin dashboard",
                "GET",
                "/admin/dashboard",
                {200},
                cookies={"admin_access": admin_token},
            )
        )
        results.append(
            run_case(
                env_name,
                base_url,
                "invalidate (safe not-found)",
                "DELETE",
                "/cache/invalidate",
                {200, 404},
                token=admin_token,
                params={"market": "__non_existing_market_for_test__"},
            )
        )
        results.append(
            run_case(
                env_name,
                base_url,
                "openapi (admin cookie)",
                "GET",
                "/openapi.json",
                {200},
                cookies={"admin_access": admin_token},
            )
        )

        if include_destructive:
            results.append(run_case(env_name, base_url, "cache clear (destructive)", "DELETE", "/cache/clear", {200}, token=admin_token))
    else:
        print(f"\n=== {env_name.upper()} | Admin endpoints skipped (no admin token provided) ===")

    return results


def extensive_run(token: str, targets: List[Tuple[str, str]]) -> None:
    print("=== extensive mode (local vs production comparison) ===")
    pool = collect_pool_values()
    pool = merge_league_discovery(pool, token, targets)

    summary = {
        "cache": {"total": 0, "same": 0},
        "batch": {"total": 0, "same": 0},
        "precision": {"total": 0, "same": 0},
        "leagues": {"total": 0, "same": 0},
    }

    print("\n[1] GET /cache combinations")
    for idx, params in enumerate(build_cache_cases(pool), start=1):
        ps, pj, pt = request_json("GET", PROD_BASE, "/cache", token=token, params=params)
        ls, lj, lt = request_json("GET", LOCAL_BASE, "/cache", token=token, params=params)
        same = ps == ls and canonical_json(normalize_for_compare(pj)) == canonical_json(normalize_for_compare(lj))
        summary["cache"]["total"] += 1
        summary["cache"]["same"] += int(same)
        print(
            f"  case={idx} request={summarize_request('/cache', params, None)} "
            f"status={ps}/{ls} time_ms={pt:.1f}/{lt:.1f} "
            f"response={summarize_response('/cache', pj)} same={same}"
        )

    print("\n[2] POST /cache/batch combinations")
    for idx, body in enumerate(build_batch_cases(pool), start=1):
        ps, pj, pt = request_json("POST", PROD_BASE, "/cache/batch", token=token, body=body)
        ls, lj, lt = request_json("POST", LOCAL_BASE, "/cache/batch", token=token, body=body)
        same = ps == ls and canonical_json(normalize_for_compare(pj)) == canonical_json(normalize_for_compare(lj))
        summary["batch"]["total"] += 1
        summary["batch"]["same"] += int(same)
        print(
            f"  case={idx} request={summarize_request('/cache/batch', None, body)} "
            f"status={ps}/{ls} time_ms={pt:.1f}/{lt:.1f} "
            f"response={summarize_response('/cache/batch', pj)} same={same}"
        )

    print("\n[3] POST /cache/batch/precision combinations")
    for idx, body in enumerate(build_precision_cases(pool), start=1):
        ps, pj, pt = request_json("POST", PROD_BASE, "/cache/batch/precision", token=token, body=body)
        ls, lj, lt = request_json("POST", LOCAL_BASE, "/cache/batch/precision", token=token, body=body)
        same = ps == ls and canonical_json(normalize_for_compare(pj)) == canonical_json(normalize_for_compare(lj))
        summary["precision"]["total"] += 1
        summary["precision"]["same"] += int(same)
        print(
            f"  case={idx} request={summarize_request('/cache/batch/precision', None, body)} "
            f"status={ps}/{ls} time_ms={pt:.1f}/{lt:.1f} "
            f"response={summarize_response('/cache/batch/precision', pj)} same={same}"
        )

    print("\n[4] GET /leagues combinations")
    for idx, params in enumerate(build_leagues_cases(pool), start=1):
        ps, pj, pt = request_json("GET", PROD_BASE, "/leagues", token=token, params=params)
        ls, lj, lt = request_json("GET", LOCAL_BASE, "/leagues", token=token, params=params)
        same = ps == ls and canonical_json(normalize_for_compare(pj)) == canonical_json(normalize_for_compare(lj))
        summary["leagues"]["total"] += 1
        summary["leagues"]["same"] += int(same)
        print(
            f"  case={idx} request={summarize_request('/leagues', params, None)} "
            f"status={ps}/{ls} time_ms={pt:.1f}/{lt:.1f} "
            f"response={summarize_response('/leagues', pj)} same={same}"
        )

    print("\n=== summary ===")
    for key in ["cache", "batch", "precision", "leagues"]:
        total = summary[key]["total"]
        same = summary[key]["same"]
        print(f"{key}: {same}/{total} identical")


def full_run(
    token: str,
    admin_token: Optional[str],
    targets: List[Tuple[str, str]],
    include_destructive: bool,
) -> None:
    pool = collect_pool_values()
    pool = merge_league_discovery(pool, token, targets)

    print("=== dynamic parameter pool ===")
    print(f"markets={len(pool['markets'])} teams={len(pool['teams'])} players={len(pool['players'])}")
    print(f"sports={pool['sports'][:5]} leagues={pool['leagues'][:5]} regions={pool['regions'][:5]}")

    all_results: List[CaseResult] = []
    for env_name, base_url in targets:
        all_results.extend(
            run_full_suite(
                env_name=env_name,
                base_url=base_url,
                user_token=token,
                admin_token=admin_token,
                pool=pool,
                include_destructive=include_destructive,
            )
        )

    total = len(all_results)
    passed = sum(1 for item in all_results if item.ok)
    failed = total - passed

    print("\n=== full suite summary ===")
    print(f"total={total} passed={passed} failed={failed}")

    if failed:
        print("\n--- failed cases ---")
        for item in all_results:
            if item.ok:
                continue
            print(
                f"[{item.env}] {item.name} | {item.method} {item.path} | "
                f"status={item.status} expected={item.expected}"
            )
        raise SystemExit(1)


def parse_targets(target: str) -> List[Tuple[str, str]]:
    if target == "prod":
        return [("prod", PROD_BASE)]
    if target == "local":
        return [("local", LOCAL_BASE)]
    return [("prod", PROD_BASE), ("local", LOCAL_BASE)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Cache API test runner")
    parser.add_argument(
        "--mode",
        choices=["quick", "compare", "extensive", "full"],
        default="full",
        help="quick: batch smoke; compare: /cache/batch diff; extensive: local-vs-prod combinations; full: endpoint health/auth/user/admin validation",
    )
    parser.add_argument(
        "--target",
        choices=["prod", "local", "both"],
        default="both",
        help="Target environment(s) for modes that support direct endpoint validation.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer user token. If omitted, CACHE_API_TOKEN or API_TOKEN env var is used.",
    )
    parser.add_argument(
        "--admin-token",
        default=None,
        help="Bearer admin token. If omitted, CACHE_API_ADMIN_TOKEN, ADMIN_API_TOKEN, or ADMIN_TOKEN env var is used.",
    )
    parser.add_argument(
        "--include-destructive",
        action="store_true",
        help="Include destructive admin endpoint tests (currently /cache/clear).",
    )
    args = parser.parse_args()

    token = args.token or os.getenv("CACHE_API_TOKEN") or os.getenv("API_TOKEN")
    if not token:
        raise SystemExit("Missing user token. Pass --token <value> or set CACHE_API_TOKEN/API_TOKEN.")

    admin_token = (
        args.admin_token
        or os.getenv("CACHE_API_ADMIN_TOKEN")
        or os.getenv("ADMIN_API_TOKEN")
        or os.getenv("ADMIN_TOKEN")
    )

    targets = parse_targets(args.target)

    if args.mode == "quick":
        quick_run(token)
        return

    if args.mode == "compare":
        compare_batch_truth(token)
        return

    if args.mode == "extensive":
        if args.target != "both":
            print("extensive mode compares local vs prod; forcing target=both")
        extensive_run(token, parse_targets("both"))
        return

    full_run(token, admin_token, targets, include_destructive=args.include_destructive)


if __name__ == "__main__":
    main()
