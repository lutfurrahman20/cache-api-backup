"""
FastAPI Cache API Service
Provides normalized cache lookups for sports betting markets, teams, and players.
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from typing import Optional, Dict, Any
import uvicorn
from cache_db import get_cache_entry

app = FastAPI(
    title="Cache API",
    description="Sports betting cache normalization service",
    version="1.0.0"
)

@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "online",
        "service": "Cache API",
        "version": "1.0.0"
    }

@app.get("/health")
async def health_check():
    """Health check for monitoring"""
    return {"status": "healthy"}

@app.get("/cache")
async def get_cache(
    market: Optional[str] = Query(None, description="Market type (e.g., 'moneyline', 'spread', 'total')"),
    team: Optional[str] = Query(None, description="Team name to look up"),
    player: Optional[str] = Query(None, description="Player name to look up"),
    sport: Optional[str] = Query(None, description="Sport name (required when searching by team)")
) -> JSONResponse:
    """
    Get normalized cache entry for market, team, or player.
    
    Parameters:
    - market: Market type to look up
    - team: Team name to normalize
    - player: Player name to normalize
    - sport: Sport name (required when searching by team)
    
    Returns:
    - Mapped/normalized entry from cache database
    
    Examples:
    - /cache?team=Lakers&sport=Basketball
    - /cache?player=LeBron James
    - /cache?market=moneyline
    """
    
    # Validate that at least one parameter is provided
    if not any([market, team, player]):
        raise HTTPException(
            status_code=400,
            detail="At least one parameter (market, team, or player) must be provided"
        )
    
    # Validate that sport is provided when searching by team (unless both team and player provided)
    if team and not player and not sport:
        raise HTTPException(
            status_code=400,
            detail="Sport parameter is required when searching by team only"
        )
    
    # Get the cache entry
    try:
        result = get_cache_entry(market=market, team=team, player=player, sport=sport)
        
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
                        "sport": sport
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
                    "sport": sport
                }
            }
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving cache entry: {str(e)}"
        )

if __name__ == "__main__":
    # Run the server on port 8001
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        reload=False,
        log_level="info"
    )
