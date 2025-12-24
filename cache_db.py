"""
Cache Database Module
Provides database access to sports data using SQLite.
"""

import sqlite3
import os
from typing import Optional, Dict, Any, List

# Database file path
DB_PATH = os.path.join(os.path.dirname(__file__), "sports_data.db")


def get_db_connection():
    """Create and return a database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_key(value: str) -> str:
    """Normalize a string for cache lookup (lowercase, strip whitespace)"""
    if not value:
        return ""
    return value.lower().strip()


def get_cache_entry(
    market: Optional[str] = None,
    team: Optional[str] = None,
    player: Optional[str] = None,
    sport: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Retrieve cache entry based on provided parameters.
    
    Args:
        market: Market type to look up
        team: Team name to look up
        player: Player name to look up
        sport: Sport name (required when searching by team)
    
    Returns:
        Dictionary with cache entry data or None if not found
    """
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Priority: team > player > market
        if team:
            normalized_team = normalize_key(team)
            normalized_sport = normalize_key(sport) if sport else None
            
            # Search for ALL teams matching name AND sport (case-insensitive)
            if normalized_sport:
                cursor.execute("""
                    SELECT t.id, t.name, t.abbreviation, t.city, t.mascot, t.nickname,
                           l.name as league_name, s.name as sport_name
                    FROM teams t
                    LEFT JOIN leagues l ON t.league_id = l.id
                    LEFT JOIN sports s ON t.sport_id = s.id
                    WHERE (LOWER(t.name) LIKE ? OR LOWER(t.nickname) LIKE ? OR LOWER(t.abbreviation) = ?)
                      AND LOWER(s.name) = ?
                    ORDER BY t.name
                """, (f'%{normalized_team}%', f'%{normalized_team}%', normalized_team, normalized_sport))
            else:
                # Fallback if sport not provided (shouldn't happen due to API validation)
                cursor.execute("""
                    SELECT t.id, t.name, t.abbreviation, t.city, t.mascot, t.nickname,
                           l.name as league_name, s.name as sport_name
                    FROM teams t
                    LEFT JOIN leagues l ON t.league_id = l.id
                    LEFT JOIN sports s ON t.sport_id = s.id
                    WHERE LOWER(t.name) LIKE ? OR LOWER(t.nickname) LIKE ? OR LOWER(t.abbreviation) = ?
                    ORDER BY t.name
                """, (f'%{normalized_team}%', f'%{normalized_team}%', normalized_team))
            
            results = cursor.fetchall()
            if results:
                teams_data = []
                
                # Process each matching team
                for result in results:
                    team_id = result["id"]
                    
                    # Get all players for this team (ONE-TO-MANY relationship)
                    cursor.execute("""
                        SELECT p.id, p.name, p.first_name, p.last_name, p.position, 
                               p.number, p.age, p.height, p.weight
                        FROM players p
                        WHERE p.team_id = ?
                        ORDER BY p.name
                    """, (team_id,))
                    
                    players = [dict(row) for row in cursor.fetchall()]
                    
                    teams_data.append({
                        "id": result["id"],
                        "normalized_name": result["name"],
                        "abbreviation": result["abbreviation"],
                        "city": result["city"],
                        "mascot": result["mascot"],
                        "nickname": result["nickname"],
                        "league": result["league_name"],
                        "sport": result["sport_name"],
                        "players": players,
                        "player_count": len(players)
                    })
                
                return {
                    "type": "team",
                    "query": team,
                    "teams": teams_data,
                    "team_count": len(teams_data)
                }
        
        if player:
            normalized_player = normalize_key(player)
            # Search for player by name (case-insensitive)
            cursor.execute("""
                SELECT p.id, p.name, p.first_name, p.last_name, p.position, p.number,
                       t.name as team_name, l.name as league_name, s.name as sport_name
                FROM players p
                LEFT JOIN teams t ON p.team_id = t.id
                LEFT JOIN leagues l ON p.league_id = l.id
                LEFT JOIN sports s ON p.sport_id = s.id
                WHERE LOWER(p.name) = ? OR LOWER(p.first_name || ' ' || p.last_name) = ?
                LIMIT 1
            """, (normalized_player, normalized_player))
            
            result = cursor.fetchone()
            if result:
                return {
                    "type": "player",
                    "query": player,
                    "normalized_name": result["name"],
                    "first_name": result["first_name"],
                    "last_name": result["last_name"],
                    "position": result["position"],
                    "number": result["number"],
                    "team": result["team_name"],
                    "league": result["league_name"],
                    "sport": result["sport_name"]
                }
        
        if market:
            normalized_market = normalize_key(market)
            # Search for market by name (case-insensitive)
            cursor.execute("""
                SELECT m.id, m.name, m.market_type_id
                FROM markets m
                WHERE LOWER(m.name) = ?
                LIMIT 1
            """, (normalized_market,))
            
            result = cursor.fetchone()
            if result:
                # Get associated sports for this market
                cursor.execute("""
                    SELECT s.name
                    FROM market_sports ms
                    JOIN sports s ON ms.sport_id = s.id
                    WHERE ms.market_id = ?
                """, (result["id"],))
                sports = [row["name"] for row in cursor.fetchall()]
                
                return {
                    "type": "market",
                    "query": market,
                    "normalized_name": result["name"],
                    "market_type_id": result["market_type_id"],
                    "sports": sports
                }
        
        # No match found
        return None
        
    finally:
        conn.close()


def get_all_teams() -> List[Dict[str, Any]]:
    """Get all teams from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT t.id, t.name, t.abbreviation, l.name as league_name, s.name as sport_name
            FROM teams t
            LEFT JOIN leagues l ON t.league_id = l.id
            LEFT JOIN sports s ON t.sport_id = s.id
        """)
        
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_all_players() -> List[Dict[str, Any]]:
    """Get all players from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT p.id, p.name, p.position, t.name as team_name, l.name as league_name, s.name as sport_name
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            LEFT JOIN leagues l ON p.league_id = l.id
            LEFT JOIN sports s ON p.sport_id = s.id
        """)
        
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_all_markets() -> List[Dict[str, Any]]:
    """Get all markets from database"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT m.id, m.name, m.market_type_id
            FROM markets m
        """)
        
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
