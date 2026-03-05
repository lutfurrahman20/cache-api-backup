"""
Request Tracking Module
Tracks all API requests from non-admin users with session-based tracking.
Stores data in SQLite database for reliability and concurrency.
"""

import sqlite3
import json
import os
import uuid
import hashlib
import secrets as _secrets
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import contextmanager
import geoip2.database

# Directory to store tracking data
TRACKING_DIR = os.path.join(os.path.dirname(__file__), "request_logs")
DB_FILE = os.path.join(TRACKING_DIR, "requests.db")
GEOIP_DB_PATH = os.path.join(os.path.dirname(__file__), "geoip", "GeoLite2-City.mmdb")

def get_location_from_ip(ip_address: str) -> Optional[str]:
    """Resolve IP address to city/country using GeoIP2."""
    if not os.path.exists(GEOIP_DB_PATH):
        return None
        
    try:
        with geoip2.database.Reader(GEOIP_DB_PATH) as reader:
            response = reader.city(ip_address)
            city = response.city.name
            country = response.country.name
            
            if city and country:
                return f"{city}, {country}"
            elif country:
                return country
            return None
    except Exception:
        return None

@contextmanager
def get_db_connection():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # Access columns by name
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_tracking():
    """Initialize tracking directory and database schemas."""
    # Create directory if it doesn't exist
    os.makedirs(TRACKING_DIR, exist_ok=True)
    
    with get_db_connection() as conn:
        # Create sessions table
        conn.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_identifier TEXT,
            ip_address TEXT,
            user_agent TEXT,
            token_type TEXT,
            created_at TEXT,
            last_activity TEXT,
            request_count INTEGER DEFAULT 0
        )
        ''')
        
        # Create requests table
        conn.execute('''
        CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY,
            session_id TEXT,
            timestamp TEXT,
            method TEXT,
            path TEXT,
            query_params TEXT,
            body_data TEXT,
            ip_address TEXT,
            user_agent TEXT,
            token_masked TEXT,
            uuid TEXT,
            response_status INTEGER,
            response_time_ms REAL,
            FOREIGN KEY (session_id) REFERENCES sessions (session_id)
        )
        ''')
        
        # Create indices for better query performance
        conn.execute('CREATE INDEX IF NOT EXISTS idx_requests_session_id ON requests(session_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sessions_last_activity ON sessions(last_activity)')

        # One-time migration: ensure location column exists.
        existing_columns = {
            row['name']
            for row in conn.execute("PRAGMA table_info(requests)")
        }
        if 'location' not in existing_columns:
            conn.execute("ALTER TABLE requests ADD COLUMN location TEXT")

        # Create missing_items table for tracking data not found in cache/database
        conn.execute('''
        CREATE TABLE IF NOT EXISTS missing_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            request_group_id TEXT,
            timestamp TEXT,
            item_type TEXT,
            item_value TEXT,
            endpoint TEXT,
            query_params TEXT,
            first_seen TEXT,
            last_seen TEXT,
            occurrence_count INTEGER DEFAULT 1,
            UNIQUE(item_type, item_value, endpoint)
        )
        ''')
        # Migration: add request_group_id for grouping missing values by request.
        try:
            conn.execute("ALTER TABLE missing_items ADD COLUMN request_group_id TEXT")
        except sqlite3.OperationalError:
            pass

        # Migration: add body_data column to store the request body/query sent.
        try:
            conn.execute("ALTER TABLE missing_items ADD COLUMN body_data TEXT")
        except sqlite3.OperationalError:
            pass

        # Create indices for missing items
        conn.execute('CREATE INDEX IF NOT EXISTS idx_missing_items_timestamp ON missing_items(timestamp)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_missing_items_type ON missing_items(item_type)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_missing_items_request_group_id ON missing_items(request_group_id)')

        # Token management tables
        conn.execute('''
        CREATE TABLE IF NOT EXISTS tokens (
            token_id TEXT PRIMARY KEY,
            token_hash TEXT UNIQUE NOT NULL,
            token_masked TEXT NOT NULL,
            name TEXT NOT NULL,
            owner TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL,
            last_used TEXT,
            last_ip TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            expires_at TEXT,
            notes TEXT
        )
        ''')
        conn.execute('''
        CREATE TABLE IF NOT EXISTS token_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT,
            token_masked TEXT,
            action TEXT NOT NULL,
            actor TEXT,
            timestamp TEXT NOT NULL,
            ip_address TEXT,
            reason TEXT
        )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tokens_status ON tokens(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_token_audit_tid ON token_audit(token_id)')

    print(f"Request tracking initialized. Database: {DB_FILE}")

def create_session(user_identifier: str, ip_address: str, user_agent: str, token_type: str) -> str:
    """
    Create a new session for a user.
    """
    session_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    
    with get_db_connection() as conn:
        conn.execute('''
        INSERT INTO sessions (
            session_id, user_identifier, ip_address, user_agent, token_type, 
            created_at, last_activity, request_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        ''', (session_id, user_identifier, ip_address, user_agent, token_type, now, now))
    
    return session_id

def get_or_create_session(token: str, ip_address: str, user_agent: str, user_identifier: Optional[str] = None) -> str:
    """
    Get existing session or create a new one based on token and IP.
    """
    # Determine token type (attempt import, fallback to check)
    try:
        from main import ADMIN_KEY
        token_type = "admin" if token == ADMIN_KEY else "non-admin"
    except ImportError:
        token_type = "non-admin" 

    # Resolve identifier early so we can use it in the session lookup
    identifier = user_identifier or "user_unknown"

    with get_db_connection() as conn:
        # Include user_identifier in the query so different API tokens from the
        # same IP/browser do not share a session record (prevents cross-user leakage).
        cursor = conn.execute('''
        SELECT session_id FROM sessions 
        WHERE ip_address = ? AND user_agent = ? AND user_identifier = ?
        ORDER BY last_activity DESC LIMIT 1
        ''', (ip_address, user_agent, identifier))
        
        row = cursor.fetchone()
        
        if row:
            session_id = row['session_id']
            # Update last activity
            conn.execute('''
            UPDATE sessions SET last_activity = ? WHERE session_id = ?
            ''', (datetime.now().isoformat(), session_id))
            return session_id

    # Create new session if not found
    return create_session(identifier, ip_address, user_agent, token_type)

def track_request(
    session_id: str,
    method: str,
    path: str,
    query_params: Dict[str, Any],
    token: str,
    ip_address: str,
    user_agent: str,
    response_status: Optional[int] = None,
    response_time_ms: Optional[float] = None,
    body_data: Optional[Dict[str, Any]] = None,
    uuid: Optional[str] = None
):
    """
    Track an API request.
    """
    try:
        from main import ADMIN_KEY
        is_admin = (token == ADMIN_KEY)
    except ImportError:
        is_admin = False

    # Only track non-admin requests
    if is_admin:
        return

    # Mask token appropriately
    uuid_str = uuid
    if uuid_str is None:
        if token.startswith("uuid:"):
            token_display = token
            uuid_str = token[5:]
        elif len(token) > 8:
            token_display = f"{token[:4]}...{token[-4:]}"
        else:
            token_display = "***"
    else:
        # User requested to see the non-admin token (masked for security, but identifiable)
        if len(token) > 8:
            token_display = f"{token[:4]}...{token[-4:]}"
        else:
            token_display = token # Short tokens shown fully

    # Location lookup
    location = get_location_from_ip(ip_address)

    # Serialize complex types to JSON
    query_params_json = json.dumps(query_params) if query_params else "{}"
    body_data_json = json.dumps(body_data) if body_data else "{}"

    import uuid as uuid_lib # Safety alias
    request_id = str(uuid_lib.uuid4())
    timestamp = datetime.now().isoformat()

    try:
        with get_db_connection() as conn:
            # Insert request
            conn.execute('''
            INSERT INTO requests (
                request_id, session_id, timestamp, method, path, query_params,
                body_data, ip_address, user_agent, token_masked, uuid,
                response_status, response_time_ms, location
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                request_id, session_id, timestamp, method, path, query_params_json,
                body_data_json, ip_address, user_agent, token_display, uuid_str,
                response_status, response_time_ms, location
            ))

            # Update session stats
            conn.execute('''
            UPDATE sessions
            SET request_count = request_count + 1, last_activity = ?
            WHERE session_id = ?
            ''', (timestamp, session_id))

    except Exception as e:
        print(f"Error tracking request: {e}")

def get_request_logs(
    session_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    path_filter: Optional[str] = None
) -> Dict[str, Any]:
    """Retrieve request logs with optional filtering."""
    try:
        with get_db_connection() as conn:
            query = "SELECT * FROM requests WHERE 1=1"
            params = []
            
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            
            if path_filter:
                query += " AND path LIKE ?"
                params.append(f"%{path_filter}%")
                
            # Count total
            count_query = query.replace("SELECT *", "SELECT COUNT(*)")
            total = conn.execute(count_query, params).fetchone()[0]
            
            # Get data (sort by timestamp DESC for logs)
            query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            
            cursor = conn.execute(query, params)
            requests = [dict(row) for row in cursor.fetchall()]
            
            # Parse JSON fields
            for req in requests:
                try:
                    req['query_params'] = json.loads(req['query_params'])
                    req['body_data'] = json.loads(req['body_data'])
                except:
                    pass

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "count": len(requests),
                "requests": requests
            }
    except Exception as e:
        print(f"Error retrieving request logs: {e}")
        return {"total": 0, "requests": [], "error": str(e)}

def get_session_summary() -> Dict[str, Any]:
    """Get summary of all active sessions."""
    try:
        with get_db_connection() as conn:
            total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            admin_sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE token_type = 'admin'").fetchone()[0]
            non_admin_sessions = total_sessions - admin_sessions
            total_requests = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            
            sessions_cursor = conn.execute("SELECT * FROM sessions ORDER BY last_activity DESC")
            sessions = [dict(row) for row in sessions_cursor.fetchall()]
            
            return {
                "total_sessions": total_sessions,
                "admin_sessions": admin_sessions,
                "non_admin_sessions": non_admin_sessions,
                "total_tracked_requests": total_requests,
                "sessions": sessions
            }
    except Exception as e:
        print(f"Error getting session summary: {e}")
        return {}

def get_session_details(session_id: str) -> Optional[Dict[str, Any]]:
    """Get details for a specific session."""
    try:
        with get_db_connection() as conn:
            session = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if not session:
                return None
            
            # We must convert row to dict
            session_dict = dict(session)
            
            # Get recent requests
            logs = get_request_logs(session_id=session_id, limit=10)
                
            return {
                "session": session_dict,
                "request_count": session_dict['request_count'],
                "recent_requests": logs['requests']
            }
    except Exception as e:
        print(f"Error getting session details: {e}")
        return None

def clear_old_sessions(days_old: int = 7):
    """Clear sessions older than specified days."""
    try:
        from datetime import timedelta
        cutoff_date = (datetime.now() - timedelta(days=days_old)).isoformat()
        
        with get_db_connection() as conn:
            conn.execute("DELETE FROM requests WHERE session_id IN (SELECT session_id FROM sessions WHERE last_activity < ?)", (cutoff_date,))
            conn.execute("DELETE FROM sessions WHERE last_activity < ?", (cutoff_date,))
            print(f"Cleared old sessions before {cutoff_date}")
    except Exception as e:
        print(f"Error clearing old sessions: {e}")

def track_missing_item(
    session_id: str,
    item_type: str,
    item_value: str,
    endpoint: str,
    query_params: Optional[Dict[str, Any]] = None,
    request_group_id: Optional[str] = None,
    body_data: Optional[Dict[str, Any]] = None
):
    """
    Track an item that was not found in cache or database.

    Args:
        session_id: Session identifier
        item_type: Type of missing item (e.g., 'market', 'team', 'player', 'league')
        item_value: The value that was searched for
        endpoint: The API endpoint that was called
        query_params: Query parameters from the request
        request_group_id: Shared ID to group all missing values from the same request
        body_data: The request body/query parameters sent with the request
    """
    try:
        timestamp = datetime.now().isoformat()
        query_params_json = json.dumps(query_params) if query_params else "{}"
        body_data_json = json.dumps(body_data) if body_data else "{}"

        with get_db_connection() as conn:
            # Check if this item was already tracked
            cursor = conn.execute('''
            SELECT id, occurrence_count FROM missing_items
            WHERE item_type = ? AND item_value = ? AND endpoint = ?
            ''', (item_type, item_value, endpoint))

            existing = cursor.fetchone()

            if existing:
                # Update existing record
                conn.execute('''
                UPDATE missing_items
                SET last_seen = ?, occurrence_count = occurrence_count + 1, session_id = ?, query_params = ?, request_group_id = ?, body_data = ?
                WHERE id = ?
                ''', (timestamp, session_id, query_params_json, request_group_id, body_data_json, existing['id']))
            else:
                # Insert new record
                conn.execute('''
                INSERT INTO missing_items (
                    session_id, request_group_id, timestamp, item_type, item_value, endpoint,
                    query_params, first_seen, last_seen, occurrence_count, body_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ''', (
                    session_id, request_group_id, timestamp, item_type, item_value, endpoint,
                    query_params_json, timestamp, timestamp, body_data_json
                ))

    except Exception as e:
        print(f"Error tracking missing item: {e}")


def get_missing_items(
    item_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: str = 'last_seen'  # 'last_seen', 'occurrence_count', 'first_seen'
) -> Dict[str, Any]:
    """
    Retrieve missing items with optional filtering.

    Args:
        item_type: Filter by item type (e.g., 'market', 'team', 'player')
        limit: Maximum number of results
        offset: Offset for pagination
        sort_by: Field to sort by

    Returns:
        Dictionary with missing items data
    """
    try:
        with get_db_connection() as conn:
            query = """
                SELECT
                    mi.*,
                    s.ip_address AS ip_address
                FROM missing_items mi
                LEFT JOIN sessions s ON s.session_id = mi.session_id
                WHERE 1=1
            """
            params = []

            if item_type:
                query += " AND mi.item_type = ?"
                params.append(item_type)

            # Count total
            count_query = """
                SELECT COUNT(*)
                FROM missing_items mi
                WHERE 1=1
            """
            count_params = list(params)
            if item_type:
                count_query += " AND mi.item_type = ?"
            total = conn.execute(count_query, count_params).fetchone()[0]

            # Sort
            valid_sorts = ['last_seen', 'occurrence_count', 'first_seen', 'timestamp']
            if sort_by not in valid_sorts:
                sort_by = 'last_seen'

            query += f" ORDER BY mi.{sort_by} DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor = conn.execute(query, params)
            items = [dict(row) for row in cursor.fetchall()]

            # Parse JSON fields and build grouped missing fields
            request_group_ids = []
            for item in items:
                try:
                    item['query_params'] = json.loads(item['query_params'])
                except Exception:
                    item['query_params'] = {}
                try:
                    item['body_data'] = json.loads(item.get('body_data') or '{}')
                except Exception:
                    item['body_data'] = {}
                if item.get('request_group_id'):
                    request_group_ids.append(item['request_group_id'])

            grouped_by_request: Dict[str, Dict[str, List[str]]] = {}
            unique_request_group_ids = list(set(request_group_ids))
            if unique_request_group_ids:
                placeholders = ','.join(['?'] * len(unique_request_group_ids))
                grouped_cursor = conn.execute(f'''
                    SELECT request_group_id, item_type, item_value
                    FROM missing_items
                    WHERE request_group_id IN ({placeholders})
                ''', unique_request_group_ids)

                for row in grouped_cursor.fetchall():
                    group_id = row['request_group_id']
                    field_name = row['item_type']
                    field_value = row['item_value']
                    if not group_id or not field_name:
                        continue
                    if group_id not in grouped_by_request:
                        grouped_by_request[group_id] = {}
                    if field_name not in grouped_by_request[group_id]:
                        grouped_by_request[group_id][field_name] = []
                    if field_value and field_value not in grouped_by_request[group_id][field_name]:
                        grouped_by_request[group_id][field_name].append(field_value)

            for item in items:
                grouped = grouped_by_request.get(item.get('request_group_id')) if item.get('request_group_id') else None
                if not grouped:
                    fallback_grouped = {}
                    if item.get('item_type') and item.get('item_value'):
                        fallback_grouped[item['item_type']] = [item['item_value']]
                    grouped = fallback_grouped
                item['missing_fields_grouped'] = grouped
                item['missing_fields'] = list(grouped.keys()) if grouped else []

            # Get summary statistics
            stats_cursor = conn.execute('''
            SELECT
                item_type,
                COUNT(*) as count,
                SUM(occurrence_count) as total_occurrences
            FROM missing_items
            GROUP BY item_type
            ''')

            stats_by_type = {row['item_type']: {
                'unique_count': row['count'],
                'total_occurrences': row['total_occurrences']
            } for row in stats_cursor.fetchall()}

            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "count": len(items),
                "items": items,
                "stats_by_type": stats_by_type
            }
    except Exception as e:
        print(f"Error retrieving missing items: {e}")
        return {"total": 0, "items": [], "stats_by_type": {}, "error": str(e)}


def clear_missing_items(item_type: Optional[str] = None):
    """
    Clear missing items records.

    Args:
        item_type: If specified, only clear items of this type
    """
    try:
        with get_db_connection() as conn:
            if item_type:
                conn.execute("DELETE FROM missing_items WHERE item_type = ?", (item_type,))
                print(f"Cleared missing items of type: {item_type}")
            else:
                conn.execute("DELETE FROM missing_items")
                print("Cleared all missing items")
    except Exception as e:
        print(f"Error clearing missing items: {e}")


# ─── Token Helpers ───────────────────────────────────────────────────────────

def _hash_token(raw_token: str) -> str:
    """Return SHA-256 hex digest of the raw token string."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _mask_token(raw_token: str) -> str:
    """Return a safely displayable masked version of a token."""
    if len(raw_token) <= 12:
        return raw_token[:4] + '****'
    return raw_token[:8] + '...' + raw_token[-4:]


# ─── Token Store ──────────────────────────────────────────────────────────────

def seed_env_tokens(admin_token: Optional[str], user_token: Optional[str]) -> None:
    """
    Seed environment-variable tokens into the managed token store on startup.
    Idempotent — already-present tokens (matched by hash) are skipped.
    """
    now = datetime.now().isoformat()
    seeds = []
    if admin_token:
        seeds.append((admin_token, 'admin', 'Env: Admin Token'))
    if user_token:
        seeds.append((user_token, 'user', 'Env: User Token'))
    try:
        with get_db_connection() as conn:
            for raw, role, name in seeds:
                token_hash = _hash_token(raw)
                existing = conn.execute(
                    'SELECT token_id FROM tokens WHERE token_hash = ?', (token_hash,)
                ).fetchone()
                if existing:
                    continue
                token_id = str(uuid.uuid4())
                masked = _mask_token(raw)
                conn.execute('''
                INSERT INTO tokens (token_id, token_hash, token_masked, name, owner, role,
                                   created_at, status, notes)
                VALUES (?, ?, ?, ?, 'system', ?, ?, 'active', 'Seeded from environment variable')
                ''', (token_id, token_hash, masked, name, role, now))
    except Exception as e:
        print(f"Error seeding env tokens: {e}")


def create_managed_token(
    name: str,
    owner: Optional[str] = None,
    role: str = 'user',
    notes: Optional[str] = None,
    expires_at: Optional[str] = None,
    actor: str = 'admin',
    ip_address: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new managed API token.
    Returns the raw token once — it is not stored in plaintext anywhere after this call.
    """
    raw_token = _secrets.token_urlsafe(32)
    token_id = str(uuid.uuid4())
    token_hash = _hash_token(raw_token)
    masked = _mask_token(raw_token)
    now = datetime.now().isoformat()

    with get_db_connection() as conn:
        conn.execute('''
        INSERT INTO tokens (token_id, token_hash, token_masked, name, owner, role,
                           created_at, status, expires_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        ''', (token_id, token_hash, masked, name, owner, role, now, expires_at, notes))
        conn.execute('''
        INSERT INTO token_audit (token_id, token_masked, action, actor, timestamp, ip_address, reason)
        VALUES (?, ?, 'created', ?, ?, ?, ?)
        ''', (token_id, masked, actor, now, ip_address, f'Token "{name}" created'))

    return {
        'token_id': token_id,
        'raw_token': raw_token,
        'token_masked': masked,
        'name': name,
        'owner': owner,
        'role': role,
        'created_at': now,
        'status': 'active',
        'expires_at': expires_at,
        'notes': notes,
    }


def get_all_tokens() -> List[Dict[str, Any]]:
    """Return all managed tokens (masked — raw token never returned after creation)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.execute('''
            SELECT token_id, token_masked, name, owner, role, created_at,
                   last_used, last_ip, status, expires_at, notes
            FROM tokens
            ORDER BY created_at DESC
            ''')
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        print(f"Error fetching tokens: {e}")
        return []


def get_token_audit(token_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Return audit log entries, optionally filtered to a single token."""
    try:
        with get_db_connection() as conn:
            if token_id:
                cursor = conn.execute(
                    'SELECT * FROM token_audit WHERE token_id = ? ORDER BY timestamp DESC LIMIT ?',
                    (token_id, limit)
                )
            else:
                cursor = conn.execute(
                    'SELECT * FROM token_audit ORDER BY timestamp DESC LIMIT ?', (limit,)
                )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        print(f"Error fetching token audit: {e}")
        return []


def revoke_token(
    token_id: str,
    actor: str = 'admin',
    reason: Optional[str] = None,
    ip_address: Optional[str] = None
) -> bool:
    """Revoke an active managed token. Returns True on success."""
    try:
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            row = conn.execute(
                'SELECT token_masked, status FROM tokens WHERE token_id = ?', (token_id,)
            ).fetchone()
            if not row:
                return False
            if row['status'] == 'revoked':
                return True
            conn.execute(
                "UPDATE tokens SET status = 'revoked' WHERE token_id = ?", (token_id,)
            )
            conn.execute('''
            INSERT INTO token_audit (token_id, token_masked, action, actor, timestamp, ip_address, reason)
            VALUES (?, ?, 'revoked', ?, ?, ?, ?)
            ''', (token_id, row['token_masked'], actor, now, ip_address, reason or 'Token revoked'))
        return True
    except Exception as e:
        print(f"Error revoking token: {e}")
        return False


def rotate_token(
    token_id: str,
    actor: str = 'admin',
    reason: Optional[str] = None,
    ip_address: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Rotate a token: revoke the old one and create a replacement with the same metadata.
    Returns new token info (including raw_token, returned once only).
    """
    try:
        with get_db_connection() as conn:
            row = conn.execute('''
            SELECT token_id, token_masked, name, owner, role, expires_at, notes
            FROM tokens WHERE token_id = ?
            ''', (token_id,)).fetchone()
            if not row:
                return None
            old_masked = row['token_masked']
            meta = dict(row)

        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE tokens SET status = 'revoked' WHERE token_id = ?", (token_id,)
            )
            conn.execute('''
            INSERT INTO token_audit (token_id, token_masked, action, actor, timestamp, ip_address, reason)
            VALUES (?, ?, 'rotated_out', ?, ?, ?, ?)
            ''', (token_id, old_masked, actor, now, ip_address,
                  reason or 'Token rotated — old token revoked'))

        return create_managed_token(
            name=meta['name'],
            owner=meta['owner'],
            role=meta['role'],
            notes=meta['notes'],
            expires_at=meta['expires_at'],
            actor=actor,
            ip_address=ip_address,
        )
    except Exception as e:
        print(f"Error rotating token: {e}")
        return None


def verify_db_token(raw_token: str) -> Optional[Dict[str, Any]]:
    """
    Check a raw token against the managed token store.
    Returns the token row if active and not expired, else None.
    """
    try:
        token_hash = _hash_token(raw_token)
        with get_db_connection() as conn:
            row = conn.execute('''
            SELECT token_id, token_masked, name, owner, role,
                   created_at, last_used, last_ip, status, expires_at, notes
            FROM tokens WHERE token_hash = ? AND status = 'active'
            ''', (token_hash,)).fetchone()
            if not row:
                return None
            row_dict = dict(row)
            if row_dict.get('expires_at'):
                try:
                    if datetime.now() > datetime.fromisoformat(row_dict['expires_at']):
                        return None
                except ValueError:
                    # Malformed expiry date — treat as expired (fail-safe, never fail-open)
                    print(f"Warning: token {row_dict.get('token_masked')!r} has malformed expires_at: {row_dict['expires_at']!r} — treating as expired")
                    return None
            return row_dict
    except Exception as e:
        print(f"Error verifying DB token: {e}")
        return None


def is_admin_db_token(raw_token: str) -> bool:
    """Return True if this token is an active admin token in the DB store."""
    row = verify_db_token(raw_token)
    return row is not None and row.get('role') == 'admin'


def log_token_use(raw_token: str, ip_address: str) -> None:
    """Update last_used and last_ip for a token (best-effort, non-blocking)."""
    try:
        token_hash = _hash_token(raw_token)
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            conn.execute(
                'UPDATE tokens SET last_used = ?, last_ip = ? WHERE token_hash = ?',
                (now, ip_address, token_hash)
            )
    except Exception:
        pass


# ─── Analytics Functions ───────────────────────────────────────────────────────

def get_failure_analytics(hours: int = 24) -> Dict[str, Any]:
    """Return failure counts grouped by endpoint and HTTP status code (>= 400)."""
    from datetime import timedelta
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with get_db_connection() as conn:
            cursor = conn.execute('''
            SELECT path, response_status, COUNT(*) as count
            FROM requests
            WHERE response_status >= 400
              AND timestamp >= ?
            GROUP BY path, response_status
            ORDER BY count DESC
            ''', (cutoff,))
            rows = [dict(r) for r in cursor.fetchall()]

        by_status: Dict[int, int] = {}
        by_path: Dict[str, int] = {}
        for r in rows:
            by_status[r['response_status']] = by_status.get(r['response_status'], 0) + r['count']
            by_path[r['path']] = by_path.get(r['path'], 0) + r['count']

        return {
            'hours': hours,
            'by_endpoint_status': rows,
            'by_status_code': [{'status': k, 'count': v} for k, v in sorted(by_status.items())],
            'by_path': [{'path': k, 'count': v} for k, v in sorted(by_path.items(), key=lambda x: -x[1])],
        }
    except Exception as e:
        print(f"Error in failure analytics: {e}")
        return {'hours': hours, 'by_endpoint_status': [], 'by_status_code': [], 'by_path': [], 'error': str(e)}


def get_top_failing_signatures(limit: int = 20, hours: int = 24) -> List[Dict[str, Any]]:
    """Return the top failing request signatures (path + query fingerprint + status)."""
    from datetime import timedelta
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with get_db_connection() as conn:
            cursor = conn.execute('''
            SELECT path, query_params, response_status,
                   COUNT(*) as count,
                   MAX(timestamp) as last_seen,
                   MIN(timestamp) as first_seen
            FROM requests
            WHERE response_status >= 400
              AND timestamp >= ?
            GROUP BY path, query_params, response_status
            ORDER BY count DESC
            LIMIT ?
            ''', (cutoff, limit))
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                try:
                    d['query_params'] = json.loads(d['query_params'])
                except Exception:
                    d['query_params'] = {}
                results.append(d)
            return results
    except Exception as e:
        print(f"Error in top failing signatures: {e}")
        return []


def get_latency_stats(hours: int = 24) -> List[Dict[str, Any]]:
    """
    Return latency statistics (min, avg, p50, p95, p99, max) per endpoint.
    Percentiles are computed in Python since SQLite has no native percentile function.
    """
    from datetime import timedelta
    from collections import defaultdict as _dd
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with get_db_connection() as conn:
            cursor = conn.execute('''
            SELECT path, response_time_ms
            FROM requests
            WHERE response_time_ms IS NOT NULL
              AND timestamp >= ?
            ORDER BY path
            ''', (cutoff,))
            rows = cursor.fetchall()

        path_times: Dict[str, List[float]] = _dd(list)
        for row in rows:
            path_times[row['path']].append(row['response_time_ms'])

        results = []
        for path, times in sorted(path_times.items()):
            times.sort()
            n = len(times)

            def pct(p: float) -> float:
                idx = max(0, int(p / 100 * (n - 1)))
                return round(times[idx], 2)

            results.append({
                'path': path,
                'count': n,
                'min_ms': round(times[0], 2),
                'avg_ms': round(sum(times) / n, 2),
                'p50_ms': pct(50),
                'p95_ms': pct(95),
                'p99_ms': pct(99),
                'max_ms': round(times[-1], 2),
            })
        return results
    except Exception as e:
        print(f"Error in latency stats: {e}")
        return []


def get_request_trends(hours: int = 24) -> List[Dict[str, Any]]:
    """Return request volume bucketed by hour for trend visualization."""
    from datetime import timedelta
    from collections import defaultdict as _dd
    try:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
        with get_db_connection() as conn:
            cursor = conn.execute('''
            SELECT strftime('%Y-%m-%dT%H:00:00', timestamp) as bucket,
                   response_status,
                   COUNT(*) as count
            FROM requests
            WHERE timestamp >= ?
            GROUP BY bucket, response_status
            ORDER BY bucket ASC
            ''', (cutoff,))
            rows = [dict(r) for r in cursor.fetchall()]

        buckets: Dict[str, Dict[str, int]] = _dd(lambda: {'total': 0, 'errors': 0, 'success': 0})
        for r in rows:
            b = r['bucket']
            c = r['count']
            buckets[b]['total'] += c
            if r['response_status'] is not None and r['response_status'] >= 400:
                buckets[b]['errors'] += c
            else:
                buckets[b]['success'] += c

        return [{'bucket': k, **v} for k, v in sorted(buckets.items())]
    except Exception as e:
        print(f"Error in request trends: {e}")
        return []


# Initialize tracking on module import
init_tracking()
