# Cache API

FastAPI service for sports cache normalization with Redis-backed caching, batch lookup support, request/session tracking, missing-item telemetry, GeoIP enrichment, and VPS auto-deployment.

## What this service does

- Normalizes cache lookups for `market`, `team`, `player`, and `league`
- Supports single query, standard batch, and precision batch modes
- Tracks every non-admin request with session continuity, GeoIP location, and per-request UUID
- Records items not found in the database so gaps can be identified and prioritized
- Provides full **token governance**: create, revoke, rotate, and audit DB-managed API tokens with SHA-256 hashing and masked display
- Exposes **request failure analytics**: failure heatmaps, latency stats, top failing query signatures, and request trends
- Exposes admin-only endpoints for cache management, request/session monitoring, and missing-item reports
- Serves a browser-based admin dashboard (8 tabs: Overview, Sessions, API Tester, Server Logs, Missing Data, Tokens, Analytics, Settings)
- Uses Redis for caching and SQLite for both lookup data and telemetry storage
- Deploys to VPS through GitHub Actions + `deploy.sh` with fork-safety guards and retry logic
- **Optional stats enrichment**: `GET /cache?include_stats=true` transparently fetches historical and live statistics from the internal `stats_api` service and merges them into the response — zero overhead when not configured
- Exposes `GET /event/check` to evaluate a single event market (`moneyline`, `spread`, `total`) against historical or live data via the internal stats service

## Tech stack

- FastAPI + Uvicorn
- Redis (`redis`, `hiredis`)
- SQLite (sports data + request tracking + UUID tracking)
- `geoip2` + MaxMind GeoLite2-City database (local IP geolocation for request logs)
- `python-multipart` (form-based dashboard login)
- systemd service management on VPS
- Nginx reverse proxy on VPS

## Project structure

```
main.py               # FastAPI app, all routes, middleware, startup logic
cache_db.py           # SQLite query layer (sports_data.db) with Redis cache integration
redis_cache.py        # Redis client, cache key generation, stats, clear, invalidate
sports_bridge.py      # Optional async enrichment bridge to the internal stats_api service
request_tracking.py   # SQLite-backed request/session/missing-item tracking
uuid_tracking.py      # UUID-based login tracking with geo-location via ip-api.com
testing.py            # Full endpoint validation runner (local vs prod)
test_pr.py            # Automated pytest suite for PRs (no live server needed)
deploy.sh             # VPS deployment script (systemd, nginx, Redis)
dashboard.html        # Admin dashboard (served at /admin/dashboard)
js/app.js             # Dashboard frontend JavaScript
css/style.css         # Dashboard styles
geoip/GeoLite2-City.mmdb  # MaxMind GeoLite2 database for IP geolocation
.github/workflows/
  deploy.yml          # CI/CD: validate + deploy on push to main
  cleanup.yml         # Auto-delete head branch on PR close
```

## Data stores

| File                       | Purpose                                                                                  |
| -------------------------- | ---------------------------------------------------------------------------------------- |
| `sports_data.db`           | Primary SQLite database for markets, teams, players, leagues (WAL mode, 10 MB cache)     |
| `request_logs/requests.db` | Request telemetry: sessions, per-request logs, missing-item records, tokens, token audit |
| `uuid_tracking.db`         | UUID login tracking with full geo-location data per visit                                |

## Quick start (local)

1. Create and activate virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create local env file:

```bash
cp .env.example .env
```

4. Set at least:

- `API_TOKEN`
- `ADMIN_API_TOKEN` (or fallback `ADMIN_TOKEN`)

5. Run API:

```bash
python main.py
```

Default local URL:

```text
http://localhost:5000
```

On Windows, if `REDIS_HOST` is not set the app automatically locates and starts any Docker container whose name or image contains `redis`. If none exists, it creates one named `local-redis` mapping port `6379`.

## Authentication

Protected endpoints require:

```text
Authorization: Bearer <token>
```

Two token sources are supported, checked in order:

1. **Environment-variable tokens** — `API_TOKEN` (user) and `ADMIN_API_TOKEN` / `ADMIN_TOKEN` (admin). Fast-path checked first.
2. **Database-managed tokens** — created via `POST /admin/tokens`, stored as SHA-256 hashes in `request_logs/requests.db`. Support expiry dates, revocation, rotation, and per-token audit logs.

If no valid tokens are configured, protected routes return a `500` error. Token values are `.strip()`-ed on load to avoid whitespace issues.

### Admin dashboard login

The dashboard at `/admin/dashboard` uses cookie-based auth. Submit the admin token via the login form:

```
POST /admin/dashboard/login
Content-Type: application/x-www-form-urlencoded

admin_token=<admin_token>
```

On success, a `HttpOnly; Secure; SameSite=Strict` cookie named `admin_access` is set (1-hour lifetime) and the browser is redirected to the dashboard. The Swagger UI at `/docs` also reads this cookie to reveal admin-tagged routes in the OpenAPI spec.

## Rate limiting

- Per-IP in-memory limiter
- Controlled by `RATE_LIMIT_PER_MINUTE` (default `60`)
- Applied to user API routes (`/cache`, `/cache/batch`, `/cache/batch/precision`, `/leagues`, `/event/check`)
- Returns `429 Too Many Requests` when exceeded

## Request tracking and observability

Every non-admin request is tracked automatically via HTTP middleware:

- A stable `session_id` (UUID) is assigned per token, persisted across requests
- A per-request `uuid` is derived deterministically from the token using `uuid5`
- IP address is resolved to a city/country via the local GeoLite2 database
- Request method, path, query params, body, response status, and latency are stored
- Items that return `null` (not found in DB) are recorded in a `missing_items` table with deduplication, `occurrence_count`, and `request_group_id` for grouping per-request

Admin requests are not tracked (no session created, no request record written).

### UUID tracking

`uuid_tracking.py` maintains a separate `uuid_tracking.db` that records every bearer-token appearance with full geo-location from the `ip-api.com` API (HTTPS, free tier: 45 req/min). Private/loopback IPs are handled gracefully and not submitted to the external API.

## GeoIP

Two separate geo-lookup mechanisms are used:

| Used in               | Source                     | Method                                                       |
| --------------------- | -------------------------- | ------------------------------------------------------------ |
| `request_tracking.py` | `geoip/GeoLite2-City.mmdb` | Local MaxMind GeoLite2 via `geoip2` library, no network call |
| `uuid_tracking.py`    | `ip-api.com` (HTTPS)       | External API call, includes ISP, coordinates, timezone       |

The MaxMind database file is required at `geoip/GeoLite2-City.mmdb`. If absent, location fields are stored as `null` without error.

## API endpoints

### Public

- `GET /`
  - Service status and version metadata

### Authenticated (user or admin token)

- `GET /cache`
  - Query params: `market`, `team`, `player`, `sport`, `league`, `include_stats` (default `false`)
  - Validation:
    - at least one of `market|team|player|league` required
    - `sport` required for team-only searches
    - `sport` required for league searches
  - Returns `404` with `found: false` when no match; missing items are recorded automatically
  - When `include_stats=true` and `STATS_API_URL` is configured, a `stats` key is merged into the `200` response with player/team historical stats and live state. Response shape is otherwise **identical** — existing callers are not affected
- `POST /cache/batch`
  - Body fields: `team[]`, `player[]`, `market[]`, `sport`, `league[]`
  - Independent lookup per category; each item resolves separately
  - Missing items tracked per request group
- `POST /cache/batch/precision`
  - Body: `{ "queries": [ { team/player/market/sport/league ... }, ... ] }`
  - Combined-parameter precision lookups; each query item can mix parameters to narrow results
  - Returns `results[]` with `total_queries`, `successful`, `failed` summary
- `GET /leagues`
  - Query params: `sport`, `search`, `region`
  - Partial-match search, priority-ordered results (top 5 leagues first)
- `GET /event/check`
  - Query params:
    - locator: either `event_id`, or `date` + `team` + `opponent`
    - market inputs: `market`, `pick`, optional `line`, optional `sport`
  - Supported markets: `moneyline`, `spread`, `total`
  - Proxies to the internal stats service and returns live or historical market evaluation
  - Returns `400` for invalid locator/date/market input, `404` when no matching event is found, and `200` with `{ found, result, outcome, settled, source, event, score, pricing }` when resolved

### Admin-only

- `GET /health`
  - Returns service status and Redis cache stats
- `GET /cache/stats`
  - Detailed Redis cache statistics
- `DELETE /cache/clear`
  - Clears all Redis cache entries
- `DELETE /cache/invalidate`
  - Query params: `market`, `team`, `player`, `sport`, `league`
  - Invalidates one specific cache key
- `GET /admin/dashboard`
  - Serves the HTML admin dashboard (auth handled client-side via cookie)
- `POST /admin/dashboard/login`
  - Form login endpoint; sets `admin_access` cookie on success
- `GET /admin/logs`
  - Query params: `limit` (default 100), `offset`, optional `session_id`, `path`
  - Paginated request log viewer
- `GET /admin/sessions`
  - Active session summary (token type, request count, last activity)
- `GET /admin/stats/cache`
  - Redis cache statistics (alias for `/cache/stats`, accessible from dashboard)
- `GET /admin/missing-items`
  - Query params: `item_type` (optional filter), `limit`, `offset`, `sort_by` (default `last_seen`)
  - Returns deduplicated log of items not found in the database, with occurrence counts
- `DELETE /admin/missing-items`
  - Query param: `item_type` (optional; omit to clear all)
  - Deletes missing-item records

### Token management (admin-only)

- `GET /admin/tokens`
  - Lists all tokens with masked values, owner, role, status, created/last-used dates
- `POST /admin/tokens`
  - Body: `{ "name": str, "role": "user"|"admin", "expires_at": "YYYY-MM-DDTHH:MM:SS" (optional) }`
  - Creates a new DB-managed token; returns the raw token once (not stored in plaintext)
  - Validates: `name` and `role` required, `expires_at` must be a valid ISO datetime if provided
- `PUT /admin/tokens/{token_id}/revoke`
  - Body: `{ "reason": str (optional) }`
  - Immediately revokes a token; rejected requests return `401` until token is deleted
- `POST /admin/tokens/{token_id}/rotate`
  - Generates a replacement token and deprecates the old one atomically
  - Returns the new raw token (shown once)
- `GET /admin/tokens/audit`
  - Returns the token usage/event audit log (create, rotate, revoke, use)

### Analytics (admin-only)

- `GET /admin/analytics/failures`
  - Failure counts grouped by endpoint and HTTP status code with time-window filtering
- `GET /admin/analytics/latency`
  - Average, p50, p95, and p99 latency per endpoint
- `GET /admin/analytics/signatures`
  - Top failing query parameter signatures (market/team/player/sport/league combinations) with counts and recent samples
- `GET /admin/analytics/trends`
  - Request volume and error-rate trends over configurable time windows

### Docs/OpenAPI behavior

- `GET /docs` — Swagger UI
- `GET /openapi.json` — hides routes tagged `admin` unless a valid `admin_access` cookie is present

## Environment variables

Core runtime variables:

| Variable                | Default     | Description                                  |
| ----------------------- | ----------- | -------------------------------------------- |
| `API_TOKEN`             | —           | User-level bearer token                      |
| `ADMIN_API_TOKEN`       | —           | Admin bearer token (fallback: `ADMIN_TOKEN`) |
| `RATE_LIMIT_PER_MINUTE` | `60`        | Max requests per IP per minute               |
| `REDIS_HOST`            | `localhost` | Redis hostname                               |
| `REDIS_PORT`            | `6379`      | Redis port                                   |
| `REDIS_DB`              | `0`         | Redis database index                         |
| `REDIS_PASSWORD`        | —           | Redis password (if required)                 |
| `CACHE_TTL`             | `3600`      | Redis cache TTL in seconds                   |

Stats API bridge variables (all optional — leave `STATS_API_URL` blank to disable enrichment entirely):

| Variable            | Default | Description                                                                                    |
| ------------------- | ------- | ---------------------------------------------------------------------------------------------- |
| `STATS_API_URL`     | —       | Base URL of the internal stats service (e.g. `http://localhost:8001`). Leave blank to disable. |
| `STATS_API_TOKEN`   | —       | Bearer token required by `stats_api.py` (optional if auth is disabled there)                   |
| `STATS_API_TIMEOUT` | `1.0`   | Hard timeout in seconds for stats requests — protects production response time                 |
| `STATS_CACHE_TTL`   | `300`   | Redis TTL for cached stat payloads in seconds                                                  |

`testing.py` event-check defaults (optional overrides for smoke runs):

| Variable               | Default      | Description                                                                                           |
| ---------------------- | ------------ | ----------------------------------------------------------------------------------------------------- |
| `EVENT_CHECK_EVENT_ID` | `761496`     | Known event ID used by `testing.py` smoke coverage                                                    |
| `EVENT_CHECK_DATE`     | `2026-03-12` | Match date used for matchup-based event-check tests                                                   |
| `EVENT_CHECK_TEAM`     | `PSG`        | Team value used for matchup-based event-check tests                                                   |
| `EVENT_CHECK_OPPONENT` | `Chelsea`    | Opponent value used for matchup-based event-check tests                                               |
| `DEPLOY_ALLOW_GIT_DB`  | `false`      | Deploy override. When `false`, VPS preserves its local `sports_data.db` during GitHub Actions deploys |

CI/CD smoke-test email alert variables (set as GitHub repository secrets):

| Variable             | Description                                      |
| -------------------- | ------------------------------------------------ |
| `GMAIL_SENDER`       | Gmail address used to send deploy-failure alerts |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password                  |
| `ADMIN_EMAIL`        | Recipient address for alert emails               |

Generate strong tokens with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Notes:

- `main.py` runs Uvicorn on port `5000` when started directly.
- On Windows without `REDIS_HOST` set, the app auto-discovers and starts any Docker Redis container at startup.

## Stats API bridge

`sports_bridge.py` is an optional async enrichment layer that connects this service to the internal `realtime_data_fetch/stats_api.py` service.

### How it works

1. A caller adds `?include_stats=true` to any `GET /cache` request.
2. If `STATS_API_URL` is set, `sports_bridge.enrich(player, team, sport)` is called after the normal cache lookup succeeds.
3. The bridge queries up to four stats endpoints — `/stats/player`, `/stats/team`, `/stats/live`, `/stats/market-check` — using the same identifiers already present in the request.
4. Results are cached in Redis under `stats_bridge:*` keys (separate namespace, no collision with `cache:*` keys) with a configurable TTL (`STATS_CACHE_TTL`, default 5 min).
5. The merged `stats` object is appended to the existing response. If the stats service is unavailable, the original response is returned unchanged.

### Event market checks

`GET /event/check` uses `sports_bridge.market_check(...)` to call the internal `/stats/market-check` endpoint.

- Uses a dedicated Redis namespace: `stats_bridge:market:*`
- Preserves handled client errors from the stats service (`400`, `404`, `422`)
- Converts upstream outages or unexpected failures into `503` so callers can distinguish bad input from backend unavailability

### Database deployment policy

`sports_data.db` should not be part of routine commits.

- Normal commits and pushes should leave `sports_data.db` untracked
- VPS deploys preserve the server's existing `sports_data.db` by default, even when Git contents differ
- If you intentionally want a Git-provided `sports_data.db` to replace the VPS copy, you must opt in explicitly

Intentional database rollout flow:

1. Confirm you really want to replace the VPS database with your local snapshot.
2. Force-add the database file because it is gitignored:

```bash
git add -f sports_data.db
```

3. Commit and push that change normally.
4. Set the GitHub Actions secret `DEPLOY_ALLOW_GIT_DB=true` before the deploy runs.
5. After the deploy completes, set `DEPLOY_ALLOW_GIT_DB=false` again so future code-only deploys keep preserving the VPS database.

If you skip step 4, the deploy will still preserve the existing VPS database even if the commit contains `sports_data.db`.

### Validation runner

`testing.py` now covers the event-check flow in addition to cache, batch, precision, leagues, and admin endpoints.

```bash
python testing.py --mode quick --target prod --token <user_token>
python testing.py --target prod --token <user_token> --admin-token <admin_token>
```

- `quick`: batch smoke plus one `/event/check` smoke case on prod and local
- `extensive`: local-vs-prod comparison for comparison-safe validation cases, including `/event/check`
- `full`: auth, user, admin, and `/event/check` endpoint validation

### Safety guarantees

| Guarantee                       | Detail                                                                                                                                                       |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Zero overhead when disabled** | If `STATS_API_URL` is not set, `enrich()` returns `None` immediately — no network activity, no latency                                                       |
| **Hard timeout**                | `STATS_API_TIMEOUT` (default 1 s) caps every fetch — a slow stats service can never stall a production response                                              |
| **Never raises**                | All exceptions (timeout, connection refused, JSON parse, Redis errors) are caught and logged at `DEBUG` level; the function returns `None`                   |
| **Opt-in only**                 | `include_stats` defaults to `false` — all existing API callers see zero change                                                                               |
| **Import-safe**                 | `sports_bridge` is imported inside a `try/except` at startup; if `httpx` is missing or the module fails, `_sports_bridge = None` and the app starts normally |

### Example enriched response

```json
{
  "found": true,
  "data": { "...existing cache fields..." },
  "stats": {
    "source": "sports_stats_api",
    "player": {
      "name": "Raphinha",
      "games": [ { "...per-game stats..." } ]
    },
    "team": {
      "name": "Barcelona",
      "record": { "wins": 18, "losses": 4, "draws": 3, "total_games": 25 },
      "recent": [ { "...last 5 results..." } ],
      "top_scorers": [ { "player": "Raphinha", "total": 14 } ]
    },
    "live": null
  },
  "query": { "team": "Barcelona", "player": "Raphinha", "sport": "Soccer", "market": null, "league": null }
}
```

### Setup

1. Start the stats service: `python realtime_data_fetch/stats_api.py` (default port `8001`)
2. Set `STATS_API_URL=http://localhost:8001` in the cache API `.env`
3. Optionally set `STATS_API_TOKEN` if the stats service has auth enabled
4. Ensure `httpx` is installed (`pip install -r requirements.txt`)

---

## Automated PR tests (`test_pr.py`)

`test_pr.py` is a `pytest` suite that runs on every pull request in GitHub Actions. It uses FastAPI's `TestClient` — no live server, no real secrets, and no external DB needed.

- Dummy tokens are injected via environment variables before the app is imported
- Redis is patched to return `None` instantly (no TCP timeouts during test runs)
- All DB functions (`get_cache_entry`, `get_batch_cache_entries`, etc.) are mocked
- Covers: root endpoint, auth layer (missing/wrong/valid token), admin-only rejection, key endpoint shapes, request body validation, token management lifecycle, and analytics endpoints

Run locally:

```bash
pip install pytest httpx
pytest test_pr.py -v -p no:playwright
```

## Testing utility (`testing.py`)

`testing.py` is a full endpoint validation runner for live environments.

### Supported modes

```bash
python testing.py --mode quick     --token <user_token>
python testing.py --mode compare   --token <user_token>
python testing.py --mode extensive --token <user_token>
python testing.py --mode full      --target prod --token <user_token> --admin-token <admin_token>
```

Mode summary:

- `quick`: smoke run for `/cache/batch` and `/event/check` on local + prod
- `compare`: deep diff of `/cache/batch` payload responses between local and prod
- `extensive`: broader local-vs-prod coverage for `/cache`, `/cache/batch`, `/cache/batch/precision`, `/leagues`, and comparison-safe `/event/check` cases
- `full` (default): endpoint health, auth, user, admin, and `/event/check` validation with pass/fail summary

### Target selection

```bash
python testing.py --mode full --target prod  --token <user_token> --admin-token <admin_token>
python testing.py --mode full --target local --token <user_token> --admin-token <admin_token>
python testing.py --mode full --target both  --token <user_token> --admin-token <admin_token>
```

- `prod`: production base URL only (`https://cache-api.eternitylabs.co`)
- `local`: local base URL only (`http://127.0.0.1:5000`)
- `both`: runs both environments (default)

### Token and environment variable fallback

- User token lookup order:
  1. `--token`
  2. `CACHE_API_TOKEN`
  3. `API_TOKEN`
- Admin token lookup order:
  1. `--admin-token`
  2. `CACHE_API_ADMIN_TOKEN`
  3. `ADMIN_API_TOKEN`
  4. `ADMIN_TOKEN`

### Optional destructive checks

By default, destructive endpoint tests are skipped.

```bash
python testing.py --mode full --target prod --token <user_token> --admin-token <admin_token> --include-destructive
```

This currently enables testing `DELETE /cache/clear`.

### Output and exit behavior

- Each test line prints endpoint, status, expected status, latency, and `ok=True/False`
- Final summary prints `total`, `passed`, `failed`
- Exit code `0` when all checks pass; `1` otherwise (CI-friendly)

### Coverage counts (full mode)

Per target environment (`prod` or `local`), `--mode full` currently runs:

- `45` checks when admin token is provided (non-destructive default)
- `46` checks with `--include-destructive` (adds `DELETE /cache/clear`)
- `37` checks when admin token is not provided (admin block skipped)

Distinct endpoints covered in full mode:

- `20` endpoints by default
- `21` endpoints when destructive check is enabled

Endpoint list covered:

- `/`, `/docs`, `/openapi.json`
- `/cache`, `/cache/batch`, `/cache/batch/precision`, `/leagues`
- `/health`, `/cache/stats`, `/cache/invalidate`
- `/admin/dashboard`, `/admin/logs`, `/admin/sessions`, `/admin/stats/cache`
- `/admin/tokens`, `/admin/tokens/audit`
- `/admin/analytics/failures`, `/admin/analytics/latency`, `/admin/analytics/signatures`, `/admin/analytics/trends`
- optional: `/cache/clear`

### Filter/combination coverage

Current suite includes the following parameter/body combination coverage:

- `GET /cache`: `15` query combinations
  - includes valid and validation-error cases (`team` without `sport`, `league` without `sport`)
- `POST /cache/batch`: `5` body combinations
  - includes mixed and sparse payloads across team/player/market/league/sport
- `POST /cache/batch/precision`: `3` precision query-set combinations
- `GET /leagues`: `8` filter combinations
  - all combinations of `{sport, search, region}` plus empty filter

## Deployment flow (GitHub Actions + VPS)

### Workflows

Two workflows live in `.github/workflows/`:

**`deploy.yml`** — triggered on push to `main`, manual dispatch, or PR targeting `main`:

**`validate` job** (runs on every PR and push):

1. Sets up Python 3.12 and installs `requirements.txt` plus `pytest`, `pytest-asyncio`, and `httpx`
2. `py_compile` syntax check on `main.py`, `cache_db.py`, `redis_cache.py`, `sports_bridge.py`, `request_tracking.py`, `uuid_tracking.py`
3. `pytest test_pr.py` — automated PR suite (see below)

**`deploy` job** (push to `main` only, skipped on PRs):

- Runs preflight fork-safety guard checks
- SSHes to VPS and executes `deploy.sh` (up to 2 attempts with 10-second retry gap)
- On success, runs a smoke test against the live service
- On smoke-test failure, sends an email alert via Gmail (configurable via secrets)
- Verifies `systemctl status` after deploy

**`cleanup.yml`** — triggered when a pull request is closed (merged or declined):

- Automatically deletes the head branch (skips `main` and `develop`)

### `deploy.sh` behavior

The deployment script (`deploy.sh`):

- Acquires a per-service lock file (`/tmp/<service-name>.deploy.lock`) to prevent parallel deploy races
- Verifies it is running as the `ubuntu` user
- Installs Redis via `apt` if not already present, then ensures `redis-server` is running
- Clones the repo into `SERVICE_DIR` if not already present; otherwise `git pull`
- Creates and activates a Python virtual environment
- Installs/upgrades dependencies from `requirements.txt`
- Writes/updates a `systemd` unit file and reloads the daemon
- Optionally removes a previous service name (`PREVIOUS_SERVICE_NAME`) to handle renames
- Configures Nginx with a server block pointing to the API port
- Restarts and verifies the service

### Required deploy secrets

- `VPS_HOST`, `VPS_PORT`, `VPS_USERNAME`, `VPS_SSH_KEY`
- `DEPLOY_SERVICE_NAME`
- `DEPLOY_DIR`
- `DEPLOY_BRANCH`
- `DEPLOY_PORT`
- `DEPLOY_REPO_URL`
- `DEPLOY_REPO_SLUG`
- `DEPLOY_PREVIOUS_SERVICE_NAME` (optional migration helper)
- `DEPLOY_PRIMARY_REPO_SLUG` (recommended)
- `DEPLOY_PRODUCTION_SERVICE_NAME` (recommended)
- `DEPLOY_PRODUCTION_PORT` (recommended)
- `DEPLOY_NGINX_SITE_NAME` (recommended)
- `DEPLOY_PROTECTED_NGINX_SITE_NAME` (recommended)

## Fork-safe production protection

The deploy system blocks non-primary repositories from colliding with production resources.

Guard error codes you may see:

- `GUARD:SERVICE_NAME_RESERVED` — non-primary repo tried to use the protected service name
- `GUARD:PORT_RESERVED` — non-primary repo tried to use the protected production port
- `GUARD:NGINX_SITE_RESERVED` — non-primary repo tried to use the protected nginx site name
- `GUARD:NGINX_SITE_EXISTS` — chosen nginx site already exists on VPS

Fix for fork/secondary deployments:

- Set a unique `DEPLOY_SERVICE_NAME`
- Set a unique `DEPLOY_DIR`
- Set a unique `DEPLOY_PORT`
- Set a unique `DEPLOY_NGINX_SITE_NAME`

## Troubleshooting

Service logs:

```bash
sudo journalctl -u <service-name> -n 100 --no-pager
```

Port check:

```bash
sudo ss -ltnp
```

Systemd status:

```bash
sudo systemctl status <service-name> --no-pager
```

Nginx config dump:

```bash
sudo nginx -T
```

Redis connectivity (local):

```bash
redis-cli ping
```

## Security hygiene

- Never commit real API/admin tokens, SSH keys, or passwords to the repository
- Keep `.env` local and rotate tokens if leaked
- Use least-privilege deploy credentials on the VPS
- Keep fork deployments isolated via unique service name, port, and nginx site
- Admin cookie (`admin_access`) is `HttpOnly`, `Secure`, and `SameSite=Strict`
- Redis SCAN is used instead of KEYS to avoid blocking production Redis

## Security hardening

The following security controls are in place (all active as of server restart):

| #   | Area              | Control                                                                                                                                                                                             |
| --- | ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Token exposure    | `GET /docs?admin_token=` triggers an immediate 302 redirect stripping the token from the URL, preventing it from appearing in server logs, browser history, or `Referer` headers                    |
| 2   | Brute force       | `POST /admin/dashboard/login` is rate-limited to 10 attempts per IP per minute; excess attempts return `429`                                                                                        |
| 3   | Timing attacks    | All token comparisons use `hmac.compare_digest` (constant-time) rather than `==`                                                                                                                    |
| 4   | Input validation  | `POST /admin/tokens` validates `expires_at` via Pydantic `@field_validator`; malformed values return `422` without silently failing. DB layer uses fail-safe expiry (`return None` on `ValueError`) |
| 5   | Error leakage     | `500` error responses return `"Internal server error"` generic text only; full exception details are printed server-side and never sent to clients                                                  |
| 6   | Schema exposure   | `/openapi.json` hides admin-tagged paths unless a valid admin cookie (`admin_access`) is present; DB-managed admin tokens are also accepted                                                         |
| 7   | Session isolation | Sessions are keyed by `(ip_address, user_agent, user_identifier)` so different tokens from the same IP/browser get independent session records                                                      |
| 8   | Input bounds      | `CreateTokenRequest` fields enforce `min_length=1`, `max_length=100`, and `role` must match `^(user\|admin)$`                                                                                       |

## Dashboard roadmap

All three planned improvements have been implemented.

### 1) Token governance and visibility — ✅ implemented

- Token inventory table (masked token, owner/name, created date, last used, status, role)
- Token distribution summary visible in the Tokens tab
- Token usage tracking via `token_audit` table (`log_token_use` records every accepted request)
- Token risk indicators: revoked tokens return `401` immediately

### 2) Token lifecycle management — ✅ implemented

- Create token with name, role (`user`/`admin`), and optional expiry date
- Revoke token immediately via `PUT /admin/tokens/{id}/revoke` with optional reason
- Rotate token atomically via `POST /admin/tokens/{id}/rotate` (old token deprecated, new token returned once)
- Full audit trail in `token_audit` table for all create/rotate/revoke/use events
- Pydantic validation on all inputs with constant-time comparison and SHA-256 storage

### 3) Request failure analytics — ✅ implemented

- Failure heatmap by endpoint + status code via `GET /admin/analytics/failures`
- Latency stats (avg, p50, p95, p99) per endpoint via `GET /admin/analytics/latency`
- Top failing query signatures (parameter pattern + count + recent samples) via `GET /admin/analytics/signatures`
- Request volume and error-rate trends via `GET /admin/analytics/trends`
- All analytics data visible in the Analytics tab of the dashboard

### 4) Missing-filter intelligence

Goal: identify user filters/queries not present in current DB mappings.

Planned features:

- "Not found" leaderboard by value and endpoint
- Missing value clustering (synonym/typo/case variants)
- Suggestions from closest known mappings (fuzzy candidates)
- Breakdown by dimension:
  - markets not mapped
  - teams not mapped
  - players not mapped
  - leagues/sports mismatches
- Prioritized backlog feed for data/mapping updates

### 5) Operational observability

Goal: connect dashboard insights to platform health.

Planned features:

- Cache performance panel (hit/miss rate by endpoint and token)
- Latency percentiles (p50/p95/p99) by endpoint
- Error budget and SLO-style tracking
- Deployment correlation (error spikes after specific releases)

### 6) Security and compliance controls

Goal: harden admin operations and access controls.

Planned features:

- Role-based admin permissions (viewer/operator/admin)
- Mandatory admin action logging with actor + timestamp + reason
- Session controls (max session age, forced logout, suspicious-login alerts)
- Optional 2FA for admin dashboard login

### 7) Suggested implementation phases

Phase 1 (MVP):

- Token inventory, request-failure table, missing-filter leaderboard, basic CSV export

Phase 2:

- Token create/revoke/rotate, scoped permissions, richer analytics and charts

Phase 3:

- Alerting, anomaly detection, SLO dashboards, advanced admin security controls
