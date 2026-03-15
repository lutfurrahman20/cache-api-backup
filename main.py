"""
FastAPI Cache API Service
Provides normalized cache lookups for sports betting markets, teams, and players.
Includes Redis caching layer for improved performance.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, HTTPException, Body, Security, Depends, Request, Cookie, Form
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, field_validator, model_validator
import hmac
import uvicorn
import os
import platform
import time
from collections import defaultdict
from dotenv import load_dotenv

# Load environment variables early so that imported modules can
# pick them up (redis_cache reads vars at import time).
load_dotenv()

# If running on Windows or otherwise in a "local" context and the
# REDIS_HOST hasn't been set, we assume the developer is using the
# Docker image mapped to localhost.  VPS deployments should explicitly
# set REDIS_HOST/REDIS_PORT in their environment.
def _find_redis_container(running_only: bool) -> str:
    """
    Return the name of the first Docker container that looks like Redis.

    Matches any container whose name OR image contains 'redis'
    (case-insensitive), covering names like: redis, local-redis,
    redis-local, my-redis, redis-stack, etc.

    Args:
        running_only: if True only inspect running containers (docker ps),
                      otherwise include stopped ones (docker ps -a).
    """
    import subprocess

    flags = ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"]
    if not running_only:
        flags.insert(2, "-a")

    try:
        result = subprocess.run(flags, capture_output=True, text=True, check=True)
    except Exception:
        return ""

    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        cname, image = parts
        if "redis" in cname.lower() or "redis" in image.lower():
            return cname.strip()

    return ""


def ensure_redis_container():
    """Make sure a Redis container is running locally.

    Discovery order (name OR image must contain 'redis'):
      1. Any currently-running container  → do nothing, use it
      2. Any stopped container            → start it
      3. No container found at all        → create 'local-redis'

    Covers all naming conventions: redis, local-redis, redis-local,
    redis-stack, my-redis, etc., so the app works across different
    developer machines without config changes.
    """
    import subprocess

    # 1. Already running?
    running_name = _find_redis_container(running_only=True)
    if running_name:
        print(f"⚙️  Redis container already running: {running_name}")
        return

    # 2. Stopped container?
    stopped_name = _find_redis_container(running_only=False)
    if stopped_name:
        print(f"⚙️  Starting existing Redis container: {stopped_name}")
        subprocess.run(["docker", "start", stopped_name])
        return

    # 3. Nothing found – create a fresh one
    fallback_name = "local-redis"
    print(f"⚙️  No Redis container found. Creating '{fallback_name}'...")
    subprocess.run([
        "docker", "run", "-d",
        "--name", fallback_name,
        "-p", "6379:6379",
        "redis:latest",
    ])

if platform.system() == "Windows" and not os.getenv('REDIS_HOST'):
    os.environ['REDIS_HOST'] = 'localhost'
    # port already defaults to 6379 elsewhere but set for clarity
    os.environ['REDIS_PORT'] = os.getenv('REDIS_PORT', '6379')
    print("[INFO] Detected Windows local environment; configuring Redis host to localhost:6379")

from cache_db import get_cache_entry, get_batch_cache_entries, get_precision_batch_cache_entries, get_all_leagues
from redis_cache import get_cache_stats, clear_all_cache, invalidate_cache
import uuid
import json
import request_tracking
import uuid_tracking

# Optional stats enrichment bridge — gracefully absent when not configured.
# Import failure (e.g. httpx not installed) is caught so startup never breaks.
try:
    import sports_bridge as _sports_bridge
except Exception:  # pragma: no cover
    _sports_bridge = None  # type: ignore[assignment]

# Load environment variables
load_dotenv()

# Security configuration
security = HTTPBearer()

# Load API keys from environment variables (NEVER hardcode in production!)
# Admin key (full access to all endpoints). older .env files may use
# "ADMIN_TOKEN", so fall back to that if the preferred name isn't set.
admin_key = os.getenv('ADMIN_API_TOKEN') or os.getenv('ADMIN_TOKEN')
if admin_key:
    admin_key = admin_key.strip()
ADMIN_KEY = admin_key

# Non-admin key (read-only access to cache endpoints)
user_key = os.getenv('API_TOKEN')
if user_key:
    user_key = user_key.strip()
NON_ADMIN_KEY = user_key

# All valid tokens (admin + non-admin)
VALID_API_TOKENS = {token for token in [ADMIN_KEY, NON_ADMIN_KEY] if token}

# Batch size limits — prevent excessively large requests from tying up the server
MAX_PRECISION_BATCH_SIZE = int(os.getenv('MAX_PRECISION_BATCH_SIZE', 20))

# Rate limiting configuration
RATE_LIMIT_PER_MINUTE = int(os.getenv('RATE_LIMIT_PER_MINUTE', 60))
LOGIN_ATTEMPT_LIMIT = 10  # max dashboard login attempts per IP per minute
rate_limit_storage = defaultdict(list)

def check_rate_limit(client_ip: str) -> bool:
    """
    Check if the client IP has exceeded the rate limit.
    Simple in-memory rate limiting - use Redis for production multi-server setup.
    
    Args:
        client_ip: The client's IP address
    
    Returns:
        True if rate limit is not exceeded, False otherwise
    """
    now = time.time()
    minute_ago = now - 60
    
    # Clean old entries
    rate_limit_storage[client_ip] = [
        timestamp for timestamp in rate_limit_storage[client_ip]
        if timestamp > minute_ago
    ]
    
    # Check if limit exceeded
    if len(rate_limit_storage[client_ip]) >= RATE_LIMIT_PER_MINUTE:
        return False
    
    # Add current request
    rate_limit_storage[client_ip].append(now)
    return True

async def verify_rate_limit(request: Request):
    """
    Middleware to check rate limiting before processing request.
    Non-admin key bypasses rate limiting entirely.
    
    Raises:
        HTTPException: If rate limit is exceeded
    """
    # Non-admin key gets unlimited access - no rate limit applied
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if NON_ADMIN_KEY and token and hmac.compare_digest(token.encode(), NON_ADMIN_KEY.encode()):
        return

    client_ip = request.client.host if request.client else "unknown"
    
    if not check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Maximum {RATE_LIMIT_PER_MINUTE} requests per minute allowed."
        )

def _safe_eq(a: str, b) -> bool:
    """Constant-time string comparison to prevent timing-based token enumeration."""
    if not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """
    Verify the API token from the Authorization header.
    Allows both admin and non-admin tokens (env-var or DB-managed).
    """
    token = credentials.credentials

    # Fast path: env-var configured tokens (constant-time to resist timing attacks)
    if any(_safe_eq(token, t) for t in VALID_API_TOKENS):
        return token

    # Fallback: DB-managed tokens (multi-token support)
    db_tok = request_tracking.verify_db_token(token)
    if db_tok:
        return token

    if not VALID_API_TOKENS:
        raise HTTPException(
            status_code=500,
            detail="No API tokens configured. Please set API_TOKEN in environment variables."
        )
    raise HTTPException(
        status_code=401,
        detail="Invalid or expired API token"
    )


def verify_admin_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """
    Verify the API token is an admin token (env-var or DB-managed admin role).
    Restricts access to admin-only endpoints.
    """
    token = credentials.credentials

    # Fast path: env-var admin token (constant-time comparison)
    if _safe_eq(token, ADMIN_KEY):
        return token

    # Fallback: DB-managed admin tokens
    if request_tracking.is_admin_db_token(token):
        return token

    raise HTTPException(
        status_code=403,
        detail="Admin access required. This endpoint requires an admin API token."
    )

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: ensure Redis is available on local Windows dev machines
    if platform.system() == "Windows" and not os.getenv('REDIS_HOST'):
        ensure_redis_container()
    # Seed env-var tokens into the managed token store (idempotent)
    await run_in_threadpool(request_tracking.seed_env_tokens, ADMIN_KEY, NON_ADMIN_KEY)
    yield
    # Shutdown: nothing to tear down (Redis is external)

app = FastAPI(
    title="Cache API",
    description="Sports betting cache normalization service with Redis caching",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# Mount static files for dashboard
app.mount("/admin/js", StaticFiles(directory="js"), name="admin_js")
app.mount("/admin/css", StaticFiles(directory="css"), name="admin_css")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/admin/dashboard", tags=["admin"])
async def serve_dashboard():
        """Serve the admin dashboard. Auth is handled client-side via JS/localStorage."""
        return FileResponse("dashboard.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.post("/admin/dashboard/login", tags=["admin"], include_in_schema=False)
async def dashboard_login(request: Request, admin_token: str = Form(...)):
    # Brute-force guard: max LOGIN_ATTEMPT_LIMIT failed attempts per IP per minute
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    login_key = f"login:{client_ip}"
    login_attempts = [t for t in rate_limit_storage.get(login_key, []) if now - t < 60]
    if len(login_attempts) >= LOGIN_ATTEMPT_LIMIT:
        return HTMLResponse(status_code=429, content="Too many login attempts. Try again later.")
    login_attempts.append(now)
    rate_limit_storage[login_key] = login_attempts

    if not _safe_eq(admin_token, ADMIN_KEY) and not request_tracking.is_admin_db_token(admin_token):
        return HTMLResponse(status_code=403, content="Invalid admin token")

    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key="admin_access",
        value=admin_token,
        max_age=3600,
        httponly=True,
        secure=True,
        samesite="strict",
    )
    return response

# Middleware for request tracking (UUID + GeoIP + Sessions)
@app.middleware("http")
async def track_request_middleware(request: Request, call_next):
    start_time = time.time()

    # --- PRE-PROCESS: create session BEFORE call_next so endpoints can read request.state.session_id ---
    session_id = None
    body_data = {}
    try:
        # Capture request body for POST/PUT/PATCH (needed for missing-item body tracking)
        try:
            body_bytes = await request.body()
            if body_bytes:
                body_data = json.loads(body_bytes)
        except Exception:
            body_data = {}
        request.state.body_data = body_data

        ip_address = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")

        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

        if token:
            is_admin = _safe_eq(token, ADMIN_KEY) or request_tracking.is_admin_db_token(token)
            request.state.is_admin = is_admin
            if not is_admin:
                user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, token))

                session_id = request_tracking.get_or_create_session(
                    token=token,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    user_identifier=user_uuid
                )

                # Store on request.state so route handlers can access it
                request.state.session_id = session_id

                uuid_tracking.track_uuid_login(
                    uuid=user_uuid,
                    ip_address=ip_address,
                    user_agent=user_agent
                )
    except Exception as e:
        print(f"Pre-tracking error: {e}")

    # Process request
    response = await call_next(request)

    # --- POST-PROCESS: record the completed request with response status/time ---
    process_time = (time.time() - start_time) * 1000

    try:
        ip_address = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")

        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

        if token:
            is_admin = getattr(request.state, 'is_admin', None)
            if is_admin is None:
                is_admin = _safe_eq(token, ADMIN_KEY) or request_tracking.is_admin_db_token(token)
            # Only update last-used when the request actually authenticated (not 401/403)
            if response.status_code not in (401, 403):
                request_tracking.log_token_use(token, ip_address)
            if not is_admin and session_id:
                user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, token))
                tracked_body = getattr(request.state, 'body_data', {})

                request_tracking.track_request(
                    session_id=session_id,
                    method=request.method,
                    path=request.url.path,
                    query_params=dict(request.query_params),
                    token=token,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    response_status=response.status_code,
                    response_time_ms=process_time,
                    uuid=user_uuid,
                    body_data=tracked_body
                )
    except Exception as e:
        print(f"Tracking error: {e}")

    return response

# Request models for batch endpoints
class BatchQueryRequest(BaseModel):
    """Request model for batch cache queries"""
    team: Optional[List[str]] = None
    player: Optional[List[str]] = None
    market: Optional[List[str]] = None
    sport: Optional[str] = None  # Sport context for team/league queries
    league: Optional[List[str]] = None

    @model_validator(mode='after')
    def require_at_least_one_list(self):
        # Reject only when ALL list fields are absent (None) — e.g. wrong schema
        # like {"queries":[...]}. Empty lists [] are valid (returns empty result).
        if all(v is None for v in [self.team, self.player, self.market, self.league]):
            raise ValueError(
                "At least one of 'team', 'player', 'market', or 'league' must be provided"
            )
        return self

class PrecisionBatchItem(BaseModel):
    """Single precision query item"""
    team: Optional[str] = None
    player: Optional[str] = None
    market: Optional[str] = None
    sport: Optional[str] = None
    league: Optional[str] = None

class PrecisionBatchRequest(BaseModel):
    """Request model for precision batch queries"""
    queries: List[PrecisionBatchItem]

    @field_validator('queries')
    @classmethod
    def limit_query_count(cls, v):
        if len(v) > MAX_PRECISION_BATCH_SIZE:
            raise ValueError(
                f"Maximum {MAX_PRECISION_BATCH_SIZE} queries per batch request allowed. "
                f"Received {len(v)}."
            )
        return v


class CreateTokenRequest(BaseModel):
    """Request model for creating a managed API token"""
    name: str = Field(..., min_length=1, max_length=100)
    owner: Optional[str] = Field(None, max_length=100)
    role: str = Field("user", pattern=r"^(user|admin)$")
    notes: Optional[str] = Field(None, max_length=500)
    expires_at: Optional[str] = Field(None)

    @field_validator('expires_at')
    @classmethod
    def validate_expires_at(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                from datetime import datetime as _dt
                _dt.fromisoformat(v)
            except ValueError:
                raise ValueError('expires_at must be a valid ISO datetime, e.g. 2027-01-01T00:00:00')
        return v


class RevokeRequest(BaseModel):
    reason: Optional[str] = None


class RotateRequest(BaseModel):
    reason: Optional[str] = None

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "online",
        "service": "Cache API",
        "version": "2.0.0",
        "features": ["Redis caching", "SQLite database", "Alias normalization"]
    }

@app.get("/health", tags=["admin"])
async def health_check(token: str = Depends(verify_admin_token)):
    """Health check for monitoring (requires admin authentication)"""
    stats = get_cache_stats()
    return {
        "status": "healthy",
        "cache": stats
    }

@app.get("/cache/stats", tags=["admin"])
async def cache_statistics(token: str = Depends(verify_admin_token)):
    """Get detailed cache statistics (requires admin authentication)"""
    stats = get_cache_stats()
    return JSONResponse(
        status_code=200,
        content=stats
    )

@app.delete("/cache/clear", tags=["admin"])
async def clear_cache(token: str = Depends(verify_admin_token)):
    """Clear all cache entries (requires admin authentication)"""
    success = clear_all_cache()
    
    if success:
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "All cache entries cleared"
            }
        )
    else:
        raise HTTPException(
            status_code=500,
            detail="Failed to clear cache"
        )

@app.delete("/cache/invalidate", tags=["admin"])
async def invalidate_specific_cache(
    market: Optional[str] = Query(None),
    team: Optional[str] = Query(None),
    player: Optional[str] = Query(None),
    sport: Optional[str] = Query(None),
    league: Optional[str] = Query(None),
    token: str = Depends(verify_admin_token)
):
    """Invalidate specific cache entry (requires admin authentication)"""
    if not any([market, team, player, league]):
        raise HTTPException(
            status_code=400,
            detail="At least one parameter must be provided"
        )
    
    success = invalidate_cache(market=market, team=team, player=player, sport=sport, league=league)
    
    if success:
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "Cache entry invalidated"
            }
        )
    else:
        return JSONResponse(
            status_code=404,
            content={
                "status": "not_found",
                "message": "Cache entry not found"
            }
        )

@app.get("/cache")
async def get_cache(
    request: Request,
    market: Optional[str] = Query(None, description="Market type (e.g., 'moneyline', 'spread', 'total')"),
    team: Optional[str] = Query(None, description="Team name to look up"),
    player: Optional[str] = Query(None, description="Player name to look up"),
    sport: Optional[str] = Query(None, description="Sport name (required when searching by team or league)"),
    league: Optional[str] = Query(None, description="League name to look up"),
    include_stats: bool = Query(False, description="Enrich response with historical/live stats (requires STATS_API_URL)"),
    token: str = Depends(verify_token),
    _: None = Depends(verify_rate_limit)
) -> JSONResponse:
    """
    Get normalized cache entry for market, team, player, or league (requires authentication).
    
    Parameters:
    - market: Market type to look up
    - team: Team name to normalize
    - player: Player name to normalize
    - sport: Sport name (required when searching by team or league only)
    - league: League name to normalize
    
    Returns:
    - Mapped/normalized entry from cache database
    
    Examples:
    - /cache?team=Lakers&sport=Basketball
    - /cache?league=Premier League&sport=Soccer
    - /cache?player=LeBron James
    - /cache?market=moneyline
    """
    
    # Validate that at least one parameter is provided
    if not any([market, team, player, league]):
        raise HTTPException(
            status_code=400,
            detail="At least one parameter (market, team, player, or league) must be provided"
        )
    
    # Validate that sport is provided when searching by team or league (unless both team and player provided)
    if team and not player and not sport:
        raise HTTPException(
            status_code=400,
            detail="Sport parameter is required when searching by team only"
        )
    
    if league and not sport:
        raise HTTPException(
            status_code=400,
            detail="Sport parameter is required when searching by league"
        )
    
    # Get the cache entry
    try:
        result = await run_in_threadpool(
            get_cache_entry,
            market=market, 
            team=team, 
            player=player, 
            sport=sport, 
            league=league
        )
        
        if result is None:
            # Track missing items for non-admin users
            session_id = getattr(request.state, 'session_id', None)
            if session_id:
                request_group_id = str(uuid.uuid4())
                cache_body = {k: v for k, v in {"market": market, "team": team, "player": player, "sport": sport, "league": league}.items() if v}
                if market:
                    request_tracking.track_missing_item(
                        session_id=session_id,
                        item_type="market",
                        item_value=market,
                        endpoint="/cache",
                        query_params={"sport": sport, "league": league, "team": team, "player": player},
                        request_group_id=request_group_id,
                        body_data=cache_body
                    )
                if team:
                    request_tracking.track_missing_item(
                        session_id=session_id,
                        item_type="team",
                        item_value=team,
                        endpoint="/cache",
                        query_params={"sport": sport, "league": league, "market": market},
                        request_group_id=request_group_id,
                        body_data=cache_body
                    )
                if player:
                    request_tracking.track_missing_item(
                        session_id=session_id,
                        item_type="player",
                        item_value=player,
                        endpoint="/cache",
                        query_params={"sport": sport, "team": team, "market": market},
                        request_group_id=request_group_id,
                        body_data=cache_body
                    )
                if league:
                    request_tracking.track_missing_item(
                        session_id=session_id,
                        item_type="league",
                        item_value=league,
                        endpoint="/cache",
                        query_params={"sport": sport, "market": market},
                        request_group_id=request_group_id,
                        body_data=cache_body
                    )
            return JSONResponse(
                status_code=404,
                content={
                    "found": False,
                    "message": "No cache entry found",
                    "query": {
                        "market": market,
                        "team": team,
                        "player": player,
                        "sport": sport,
                        "league": league
                    }
                }
            )
        
        response_content: dict = {
            "found": True,
            "data": result,
            "query": {
                "market": market,
                "team": team,
                "player": player,
                "sport": sport,
                "league": league
            }
        }

        # Optional stats enrichment — never raises, never changes default behaviour
        if include_stats and _sports_bridge is not None:
            try:
                stats_data = await _sports_bridge.enrich(player, team, sport)
                if stats_data is not None:
                    response_content["stats"] = stats_data
                else:
                    response_content["stats_unavailable"] = True
            except Exception as _stats_exc:
                print(f"[WARN] stats enrichment failed (non-critical): {_stats_exc}")
                response_content["stats_unavailable"] = True
        elif include_stats and _sports_bridge is None:
            response_content["stats_unavailable"] = True

        return JSONResponse(status_code=200, content=response_content)
        
    except Exception as e:
        print(f"[ERROR] GET /cache: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/cache/batch")
async def get_batch_cache(
    request: Request,
    request_body: BatchQueryRequest = Body(...),
    token: str = Depends(verify_token),
    _: None = Depends(verify_rate_limit)
) -> JSONResponse:
    """
    Batch cache query endpoint - independent searches for multiple items per category (requires authentication).
    
    Queries multiple teams, players, markets, and leagues in a single request.
    Each item is searched independently (not combined for precision).
    
    Request body:
    {
        "team": ["Lakers", "Warriors", "Bulls"],
        "player": ["LeBron James", "Stephen Curry"],
        "market": ["moneyline", "spread", "total"],
        "sport": "Basketball",  // Optional: context for team/league searches
        "league": ["NBA", "EuroLeague"]
    }
    
    Response:
    {
        "team": {
            "Lakers": {...},
            "Warriors": {...},
            "Bulls": null  // if not found
        },
        "player": {
            "LeBron James": {...},
            "Stephen Curry": {...}
        },
        "market": {
            "moneyline": {...},
            "spread": {...},
            "total": {...}
        },
        "league": {
            "NBA": {...},
            "EuroLeague": null
        }
    }
    """
    try:
        result = await run_in_threadpool(
            get_batch_cache_entries,
            teams=request_body.team,
            players=request_body.player,
            markets=request_body.market,
            sport=request_body.sport,
            leagues=request_body.league
        )
        
        # Track missing items (only for non-admin users)
        session_id = getattr(request.state, 'session_id', None)
        if session_id:
            request_group_id = str(uuid.uuid4())
            batch_body = request_body.model_dump(exclude_none=True)
            # Track missing teams
            if 'team' in result:
                for team_name, team_data in result['team'].items():
                    if team_data is None:
                        request_tracking.track_missing_item(
                            session_id=session_id,
                            item_type='team',
                            item_value=team_name,
                            endpoint='/cache/batch',
                            query_params={'sport': request_body.sport} if request_body.sport else {},
                            request_group_id=request_group_id,
                            body_data=batch_body
                        )
            # Track missing players
            if 'player' in result:
                for player_name, player_data in result['player'].items():
                    if player_data is None:
                        request_tracking.track_missing_item(
                            session_id=session_id,
                            item_type='player',
                            item_value=player_name,
                            endpoint='/cache/batch',
                            query_params={},
                            request_group_id=request_group_id,
                            body_data=batch_body
                        )
            # Track missing markets
            if 'market' in result:
                for market_name, market_data in result['market'].items():
                    if market_data is None:
                        request_tracking.track_missing_item(
                            session_id=session_id,
                            item_type='market',
                            item_value=market_name,
                            endpoint='/cache/batch',
                            query_params={},
                            request_group_id=request_group_id,
                            body_data=batch_body
                        )
            # Track missing leagues
            if 'league' in result:
                for league_name, league_data in result['league'].items():
                    if league_data is None:
                        request_tracking.track_missing_item(
                            session_id=session_id,
                            item_type='league',
                            item_value=league_name,
                            endpoint='/cache/batch',
                            query_params={'sport': request_body.sport} if request_body.sport else {},
                            request_group_id=request_group_id,
                            body_data=batch_body
                        )

        return JSONResponse(
            status_code=200,
            content=result
        )
        
    except Exception as e:
        print(f"[ERROR] POST /cache/batch: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/cache/batch/precision")
async def get_precision_batch_cache(
    req: Request,
    request_body: PrecisionBatchRequest = Body(...),
    token: str = Depends(verify_token),
    _: None = Depends(verify_rate_limit)
) -> JSONResponse:
    """
    Precision batch cache query endpoint - combined parameter searches in batch (requires authentication).
    
    Allows multiple precise queries where parameters can be combined for specificity.
    Each query item can have multiple parameters that narrow the search.
    
    Request body:
    {
        "queries": [
            {"team": "Lakers", "player": "LeBron James", "sport": "Basketball"},
            {"team": "Warriors", "sport": "Basketball"},
            {"player": "Messi", "sport": "Soccer"},
            {"market": "moneyline"},
            {"league": "Premier League", "sport": "Soccer"}
        ]
    }
    
    Response:
    {
        "results": [
            {
                "query": {"team": "Lakers", "player": "LeBron James", "sport": "Basketball"},
                "found": true,
                "data": {...}
            },
            {
                "query": {"team": "Warriors", "sport": "Basketball"},
                "found": true,
                "data": {...}
            },
            {
                "query": {"player": "Messi", "sport": "Soccer"},
                "found": true,
                "data": {...}
            },
            {
                "query": {"market": "moneyline"},
                "found": true,
                "data": {...}
            },
            {
                "query": {"league": "Premier League", "sport": "Soccer"},
                "found": false,
                "data": null
            }
        ],
        "total_queries": 5,
        "successful": 4,
        "failed": 1
    }
    """
    try:
        query_dicts = [query.model_dump(exclude_none=True) for query in request_body.queries]
        result = await run_in_threadpool(get_precision_batch_cache_entries, query_dicts)
        
        return JSONResponse(
            status_code=200,
            content=result
        )
        
    except Exception as e:
        print(f"[ERROR] POST /cache/batch/precision: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/leagues")
async def get_leagues(
    request: Request,
    sport: Optional[str] = Query(None, description="Filter by sport name (e.g., 'Soccer', 'Basketball')"),
    search: Optional[str] = Query(None, description="Search term to filter league names"),
    region: Optional[str] = Query(None, description="Filter by region (e.g., 'Europe', 'North America')"),
    token: str = Depends(verify_token),
    _: None = Depends(verify_rate_limit)
) -> JSONResponse:
    """
    Get all leagues with optional filtering (requires authentication).
    
    Parameters:
    - sport: Filter by sport name (e.g., 'Soccer', 'Basketball', 'American Football')
    - search: Search term to filter league names (partial match)
    - region: Filter by region
    
    Returns:
    - List of leagues with their details and aliases
    
    Examples:
    - /leagues (get all leagues)
    - /leagues?sport=Soccer
    - /leagues?search=premier
    - /leagues?sport=Soccer&region=europe
    - /leagues?search=NBA
    """
    try:
        result = await run_in_threadpool(
            get_all_leagues,
            sport=sport,
            search=search,
            region=region
        )
        
        return JSONResponse(
            status_code=200,
            content=result
        )
        
    except Exception as e:
        print(f"[ERROR] GET /leagues: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/admin/logs", tags=["admin"])
async def get_request_logs(
    limit: int = 100,
    offset: int = 0,
    session_id: Optional[str] = None,
    path: Optional[str] = None,
    token: str = Depends(verify_admin_token)
):
    """Get request logs (requires admin authentication)"""
    return request_tracking.get_request_logs(
        limit=limit, 
        offset=offset, 
        session_id=session_id, 
        path_filter=path
    )

@app.get("/admin/sessions", tags=["admin"])
async def get_sessions(token: str = Depends(verify_admin_token)):
    """Get active sessions summary (requires admin authentication)"""
    return request_tracking.get_session_summary()

@app.get("/admin/stats/cache", tags=["admin"])
async def get_cache_statistics(token: str = Depends(verify_admin_token)):
    """Get Redis cache statistics (requires admin authentication)"""
    return get_cache_stats()

@app.get("/admin/missing-items", tags=["admin"])
async def get_missing_items(
    item_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = "last_seen",
    token: str = Depends(verify_admin_token)
):
    """Get missing items that were not found in cache or database (requires admin authentication)"""
    return request_tracking.get_missing_items(
        item_type=item_type,
        limit=limit,
        offset=offset,
        sort_by=sort_by
    )

@app.delete("/admin/missing-items", tags=["admin"])
async def clear_missing_items(
    item_type: Optional[str] = None,
    token: str = Depends(verify_admin_token)
):
    """Clear missing items records (requires admin authentication)"""
    request_tracking.clear_missing_items(item_type=item_type)
    return {"message": "Missing items cleared successfully"}


# ─── Token Management Endpoints ─────────────────────────────────────────────────────

@app.get("/admin/tokens", tags=["admin"])
async def list_tokens(token: str = Depends(verify_admin_token)):
    """List all managed tokens with metadata (requires admin authentication)"""
    tokens = await run_in_threadpool(request_tracking.get_all_tokens)
    return {"tokens": tokens, "total": len(tokens)}


@app.post("/admin/tokens", tags=["admin"])
async def create_token(
    request: Request,
    body: CreateTokenRequest = Body(...),
    token: str = Depends(verify_admin_token)
):
    """Create a new managed API token (requires admin authentication). The raw token is returned ONCE — store it immediately."""
    if body.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")
    ip_address = request.client.host if request.client else "unknown"
    result = await run_in_threadpool(
        request_tracking.create_managed_token,
        name=body.name,
        owner=body.owner,
        role=body.role,
        notes=body.notes,
        expires_at=body.expires_at,
        actor="admin",
        ip_address=ip_address,
    )
    return result


@app.put("/admin/tokens/{token_id}/revoke", tags=["admin"])
async def revoke_managed_token(
    request: Request,
    token_id: str,
    body: RevokeRequest = Body(default=RevokeRequest()),
    token: str = Depends(verify_admin_token)
):
    """Revoke a managed token (requires admin authentication)"""
    ip_address = request.client.host if request.client else "unknown"
    ok = await run_in_threadpool(
        request_tracking.revoke_token,
        token_id=token_id,
        actor="admin",
        reason=body.reason,
        ip_address=ip_address,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"status": "revoked", "token_id": token_id}


@app.post("/admin/tokens/{token_id}/rotate", tags=["admin"])
async def rotate_managed_token(
    request: Request,
    token_id: str,
    body: RotateRequest = Body(default=RotateRequest()),
    token: str = Depends(verify_admin_token)
):
    """Rotate a managed token: old token revoked, new token returned once (requires admin authentication)"""
    ip_address = request.client.host if request.client else "unknown"
    result = await run_in_threadpool(
        request_tracking.rotate_token,
        token_id=token_id,
        actor="admin",
        reason=body.reason,
        ip_address=ip_address,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Token not found")
    return result


@app.get("/admin/tokens/audit", tags=["admin"])
async def get_token_audit_log(
    token_id: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    token: str = Depends(verify_admin_token)
):
    """Get token audit log (requires admin authentication)"""
    entries = await run_in_threadpool(
        request_tracking.get_token_audit,
        token_id=token_id,
        limit=limit,
    )
    return {"audit": entries, "total": len(entries)}


# ─── Analytics Endpoints ───────────────────────────────────────────────────────────

@app.get("/admin/analytics/failures", tags=["admin"])
async def analytics_failures(
    hours: int = Query(24, ge=1, le=720),
    token: str = Depends(verify_admin_token)
):
    """Failure analytics grouped by endpoint and status code (requires admin authentication)"""
    return await run_in_threadpool(request_tracking.get_failure_analytics, hours)


@app.get("/admin/analytics/signatures", tags=["admin"])
async def analytics_signatures(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(20, le=100),
    token: str = Depends(verify_admin_token)
):
    """Top failing request signatures with occurrence counts (requires admin authentication)"""
    result = await run_in_threadpool(request_tracking.get_top_failing_signatures, limit, hours)
    return {"signatures": result, "total": len(result), "hours": hours}


@app.get("/admin/analytics/latency", tags=["admin"])
async def analytics_latency(
    hours: int = Query(24, ge=1, le=720),
    token: str = Depends(verify_admin_token)
):
    """Latency percentiles per endpoint (min, avg, p50, p95, p99, max) (requires admin authentication)"""
    result = await run_in_threadpool(request_tracking.get_latency_stats, hours)
    return {"endpoints": result, "hours": hours}


@app.get("/admin/analytics/trends", tags=["admin"])
async def analytics_trends(
    hours: int = Query(24, ge=1, le=720),
    token: str = Depends(verify_admin_token)
):
    """Request volume trends bucketed by hour (requires admin authentication)"""
    result = await run_in_threadpool(request_tracking.get_request_trends, hours)
    return {"buckets": result, "hours": hours}


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(admin_token: Optional[str] = Query(None)):
    """
    Custom Swagger UI. When admin_token is provided as a query parameter, set
    the auth cookie then immediately redirect to /docs (token stripped from URL
    so it never appears in server logs, browser history, or Referer headers).
    """
    if admin_token:
        is_valid = _safe_eq(admin_token, ADMIN_KEY) or request_tracking.is_admin_db_token(admin_token)
        redirect = RedirectResponse(url="/docs", status_code=302)
        if is_valid:
            redirect.set_cookie(
                key="admin_access", value=admin_token,
                max_age=3600, httponly=True, secure=True, samesite="strict"
            )
        return redirect
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=app.title + " - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui.css",
    )

@app.get("/openapi.json", include_in_schema=False)
async def custom_openapi(admin_access: Optional[str] = Cookie(None)):
    """
    Custom OpenAPI schema endpoint that filters admin routes if no valid admin cookie is present.
    """
    if admin_access and (_safe_eq(admin_access, ADMIN_KEY) or request_tracking.is_admin_db_token(admin_access)):
        return get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
    else:
        # User is not admin, filter out routes with "admin" tag
        filtered_routes = []
        for route in app.routes:
            # Check if route is an APIRoute or similar and has tags
            route_tags = getattr(route, "tags", None)
            if route_tags and "admin" in route_tags:
                continue
            filtered_routes.append(route)
        
        return get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=filtered_routes,
        )


if __name__ == "__main__":
    # Run the server on port 5000
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=5000,
        reload=False,
        log_level="info"
    )
