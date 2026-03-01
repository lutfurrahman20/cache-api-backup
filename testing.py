import argparse
import copy
import itertools
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


PROD_BASE = "https://cache-api.eternitylabs.co"
LOCAL_BASE = "http://localhost:5000"
TIMEOUT = 20


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


def json_size_bytes(value: Any) -> int:
    try:
        return len(canonical_json(value).encode("utf-8"))
    except Exception:
        return 0


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
    return f"{path}"


def summarize_response(path: str, data: Any) -> str:
    size = json_size_bytes(data)
    if not isinstance(data, dict):
        return f"size={size}B"
    if path == "/cache/batch":
        parts = []
        for key in ("team", "player", "market", "league"):
            section = data.get(key)
            if isinstance(section, dict):
                total = len(section)
                found = sum(1 for v in section.values() if v is not None)
                parts.append(f"{key}:{found}/{total}")
        return f"{' '.join(parts)} size={size}B"
    if path == "/cache/batch/precision":
        total = data.get("total_queries")
        ok = data.get("successful")
        bad = data.get("failed")
        if total is not None:
            return f"results:{ok}/{total} failed:{bad} size={size}B"
    if path == "/cache":
        found = data.get("found")
        return f"found={found} keys={len(data.keys())} size={size}B"
    if path == "/leagues":
        leagues = data.get("leagues")
        if isinstance(leagues, list):
            return f"leagues={len(leagues)} size={size}B"
    return f"keys={len(data.keys())} size={size}B"


def request_json(
    method: str,
    base_url: str,
    path: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any, float]:
    start = time.perf_counter()
    try:
        r = requests.request(
            method=method,
            url=f"{base_url}{path}",
            headers=auth_headers(token),
            params=params,
            json=body,
            timeout=TIMEOUT,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        return r.status_code, safe_json(r), elapsed_ms
    except requests.RequestException as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return 0, {"_request_error": str(exc), "url": f"{base_url}{path}"}, elapsed_ms


def test_endpoint(url, token, payloads):
    headers = auth_headers(token)
    for i, p in enumerate(payloads, start=1):
        start = time.perf_counter()
        r = requests.post(url, json=p, headers=headers, timeout=TIMEOUT)
        elapsed_ms = (time.perf_counter() - start) * 1000
        body = safe_json(r)
        req_desc = summarize_request("/cache/batch", None, p)
        res_desc = summarize_response("/cache/batch", body)
        print(
            f"{i}. request={req_desc} status={r.status_code} "
            f"time_ms={elapsed_ms:.1f} response={res_desc}"
        )

# Token is intentionally NOT hardcoded.
# Provide via --token or CACHE_API_TOKEN environment variable.
PUBLIC_TOKEN = None
payloads = [
    {"team":["hanwha life challengers","shifters","Joint","mythic","Bronzet",
             "Fossa","Navarrette","ksu","faze clan","alt f4","Kudermet",
             "Basilashvi","Pagani","Grabher","Humbert"],
     "player":["Riccardo Orsolini","Tiffany Hayes","Yanni Gourde",
               "Cassandre Prosper","Sonic","Nicolas Bruna","Maynter",
               "Jake Guentzel","Erik Cernak","Vaishnavi Adkar",
               "Adrien Rabiot","Axel Tuanzebe","Matt Scharff",
               "Darja Semenistaja","Stephon Castle"],
     "market":["Avg Yards Per Punt","50+ Rush Yards In Each Half",
               "1H Pass Tds","Game High Rec Yards","Rush + Rec Yards",
               "Double-Doubles","Highest Checkout",
               "10+ Rush Yards In Each Quarter","Rush + Rec Tds",
               "Holes 6-10 Strokes","Points In First 5 Min.",
               "1St Sack Taken","Receiving Tds","Sacks Taken",
               "1Q Pts + Rebs + Asts"]},
    {"team":["sea","ne"],
     "player":["Cooper Kupp","Kyle Williams","Nick Emmanwori",
               "Harold Landry III","George Holani","Cory Durden",
               "Khyiris Tonga","Andy Borregales","Robert Spillane",
               "Sam Darnold"],
     "market":["Completion Percentage","Game Longest Punt Return",
               "1H Targets","1St To 20+ Rush Yards",
               "1+ Fg Or Xp Made In Each Quarter",
               "Rush + Rec Yards","1St 20+ Yard Reception Of Game",
               "10+ Rush Yards In Each Quarter","Sacks Taken",
               "1St Sack Taken"]},
    {"market":["1St Drive Receptions","1St Drive Rush Yards","Rush Yards",
               "Missed Fg","Ints Thrown","50+ Pass Yards In Each Quarter",
               "Receiving Tds","1+ Reception In Each Quarter",
               "First Td Scorer","1St To 20+ Rec Yards"]},
    {"player":["Drake Thomas","Jaxon Smith-Njigba","Stefon Diggs",
               "Jake Bobo","Rhamondre Stevenson","Christian Gonzalez",
               "Eric Saubert","Mack Hollins","Leonard Taylor",
               "Craig Woodson"],
     "market":["Game High Rush Yards","1H Pass Tds","Tackles + Assists",
               "Completions","Doinks","1St Team Reception","Fumbles Lost",
               "Longest Reception","1St Drive Fg Or Xp Made",
               "1St Reception (Yards)"]},
    {"team":[],"player":["Drake Thomas","Jarran Reed","Dareke Young",
               "Khyiris Tonga","Jack Westover","Christian Barmore",
               "Eric Saubert","Brady Russell","Nick Emmanwori",
               "Ernest Jones"],
     "market":["1St Reception (Yards)","Rush + Rec Yards","Fg Made",
               "Completion Percentage","Fantasy Points","Missed Fg",
               "1St Drive Pass Yards","Team 1St Td Scorer",
               "25+ Rush Yards In Each Half","Solo Tackles"]},
    {"team":[],"player":["Milton Williams","Christian Elliss","Robbie Ouzts",
               "Hunter Henry","DeMario Douglas","DeMarcus Lawrence",
               "Tyrice Knight","Drake Maye","TreVeyon Henderson",
               "Jaxon Smith-Njigba"],
     "market":["1H Targets","100+ Pass Yards In Each Half",
               "Defensive Ints","25+ Pass Yards In Each Quarter",
               "1St Attempt Completions","1Q Pass Tds",
               "Game Longest Punt Return","50+ Rush Yards In Each Half",
               "Game High Rec Yards","1St Pass Td (Yards)"]},
    {"team":[],"player":["Harold Landry III","Marcus Jones","Josh Jobe"],
     "market":["50+ Pass Yards In Each Quarter","1St To 20+ Rush Yards",
               "Receiving Tds","Kicking Points",
               "1St 10+ Yard Rush Of Game","Pass Yards",
               "1Q Most Rec Yards","Game Longest Rush","Completions",
               "1St Sack Taken"]},
    {"team":[],"player":["AJ Barner","Sam Darnold","Leonard Williams",
               "Coby Bryant","Boye Mafe","Craig Woodson","Andy Borregales",
               "Carlton Davis","Uchenna Nwosu","Elijah Arroyo"],
     "market":["Targets","Rush Attempt Or Target Each Drive 1H",
               "Pass Tds","1H Pass Tds","1Q Pass Yards",
               "Completions In 1St 5 Attempts","Rush Yards","1H Rec Yards",
               "50+ Rec Yards In Each Half","Rush Attempts"]}
]


SAMPLES = {
    "market": "Pass Yards",
    "team": "Seattle Seahawks",
    "player": "Sam Darnold",
    "sport": "Football",
    "league": "NFL",
}

PRO_LEAGUES = {
    "NFL",
    "NBA",
    "NHL",
    "MLB",
    "WNBA",
    "EPL",
    "Serie A",
    "La Liga",
    "Bundesliga",
    "Ligue 1",
}


def normalize_for_compare(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                if all("id" in x for x in v):
                    out[k] = sorted(
                        [normalize_for_compare(x) for x in v],
                        key=lambda x: str(x.get("id")),
                    )
                else:
                    out[k] = [normalize_for_compare(x) for x in v]
            else:
                out[k] = normalize_for_compare(v)
        return out
    if isinstance(value, list):
        return [normalize_for_compare(x) for x in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def deep_diff(a: Any, b: Any, path: str = "") -> List[Tuple[str, Any, Any]]:
    diffs: List[Tuple[str, Any, Any]] = []
    if type(a) != type(b):
        diffs.append((path, f"type:{type(a).__name__}", f"type:{type(b).__name__}"))
        return diffs
    if isinstance(a, dict):
        ka, kb = set(a.keys()), set(b.keys())
        for k in sorted(ka - kb):
            p = f"{path}.{k}" if path else k
            diffs.append((p, "present", "missing"))
        for k in sorted(kb - ka):
            p = f"{path}.{k}" if path else k
            diffs.append((p, "missing", "present"))
        for k in sorted(ka & kb):
            p = f"{path}.{k}" if path else k
            diffs.extend(deep_diff(a[k], b[k], p))
        return diffs
    if isinstance(a, list):
        if len(a) != len(b):
            diffs.append((path, f"len={len(a)}", f"len={len(b)}"))
        n = min(len(a), len(b))
        for i in range(n):
            diffs.extend(deep_diff(a[i], b[i], f"{path}[{i}]"))
        return diffs
    if a != b:
        diffs.append((path, a, b))
    return diffs


def score_record(record: Dict[str, Any]) -> int:
    score = 0
    team = record.get("team")
    league = str(record.get("league") or "")
    age = record.get("age")
    if team:
        score += 5
    if any(pro in league for pro in PRO_LEAGUES):
        score += 4
    if isinstance(age, int) and age > 5:
        score += 2
    if record.get("number") not in (None, 0):
        score += 1
    return score


def truth_preference(prod: Dict[str, Any], local: Dict[str, Any]) -> str:
    ps = score_record(prod)
    ls = score_record(local)
    if ps > ls:
        return "production"
    if ls > ps:
        return "local"
    return "tie"


def quick_run(token: str) -> None:
    print("--- production ---")
    test_endpoint(f"{PROD_BASE}/cache/batch", token, payloads)
    print("--- local ---")
    test_endpoint(f"{LOCAL_BASE}/cache/batch", token, payloads)


def compare_batch_truth(token: str, max_show: int = 25) -> None:
    print("=== compare mode: /cache/batch payloads ===")
    all_equal = True
    for i, p in enumerate(payloads, start=1):
        ps, pj, pt = request_json("POST", PROD_BASE, "/cache/batch", token, body=p)
        ls, lj, lt = request_json("POST", LOCAL_BASE, "/cache/batch", token, body=p)
        same_status = ps == ls
        same_raw = canonical_json(pj) == canonical_json(lj)
        same_normalized = (
            canonical_json(normalize_for_compare(pj))
            == canonical_json(normalize_for_compare(lj))
        )
        overall = same_status and same_raw
        all_equal = all_equal and overall
        print(
            f"case={i} status(prod/local)={ps}/{ls} "
            f"time_ms(prod/local)={pt:.1f}/{lt:.1f} "
            f"same_raw={same_raw} same_order_insensitive={same_normalized}"
        )
        if same_raw:
            continue

        raw_diffs = deep_diff(pj, lj)
        print(f"  raw_diffs={len(raw_diffs)}")
        for idx, (path, pv, lv) in enumerate(raw_diffs[:max_show], start=1):
            pvs = str(pv)
            lvs = str(lv)
            if len(pvs) > 100:
                pvs = pvs[:97] + "..."
            if len(lvs) > 100:
                lvs = lvs[:97] + "..."
            print(f"  {idx}. {path}")
            print(f"     prod={pvs}")
            print(f"     local={lvs}")
            if path.endswith(".players[0].id") or path.endswith(".players[1].id"):
                # only a lightweight hint; full truth by scoring is below
                pass
        if len(raw_diffs) > max_show:
            print(f"  ... and {len(raw_diffs) - max_show} more diffs")

        # Truth hints for duplicate name conflicts in player sections.
        prod_player = pj.get("player") if isinstance(pj, dict) else None
        local_player = lj.get("player") if isinstance(lj, dict) else None
        if isinstance(prod_player, dict) and isinstance(local_player, dict):
            for name in sorted(set(prod_player.keys()) & set(local_player.keys())):
                pr = prod_player.get(name)
                lr = local_player.get(name)
                if (
                    isinstance(pr, dict)
                    and isinstance(lr, dict)
                    and isinstance(pr.get("players"), list)
                    and isinstance(lr.get("players"), list)
                    and pr.get("players")
                    and lr.get("players")
                ):
                    p0 = pr["players"][0]
                    l0 = lr["players"][0]
                    if (
                        isinstance(p0, dict)
                        and isinstance(l0, dict)
                        and p0.get("id") != l0.get("id")
                    ):
                        pref = truth_preference(p0, l0)
                        print(
                            f"  truth_hint player={name}: preferred={pref} "
                            f"(prod_id={p0.get('id')} local_id={l0.get('id')})"
                        )
    print(f"ALL_IDENTICAL={all_equal}")


def _all_nonempty_subsets(keys: List[str]) -> List[Tuple[str, ...]]:
    out: List[Tuple[str, ...]] = []
    for r in range(1, len(keys) + 1):
        out.extend(itertools.combinations(keys, r))
    return out


def build_cache_param_combinations() -> List[Dict[str, Any]]:
    keys = ["market", "team", "player", "league"]
    combos: List[Dict[str, Any]] = []
    for subset in _all_nonempty_subsets(keys):
        params = {k: SAMPLES[k] for k in subset}
        # API requires sport for team-only / league-included lookups (unless player is present with team)
        if ("team" in params and "player" not in params) or ("league" in params):
            params["sport"] = SAMPLES["sport"]
        combos.append(params)

    # Explicit invalid cases to validate errors
    combos.append({"team": SAMPLES["team"]})  # missing sport
    combos.append({"league": SAMPLES["league"]})  # missing sport
    return combos


def build_batch_combinations() -> List[Dict[str, Any]]:
    keys = ["team", "player", "market", "league"]
    seed = {
        "team": [SAMPLES["team"]],
        "player": [SAMPLES["player"]],
        "market": [SAMPLES["market"]],
        "league": [SAMPLES["league"]],
    }
    combos: List[Dict[str, Any]] = []
    for subset in _all_nonempty_subsets(keys):
        body = {k: copy.deepcopy(seed[k]) for k in subset}
        if "team" in subset or "league" in subset:
            body["sport"] = SAMPLES["sport"]
        combos.append(body)
    # Edge cases
    combos.append({"team": [], "player": [], "market": [], "league": []})
    combos.append({"team": [SAMPLES["team"]]})  # no sport (should still return 200 from batch implementation)
    return combos


def build_precision_combinations() -> List[Dict[str, Any]]:
    keys = ["market", "team", "player", "league"]
    combos: List[Dict[str, Any]] = []
    for subset in _all_nonempty_subsets(keys):
        item = {k: SAMPLES[k] for k in subset}
        if ("team" in item and "player" not in item) or ("league" in item):
            item["sport"] = SAMPLES["sport"]
        combos.append({"queries": [item]})
    # Multi-query mixed combination
    combos.append(
        {
            "queries": [
                {"team": SAMPLES["team"], "sport": SAMPLES["sport"]},
                {"player": SAMPLES["player"]},
                {"market": SAMPLES["market"]},
                {"league": SAMPLES["league"], "sport": SAMPLES["sport"]},
                {"team": SAMPLES["team"], "player": SAMPLES["player"], "sport": SAMPLES["sport"]},
            ]
        }
    )
    return combos


def build_leagues_combinations() -> List[Dict[str, Any]]:
    options = {
        "sport": "Football",
        "search": "premier",
        "region": "Europe",
    }
    keys = ["sport", "search", "region"]
    combos: List[Dict[str, Any]] = [{}]  # include empty query
    for subset in _all_nonempty_subsets(keys):
        combos.append({k: options[k] for k in subset})
    return combos


def compare_call(
    method: str,
    path: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ps, pj, pt = request_json(method, PROD_BASE, path, token, params=params, body=body)
    ls, lj, lt = request_json(method, LOCAL_BASE, path, token, params=params, body=body)
    same_status = ps == ls
    same_body = canonical_json(normalize_for_compare(pj)) == canonical_json(
        normalize_for_compare(lj)
    )
    return {
        "same": same_status and same_body,
        "same_status": same_status,
        "same_body": same_body,
        "prod_status": ps,
        "local_status": ls,
        "prod_time_ms": pt,
        "local_time_ms": lt,
        "prod_json": pj,
        "local_json": lj,
    }


def extensive_run(token: str) -> None:
    print("=== extensive mode ===")
    summary = {
        "cache": {"total": 0, "same": 0},
        "batch": {"total": 0, "same": 0},
        "precision": {"total": 0, "same": 0},
        "leagues": {"total": 0, "same": 0},
    }

    print("\n[1] GET /cache combinations")
    for i, params in enumerate(build_cache_param_combinations(), start=1):
        res = compare_call("GET", "/cache", token, params=params)
        summary["cache"]["total"] += 1
        summary["cache"]["same"] += int(res["same"])
        print(
            f"  case={i} request={summarize_request('/cache', params, None)} "
            f"status={res['prod_status']}/{res['local_status']} "
            f"time_ms={res['prod_time_ms']:.1f}/{res['local_time_ms']:.1f} "
            f"response={summarize_response('/cache', res['prod_json'])} "
            f"same={res['same']}"
        )

    print("\n[2] POST /cache/batch combinations")
    for i, body in enumerate(build_batch_combinations(), start=1):
        res = compare_call("POST", "/cache/batch", token, body=body)
        summary["batch"]["total"] += 1
        summary["batch"]["same"] += int(res["same"])
        print(
            f"  case={i} request={summarize_request('/cache/batch', None, body)} "
            f"status={res['prod_status']}/{res['local_status']} "
            f"time_ms={res['prod_time_ms']:.1f}/{res['local_time_ms']:.1f} "
            f"response={summarize_response('/cache/batch', res['prod_json'])} "
            f"same={res['same']}"
        )

    print("\n[3] POST /cache/batch/precision combinations")
    for i, body in enumerate(build_precision_combinations(), start=1):
        res = compare_call("POST", "/cache/batch/precision", token, body=body)
        summary["precision"]["total"] += 1
        summary["precision"]["same"] += int(res["same"])
        print(
            f"  case={i} request={summarize_request('/cache/batch/precision', None, body)} "
            f"status={res['prod_status']}/{res['local_status']} "
            f"time_ms={res['prod_time_ms']:.1f}/{res['local_time_ms']:.1f} "
            f"response={summarize_response('/cache/batch/precision', res['prod_json'])} "
            f"same={res['same']}"
        )

    print("\n[4] GET /leagues combinations")
    for i, params in enumerate(build_leagues_combinations(), start=1):
        res = compare_call("GET", "/leagues", token, params=params)
        summary["leagues"]["total"] += 1
        summary["leagues"]["same"] += int(res["same"])
        print(
            f"  case={i} request={summarize_request('/leagues', params, None)} "
            f"status={res['prod_status']}/{res['local_status']} "
            f"time_ms={res['prod_time_ms']:.1f}/{res['local_time_ms']:.1f} "
            f"response={summarize_response('/leagues', res['prod_json'])} "
            f"same={res['same']}"
        )

    print("\n=== summary ===")
    for k in ["cache", "batch", "precision", "leagues"]:
        t = summary[k]["total"]
        s = summary[k]["same"]
        print(f"{k}: {s}/{t} identical")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cache API local vs production tester")
    parser.add_argument(
        "--mode",
        choices=["quick", "compare", "extensive"],
        default="quick",
        help="quick: original batch output; compare: batch with diffs; extensive: all endpoint combinations",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token to use. If omitted, CACHE_API_TOKEN env var is used.",
    )
    args = parser.parse_args()
    token = args.token or os.getenv("CACHE_API_TOKEN")
    if not token:
        raise SystemExit(
            "Missing token. Pass --token <value> or set CACHE_API_TOKEN environment variable."
        )

    if args.mode == "quick":
        quick_run(token)
    elif args.mode == "compare":
        compare_batch_truth(token)
    else:
        extensive_run(token)


if __name__ == "__main__":
    main()
