"""
PR validation tests for Cache API.

These tests run automatically on every pull request via GitHub Actions.
They use FastAPI's TestClient so no live server or secrets are required —
dummy tokens are injected via environment variables before the app is imported.

Coverage:
  - App boots without crashing
  - Root endpoint shape
  - Auth layer: missing token, wrong token, valid token
  - Admin-only endpoints reject non-admin tokens
  - Key endpoints return sane HTTP status codes (not 500)
  - Request body validation (400 on bad input, not 500)
  - Token management endpoints (create, list, revoke, rotate, audit)
  - Analytics endpoints (failures, latency, signatures, trends)
"""

import os
from unittest.mock import patch, MagicMock

# ── Inject dummy tokens BEFORE importing the app ──────────────────────────────
# This lets the app boot without real secrets.  Tests validate that the auth
# layer works correctly using these known values.
os.environ.setdefault("API_TOKEN", "ci-user-token")
os.environ.setdefault("ADMIN_API_TOKEN", "ci-admin-token")
# Setting REDIS_HOST prevents the Windows startup event from running Docker
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
# ─────────────────────────────────────────────────────────────────────────────

# ── Patch Redis BEFORE importing the app ──────────────────────────────────────
# Without this, every endpoint call re-attempts the Redis TCP handshake and
# waits for socket_connect_timeout=5s each time, making 26 tests take 4+ min.
# Returning None instantly tells the app to skip Redis and go straight to DB.
_redis_none_patcher = patch("redis_cache.get_redis_client", return_value=None)
_redis_none_patcher.start()
# ─────────────────────────────────────────────────────────────────────────────

import pytest
from fastapi.testclient import TestClient

# Import after env vars and patches are in place
from main import app  # noqa: E402

CLIENT = TestClient(app, raise_server_exceptions=False)

USER_TOKEN  = "ci-user-token"
ADMIN_TOKEN = "ci-admin-token"
WRONG_TOKEN = "totally-wrong-token"


@pytest.fixture(scope="session", autouse=True)
def mock_db_and_cleanup():
    """
    Mock all DB-touching functions for the entire test session.

    sports_data.db is gitignored and does not exist in CI.
    Mocking these makes tests fully self-contained with no external files needed.

    Return values chosen so endpoints respond correctly:
      - get_cache_entry → None       → /cache returns 404 (not found, not a crash)
      - get_batch_cache_entries → {} → /cache/batch returns 200 with empty body
      - get_precision_batch_cache_entries → valid empty shape → 200
      - get_all_leagues → valid empty shape → 200
    """
    with (
        patch("main.get_cache_entry", return_value=None),
        patch("main.get_batch_cache_entries", return_value={}),
        patch("main.get_precision_batch_cache_entries", return_value={
            "results": [], "total_queries": 0, "successful": 0, "failed": 0,
        }),
        patch("main.get_all_leagues", return_value={"leagues": [], "total": 0}),
    ):
        yield
    _redis_none_patcher.stop()


def user_headers():
    return {"Authorization": f"Bearer {USER_TOKEN}"}


def admin_headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def wrong_headers():
    return {"Authorization": f"Bearer {WRONG_TOKEN}"}


# ─────────────────────────────────────────────────────────────────────────────
# Root
# ─────────────────────────────────────────────────────────────────────────────

class TestRoot:
    def test_root_is_online(self):
        r = CLIENT.get("/")
        assert r.status_code == 200

    def test_root_returns_status_online(self):
        r = CLIENT.get("/")
        assert r.json().get("status") == "online"

    def test_root_has_service_field(self):
        r = CLIENT.get("/")
        assert "service" in r.json()

    def test_root_has_version_field(self):
        r = CLIENT.get("/")
        assert "version" in r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Auth layer — /cache (user-auth endpoint)
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthLayer:
    def test_cache_no_token_returns_401_or_403(self):
        r = CLIENT.get("/cache", params={"market": "moneyline"})
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"

    def test_cache_wrong_token_returns_401_or_403(self):
        r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=wrong_headers())
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"

    def test_cache_valid_token_does_not_return_401_or_403(self):
        r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert r.status_code not in (401, 403), f"Valid token was rejected with {r.status_code}"

    def test_cache_valid_token_does_not_crash(self):
        r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"

    def test_batch_no_token_returns_401_or_403(self):
        r = CLIENT.post("/cache/batch", json={})
        assert r.status_code in (401, 403)

    def test_batch_wrong_token_returns_401_or_403(self):
        r = CLIENT.post("/cache/batch", json={}, headers=wrong_headers())
        assert r.status_code in (401, 403)

    def test_leagues_no_token_returns_401_or_403(self):
        r = CLIENT.get("/leagues")
        assert r.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────────────────
# Admin-only endpoints must reject non-admin tokens
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminOnly:
    def test_health_rejects_user_token(self):
        r = CLIENT.get("/health", headers=user_headers())
        assert r.status_code in (401, 403), f"User token should not access /health, got {r.status_code}"

    def test_health_accepts_admin_token(self):
        r = CLIENT.get("/health", headers=admin_headers())
        assert r.status_code not in (401, 403), f"Admin token was rejected from /health with {r.status_code}"

    def test_cache_stats_rejects_user_token(self):
        r = CLIENT.get("/cache/stats", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_cache_stats_accepts_admin_token(self):
        r = CLIENT.get("/cache/stats", headers=admin_headers())
        assert r.status_code not in (401, 403)

    def test_admin_logs_rejects_no_token(self):
        r = CLIENT.get("/admin/logs")
        assert r.status_code in (401, 403)


# ─────────────────────────────────────────────────────────────────────────────
# Input validation — bad params must return 400, not 500
# ─────────────────────────────────────────────────────────────────────────────

class TestInputValidation:
    def test_cache_missing_params_returns_400(self):
        """At least one of market/team/player/league is required."""
        r = CLIENT.get("/cache", headers=user_headers())
        assert r.status_code == 400, f"Expected 400 for missing params, got {r.status_code}"

    def test_cache_team_without_sport_returns_400(self):
        """Team-only search requires sport parameter."""
        r = CLIENT.get("/cache", params={"team": "Lakers"}, headers=user_headers())
        assert r.status_code == 400, f"Expected 400 for team without sport, got {r.status_code}"

    def test_cache_league_without_sport_returns_400(self):
        """League search requires sport parameter."""
        r = CLIENT.get("/cache", params={"league": "Premier League"}, headers=user_headers())
        assert r.status_code == 400, f"Expected 400 for league without sport, got {r.status_code}"

    def test_cache_market_query_does_not_crash(self):
        """Valid market query returns 200 or 404, never 500."""
        r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert r.status_code in (200, 404), f"Unexpected status {r.status_code}: {r.text[:200]}"

    def test_batch_empty_body_does_not_crash(self):
        """Empty batch body should not cause a server crash."""
        r = CLIENT.post("/cache/batch", json={}, headers=user_headers())
        assert r.status_code != 500, f"Server crashed on empty batch: {r.text[:200]}"

    def test_batch_valid_market_does_not_crash(self):
        r = CLIENT.post(
            "/cache/batch",
            json={"market": ["moneyline", "spread"]},
            headers=user_headers(),
        )
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"

    def test_leagues_does_not_crash_with_auth(self):
        r = CLIENT.get("/leagues", headers=user_headers())
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# Response shape checks
# ─────────────────────────────────────────────────────────────────────────────

class TestResponseShapes:
    def test_health_response_has_status_field(self):
        r = CLIENT.get("/health", headers=admin_headers())
        if r.status_code == 200:
            assert "status" in r.json()

    def test_cache_response_is_json(self):
        r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        # Should always return JSON regardless of result
        try:
            r.json()
        except Exception:
            pytest.fail("Response was not valid JSON")

    def test_batch_response_is_json(self):
        r = CLIENT.post(
            "/cache/batch",
            json={"market": ["moneyline"]},
            headers=user_headers(),
        )
        try:
            r.json()
        except Exception:
            pytest.fail("Batch response was not valid JSON")


# ─────────────────────────────────────────────────────────────────────────────
# /cache/batch/precision
# ─────────────────────────────────────────────────────────────────────────────

class TestPrecisionBatch:
    def test_no_token_returns_401_or_403(self):
        r = CLIENT.post("/cache/batch/precision", json={"queries": []})
        assert r.status_code in (401, 403)

    def test_wrong_token_returns_401_or_403(self):
        r = CLIENT.post("/cache/batch/precision", json={"queries": []}, headers=wrong_headers())
        assert r.status_code in (401, 403)

    def test_valid_token_does_not_crash(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"market": "moneyline"}]},
            headers=user_headers(),
        )
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"

    def test_response_has_results_key(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"market": "moneyline"}]},
            headers=user_headers(),
        )
        if r.status_code == 200:
            assert "results" in r.json()

    def test_response_has_total_queries_key(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"market": "moneyline"}]},
            headers=user_headers(),
        )
        if r.status_code == 200:
            assert "total_queries" in r.json()

    def test_multiple_queries_do_not_crash(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={
                "queries": [
                    {"market": "moneyline"},
                    {"team": "Lakers", "sport": "Basketball"},
                    {"player": "LeBron James"},
                ]
            },
            headers=user_headers(),
        )
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# /leagues with filters
# ─────────────────────────────────────────────────────────────────────────────

class TestLeagues:
    def test_all_leagues_returns_200(self):
        r = CLIENT.get("/leagues", headers=user_headers())
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"

    def test_leagues_response_is_json(self):
        r = CLIENT.get("/leagues", headers=user_headers())
        try:
            r.json()
        except Exception:
            pytest.fail("Leagues response was not valid JSON")

    def test_leagues_filter_by_sport_does_not_crash(self):
        r = CLIENT.get("/leagues", params={"sport": "Soccer"}, headers=user_headers())
        assert r.status_code != 500

    def test_leagues_filter_by_search_does_not_crash(self):
        r = CLIENT.get("/leagues", params={"search": "premier"}, headers=user_headers())
        assert r.status_code != 500

    def test_leagues_filter_by_region_does_not_crash(self):
        r = CLIENT.get("/leagues", params={"sport": "Soccer", "region": "Europe"}, headers=user_headers())
        assert r.status_code != 500


# ─────────────────────────────────────────────────────────────────────────────
# /cache/clear and /cache/invalidate (admin DELETE endpoints)
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminCacheManagement:
    def test_clear_rejects_no_token(self):
        r = CLIENT.request("DELETE", "/cache/clear")
        assert r.status_code in (401, 403)

    def test_clear_rejects_user_token(self):
        r = CLIENT.request("DELETE", "/cache/clear", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_clear_accepts_admin_token_does_not_crash(self):
        # With Redis mocked out, clear_all_cache() returns False → endpoint returns 500
        # with "Failed to clear cache". This is correct behaviour — not a code crash.
        # We only verify the admin token is accepted (not 401/403) and a JSON response is returned.
        r = CLIENT.request("DELETE", "/cache/clear", headers=admin_headers())
        assert r.status_code not in (401, 403), f"Admin token was rejected: {r.status_code}"
        try:
            r.json()
        except Exception:
            pytest.fail("Response was not valid JSON")

    def test_invalidate_rejects_no_token(self):
        r = CLIENT.request("DELETE", "/cache/invalidate", params={"market": "moneyline"})
        assert r.status_code in (401, 403)

    def test_invalidate_rejects_user_token(self):
        r = CLIENT.request("DELETE", "/cache/invalidate", params={"market": "moneyline"}, headers=user_headers())
        assert r.status_code in (401, 403)

    def test_invalidate_missing_params_returns_400(self):
        r = CLIENT.request("DELETE", "/cache/invalidate", headers=admin_headers())
        assert r.status_code == 400

    def test_invalidate_with_param_does_not_crash(self):
        r = CLIENT.request(
            "DELETE", "/cache/invalidate",
            params={"market": "moneyline"},
            headers=admin_headers(),
        )
        assert r.status_code not in (401, 403, 500), f"Unexpected: {r.status_code} {r.text[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# Admin log / session / stats / missing-items endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminInfoEndpoints:
    def test_logs_rejects_user_token(self):
        r = CLIENT.get("/admin/logs", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_logs_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/logs", headers=admin_headers())
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"

    def test_logs_with_limit_param(self):
        r = CLIENT.get("/admin/logs", params={"limit": 10, "offset": 0}, headers=admin_headers())
        assert r.status_code != 500

    def test_sessions_rejects_user_token(self):
        r = CLIENT.get("/admin/sessions", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_sessions_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/sessions", headers=admin_headers())
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"

    def test_admin_stats_cache_rejects_user_token(self):
        r = CLIENT.get("/admin/stats/cache", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_admin_stats_cache_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/stats/cache", headers=admin_headers())
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"

    def test_missing_items_rejects_user_token(self):
        r = CLIENT.get("/admin/missing-items", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_missing_items_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/missing-items", headers=admin_headers())
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"

    def test_missing_items_delete_rejects_user_token(self):
        r = CLIENT.request("DELETE", "/admin/missing-items", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_missing_items_delete_accepts_admin_does_not_crash(self):
        r = CLIENT.request("DELETE", "/admin/missing-items", headers=admin_headers())
        assert r.status_code != 500, f"Server crashed: {r.text[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# /admin/dashboard (cookie-based, no Bearer token)
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminDashboard:
    def test_dashboard_no_cookie_still_returns_200(self):
        """Dashboard is always served; JS handles auth so no cookie needed."""
        r = CLIENT.get("/admin/dashboard")
        assert r.status_code == 200

    def test_dashboard_wrong_cookie_still_returns_200(self):
        """Wrong cookie no longer gates the page; JS/localStorage owns auth."""
        r = CLIENT.get("/admin/dashboard", headers={"Cookie": "admin_access=wrong-token"})
        assert r.status_code == 200

    def test_dashboard_valid_cookie_returns_200(self):
        r = CLIENT.get("/admin/dashboard", headers={"Cookie": f"admin_access={ADMIN_TOKEN}"})
        assert r.status_code == 200

    def test_dashboard_login_wrong_token_returns_403(self):
        r = CLIENT.post(
            "/admin/dashboard/login",
            data={"admin_token": "wrong"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 403

    def test_dashboard_login_valid_token_redirects(self):
        r = CLIENT.post(
            "/admin/dashboard/login",
            data={"admin_token": ADMIN_TOKEN},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), f"Expected redirect, got {r.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# /docs and /openapi.json
# ─────────────────────────────────────────────────────────────────────────────

class TestDocsEndpoints:
    def test_docs_returns_200(self):
        r = CLIENT.get("/docs")
        assert r.status_code == 200

    def test_openapi_json_returns_200(self):
        r = CLIENT.get("/openapi.json")
        assert r.status_code == 200

    def test_openapi_json_is_valid_json(self):
        r = CLIENT.get("/openapi.json")
        data = r.json()
        assert "paths" in data, "openapi.json missing 'paths' key"
        assert "info" in data, "openapi.json missing 'info' key"

    def test_openapi_json_with_admin_cookie_shows_admin_paths(self):
        r = CLIENT.get("/openapi.json", headers={"Cookie": f"admin_access={ADMIN_TOKEN}"})
        data = r.json()
        paths = data.get("paths", {})
        assert any("admin" in p for p in paths), "Admin paths not visible with admin cookie"


# ─────────────────────────────────────────────────────────────────────────────
# Static source file safety — wrong ports and production URLs
# ─────────────────────────────────────────────────────────────────────────────

import re
from pathlib import Path

# Ports explicitly allowed to be hardcoded in source files
_ALLOWED_PORTS = {
    5000,   # production API port
    6379,   # Redis default
    6380,   # Redis failover/sentinel
}

# Production URL patterns that must NOT appear in source files
_FORBIDDEN_URL_PATTERNS = [
    r"eternitylabs\.co",
    r"cache-api\.\w+\.co",
]

# The 5 core source files to scan
_SOURCE_FILES = ["main.py", "redis_cache.py", "request_tracking.py", "cache_db.py", "deploy.sh"]


class TestSourceFileSafety:
    """
    Static scans of the 5 core source files.
    Catches hardcoded wrong ports and hardcoded production URLs
    before they can break the production deployment.
    """

    def _read(self, filename: str) -> str:
        p = Path(filename)
        if not p.exists():
            pytest.skip(f"{filename} not present in this environment")
        return p.read_text(encoding="utf-8")

    def _check_ports(self, filename: str, pattern: str):
        content = self._read(filename)
        found = {int(m) for m in re.findall(pattern, content, re.IGNORECASE)}
        bad = found - _ALLOWED_PORTS
        assert not bad, (
            f"{filename} contains hardcoded port(s) {bad} outside the allowed set {_ALLOWED_PORTS}.\n"
            f"Use environment variables (e.g. os.getenv('API_PORT', '5000')) instead of hardcoding ports."
        )

    def _check_urls(self, filename: str):
        content = self._read(filename)
        for pattern in _FORBIDDEN_URL_PATTERNS:
            hits = re.findall(pattern, content)
            assert not hits, (
                f"{filename} contains a hardcoded production URL matching '{pattern}': {hits}\n"
                f"Production URLs must only appear in testing.py or be read from environment variables."
            )

    def test_main_py_no_wrong_ports(self):
        self._check_ports("main.py", r"(?:port\s*=\s*|:)(\d{4,5})\b")

    def test_redis_cache_py_no_wrong_ports(self):
        self._check_ports("redis_cache.py", r"(?:port\s*=\s*|:)(\d{4,5})\b")

    def test_request_tracking_py_no_wrong_ports(self):
        self._check_ports("request_tracking.py", r"(?:port\s*=\s*|:)(\d{4,5})\b")

    def test_cache_db_py_no_wrong_ports(self):
        self._check_ports("cache_db.py", r"(?:port\s*=\s*|:)(\d{4,5})\b")

    def test_deploy_sh_no_wrong_ports(self):
        # In shell, default port values appear as :-5000 or port=5000
        self._check_ports("deploy.sh", r"(?::-|port\s*=\s*[\"']?)(\d{4,5})\b")

    def test_main_py_no_hardcoded_production_url(self):
        self._check_urls("main.py")

    def test_redis_cache_py_no_hardcoded_production_url(self):
        self._check_urls("redis_cache.py")

    def test_request_tracking_py_no_hardcoded_production_url(self):
        self._check_urls("request_tracking.py")

    def test_cache_db_py_no_hardcoded_production_url(self):
        self._check_urls("cache_db.py")

    def test_deploy_sh_no_hardcoded_production_url(self):
        self._check_urls("deploy.sh")


# ─────────────────────────────────────────────────────────────────────────────
# Source file coverage snapshot — fails when new functions/endpoints are added
# ─────────────────────────────────────────────────────────────────────────────
#
# HOW TO FIX A FAILURE:
#   1. Add tests in test_pr.py covering the new endpoint or function.
#   2. Add the new item to the relevant KNOWN_* set below.
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_ENDPOINTS = {
    # (HTTP_METHOD, "/path")
    ("GET",    "/"),
    ("GET",    "/health"),
    ("GET",    "/cache/stats"),
    ("DELETE", "/cache/clear"),
    ("DELETE", "/cache/invalidate"),
    ("GET",    "/cache"),
    ("GET",    "/event/check"),
    ("POST",   "/cache/batch"),
    ("POST",   "/cache/batch/precision"),
    ("GET",    "/leagues"),
    ("GET",    "/admin/logs"),
    ("GET",    "/admin/sessions"),
    ("GET",    "/admin/stats/cache"),
    ("GET",    "/admin/missing-items"),
    ("DELETE", "/admin/missing-items"),
    ("GET",    "/admin/dashboard"),
    ("POST",   "/admin/dashboard/login"),
    ("GET",    "/docs"),
    ("GET",    "/openapi.json"),
    # Token management
    ("GET",    "/admin/tokens"),
    ("POST",   "/admin/tokens"),
    ("PUT",    "/admin/tokens/{token_id}/revoke"),
    ("POST",   "/admin/tokens/{token_id}/rotate"),
    ("GET",    "/admin/tokens/audit"),
    # Analytics
    ("GET",    "/admin/analytics/failures"),
    ("GET",    "/admin/analytics/latency"),
    ("GET",    "/admin/analytics/signatures"),
    ("GET",    "/admin/analytics/trends"),
}

KNOWN_CACHE_DB_FUNCTIONS = {
    "get_db_connection", "normalize_key", "get_league_priority",
    "expand_sports_terms", "get_cache_entry", "get_all_teams",
    "get_all_players", "_chunk_list", "_resolve_batch_teams",
    "_resolve_batch_players", "_resolve_bulk_markets",
    "get_batch_cache_entries", "get_precision_batch_cache_entries",
    "get_all_leagues",
}

KNOWN_REDIS_CACHE_FUNCTIONS = {
    "_iter_cache_keys", "_count_cache_keys", "get_redis_client",
    "generate_cache_key", "get_cached_data", "set_cached_data",
    "invalidate_cache", "clear_all_cache", "get_cache_stats",
}

KNOWN_REQUEST_TRACKING_FUNCTIONS = {
    "get_location_from_ip", "get_db_connection", "init_tracking",
    "create_session", "get_or_create_session", "track_request",
    "get_request_logs", "get_session_summary", "get_session_details",
    "clear_old_sessions", "track_missing_item", "get_missing_items",
    "clear_missing_items",
    # Token management
    "_hash_token", "_mask_token", "seed_env_tokens", "create_managed_token",
    "get_all_tokens", "revoke_token", "rotate_token", "verify_db_token",
    "is_admin_db_token", "log_token_use", "get_token_audit",
    # Analytics
    "get_failure_analytics", "get_latency_stats",
    "get_top_failing_signatures", "get_request_trends",
}

KNOWN_DEPLOY_SH_VARS = {
    "SERVICE_NAME", "SERVICE_DIR", "VENV_DIR", "SERVICE_FILE",
    "REPO_URL", "DEPLOY_BRANCH", "API_PORT", "EXPECTED_REPO_SLUG",
    "PREVIOUS_SERVICE_NAME", "SOURCE_REPO_SLUG", "PRIMARY_REPO_SLUG",
    "ALLOW_PRIMARY_SERVICE_NAME", "PRODUCTION_SERVICE_NAME",
    "PRODUCTION_PORT", "NGINX_SITE_NAME", "PROTECTED_NGINX_SITE_NAME",
    "REQUIRE_UNIQUE_NAME", "LOCK_FILE",
    # ANSI colour helpers (not deployment config)
    "RED", "GREEN", "YELLOW", "NC",
}


def _scan_endpoints(filepath: str) -> set:
    """Extract all (METHOD, /path) pairs from @app.{method}("/path") decorators."""
    content = Path(filepath).read_text(encoding="utf-8")
    pattern = re.compile(r'@app\.(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']', re.IGNORECASE)
    return {(m.group(1).upper(), m.group(2)) for m in pattern.finditer(content)}


def _scan_functions(filepath: str) -> set:
    """Extract all top-level def names from a Python file."""
    content = Path(filepath).read_text(encoding="utf-8")
    return {m.group(1) for m in re.finditer(r'^(?:async\s+)?def\s+(\w+)\s*\(', content, re.MULTILINE)}


def _scan_deploy_vars(filepath: str) -> set:
    """Extract all UPPER_CASE variable assignments from deploy.sh."""
    content = Path(filepath).read_text(encoding="utf-8")
    return {m.group(1) for m in re.finditer(r'^([A-Z_][A-Z0-9_]+)\s*=', content, re.MULTILINE)}


# ─────────────────────────────────────────────────────────────────────────────
# Token management endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenManagement:
    def test_list_tokens_rejects_no_token(self):
        r = CLIENT.get("/admin/tokens")
        assert r.status_code in (401, 403)

    def test_list_tokens_rejects_user_token(self):
        r = CLIENT.get("/admin/tokens", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_list_tokens_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/tokens", headers=admin_headers())
        assert r.status_code not in (401, 403, 500), f"Unexpected: {r.status_code} {r.text[:200]}"

    def test_list_tokens_response_is_json(self):
        r = CLIENT.get("/admin/tokens", headers=admin_headers())
        if r.status_code == 200:
            try:
                r.json()
            except Exception:
                pytest.fail("Response was not valid JSON")

    def test_create_token_rejects_user_token(self):
        r = CLIENT.post("/admin/tokens", json={"name": "t", "role": "user"}, headers=user_headers())
        assert r.status_code in (401, 403)

    def test_create_token_missing_name_returns_422(self):
        r = CLIENT.post("/admin/tokens", json={"role": "user"}, headers=admin_headers())
        assert r.status_code == 422

    def test_create_token_invalid_role_returns_422(self):
        r = CLIENT.post("/admin/tokens", json={"name": "pr-test", "role": "superuser"}, headers=admin_headers())
        assert r.status_code == 422

    def test_create_token_bad_expires_at_returns_422(self):
        r = CLIENT.post("/admin/tokens", json={"name": "pr-test", "role": "user", "expires_at": "NOT-A-DATE"}, headers=admin_headers())
        assert r.status_code == 422

    def test_create_and_revoke_lifecycle(self):
        """Create a token then immediately revoke it — verifies full lifecycle."""
        r = CLIENT.post("/admin/tokens", json={"name": "pr-lifecycle-test", "role": "user"}, headers=admin_headers())
        assert r.status_code == 200, f"Create failed: {r.status_code} {r.text[:200]}"
        data = r.json()
        token_id = data.get("token_id")
        assert token_id, "No token_id in response"
        # Revoke it
        rv = CLIENT.put(f"/admin/tokens/{token_id}/revoke", json={"reason": "pr test cleanup"}, headers=admin_headers())
        assert rv.status_code == 200, f"Revoke failed: {rv.status_code}"

    def test_revoke_nonexistent_token_returns_404(self):
        r = CLIENT.put("/admin/tokens/99999999/revoke", json={"reason": "test"}, headers=admin_headers())
        assert r.status_code == 404

    def test_revoke_rejects_user_token(self):
        r = CLIENT.put("/admin/tokens/1/revoke", json={"reason": "test"}, headers=user_headers())
        assert r.status_code in (401, 403)

    def test_rotate_rejects_user_token(self):
        r = CLIENT.post("/admin/tokens/1/rotate", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_audit_log_rejects_user_token(self):
        r = CLIENT.get("/admin/tokens/audit", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_audit_log_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/tokens/audit", headers=admin_headers())
        assert r.status_code not in (401, 403, 500), f"Unexpected: {r.status_code} {r.text[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# Analytics endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyticsEndpoints:
    def test_failures_rejects_user_token(self):
        r = CLIENT.get("/admin/analytics/failures", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_failures_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/analytics/failures", headers=admin_headers())
        assert r.status_code not in (401, 403, 500), f"Unexpected: {r.status_code} {r.text[:200]}"

    def test_latency_rejects_user_token(self):
        r = CLIENT.get("/admin/analytics/latency", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_latency_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/analytics/latency", headers=admin_headers())
        assert r.status_code not in (401, 403, 500), f"Unexpected: {r.status_code} {r.text[:200]}"

    def test_signatures_rejects_user_token(self):
        r = CLIENT.get("/admin/analytics/signatures", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_signatures_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/analytics/signatures", headers=admin_headers())
        assert r.status_code not in (401, 403, 500), f"Unexpected: {r.status_code} {r.text[:200]}"

    def test_trends_rejects_user_token(self):
        r = CLIENT.get("/admin/analytics/trends", headers=user_headers())
        assert r.status_code in (401, 403)

    def test_trends_accepts_admin_does_not_crash(self):
        r = CLIENT.get("/admin/analytics/trends", headers=admin_headers())
        assert r.status_code not in (401, 403, 500), f"Unexpected: {r.status_code} {r.text[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# Batch size limiting (DoS protection)
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchSizeLimit:
    def test_precision_batch_at_max_size_is_accepted(self):
        """Exactly 20 queries (the limit) must be accepted."""
        queries = [{"market": "moneyline"}] * 20
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": queries},
            headers=user_headers(),
        )
        assert r.status_code not in (422, 500), (
            f"Max-size batch was unexpectedly rejected: {r.status_code} {r.text[:200]}"
        )

    def test_precision_batch_over_limit_returns_422(self):
        """21 queries must be rejected with 422."""
        queries = [{"market": "moneyline"}] * 21
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": queries},
            headers=user_headers(),
        )
        assert r.status_code == 422, (
            f"Expected 422 for oversized batch, got {r.status_code}: {r.text[:200]}"
        )

    def test_precision_batch_over_limit_message_mentions_limit(self):
        """The 422 response body must mention the size limit."""
        queries = [{"market": "moneyline"}] * 25
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": queries},
            headers=user_headers(),
        )
        assert r.status_code == 422
        body = r.text
        assert any(word in body for word in ("20", "Maximum", "maximum", "queries")), (
            f"422 message should mention the limit: {body[:300]}"
        )

    def test_precision_batch_empty_queries_accepted(self):
        """Empty queries list [] must be accepted (returns empty result)."""
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": []},
            headers=user_headers(),
        )
        assert r.status_code in (200,), (
            f"Empty queries should return 200, got {r.status_code}: {r.text[:200]}"
        )

    def test_precision_batch_counters_are_consistent(self):
        """successful + failed must equal total_queries in any response."""
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"market": "moneyline"}, {"player": "LeBron James"}]},
            headers=user_headers(),
        )
        if r.status_code == 200:
            data = r.json()
            succ = data.get("successful", 0)
            fail = data.get("failed", 0)
            total = data.get("total_queries", 0)
            assert succ + fail == total, (
                f"successful({succ}) + failed({fail}) != total_queries({total})"
            )


# ─────────────────────────────────────────────────────────────────────────────
# /cache/batch schema validation
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchSchemaValidation:
    def test_wrong_schema_precision_format_returns_422(self):
        """{queries:[...]} sent to /cache/batch must be rejected, not silently {}."""
        r = CLIENT.post(
            "/cache/batch",
            json={"queries": [{"player": "LeBron James", "sport": "basketball"}]},
            headers=user_headers(),
        )
        assert r.status_code == 422, (
            f"Expected 422 for wrong schema (queries key), got {r.status_code}: {r.text[:200]}"
        )

    def test_sport_only_body_returns_422(self):
        """sport alone with no list field must be rejected."""
        r = CLIENT.post(
            "/cache/batch",
            json={"sport": "basketball"},
            headers=user_headers(),
        )
        assert r.status_code == 422, (
            f"sport-only body should be 422, got {r.status_code}: {r.text[:200]}"
        )

    def test_all_empty_lists_returns_200(self):
        """All-empty lists {team:[], player:[], ...} is valid — returns 200 with {}."""
        r = CLIENT.post(
            "/cache/batch",
            json={"team": [], "player": [], "market": [], "league": []},
            headers=user_headers(),
        )
        assert r.status_code == 200, (
            f"All-empty lists should return 200, got {r.status_code}: {r.text[:200]}"
        )

    def test_single_list_field_accepted(self):
        """A body with just one list key (e.g., market) is valid."""
        r = CLIENT.post(
            "/cache/batch",
            json={"market": ["moneyline"]},
            headers=user_headers(),
        )
        assert r.status_code not in (422, 500), (
            f"Single-field batch rejected: {r.status_code}: {r.text[:200]}"
        )

    def test_precision_batch_missing_queries_key_returns_422(self):
        """No queries key at all → 422 from Pydantic."""
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"player": "LeBron James"},
            headers=user_headers(),
        )
        assert r.status_code == 422, (
            f"Expected 422 for missing queries key, got {r.status_code}: {r.text[:200]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# include_stats query parameter on GET /cache
# ─────────────────────────────────────────────────────────────────────────────

class TestIncludeStats:
    def test_invalid_bool_returns_422(self):
        """include_stats=notabool must be rejected by FastAPI's type coercion."""
        r = CLIENT.get(
            "/cache",
            params={"market": "moneyline", "include_stats": "notabool"},
            headers=user_headers(),
        )
        assert r.status_code == 422, (
            f"Expected 422 for invalid bool, got {r.status_code}"
        )

    def test_default_no_stats_key_in_response(self):
        """Default include_stats=false: stats and stats_unavailable must be absent."""
        with patch("main.get_cache_entry", return_value={"type": "market", "normalized_name": "Moneyline"}):
            r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert r.status_code == 200
        data = r.json()
        assert "stats" not in data, "stats key must not appear when include_stats=false"
        assert "stats_unavailable" not in data, "stats_unavailable must not appear by default"

    def test_include_stats_true_bridge_none_adds_unavailable(self):
        """When _sports_bridge is None, include_stats=true → stats_unavailable:true."""
        with (
            patch("main.get_cache_entry", return_value={"type": "market", "normalized_name": "Moneyline"}),
            patch("main._sports_bridge", None),
        ):
            r = CLIENT.get(
                "/cache",
                params={"market": "moneyline", "include_stats": "true"},
                headers=user_headers(),
            )
        assert r.status_code == 200
        assert r.json().get("stats_unavailable") is True, (
            f"Expected stats_unavailable=true when bridge is None: {r.json()}"
        )

    def test_include_stats_true_bridge_returns_none_adds_unavailable(self):
        """When bridge returns None (no data found), stats_unavailable:true is set."""
        async def _null_enrich(*a, **kw):
            return None

        mock_bridge = MagicMock()
        mock_bridge.enrich = _null_enrich
        with (
            patch("main.get_cache_entry", return_value={"type": "market", "normalized_name": "Moneyline"}),
            patch("main._sports_bridge", mock_bridge),
        ):
            r = CLIENT.get(
                "/cache",
                params={"market": "moneyline", "include_stats": "true"},
                headers=user_headers(),
            )
        assert r.status_code == 200
        assert r.json().get("stats_unavailable") is True, (
            f"Expected stats_unavailable=true when bridge returns None: {r.json()}"
        )

    def test_include_stats_true_bridge_returns_data_adds_stats_key(self):
        """When bridge returns data, stats key is present and stats_unavailable is absent."""
        mock_stats = {"source": "sports_api", "player": {"name": "LeBron James", "games": []}}

        async def _enrich(*a, **kw):
            return mock_stats

        mock_bridge = MagicMock()
        mock_bridge.enrich = _enrich
        with (
            patch("main.get_cache_entry", return_value={"type": "player", "normalized_name": "LeBron James"}),
            patch("main._sports_bridge", mock_bridge),
        ):
            r = CLIENT.get(
                "/cache",
                params={"player": "LeBron James", "include_stats": "true"},
                headers=user_headers(),
            )
        assert r.status_code == 200
        data = r.json()
        assert "stats" in data, f"Expected stats key when bridge returns data: {data}"
        assert "stats_unavailable" not in data, "stats_unavailable must not appear when stats is present"


class TestEventCheck:
    def test_event_check_requires_locator(self):
        r = CLIENT.get(
            "/event/check",
            params={"market": "total", "pick": "over", "line": 5.5},
            headers=user_headers(),
        )
        assert r.status_code == 400

    def test_event_check_bridge_unavailable_returns_503(self):
        with patch("main._sports_bridge", None):
            r = CLIENT.get(
                "/event/check",
                params={
                    "date": "2026-03-12",
                    "team": "PSG",
                    "opponent": "Chelsea",
                    "market": "total",
                    "pick": "over",
                    "line": 5.5,
                },
                headers=user_headers(),
            )
        assert r.status_code == 503

    def test_event_check_bridge_returns_payload(self):
        mock_result = {
            "found": True,
            "market": "total",
            "pick": "over",
            "line": 5.5,
            "result": True,
            "outcome": "win",
        }

        async def _market_check(*a, **kw):
            return mock_result

        mock_bridge = MagicMock()
        mock_bridge.market_check = _market_check

        with patch("main._sports_bridge", mock_bridge):
            r = CLIENT.get(
                "/event/check",
                params={
                    "date": "2026-03-12",
                    "team": "PSG",
                    "opponent": "Chelsea",
                    "market": "total",
                    "pick": "over",
                    "line": 5.5,
                },
                headers=user_headers(),
            )
        assert r.status_code == 200
        assert r.json().get("result") is True
        assert r.json().get("outcome") == "win"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP method enforcement
# ─────────────────────────────────────────────────────────────────────────────

class TestHTTPMethodEnforcement:
    def test_cache_post_returns_405(self):
        r = CLIENT.post("/cache", json={}, headers=user_headers())
        assert r.status_code == 405, f"Expected 405 for POST /cache, got {r.status_code}"

    def test_cache_delete_returns_405(self):
        r = CLIENT.delete("/cache", headers=admin_headers())
        assert r.status_code == 405, f"Expected 405 for DELETE /cache, got {r.status_code}"

    def test_cache_put_returns_405(self):
        r = CLIENT.put("/cache", json={}, headers=admin_headers())
        assert r.status_code == 405, f"Expected 405 for PUT /cache, got {r.status_code}"

    def test_cache_batch_get_returns_405(self):
        r = CLIENT.get("/cache/batch", headers=user_headers())
        assert r.status_code == 405, f"Expected 405 for GET /cache/batch, got {r.status_code}"

    def test_cache_batch_precision_get_returns_405(self):
        r = CLIENT.get("/cache/batch/precision", headers=user_headers())
        assert r.status_code == 405, f"Expected 405 for GET /cache/batch/precision, got {r.status_code}"

    def test_leagues_post_returns_405(self):
        r = CLIENT.post("/leagues", json={}, headers=user_headers())
        assert r.status_code == 405, f"Expected 405 for POST /leagues, got {r.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# Input edge cases — injection, unicode, extreme lengths
# ─────────────────────────────────────────────────────────────────────────────

class TestInputEdgeCases:
    def test_sql_injection_in_player_does_not_crash(self):
        r = CLIENT.get(
            "/cache",
            params={"player": "' OR 1=1--", "sport": "basketball"},
            headers=user_headers(),
        )
        assert r.status_code in (200, 404), f"SQL injection unexpected status: {r.status_code}"
        assert r.status_code != 500, "SQL injection caused server crash"

    def test_xss_payload_in_player_does_not_crash(self):
        r = CLIENT.get(
            "/cache",
            params={"player": "<script>alert(1)</script>", "sport": "basketball"},
            headers=user_headers(),
        )
        assert r.status_code in (200, 404), f"XSS payload unexpected status: {r.status_code}"
        assert r.status_code != 500, "XSS payload caused server crash"

    def test_unicode_player_name_does_not_crash(self):
        r = CLIENT.get(
            "/cache",
            params={"player": "Ñoño Ünïcödé Dąbrowskï", "sport": "soccer"},
            headers=user_headers(),
        )
        assert r.status_code in (200, 404), f"Unicode caused crash: {r.status_code}"

    def test_very_long_player_name_does_not_crash(self):
        r = CLIENT.get(
            "/cache",
            params={"player": "A" * 200, "sport": "basketball"},
            headers=user_headers(),
        )
        assert r.status_code in (200, 404), f"Long name unexpected status: {r.status_code}"
        assert r.status_code != 500, "Long name caused server crash"

    def test_precision_batch_sql_injection_does_not_crash(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"player": "' OR 1=1--", "sport": "basketball"}]},
            headers=user_headers(),
        )
        assert r.status_code != 500, f"SQL injection in precision batch crashed: {r.text[:200]}"

    def test_batch_multiple_injection_payloads_do_not_crash(self):
        r = CLIENT.post(
            "/cache/batch",
            json={"player": ["' OR 1=1--", "<script>x</script>", "A" * 150], "sport": "basketball"},
            headers=user_headers(),
        )
        assert r.status_code != 500, f"Injection payloads in batch crashed: {r.text[:200]}"

    def test_precision_batch_empty_string_fields_do_not_crash(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"player": "", "sport": "basketball"}]},
            headers=user_headers(),
        )
        assert r.status_code != 500, f"Empty string in batch crashed: {r.text[:200]}"

    def test_xml_content_type_to_batch_returns_422(self):
        """Non-JSON content type must be rejected, not crash."""
        r = CLIENT.post(
            "/cache/batch/precision",
            content=b"<query><player>LeBron</player></query>",
            headers={**user_headers(), "Content-Type": "text/xml"},
        )
        assert r.status_code == 422, f"Expected 422 for XML body, got {r.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# Response shape when result IS found
# ─────────────────────────────────────────────────────────────────────────────

class TestFoundResponseShape:
    _MOCK_MARKET = {
        "type": "market",
        "normalized_name": "Moneyline",
        "market_type_id": 7,
        "sports": ["Basketball"],
    }

    def test_found_response_has_found_true(self):
        with patch("main.get_cache_entry", return_value=self._MOCK_MARKET):
            r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert r.status_code == 200
        assert r.json().get("found") is True

    def test_found_response_has_data_key(self):
        with patch("main.get_cache_entry", return_value=self._MOCK_MARKET):
            r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert "data" in r.json()

    def test_found_response_has_query_key(self):
        with patch("main.get_cache_entry", return_value=self._MOCK_MARKET):
            r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert "query" in r.json()

    def test_not_found_response_has_found_false(self):
        # session mock already returns None → 404
        r = CLIENT.get("/cache", params={"market": "moneyline"}, headers=user_headers())
        assert r.status_code == 404
        assert r.json().get("found") is False

    def test_precision_batch_result_items_have_required_fields(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"market": "moneyline"}]},
            headers=user_headers(),
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                item = results[0]
                assert "query" in item, "Each precision result must have a 'query' field"
                assert "found" in item, "Each precision result must have a 'found' field"
                assert "data" in item, "Each precision result must have a 'data' field"

    def test_precision_batch_has_all_counter_fields(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"market": "moneyline"}]},
            headers=user_headers(),
        )
        if r.status_code == 200:
            data = r.json()
            for key in ("results", "total_queries", "successful", "failed"):
                assert key in data, f"Missing key '{key}' in precision batch response"

    def test_precision_batch_counters_are_consistent(self):
        r = CLIENT.post(
            "/cache/batch/precision",
            json={"queries": [{"market": "moneyline"}, {"player": "LeBron James"}]},
            headers=user_headers(),
        )
        if r.status_code == 200:
            data = r.json()
            succ = data.get("successful", 0)
            fail = data.get("failed", 0)
            total = data.get("total_queries", 0)
            assert succ + fail == total, (
                f"successful({succ}) + failed({fail}) != total_queries({total})"
            )


class TestSourceFileCoverage:
    """
    Snapshot tests: fail when new endpoints or functions are added to key files
    without corresponding tests being written.

    If this test fails on your PR:
      1. Add tests in test_pr.py for the new endpoint/function.
      2. Add the new item to the relevant KNOWN_* set in this file.
    """

    def _assert_snapshot(self, label: str, actual: set, known: set):
        new = actual - known
        removed = known - actual
        assert not new, (
            f"\n❌ NEW items in {label} — add tests, then add these to the KNOWN set:\n"
            + "\n".join(f"  {i}" for i in sorted(str(x) for x in new))
        )
        assert not removed, (
            f"\n⚠️  Items removed from {label} but still in the KNOWN set — remove them:\n"
            + "\n".join(f"  {i}" for i in sorted(str(x) for x in removed))
        )

    def test_main_py_no_new_endpoints(self):
        if not Path("main.py").exists():
            pytest.skip("main.py not present")
        self._assert_snapshot("main.py endpoints", _scan_endpoints("main.py"), KNOWN_ENDPOINTS)

    def test_cache_db_py_no_new_functions(self):
        if not Path("cache_db.py").exists():
            pytest.skip("cache_db.py not present")
        self._assert_snapshot("cache_db.py functions", _scan_functions("cache_db.py"), KNOWN_CACHE_DB_FUNCTIONS)

    def test_redis_cache_py_no_new_functions(self):
        if not Path("redis_cache.py").exists():
            pytest.skip("redis_cache.py not present")
        self._assert_snapshot("redis_cache.py functions", _scan_functions("redis_cache.py"), KNOWN_REDIS_CACHE_FUNCTIONS)

    def test_request_tracking_py_no_new_functions(self):
        if not Path("request_tracking.py").exists():
            pytest.skip("request_tracking.py not present")
        self._assert_snapshot("request_tracking.py functions", _scan_functions("request_tracking.py"), KNOWN_REQUEST_TRACKING_FUNCTIONS)

    def test_deploy_sh_no_new_config_vars(self):
        if not Path("deploy.sh").exists():
            pytest.skip("deploy.sh not present")
        self._assert_snapshot("deploy.sh variables", _scan_deploy_vars("deploy.sh"), KNOWN_DEPLOY_SH_VARS)
