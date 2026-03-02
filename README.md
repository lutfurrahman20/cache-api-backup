# Cache API

FastAPI service for sports cache normalization with Redis-backed caching, batch lookup support, request/session tracking, and VPS auto-deployment.

## What this service does

- Normalizes cache lookups for `market`, `team`, `player`, and `league`
- Supports single query and two batch modes
- Exposes admin-only cache and request/session monitoring endpoints
- Uses Redis for caching and SQLite-backed lookup data
- Deploys to VPS through GitHub Actions + `deploy.sh`

## Tech stack

- FastAPI + Uvicorn
- Redis (`redis`, `hiredis`)
- SQLite data files
- systemd service management
- Nginx reverse proxy on VPS

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

## Authentication

Protected endpoints require:

```text
Authorization: Bearer <token>
```

- `API_TOKEN`: user token for non-admin endpoints
- `ADMIN_API_TOKEN` / `ADMIN_TOKEN`: admin token for admin endpoints

If no valid tokens are configured, protected routes return server error.

## Rate limiting

- Per-IP in-memory limiter
- Controlled by `RATE_LIMIT_PER_MINUTE` (default `60`)
- Applied to user API routes (`/cache`, `/cache/batch`, `/cache/batch/precision`, `/leagues`)

## API endpoints

### Public

- `GET /`
  - Service status/metadata

### Authenticated (user or admin token)

- `GET /cache`
  - Query params: `market`, `team`, `player`, `sport`, `league`
  - Validation:
    - at least one of `market|team|player|league` is required
    - `sport` required for team-only search
    - `sport` required for league search
- `POST /cache/batch`
  - Body fields: `team[]`, `player[]`, `market[]`, `sport`, `league[]`
  - Independent batch lookup per category
- `POST /cache/batch/precision`
  - Body: `{ "queries": [ {team/player/market/sport/league...}, ... ] }`
  - Combined-parameter precision lookups
- `GET /leagues`
  - Query params: `sport`, `search`, `region`

### Admin-only

- `GET /health`
- `GET /cache/stats`
- `DELETE /cache/clear`
- `DELETE /cache/invalidate` (query params: `market`, `team`, `player`, `sport`, `league`)
- `GET /admin/dashboard`
- `GET /admin/logs` (`limit`, `offset`, optional `session_id`, `path`)
- `GET /admin/sessions`
- `GET /admin/stats/cache`

### Docs/OpenAPI behavior

- `GET /docs` serves Swagger UI
- `GET /openapi.json` hides routes tagged `admin` unless valid admin cookie is present

## Environment variables

Core runtime variables:

- `API_TOKEN`
- `ADMIN_API_TOKEN` (fallback: `ADMIN_TOKEN`)
- `RATE_LIMIT_PER_MINUTE` (default `60`)
- `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_PASSWORD`
- `CACHE_TTL`

Notes:

- `main.py` currently runs Uvicorn on port `5000` when started directly.
- On Windows local development, app attempts to auto-start Docker container `local-redis` if `REDIS_HOST` is unset.

## Testing utility

`testing.py` is now a full endpoint validation runner.

### Supported modes

```bash
python testing.py --mode quick --token <user_token>
python testing.py --mode compare --token <user_token>
python testing.py --mode extensive --token <user_token>
python testing.py --mode full --target prod --token <user_token> --admin-token <admin_token>
```

Mode summary:

- `quick`: smoke run for `/cache/batch` on local + prod
- `compare`: deep diff for `/cache/batch` payload responses (local vs prod)
- `extensive`: broader local-vs-prod combinations for `/cache`, `/cache/batch`, `/cache/batch/precision`, `/leagues`
- `full` (default): endpoint health/auth/user/admin validation with pass/fail summary

### Target selection

```bash
python testing.py --mode full --target prod  --token <user_token> --admin-token <admin_token>
python testing.py --mode full --target local --token <user_token> --admin-token <admin_token>
python testing.py --mode full --target both  --token <user_token> --admin-token <admin_token>
```

- `prod`: only production base URL
- `local`: only local base URL
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
- Exit code is `0` only when all checks pass; otherwise `1` (CI-friendly)

### Coverage counts (full mode)

Per target environment (`prod` or `local`), `--mode full` currently runs:

- `45` checks when admin token is provided (non-destructive default)
- `46` checks with `--include-destructive` (adds `DELETE /cache/clear`)
- `37` checks when admin token is not provided (admin block skipped)

Distinct endpoints covered in full mode:

- `14` endpoints by default
- `15` endpoints when destructive check is enabled

Endpoint list covered:

- `/`, `/docs`, `/openapi.json`
- `/cache`, `/cache/batch`, `/cache/batch/precision`, `/leagues`
- `/health`, `/cache/stats`, `/cache/invalidate`
- `/admin/dashboard`, `/admin/logs`, `/admin/sessions`, `/admin/stats/cache`
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

Push to `main` triggers `.github/workflows/deploy.yml`, which:

1. Runs deployment preflight guard checks
2. SSHes to VPS
3. Executes `deploy.sh`
4. Updates repo in target service directory
5. Installs/updates dependencies
6. Manages systemd service + restart + verification

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

The deploy system now blocks non-primary repositories from using protected production resources.

Guard error codes you may see:

- `GUARD:SERVICE_NAME_RESERVED`
  - Non-primary repo tried to use protected service name
- `GUARD:PORT_RESERVED`
  - Non-primary repo tried to use protected production port
- `GUARD:NGINX_SITE_RESERVED`
  - Non-primary repo tried to use protected nginx site name
- `GUARD:NGINX_SITE_EXISTS`
  - Chosen nginx site already exists on VPS

Fix for fork/secondary deployments:

- Set unique `DEPLOY_SERVICE_NAME`
- Set unique `DEPLOY_DIR`
- Set unique `DEPLOY_PORT`
- Set unique `DEPLOY_NGINX_SITE_NAME`

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

## Security hygiene

- Never commit real API/admin tokens, SSH keys, or passwords
- Keep `.env` local and rotated if leaked
- Use least-privilege deploy credentials
- Keep fork deployments isolated by unique service/port/nginx identity
