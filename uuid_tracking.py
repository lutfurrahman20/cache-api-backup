"""
UUID Login Tracking Module
Tracks UUID-based login attempts with geo-location information.
"""

import sqlite3
import os
import json
from typing import Optional, Dict, Any, List
from datetime import datetime
import requests

# Database file path for tracking
TRACKING_DB_PATH = os.path.join(os.path.dirname(__file__), "uuid_tracking.db")


def init_tracking_db():
    """Initialize the UUID tracking database with the required schema"""
    conn = sqlite3.connect(TRACKING_DB_PATH)
    cursor = conn.cursor()
    
    # Create UUID login tracking table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS uuid_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            country TEXT,
            country_code TEXT,
            region TEXT,
            city TEXT,
            latitude REAL,
            longitude REAL,
            timezone TEXT,
            isp TEXT,
            user_agent TEXT,
            geo_data_raw TEXT
        )
    """)
    
    # Create index on UUID for faster lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_uuid 
        ON uuid_login_attempts(uuid)
    """)
    
    # Create index on timestamp for faster chronological queries
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_timestamp 
        ON uuid_login_attempts(timestamp)
    """)
    
    conn.commit()
    conn.close()
    print(f"UUID tracking database initialized at: {TRACKING_DB_PATH}")


def get_geo_location(ip_address: str) -> Dict[str, Any]:
    """
    Get geo-location information for an IP address using ip-api.com (free service).
    
    Args:
        ip_address: IP address to lookup
        
    Returns:
        Dictionary with geo-location data
    """
    # Handle localhost/private IPs
    if ip_address in ['127.0.0.1', '::1', 'localhost'] or ip_address.startswith('192.168.') or ip_address.startswith('10.'):
        return {
            'status': 'private',
            'message': 'Private/localhost IP address',
            'query': ip_address
        }
    
    try:
        # HTTPS-only request for transport security.
        # ip-api.com free tier limit: 45 requests/minute.
        response = requests.get(
            f"https://ip-api.com/json/{ip_address}",
            timeout=5
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                return data
            else:
                return {
                    'status': 'fail',
                    'message': data.get('message', 'Unknown error'),
                    'query': ip_address
                }
        else:
            return {
                'status': 'error',
                'message': f'HTTP {response.status_code}',
                'query': ip_address
            }
    except Exception as e:
        return {
            'status': 'error',
            'message': str(e),
            'query': ip_address
        }


def track_uuid_login(
    uuid: str,
    ip_address: str,
    user_agent: Optional[str] = None
) -> Dict[str, Any]:
    """
    Track a UUID login attempt with geo-location data.
    
    Args:
        uuid: The UUID used for login
        ip_address: The IP address of the request
        user_agent: Optional user agent string
        
    Returns:
        Dictionary with tracking confirmation and geo data
    """
    # Get geo-location data
    geo_data = get_geo_location(ip_address)
    
    # Initialize DB if it doesn't exist
    if not os.path.exists(TRACKING_DB_PATH):
        init_tracking_db()
    
    conn = sqlite3.connect(TRACKING_DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Insert tracking record
        cursor.execute("""
            INSERT INTO uuid_login_attempts 
            (uuid, ip_address, country, country_code, region, city, 
             latitude, longitude, timezone, isp, user_agent, geo_data_raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            uuid,
            ip_address,
            geo_data.get('country'),
            geo_data.get('countryCode'),
            geo_data.get('regionName'),
            geo_data.get('city'),
            geo_data.get('lat'),
            geo_data.get('lon'),
            geo_data.get('timezone'),
            geo_data.get('isp'),
            user_agent,
            json.dumps(geo_data)
        ))
        
        conn.commit()
        record_id = cursor.lastrowid
        
        return {
            'status': 'success',
            'message': 'UUID login attempt tracked',
            'record_id': record_id,
            'uuid': uuid,
            'ip_address': ip_address,
            'geo_location': {
                'country': geo_data.get('country'),
                'country_code': geo_data.get('countryCode'),
                'region': geo_data.get('regionName'),
                'city': geo_data.get('city'),
                'latitude': geo_data.get('lat'),
                'longitude': geo_data.get('lon'),
                'timezone': geo_data.get('timezone'),
                'isp': geo_data.get('isp')
            }
        }
        
    except Exception as e:
        conn.rollback()
        return {
            'status': 'error',
            'message': f'Failed to track UUID login: {str(e)}',
            'uuid': uuid,
            'ip_address': ip_address
        }
    finally:
        conn.close()


def get_uuid_login_logs(
    uuid: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> Dict[str, Any]:
    """
    Retrieve UUID login tracking logs.
    
    Args:
        uuid: Optional UUID to filter by
        limit: Maximum number of records to return
        offset: Number of records to skip
        
    Returns:
        Dictionary with login logs and metadata
    """
    if not os.path.exists(TRACKING_DB_PATH):
        init_tracking_db()
        return {
            'total': 0,
            'limit': limit,
            'offset': offset,
            'logs': []
        }
    
    conn = sqlite3.connect(TRACKING_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Build query
        if uuid:
            count_query = "SELECT COUNT(*) as count FROM uuid_login_attempts WHERE uuid = ?"
            data_query = """
                SELECT id, uuid, ip_address, timestamp, country, country_code, 
                       region, city, latitude, longitude, timezone, isp, user_agent
                FROM uuid_login_attempts 
                WHERE uuid = ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(count_query, (uuid,))
            total = cursor.fetchone()['count']
            
            cursor.execute(data_query, (uuid, limit, offset))
        else:
            count_query = "SELECT COUNT(*) as count FROM uuid_login_attempts"
            data_query = """
                SELECT id, uuid, ip_address, timestamp, country, country_code, 
                       region, city, latitude, longitude, timezone, isp, user_agent
                FROM uuid_login_attempts 
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(count_query)
            total = cursor.fetchone()['count']
            
            cursor.execute(data_query, (limit, offset))
        
        rows = cursor.fetchall()
        logs = []
        
        for row in rows:
            logs.append({
                'id': row['id'],
                'uuid': row['uuid'],
                'ip_address': row['ip_address'],
                'timestamp': row['timestamp'],
                'geo_location': {
                    'country': row['country'],
                    'country_code': row['country_code'],
                    'region': row['region'],
                    'city': row['city'],
                    'latitude': row['latitude'],
                    'longitude': row['longitude'],
                    'timezone': row['timezone'],
                    'isp': row['isp']
                },
                'user_agent': row['user_agent']
            })
        
        return {
            'total': total,
            'limit': limit,
            'offset': offset,
            'count': len(logs),
            'logs': logs
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'message': f'Failed to retrieve logs: {str(e)}',
            'total': 0,
            'logs': []
        }
    finally:
        conn.close()


# Initialize database on module import
if __name__ != "__main__":
    init_tracking_db()
