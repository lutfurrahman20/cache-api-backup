# Cache API

FastAPI service for normalized sports cache lookups with Redis-backed caching.

## Features

- Token-authenticated API access (`Bearer` token)
- Single query endpoint (`/cache`)
- Batch query endpoint (`/cache/batch`)
- Precision batch endpoint (`/cache/batch/precision`)
- League listing endpoint (`/leagues`)
- Redis cache stats/invalidation/clear (admin-only)
- GitHub Actions deployment to VPS via SSH

## Tech Stack

- Python + FastAPI
- SQLite (data source)
- Redis (cache layer)
- systemd (service management)
- Nginx (reverse proxy)

## Local Run

1. Create and activate virtual env.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create `.env` from template and set tokens:

```bash
cp .env.example .env
```

4. Run:

```bash
python main.py
```

Default local URL:

```text
http://localhost:5000
```

## Authentication

All protected endpoints require:

```text
Authorization: Bearer <token>
```

- `API_TOKEN`: non-admin token
- `ADMIN_API_TOKEN` (or `ADMIN_TOKEN`): admin token

Never commit real token values to git.

## Endpoints

- `GET /` public health/info
- `GET /cache` protected
- `POST /cache/batch` protected
- `POST /cache/batch/precision` protected
- `GET /leagues` protected
- `GET /health` admin
- `GET /cache/stats` admin
- `DELETE /cache/invalidate` admin
- `DELETE /cache/clear` admin

## Testing

`testing.py` supports 3 modes:

```bash
python testing.py --mode quick
python testing.py --mode compare
python testing.py --mode extensive
```

- `quick`: concise per-request summary
- `compare`: local vs production diff + timing
- `extensive`: endpoint/parameter combination checks

Output is summarized (status, latency, response size/count), not full payload dumps.

## Deployment (Hardened)

Deployment is done by GitHub Actions (`.github/workflows/deploy.yml`) and calls `deploy.sh`.

The deployment is isolated by these secrets:

- `DEPLOY_SERVICE_NAME`
- `DEPLOY_DIR`
- `DEPLOY_PORT`
- `DEPLOY_BRANCH`
- `DEPLOY_REPO_URL`
- `DEPLOY_REPO_SLUG`
- `VPS_HOST`
- `VPS_PORT`
- `VPS_USERNAME`
- `VPS_SSH_KEY`

### Important

- Use unique `DEPLOY_SERVICE_NAME`, `DEPLOY_DIR`, and `DEPLOY_PORT` per project.
- Do not share deploy SSH keys across unrelated repos.
- Do not expose Actions secrets to forks.

## Security Notes

- Do not store real tokens, private keys, or passwords in repository files.
- Keep `.env` out of version control.
- Rotate secrets immediately if leaked.
- Restrict Redis to local/private network only.
- Prefer least-privilege SSH keys for deployment.

## Troubleshooting

Check service logs:

```bash
sudo journalctl -u <service-name> -n 100 --no-pager
```

Check active ports:

```bash
sudo ss -tulpn
```

Check nginx config:

```bash
sudo nginx -T
```

Restart service:

```bash
sudo systemctl restart <service-name>
sudo systemctl status <service-name> --no-pager
```

## Repository Hygiene

- Keep `.env` and local secret files ignored.
- Keep `README.md` free of real credentials, real tokens, and private keys.
- Use placeholders in docs:
  - `<your-domain>`
  - `<your-token>`
  - `<your-vps-ip>`

