"""
Cache Database Module
Provides database access to sports data using SQLite with Redis caching.
"""

import sqlite3
import os
import time
from typing import Optional, Dict, Any, List
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from redis_cache import get_cached_data, set_cached_data

# Database file path
DB_PATH = os.path.join(os.path.dirname(__file__), "sports_data.db")


def get_db_connection():
    """Create and return a database connection with optimizations"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrent read performance
    conn.execute("PRAGMA journal_mode=WAL")
    # Increase cache size (default is 2MB, set to 10MB)
    conn.execute("PRAGMA cache_size=-10000")
    # Use memory for temporary tables
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def normalize_key(value: str) -> str:
    """Normalize a string for cache lookup (lowercase, strip whitespace)"""
    if not value:
        return ""
    return value.lower().strip()


def get_league_priority(league_name: str) -> int:
    """
    Get sorting priority for leagues.
    Lower number = higher priority
    """
    if not league_name:
        return 999
    
    league_lower = league_name.lower()
    
    # Priority leagues
    if 'premier league' in league_lower and 'england' in league_lower:
        return 1
    elif 'la liga' in league_lower or ('liga' in league_lower and 'spain' in league_lower):
        return 2
    elif 'bundesliga' in league_lower or ('bundesliga' in league_lower and 'germany' in league_lower):
        return 3
    elif 'serie a' in league_lower or ('serie' in league_lower and 'italy' in league_lower):
        return 4
    elif 'ligue 1' in league_lower or ('ligue' in league_lower and 'france' in league_lower):
        return 5
    else:
        return 999  # All other leagues


def expand_sports_terms(text: str) -> str:
    """
    Expand common sports abbreviations for better fuzzy matching.
    """
    text = text.lower()
    replacements = {
        "rush ": "rushing ",
        "rec ": "receiving ",
        "tds": "touchdowns",
        "ints": "interceptions",
        "fg": "field goal",
        "xp": "extra point",
        "1h": "1st half",
        "2h": "2nd half",
        "1st": "1st", # ensure casing standard if needed, though we use lower()
        "yrds": "yards",
        "yds": "yards",
        "att": "attempts"
    }
    
    for abbr, full in replacements.items():
        if abbr in text:
            text = text.replace(abbr, full)
            
    return text

def get_cache_entry(
    market: Optional[str] = None,
    team: Optional[str] = None,
    player: Optional[str] = None,
    sport: Optional[str] = None,
    league: Optional[str] = None,
    active_connection: Optional[sqlite3.Connection] = None
) -> Optional[Dict[str, Any]]:
    """
    Retrieve cache entry based on provided parameters.
    Uses Redis for caching with fallback to SQLite database.
    
    Relationships:
    - ONE-TO-MANY: Team → Players (one team has many players)
    - ONE-TO-ONE: Player → Team (each player belongs to exactly one team)
    - ONE-TO-MANY: League → Teams (one league has many teams)
    
    Args:
        market: Market type to look up
        team: Team name to look up
        player: Player name to look up
        sport: Sport name
        league: League name
        active_connection: Optional existing DB connection to reuse (optimization for batch queries)
    
    Returns:
        Dictionary with cache entry data or None if not found
    """
    
    # Try to get from Redis cache first
    cached_result = get_cached_data(market=market, team=team, player=player, sport=sport, league=league)
    if cached_result is not None:
        return cached_result
    
    # Cache miss - query database
    if active_connection:
        conn = active_connection
        should_close = False
    else:
        conn = get_db_connection()
        should_close = True
        
    cursor = conn.cursor()
    
    try:
        # Special case: BOTH team AND player provided - search for player filtered by team
        if team and player:
            normalized_team = normalize_key(team)
            normalized_player = normalize_key(player)
            normalized_sport = normalize_key(sport) if sport else None
            
            # Search for player in BOTH player_aliases AND players table
            # 1. Check player_aliases
            cursor.execute("""
                SELECT DISTINCT player_id FROM player_aliases
                WHERE LOWER(alias) = ?
            """, (normalized_player,))
            player_ids_from_aliases = [row[0] for row in cursor.fetchall()]
            
            # 2. Check players table directly
            # Try exact match first (much faster)
            cursor.execute("""
                SELECT DISTINCT id FROM players
                WHERE name = ? COLLATE NOCASE
            """, (player.strip(),))
            player_ids_from_main = [row[0] for row in cursor.fetchall()]

            if not player_ids_from_main and len(normalized_player) > 2:
                # Fallback to slower partial match - Try prefix first (Index Friendly)
                cursor.execute("""
                    SELECT DISTINCT id FROM players
                    WHERE name LIKE ? OR first_name LIKE ? OR last_name LIKE ?
                """, (f"{normalized_player}%", f"{normalized_player}%", f"{normalized_player}%"))
                player_ids_from_main = [row[0] for row in cursor.fetchall()]

                # STRICT PERFORMANCE MODE: Disabled full wildcard scan for players
                # Only prefix matching is allowed to prevent DB lockups during batch processing.
                # If specific fuzzy matching is needed, use a dedicated search endpoint or search service.


            
            player_ids = list(set(player_ids_from_aliases + player_ids_from_main))
            
            if not player_ids:
                return None
            
            # Search for team in BOTH team_aliases AND teams table
            # 1. Check team_aliases
            cursor.execute("""
                SELECT DISTINCT team_id FROM team_aliases
                WHERE LOWER(alias) = ?
            """, (normalized_team,))
            team_ids_from_aliases = [row[0] for row in cursor.fetchall()]
            
            # 2. Check teams table directly
            team_ids_from_main = []
            
            # Try exact match first
            if normalized_sport:
                cursor.execute("""
                    SELECT DISTINCT t.id FROM teams t
                    LEFT JOIN sports s ON t.sport_id = s.id
                    WHERE (t.name = ? COLLATE NOCASE OR t.abbreviation = ? COLLATE NOCASE)
                      AND LOWER(s.name) = ?
                """, (team.strip(), team.strip(), normalized_sport))
            else:
                cursor.execute("""
                    SELECT DISTINCT id FROM teams
                    WHERE name = ? COLLATE NOCASE OR abbreviation = ? COLLATE NOCASE
                """, (team.strip(), team.strip()))
            
            team_ids_from_main = [row[0] for row in cursor.fetchall()]
            
            if not team_ids_from_main:
                # Fallback to slower partial match
                if normalized_sport:
                    cursor.execute("""
                        SELECT DISTINCT t.id FROM teams t
                        LEFT JOIN sports s ON t.sport_id = s.id
                        WHERE (LOWER(t.name) LIKE ? OR LOWER(t.nickname) LIKE ? OR LOWER(t.abbreviation) = ?)
                          AND LOWER(s.name) = ?
                    """, (f"%{normalized_team}%", f"%{normalized_team}%", normalized_team, normalized_sport))
                else:
                    cursor.execute("""
                        SELECT DISTINCT id FROM teams
                        WHERE LOWER(name) LIKE ? OR LOWER(nickname) LIKE ? OR LOWER(abbreviation) = ?
                    """, (f"%{normalized_team}%", f"%{normalized_team}%", normalized_team))
                
                team_ids_from_main = [row[0] for row in cursor.fetchall()]
            
            team_ids = list(set(team_ids_from_aliases + team_ids_from_main))
            
            if not team_ids:
                return None
            
            # Search for player(s) matching player_id AND team_id, filtered by sport if provided
            placeholders_players = ','.join('?' * len(player_ids))
            placeholders_teams = ','.join('?' * len(team_ids))
            
            if normalized_sport:
                cursor.execute(f"""
                    SELECT p.id, p.name, p.first_name, p.last_name, p.position, p.number,
                           p.age, p.height, p.weight,
                           t.name as team_name, t.abbreviation, t.city,
                           l.name as league_name, s.name as sport_name
                    FROM players p
                    JOIN teams t ON p.team_id = t.id
                    LEFT JOIN leagues l ON p.league_id = l.id
                    LEFT JOIN sports s ON p.sport_id = s.id
                    WHERE p.id IN ({placeholders_players})
                      AND p.team_id IN ({placeholders_teams})
                      AND LOWER(s.name) = ?
                    ORDER BY p.name
                """, (*player_ids, *team_ids, normalized_sport))
            else:
                cursor.execute(f"""
                    SELECT p.id, p.name, p.first_name, p.last_name, p.position, p.number,
                           p.age, p.height, p.weight,
                           t.name as team_name, t.abbreviation, t.city,
                           l.name as league_name, s.name as sport_name
                    FROM players p
                    JOIN teams t ON p.team_id = t.id
                    LEFT JOIN leagues l ON p.league_id = l.id
                    LEFT JOIN sports s ON p.sport_id = s.id
                    WHERE p.id IN ({placeholders_players})
                      AND p.team_id IN ({placeholders_teams})
                    ORDER BY p.name
                """, (*player_ids, *team_ids))
            
            results = cursor.fetchall()
            if results:
                players_data = []
                
                for result in results:
                    players_data.append({
                        "id": result["id"],
                        "normalized_name": result["name"],
                        "first_name": result["first_name"],
                        "last_name": result["last_name"],
                        "position": result["position"],
                        "number": result["number"],
                        "age": result["age"],
                        "height": result["height"],
                        "weight": result["weight"],
                        "team": result["team_name"],
                        "team_abbreviation": result["abbreviation"],
                        "team_city": result["city"],
                        "league": result["league_name"],
                        "sport": result["sport_name"]
                    })
                
                result_data = {
                    "type": "player",
                    "query": {
                        "player": player,
                        "team": team,
                        "sport": sport
                    },
                    "players": players_data,
                    "player_count": len(players_data)
                }
                
                # Cache the result
                set_cached_data(result_data, market=market, team=team, player=player, sport=sport)
                return result_data
            else:
                # No player found in that specific team
                return None
        
        # Priority: team > player > market
        if team:
            normalized_team = normalize_key(team)
            normalized_sport = normalize_key(sport) if sport else None
            
            # Search in BOTH team_aliases AND teams table
            # 1. Check team_aliases table
            cursor.execute("""
                SELECT DISTINCT team_id FROM team_aliases
                WHERE LOWER(alias) = ?
            """, (normalized_team,))
            team_ids_from_aliases = [row[0] for row in cursor.fetchall()]
            
            # 2. Check teams table directly (name, nickname, abbreviation)
            team_ids_from_main = []
            
            # Try exact match first
            if normalized_sport:
                cursor.execute("""
                    SELECT DISTINCT t.id FROM teams t
                    LEFT JOIN sports s ON t.sport_id = s.id
                    WHERE (t.name = ? COLLATE NOCASE OR t.abbreviation = ? COLLATE NOCASE)
                      AND LOWER(s.name) = ?
                """, (team.strip(), team.strip(), normalized_sport))
            else:
                cursor.execute("""
                    SELECT DISTINCT id FROM teams
                    WHERE name = ? COLLATE NOCASE OR abbreviation = ? COLLATE NOCASE
                """, (team.strip(), team.strip()))
                
            team_ids_from_main = [row[0] for row in cursor.fetchall()]
            
            # For short strings, SKIP fuzzy search to prevent performance kill (LIKE '%a%' matches everything)
            if not team_ids_from_main and len(normalized_team) > 2:
                if normalized_sport:
                    # Try prefix match first (Index Friendly)
                    cursor.execute("""
                        SELECT DISTINCT t.id FROM teams t
                        LEFT JOIN sports s ON t.sport_id = s.id
                        WHERE (t.name LIKE ? OR t.nickname LIKE ? OR t.abbreviation = ?)
                          AND LOWER(s.name) = ?
                    """, (f"{normalized_team}%", f"{normalized_team}%", normalized_team, normalized_sport))
                    team_ids_from_main = [row[0] for row in cursor.fetchall()]
                    
                    # STRICT PERFORMANCE MODE: Disabled full wildcard scan for teams
                    
                else:
                    # Try prefix match first (Index Friendly)
                    cursor.execute("""
                        SELECT DISTINCT id FROM teams
                        WHERE name LIKE ? OR nickname LIKE ? OR abbreviation = ?
                    """, (f"{normalized_team}%", f"{normalized_team}%", normalized_team))
                    team_ids_from_main = [row[0] for row in cursor.fetchall()]

                    # STRICT PERFORMANCE MODE: Disabled full wildcard scan for teams

            team_ids = list(set(team_ids_from_aliases + team_ids_from_main))
            
            if not team_ids:
                return None
            
            # Search for ALL teams matching team_id(s) AND sport (case-insensitive)
            placeholders = ','.join('?' * len(team_ids))
            
            if normalized_sport:
                cursor.execute(f"""
                    SELECT t.id, t.name, t.abbreviation, t.city, t.mascot, t.nickname,
                           l.name as league_name, s.name as sport_name
                    FROM teams t
                    LEFT JOIN leagues l ON t.league_id = l.id
                    LEFT JOIN sports s ON t.sport_id = s.id
                    WHERE t.id IN ({placeholders})
                      AND LOWER(s.name) = ?
                    ORDER BY t.name
                """, (*team_ids, normalized_sport))
            else:
                # Fallback if sport not provided (shouldn't happen due to API validation)
                cursor.execute(f"""
                    SELECT t.id, t.name, t.abbreviation, t.city, t.mascot, t.nickname,
                           l.name as league_name, s.name as sport_name
                    FROM teams t
                    LEFT JOIN leagues l ON t.league_id = l.id
                    LEFT JOIN sports s ON t.sport_id = s.id
                    WHERE t.id IN ({placeholders})
                    ORDER BY t.name
                """, tuple(team_ids))
            
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
                    
                    player_rows = cursor.fetchall()
                    team_filename = result["name"].replace(" ", "_")
                    sport_lower = (result["sport_name"] or "").lower()
                    league_lower = (result["league_name"] or "").lower()
                    team_folder = result["name"].replace(" ", "_").lower()

                    players = []
                    for p_row in player_rows:
                        p_dict = dict(p_row)
                        player_filename = (p_row["name"] or "").replace(" ", "_").lower()
                        p_logo_path = os.path.join(os.path.dirname(__file__), "static", "logo", "players", sport_lower, league_lower, team_folder, f"{player_filename}.png")
                        p_dict["logo_url"] = f"/static/logo/players/{sport_lower}/{league_lower}/{team_folder}/{player_filename}.png" if os.path.exists(p_logo_path) else None
                        players.append(p_dict)

                    logo_path = os.path.join(os.path.dirname(__file__), "static", "logo", "teams", sport_lower, league_lower, f"{team_filename}.png")
                    logo_url = f"/static/logo/teams/{sport_lower}/{league_lower}/{team_filename}.png" if os.path.exists(logo_path) else None

                    teams_data.append({
                        "id": result["id"],
                        "normalized_name": result["name"],
                        "abbreviation": result["abbreviation"],
                        "city": result["city"],
                        "mascot": result["mascot"],
                        "nickname": result["nickname"],
                        "league": result["league_name"],
                        "sport": result["sport_name"],
                        "logo_url": logo_url,
                        "players": players,
                        "player_count": len(players)
                    })
                
                # Sort teams by league priority
                teams_data.sort(key=lambda x: (get_league_priority(x.get("league", "")), x.get("normalized_name", "")))
                
                result_data = {
                    "type": "team",
                    "query": team,
                    "teams": teams_data,
                    "team_count": len(teams_data)
                }
                
                # Cache the result
                set_cached_data(result_data, market=market, team=team, player=player, sport=sport)
                return result_data
        
        if player:
            normalized_player = normalize_key(player)
            
            # Search in BOTH player_aliases AND players table
            # 1. Check player_aliases table
            cursor.execute("""
                SELECT DISTINCT player_id FROM player_aliases
                WHERE LOWER(alias) = ?
            """, (normalized_player,))
            player_ids_from_aliases = [row[0] for row in cursor.fetchall()]
            
            # 2. Check players table directly (name, first_name, last_name)
            # Try exact match first
            cursor.execute("""
                SELECT DISTINCT id FROM players
                WHERE name = ? COLLATE NOCASE
            """, (player.strip(),))
            player_ids_from_main = [row[0] for row in cursor.fetchall()]
            
            if not player_ids_from_main and len(normalized_player) > 2:
                cursor.execute("""
                    SELECT DISTINCT id FROM players
                    WHERE LOWER(name) LIKE ? OR LOWER(first_name) LIKE ? OR LOWER(last_name) LIKE ?
                """, (f"%{normalized_player}%", f"%{normalized_player}%", f"%{normalized_player}%"))
                player_ids_from_main = [row[0] for row in cursor.fetchall()]
            
            # Combine and deduplicate player IDs
            player_ids = list(set(player_ids_from_aliases + player_ids_from_main))
            
            if not player_ids:
                return None
            
            # Search for ALL players with matching player_id (case-insensitive)
            placeholders = ','.join('?' * len(player_ids))
            cursor.execute(f"""
                SELECT p.id, p.name, p.first_name, p.last_name, p.position, p.number,
                       p.age, p.height, p.weight,
                       t.name as team_name, l.name as league_name, s.name as sport_name
                FROM players p
                LEFT JOIN teams t ON p.team_id = t.id
                LEFT JOIN leagues l ON p.league_id = l.id
                LEFT JOIN sports s ON p.sport_id = s.id
                WHERE p.id IN ({placeholders})
                ORDER BY p.name
            """, tuple(player_ids))
            
            results = cursor.fetchall()
            if results:
                players_data = []
                
                for result in results:
                    players_data.append({
                        "id": result["id"],
                        "normalized_name": result["name"],
                        "first_name": result["first_name"],
                        "last_name": result["last_name"],
                        "position": result["position"],
                        "number": result["number"],
                        "age": result["age"],
                        "height": result["height"],
                        "weight": result["weight"],
                        "team": result["team_name"],
                        "league": result["league_name"],
                        "sport": result["sport_name"]
                    })
                
                # Sort players by league priority
                players_data.sort(key=lambda x: (get_league_priority(x.get("league", "")), x.get("normalized_name", "")))
                
                result_data = {
                    "type": "player",
                    "query": player,
                    "players": players_data,
                    "player_count": len(players_data)
                }
                
                # Cache the result
                set_cached_data(result_data, market=market, team=team, player=player, sport=sport)
                return result_data
        
        if league:
            normalized_league = normalize_key(league)
            normalized_sport = normalize_key(sport) if sport else None
            
            # Search in BOTH league_aliases AND leagues table
            # 1. Check league_aliases table
            cursor.execute("""
                SELECT DISTINCT league_id FROM league_aliases
                WHERE LOWER(alias) = ?
            """, (normalized_league,))
            league_ids_from_aliases = [row[0] for row in cursor.fetchall()]
            
            # 2. Check leagues table directly
            league_ids_from_main = []
            
            # Try exact match first
            if normalized_sport:
                cursor.execute("""
                    SELECT DISTINCT l.id FROM leagues l
                    LEFT JOIN sports s ON l.sport_id = s.id
                    WHERE l.name = ? COLLATE NOCASE
                      AND LOWER(s.name) = ?
                """, (league.strip(), normalized_sport))
            else:
                cursor.execute("""
                    SELECT DISTINCT id FROM leagues
                    WHERE name = ? COLLATE NOCASE
                """, (league.strip(),))
            
            league_ids_from_main = [row[0] for row in cursor.fetchall()]

            if not league_ids_from_main:
                if normalized_sport:
                    cursor.execute("""
                        SELECT DISTINCT l.id FROM leagues l
                        LEFT JOIN sports s ON l.sport_id = s.id
                        WHERE LOWER(l.name) LIKE ?
                          AND LOWER(s.name) = ?
                    """, (f"%{normalized_league}%", normalized_sport))
                else:
                    cursor.execute("""
                        SELECT DISTINCT id FROM leagues
                        WHERE LOWER(name) LIKE ?
                    """, (f"%{normalized_league}%",))
                
                league_ids_from_main = [row[0] for row in cursor.fetchall()]
            
            # Combine and deduplicate league IDs
            league_ids = list(set(league_ids_from_aliases + league_ids_from_main))
            
            if not league_ids:
                return None
            
            # Search for ALL leagues matching league_id(s), filtered by sport if provided
            placeholders = ','.join('?' * len(league_ids))
            
            if normalized_sport:
                cursor.execute(f"""
                    SELECT l.id, l.name, s.name as sport_name
                    FROM leagues l
                    LEFT JOIN sports s ON l.sport_id = s.id
                    WHERE l.id IN ({placeholders})
                      AND LOWER(s.name) = ?
                    ORDER BY l.name
                """, (*league_ids, normalized_sport))
            else:
                cursor.execute(f"""
                    SELECT l.id, l.name, s.name as sport_name
                    FROM leagues l
                    LEFT JOIN sports s ON l.sport_id = s.id
                    WHERE l.id IN ({placeholders})
                    ORDER BY l.name
                """, tuple(league_ids))
            
            results = cursor.fetchall()
            if results:
                leagues_data = []
                
                # Process each matching league
                for result in results:
                    league_id = result["id"]
                    
                    # Get all teams for this league (ONE-TO-MANY relationship)
                    cursor.execute("""
                        SELECT t.id, t.name, t.abbreviation, t.city, t.mascot, t.nickname
                        FROM teams t
                        WHERE t.league_id = ?
                        ORDER BY t.name
                    """, (league_id,))
                    
                    team_rows = cursor.fetchall()
                    sport_lower = (result["sport_name"] or "").lower()
                    league_lower = (result["name"] or "").lower()

                    teams = []
                    for t_row in team_rows:
                        t_dict = dict(t_row)
                        team_filename = (t_row["name"] or "").replace(" ", "_")
                        t_logo_path = os.path.join(os.path.dirname(__file__), "static", "logo", "teams", sport_lower, league_lower, f"{team_filename}.png")
                        t_dict["logo_url"] = f"/static/logo/teams/{sport_lower}/{league_lower}/{team_filename}.png" if os.path.exists(t_logo_path) else None
                        teams.append(t_dict)

                    leagues_data.append({
                        "id": result["id"],
                        "normalized_name": result["name"],
                        "sport": result["sport_name"],
                        "teams": teams,
                        "team_count": len(teams)
                    })
                
                # Sort leagues by priority
                leagues_data.sort(key=lambda x: (get_league_priority(x.get("normalized_name", "")), x.get("normalized_name", "")))
                
                result_data = {
                    "type": "league",
                    "query": league,
                    "leagues": leagues_data,
                    "league_count": len(leagues_data)
                }
                
                # Cache the result
                set_cached_data(result_data, market=market, team=team, player=player, sport=sport, league=league)
                return result_data
        
        if market:
            normalized_market = normalize_key(market)
            # Create a stripped version for robust matching (no spaces, no underscores)
            market_search_term = market.lower().replace(" ", "").replace("_", "")
            
            # First, try exact alias match (normalized)
            cursor.execute("""
                SELECT DISTINCT market_id FROM market_aliases
                WHERE LOWER(REPLACE(REPLACE(alias, ' ', ''), '_', '')) = ?
                LIMIT 1
            """, (market_search_term,))
            
            alias_result = cursor.fetchone()
            
            if alias_result:
                market_id = alias_result[0]
            else:
                # No exact alias match, try direct match on markets table
                # 1. Try exact stripped match
                cursor.execute("""
                    SELECT id FROM markets
                    WHERE LOWER(REPLACE(REPLACE(name, ' ', ''), '_', '')) = ?
                    LIMIT 1
                """, (market_search_term,))
                direct_result = cursor.fetchone()
                
                if direct_result:
                    market_id = direct_result[0]
                else:
                    # 2. Try fuzzy contain match (e.g. "Completion Percentage" -> "Player Passing Completion Percentage")
                    # We normalize the input first to ensure good matching
                    normalized_input = market.lower().strip()
                    
                    # Search 2a: Direct fuzzy on input (Prefix first)
                    cursor.execute("""
                        SELECT id FROM markets
                        WHERE name LIKE ?
                        ORDER BY LENGTH(name) ASC 
                        LIMIT 1
                    """, (f"{normalized_input}%",))
                    fuzzy_result = cursor.fetchone()
                    
                    if not fuzzy_result and len(normalized_input) > 3:
                        # STRICT PERFORMANCE MODE: Disabled full wildcard scan for markets
                        pass

                    if fuzzy_result:
                         market_id = fuzzy_result[0]
                    else:
                        # Search 2b: Expanded abbreviations (Rush -> Rushing, etc)
                        expanded_input = expand_sports_terms(normalized_input)
                        if expanded_input != normalized_input:
                            cursor.execute("""
                                SELECT id FROM markets
                                WHERE name LIKE ?
                                ORDER BY LENGTH(name) ASC
                                LIMIT 1
                            """, (f"{expanded_input}%",))
                            expanded_result = cursor.fetchone()
                            
                            if not expanded_result and len(expanded_input) > 3:
                                # STRICT PERFORMANCE MODE: Disabled full wildcard scan for markets
                                pass

                            if expanded_result:
                                market_id = expanded_result[0]
                            else:
                                return None
                        else:
                            return None
            
            # Search for market by resolved ID
            cursor.execute("""
                SELECT m.id, m.name, m.market_type_id
                FROM markets m
                WHERE m.id = ?
            """, (market_id,))
            
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
                
                result_data = {
                    "type": "market",
                    "query": market,
                    "normalized_name": result["name"],
                    "market_type_id": result["market_type_id"],
                    "sports": sports
                }
                
                # Cache the result
                set_cached_data(result_data, market=market, team=team, player=player, sport=sport)
                return result_data
        
        # No match found
        return None
        
    finally:
        cursor.close()
        if should_close:
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


    try:
        cursor.execute("""
            SELECT m.id, m.name, m.market_type_id
            FROM markets m
        """)
        
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _resolve_batch_teams(conn: sqlite3.Connection, team_names: List[str], sport: Optional[str]) -> Dict[str, Any]:
    """Batch resolve teams using optimized set-based queries"""
    results = {}
    normalized_sport = normalize_key(sport) if sport else None
    
    # 1. Maximize cache hits by mapping detailed inputs to normalized keys
    input_map = {}
    for original in team_names:
        norm = normalize_key(original)
        if norm:
            if norm not in input_map:
                input_map[norm] = []
            input_map[norm].append(original)
    
    unique_norms = list(input_map.keys())
    if not unique_norms:
        return {}

    found_ids = defaultdict(set) # norm -> set of team_ids
    
    # Chunking to prevent SQLite limit errors
    for chunk in _chunk_list(unique_norms, 50):
        # 2. Check Aliases (Bulk)
        if not chunk: continue
        
        placeholders = ','.join('?' * len(chunk))
        cursor = conn.execute(f"SELECT alias, team_id FROM team_aliases WHERE alias IN ({placeholders})", chunk)
        for row in cursor:
            found_ids[row[0]].add(row[1])
            
        # 3. Check Main Table (Exact Match)
        # We search ALL items in chunk, not just remaining, to find all possible matches
        placeholders = ','.join('?' * len(chunk))
        # Check name, nickname, abbreviation
        query = f"""
            SELECT id, name, nickname, abbreviation FROM teams 
            WHERE name COLLATE NOCASE IN ({placeholders})
            OR nickname COLLATE NOCASE IN ({placeholders})
            OR abbreviation COLLATE NOCASE IN ({placeholders})
        """
        cursor = conn.execute(query, chunk * 3)
        
        for row in cursor:
            tid = row[0]
            # Map back to all matching inputs
            matches = [row[1], row[2], row[3]] # name, nick, abbrev
            for m in matches:
                if not m: continue
                norm_m = normalize_key(m)
                
                # Direct match check
                if norm_m in chunk:
                    found_ids[norm_m].add(tid)
                    
                # Double check against chunk keys
                for r in chunk:
                    if r == norm_m:
                        found_ids[r].add(tid)

        # 4. Check Prefix (Fallback/Expansion) - For longer strings
        long_keys = [n for n in chunk if len(n) > 2]
        if long_keys:
             or_clauses = []
             params = []
             for m in long_keys:
                 # Perform Prefix match
                 or_clauses.append("(name LIKE ? OR nickname LIKE ? OR abbreviation LIKE ?)")
                 params.extend([f"{m}%", f"{m}%", f"{m}%"])
             
             if or_clauses:
                 clause_str = " OR ".join(or_clauses)
                 query = f"SELECT id, name, nickname, abbreviation FROM teams WHERE {clause_str}"
                 cursor = conn.execute(query, params)
                 
                 for row in cursor:
                     tid, name, nick, abbr = row[0], row[1], row[2], row[3]
                     norm_name = normalize_key(name)
                     norm_nick = normalize_key(nick)
                     norm_abbr = normalize_key(abbr) if abbr else ""
                     
                     for m in long_keys:
                         if norm_name.startswith(m) or \
                            (norm_nick and norm_nick.startswith(m)) or \
                            (norm_abbr and norm_abbr.startswith(m)):
                             found_ids[m].add(tid)

    # Fetch Data for all unique matching IDs
    all_tids = set()
    for tids in found_ids.values():
        all_tids.update(tids)

    unique_team_ids = list(all_tids)
    team_data_map = {}
    
    if unique_team_ids:
        placeholders = ','.join('?' * len(unique_team_ids))
        query = f"""
            SELECT t.id, t.name, t.abbreviation, t.city, t.mascot, t.nickname,
                   l.name as league_name, s.name as sport_name, t.league_id
            FROM teams t
            LEFT JOIN leagues l ON t.league_id = l.id
            LEFT JOIN sports s ON t.sport_id = s.id
            WHERE t.id IN ({placeholders})
        """
        params = list(unique_team_ids)
        if normalized_sport:
            query += " AND LOWER(s.name) = ?"
            params.append(normalized_sport)
            
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        
        # Batch fetch players for these teams
        found_tids_actual = [row["id"] for row in rows]
        players_by_team = {}
        
        if found_tids_actual:
            p_placeholders = ','.join('?' * len(found_tids_actual))
            p_query = f"""
                SELECT p.id, p.name, p.first_name, p.last_name, p.position, 
                       p.number, p.age, p.height, p.weight, p.team_id
                FROM players p
                WHERE p.team_id IN ({p_placeholders})
                ORDER BY p.name
            """
            cursor = conn.execute(p_query, found_tids_actual)
            for p in cursor:
                tid = p["team_id"]
                if tid not in players_by_team:
                    players_by_team[tid] = []
                players_by_team[tid].append(dict(p))

        # Build Team Objects
        for row in rows:
            tid = row["id"]
            team_obj = {
                "id": tid,
                "normalized_name": row["name"],
                "abbreviation": row["abbreviation"],
                "city": row["city"],
                "mascot": row["mascot"],
                "nickname": row["nickname"],
                "league": row["league_name"],
                "sport": row["sport_name"],
                "players": players_by_team.get(tid, []),
                "player_count": len(players_by_team.get(tid, []))
            }
            team_data_map[tid] = team_obj

    # Map results back to original inputs
    for norm in unique_norms:
        # Get list of teams for this norm
        teams_list = []
        if norm in found_ids:
            for tid in found_ids[norm]:
                if tid in team_data_map:
                    teams_list.append(team_data_map[tid])
        
        # Sort teams by league priority
        teams_list.sort(key=lambda x: (get_league_priority(x.get("league", "")), x.get("normalized_name", "")))

        # Assign to all original keys that mapped to this norm
        for original in input_map[norm]:
            if teams_list:
                final_obj = {
                    "type": "team",
                    "query": original,
                    "teams": teams_list,
                    "team_count": len(teams_list)
                }
                results[original] = final_obj
            else:
                results[original] = None
                
    return results


def _resolve_batch_players(conn: sqlite3.Connection, player_names: List[str]) -> Dict[str, Any]:
    """Batch resolve players using optimized set-based queries"""
    results = {}
    input_map = {}
    for original in player_names:
        norm = normalize_key(original)
        if norm:
            if norm not in input_map:
                input_map[norm] = []
            input_map[norm].append(original)
            
    unique_norms = list(input_map.keys())
    if not unique_norms:
        return {}

    found_ids = defaultdict(set) # norm -> set of ids
    
    for chunk in _chunk_list(unique_norms, 50):
        # 1. Check Aliases
        placeholders = ','.join('?' * len(chunk))
        cursor = conn.execute(f"SELECT alias, player_id FROM player_aliases WHERE alias IN ({placeholders})", chunk)
        for row in cursor:
            found_ids[row[0]].add(row[1])
            
        # 2. Check Main Table (Exact Match)
        placeholders = ','.join('?' * len(chunk))
        cursor = conn.execute(f"SELECT id, name FROM players WHERE name COLLATE NOCASE IN ({placeholders})", chunk)
        for row in cursor:
            # Map logic
            pid = row[0]
            norm_name = normalize_key(row[1])
            
            if norm_name in chunk:
                 found_ids[norm_name].add(pid)
                 
            for r in chunk:
                if r == norm_name:
                    found_ids[r].add(pid)
                        
        # 3. Filter Prefix (and partial)
        long_keys = [n for n in chunk if len(n) > 2]
        if long_keys:
             or_clauses = ["(name LIKE ? OR first_name LIKE ? OR last_name LIKE ?)"] * len(long_keys)
             params = []
             for m in long_keys:
                 params.extend([f"{m}%", f"{m}%", f"{m}%"])
             
             query = f"SELECT id, name, first_name, last_name FROM players WHERE {' OR '.join(or_clauses)}"
             cursor = conn.execute(query, params)
             for row in cursor:
                 pid = row[0]
                 norm_name = normalize_key(row[1])
                 norm_first = normalize_key(row[2]) if row[2] else ""
                 norm_last = normalize_key(row[3]) if row[3] else ""
                 
                 for m in long_keys:
                     if norm_name.startswith(m) or norm_first.startswith(m) or norm_last.startswith(m):
                         found_ids[m].add(pid)
                         
    # Fetch Data
    all_pids = set()
    for pids in found_ids.values():
        all_pids.update(pids)

    unique_pids = list(all_pids)
    player_data_map = {}
    
    if unique_pids:
        placeholders = ','.join('?' * len(unique_pids))
        query = f"""
            SELECT p.id, p.name, p.first_name, p.last_name, p.position, p.number,
                   p.age, p.height, p.weight,
                   t.name as team_name, l.name as league_name, s.name as sport_name
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            LEFT JOIN leagues l ON p.league_id = l.id
            LEFT JOIN sports s ON p.sport_id = s.id
            WHERE p.id IN ({placeholders})
        """
        cursor = conn.execute(query, unique_pids)
        for result in cursor:
            pid = result["id"]
            player_obj = {
                "id": pid,
                "normalized_name": result["name"],
                "first_name": result["first_name"],
                "last_name": result["last_name"],
                "position": result["position"],
                "number": result["number"],
                "age": result["age"],
                "height": result["height"],
                "weight": result["weight"],
                "team": result["team_name"],
                "league": result["league_name"],
                "sport": result["sport_name"]
            }
            player_data_map[pid] = player_obj

    # Map back
    for norm in unique_norms:
        players_list = []
        if norm in found_ids:
             for pid in found_ids[norm]:
                 if pid in player_data_map:
                     players_list.append(player_data_map[pid])
        
        # Sort players (by league priority then name)
        players_list.sort(key=lambda x: (get_league_priority(x.get("league", "")), x.get("normalized_name", "")))

        for original in input_map[norm]:
            if players_list:
                final_obj = {
                     "type": "player",
                     "query": original,
                     "players": players_list,
                     "player_count": len(players_list)
                }
                results[original] = final_obj
            else:
                results[original] = None
                
    return results


def _resolve_bulk_markets(conn: sqlite3.Connection, market_names: List[str]) -> Dict[str, Any]:
    """Bulk resolve markets by loading all markets in memory (small dataset)"""
    results = {}
    
    # Load ALL markets (ID, name)
    # This is much faster than 50 LIKE queries
    cursor = conn.execute("SELECT id, name FROM markets")
    all_markets = cursor.fetchall() # List of Row
    
    # Create helper maps
    # 1. Exact Name Map
    # 2. Stripped Name Map
    exact_map = {m["name"].lower(): m["id"] for m in all_markets}
    stripped_map = {m["name"].lower().replace(" ", "").replace("_", ""): m["id"] for m in all_markets}
    
    # 3. Check alias table
    cursor = conn.execute("SELECT alias, market_id FROM market_aliases")
    alias_rows = cursor.fetchall()
    alias_map = {row["alias"].lower().replace(" ", "").replace("_", ""): row["market_id"] for row in alias_rows}
    
    # Process inputs
    found_ids = {} # original -> id
    
    for original in market_names:
        norm = original.lower().strip()
        stripped = norm.replace(" ", "").replace("_", "")
        
        mid = None
        
        # 1. Alias Check
        if stripped in alias_map:
            mid = alias_map[stripped]
        
        # 2. Exact/Stripped Check in Main
        if not mid:
            if norm in exact_map:
                mid = exact_map[norm]
            elif stripped in stripped_map:
                mid = stripped_map[stripped]
                
        # 3. Fuzzy Check (Prefix) - In Memory
        if not mid:
            # simple linear scan is fast for < 1000 items
            for m in all_markets:
                m_name = m["name"].lower()
                if m_name.startswith(norm):
                    mid = m["id"]
                    break
                    
        if mid:
            found_ids[original] = mid
            
    # Fetch details for found IDs
    unique_mids = list(set(found_ids.values()))
    market_data_map = {}
    
    if unique_mids:
        placeholders = ','.join('?' * len(unique_mids))
        cursor = conn.execute(f"SELECT id, name, market_type_id FROM markets WHERE id IN ({placeholders})", unique_mids)
        rows = cursor.fetchall()
        
        # Fetch sports for these markets
        # ONE-TO-MANY (Market -> Sports)
        # SELECT market_id, sport_name ...
        # Join optimization
        cursor = conn.execute(f"""
            SELECT ms.market_id, s.name 
            FROM market_sports ms 
            JOIN sports s ON ms.sport_id = s.id 
            WHERE ms.market_id IN ({placeholders})
        """, unique_mids)
        sports_map = {}
        for row in cursor:
            mid = row[0]
            sname = row[1]
            if mid not in sports_map:
                sports_map[mid] = []
            sports_map[mid].append(sname)
            
        for row in rows:
            mid = row["id"]
            market_data_map[mid] = {
                "type": "market",
                "normalized_name": row["name"],
                "market_type_id": row["market_type_id"],
                "sports": sports_map.get(mid, [])
            }
            
    # Map back
    for original in market_names:
        if original in found_ids and found_ids[original] in market_data_map:
            res = market_data_map[found_ids[original]].copy()
            res["query"] = original
            results[original] = res
        else:
            results[original] = None
            
    return results


def get_batch_cache_entries(
    teams: Optional[List[str]] = None,
    players: Optional[List[str]] = None,
    markets: Optional[List[str]] = None,
    sport: Optional[str] = None,
    leagues: Optional[List[str]] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Batch query for multiple items across categories.
    OPTIMIZED: Uses specialized set-based SQL queries for extreme performance.
    """
    result = {}
    if teams: result["team"] = {}
    if players: result["player"] = {}
    if markets: result["market"] = {}
    if leagues: result["league"] = {}

    # 1. Check Redis for all items first (Fastest)
    # Lists to process via DB
    miss_teams = []
    miss_players = []
    miss_markets = []
    miss_leagues = [] # Leagues implementation optional/omitted for brevity if needed, but best to do
    
    if teams:
        for t in teams:
            cached = get_cached_data(team=t, sport=sport)
            if cached:
                result["team"][t] = cached
            else:
                miss_teams.append(t)
                
    if players:
        for p in players:
            cached = get_cached_data(player=p)
            if cached:
                result["player"][p] = cached
            else:
                miss_players.append(p)
                
    if markets:
        for m in markets:
            cached = get_cached_data(market=m)
            if cached:
                result["market"][m] = cached
            else:
                miss_markets.append(m)
                
    # Leagues - loop fallback for now as it wasn't the main bottleneck
    # (But better to batch if possible. Let's stick to loop for legacy for now to save tokens/time, or just accept the tiny overhead?)
    # The user issue is "Missing Data" payloads which have teams/players/markets.
    # Leagues usually match or don't, and are few.
    
    # 2. Bulk DB Resolve for Misses
    conn = get_db_connection()
    try:
        if miss_teams:
            team_results = _resolve_batch_teams(conn, miss_teams, sport)
            for t, data in team_results.items():
                result["team"][t] = data
                if data:
                    set_cached_data(data, team=t, sport=sport)

        if miss_players:
            player_results = _resolve_batch_players(conn, miss_players)
            for p, data in player_results.items():
                result["player"][p] = data
                if data:
                    set_cached_data(data, player=p)

        if miss_markets:
            market_results = _resolve_bulk_markets(conn, miss_markets)
            for m, data in market_results.items():
                result["market"][m] = data
                if data:
                    set_cached_data(data, market=m)
        
        # Legacy loop for Leagues
        if leagues:
            for l in leagues:
                # Check Redis
                cached = get_cached_data(league=l, sport=sport)
                if cached:
                     result["league"][l] = cached
                else:
                    # DB
                    entry = get_cache_entry(league=l, sport=sport, active_connection=conn)
                    result["league"][l] = entry
                    # save cache done inside get_cache_entry
                    
    except Exception as e:
        print(f"Batch Error: {e}")
    finally:
        conn.close()
    
    return result


def get_precision_batch_cache_entries(queries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Precision batch query where each query can combine multiple parameters.
    OPTIMIZED: Uses a single shared DB connection and sequential processing.
    """
    results_list = []
    successful = 0
    failed = 0
    
    conn = get_db_connection()
    
    try:
        for idx, query_item in enumerate(queries):
            # Convert Pydantic model to dict if needed
            if hasattr(query_item, 'model_dump'):
                query_dict = query_item.model_dump(exclude_none=True)
            elif hasattr(query_item, 'dict'):
                query_dict = query_item.dict(exclude_none=True)
            else:
                query_dict = query_item
            
            try:
                entry = get_cache_entry(
                    team=query_dict.get("team"),
                    player=query_dict.get("player"),
                    market=query_dict.get("market"),
                    sport=query_dict.get("sport"),
                    league=query_dict.get("league"),
                    active_connection=conn
                )
                
                found = entry is not None
                if found:
                    successful += 1
                else:
                    failed += 1
                    
                results_list.append({
                    "query": query_dict,
                    "found": found,
                    "data": entry
                })
                
            except Exception as e:
                failed += 1
                results_list.append({
                    "query": query_dict,
                    "found": False,
                    "data": None,
                    "error": str(e)
                })
                
    finally:
        conn.close()
    
    return {
        "results": results_list,
        "total_queries": len(queries),
        "successful": successful,
        "failed": failed
    }


def get_all_leagues(
    sport: Optional[str] = None,
    search: Optional[str] = None,
    region: Optional[str] = None,
    active_connection: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Get all leagues from the database with optional filtering.
    
    Args:
        sport: Filter by sport name (optional)
        search: Search term to filter league names (optional)
        region: Filter by region (optional)
        active_connection: Existing database connection to use (optional)
    
    Returns:
        Dictionary with leagues data and metadata
    """
    if active_connection:
        conn = active_connection
        should_close = False
    else:
        conn = get_db_connection()
        should_close = True
    
    try:
        cursor = conn.cursor()
        
        # Build query with filters
        query = """
            SELECT 
                l.id,
                l.name,
                l.numerical_id,
                l.sport_id,
                s.name as sport_name,
                l.region,
                l.region_code,
                l.gender,
                l.logo
            FROM leagues l
            LEFT JOIN sports s ON l.sport_id = s.id
            WHERE 1=1
        """
        params = []
        
        # Add sport filter
        if sport:
            query += " AND LOWER(s.name) = ?"
            params.append(normalize_key(sport))
        
        # Add search filter
        if search:
            query += " AND LOWER(l.name) LIKE ?"
            params.append(f"%{normalize_key(search)}%")
        
        # Add region filter
        if region:
            query += " AND LOWER(l.region) = ?"
            params.append(normalize_key(region))
        
        query += " ORDER BY s.name, l.name"
        
        cursor.execute(query, params)
        results = cursor.fetchall()
        
        leagues_data = []
        for row in results:
            league_dict = {
                "id": row["id"],
                "name": row["name"],
                "numerical_id": row["numerical_id"],
                "sport_id": row["sport_id"],
                "sport_name": row["sport_name"],
                "region": row["region"],
                "region_code": row["region_code"],
                "gender": row["gender"],
                "logo": row["logo"]
            }
            
            # Get aliases for this league
            cursor.execute("""
                SELECT alias FROM league_aliases
                WHERE league_id = ?
                ORDER BY alias
            """, (row["id"],))
            aliases = [alias_row["alias"] for alias_row in cursor.fetchall()]
            league_dict["aliases"] = aliases
            
            leagues_data.append(league_dict)
        
        return {
            "total": len(leagues_data),
            "leagues": leagues_data,
            "filters": {
                "sport": sport,
                "search": search,
                "region": region
            }
        }
        
    finally:
        if should_close:
            conn.close()


