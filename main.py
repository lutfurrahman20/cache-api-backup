"""
FastAPI Cache API Service
Provides normalized cache lookups for sports betting markets, teams, and players.
Includes Redis caching layer for improved performance.
"""

from fastapi import FastAPI, Query, HTTPException, Body, Security, Depends, Request, Cookie, Form
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.concurrency import run_in_threadpool
from typing import Optional, Dict, Any, List
from pydantic import BaseModel
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
    print("⚙️  Detected Windows local environment; configuring Redis host to localhost:6379")

from cache_db import get_cache_entry, get_batch_cache_entries, get_precision_batch_cache_entries, get_all_leagues
from redis_cache import get_cache_stats, clear_all_cache, invalidate_cache
import uuid
import request_tracking
import uuid_tracking

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

# Rate limiting configuration
RATE_LIMIT_PER_MINUTE = int(os.getenv('RATE_LIMIT_PER_MINUTE', 60))
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
    
    Raises:
        HTTPException: If rate limit is exceeded
    """
    client_ip = request.client.host if request.client else "unknown"
    
    if not check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded. Maximum {RATE_LIMIT_PER_MINUTE} requests per minute allowed."
        )

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """
    Verify the API token from the Authorization header.
    Allows both admin and non-admin tokens.
    
    Raises:
        HTTPException: If token is invalid or missing
    
    Returns:
        The validated token
    """
    if not VALID_API_TOKENS:
        raise HTTPException(
            status_code=500,
            detail="No API tokens configured. Please set API_TOKEN in environment variables."
        )
    
    token = credentials.credentials
    if token not in VALID_API_TOKENS:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API token"
        )
    
    return token

def verify_admin_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """
    Verify the API token is an admin token.
    Restricts access to admin-only endpoints.
    
    Raises:
        HTTPException: If token is invalid, missing, or not an admin token
    
    Returns:
        The validated admin token
    """
    token = credentials.credentials
    if token != ADMIN_KEY:
        raise HTTPException(
            status_code=403,
            detail="Admin access required. This endpoint requires an admin API token."
        )
    
    return token

app = FastAPI(
    title="Cache API",
    description="Sports betting cache normalization service with Redis caching",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

# On startup, ensure dockerized Redis is running when we're in a local
# Windows environment and no explicit REDIS_HOST has been provided.
@app.on_event("startup")
async def startup_containers():
    if platform.system() == "Windows" and not os.getenv('REDIS_HOST'):
        ensure_redis_container()

# Mount static files for dashboard
app.mount("/admin/js", StaticFiles(directory="js"), name="admin_js")
app.mount("/admin/css", StaticFiles(directory="css"), name="admin_css")

@app.get("/admin/dashboard", tags=["admin"])
async def serve_dashboard(admin_access: Optional[str] = Cookie(None)):
        """Serve the admin dashboard (cookie-auth in browser)."""
        if admin_access and admin_access == ADMIN_KEY:
                return FileResponse("dashboard.html")

        return HTMLResponse(
                status_code=401,
                content="""
<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>Admin Login</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f5f7fb; margin: 0; }
        .card { max-width: 420px; margin: 12vh auto; background: #fff; padding: 24px; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); }
        h2 { margin-top: 0; font-size: 22px; }
        p { color: #555; }
        input { width: 100%; box-sizing: border-box; padding: 10px; margin: 10px 0 14px; border: 1px solid #d0d7e2; border-radius: 6px; }
        button { width: 100%; padding: 10px; border: 0; border-radius: 6px; background: #1f6feb; color: #fff; font-weight: 600; cursor: pointer; }
    </style>
</head>
<body>
    <div class=\"card\">
        <h2>Admin Dashboard Login</h2>
        <p>Enter your admin token to continue.</p>
        <form method=\"post\" action=\"/admin/dashboard/login\">
            <input type=\"password\" name=\"admin_token\" placeholder=\"Admin token\" required />
            <button type=\"submit\">Open Dashboard</button>
        </form>
    </div>
</body>
</html>
                """,
        )


@app.post("/admin/dashboard/login", tags=["admin"], include_in_schema=False)
async def dashboard_login(admin_token: str = Form(...)):
        if admin_token != ADMIN_KEY:
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
    
    # Process request
    response = await call_next(request)
    
    # Post-processing time
    process_time = (time.time() - start_time) * 1000
    
    try:
        # Extract IP
        ip_address = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        
        # Extract Token
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            
        if token:
            # Check if admin (ADMIN_KEY defined in global scope)
            is_admin = (token == ADMIN_KEY)
            
            # Only track non-admin users
            if not is_admin:
                # Generate deterministic UUID from token
                # This ensures the same token always maps to the same UUID
                user_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, token))
                
                # Create/Get Session
                session_id = request_tracking.get_or_create_session(
                    token=token,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    user_identifier=user_uuid
                )
                
                # Track UUID Login (Geo Location)
                uuid_tracking.track_uuid_login(
                    uuid=user_uuid,
                    ip_address=ip_address,
                    user_agent=user_agent
                )
                
                # Track Request Details
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
                    uuid=user_uuid
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
        
        return JSONResponse(
            status_code=200,
            content={
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
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving cache entry: {str(e)}"
        )

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
        
        return JSONResponse(
            status_code=200,
            content=result
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing batch query: {str(e)}"
        )

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
        raise HTTPException(
            status_code=500,
            detail=f"Error processing precision batch query: {str(e)}"
        )


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
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving leagues: {str(e)}"
        )


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

@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(admin_token: Optional[str] = Query(None)):
    """
    Custom Swagger UI that can set an admin cookie if provided in query param.
    """
    response = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=app.title + " - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui-bundle.js",
        swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.9.0/swagger-ui.css",
    )
    if admin_token and admin_token == ADMIN_KEY:
        # Set max_age to 1 hour (3600 seconds)
        response.set_cookie(key="admin_access", value=admin_token, max_age=3600, httponly=True, secure=True, samesite="strict")
    return response

@app.get("/openapi.json", include_in_schema=False)
async def custom_openapi(admin_access: Optional[str] = Cookie(None)):
    """
    Custom OpenAPI schema endpoint that filters admin routes if no valid admin cookie is present.
    """
    if admin_access and admin_access == ADMIN_KEY:
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
