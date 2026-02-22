"""Local SQLite database for offline operation."""

import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Set, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class LocalDB:
    """SQLite database for local caching and offline queue."""
    
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self._connect()
        self._init_schema()
    
    def _connect(self):
        """Establish database connection."""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
    
    def _init_schema(self):
        """Initialize database schema."""
        schema = '''
        -- Authentication cache
        CREATE TABLE IF NOT EXISTS auth_cache (
            card_uid TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            user_name TEXT,
            cabinet_id INTEGER,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        );
        
        -- Item state cache (knows which user holds each item)
        CREATE TABLE IF NOT EXISTS item_cache (
            rfid_tag TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            name TEXT,
            status TEXT,
            holder_id TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        -- RFID snapshots (for local diff calculation)
        CREATE TABLE IF NOT EXISTS rfid_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            cabinet_id INTEGER NOT NULL,
            rfid_tag TEXT NOT NULL,
            present BOOLEAN NOT NULL,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        -- Session diff results (for immediate display)
        CREATE TABLE IF NOT EXISTS session_diffs (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            borrowed TEXT,  -- JSON array
            returned TEXT,  -- JSON array
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            synced BOOLEAN DEFAULT FALSE,
            synced_at TIMESTAMP
        );
        
        -- Pending sync sessions (with idempotency check)
        CREATE TABLE IF NOT EXISTS pending_sync (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,  -- UNIQUE prevents duplicates
            user_id TEXT NOT NULL,
            rfids TEXT,  -- JSON array
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_attempt TIMESTAMP
        );
        
        -- Access logs
        CREATE TABLE IF NOT EXISTS access_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_uid TEXT,
            user_id TEXT,
            session_id TEXT,
            tags_found TEXT,  -- JSON array
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        -- Create indexes
        CREATE INDEX IF NOT EXISTS idx_auth_expires ON auth_cache(expires_at);
        CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_sync(created_at);
        CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_sync(session_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_session ON rfid_snapshots(session_id);
        CREATE INDEX IF NOT EXISTS idx_diffs_session ON session_diffs(session_id);
        '''
        
        self._conn.executescript(schema)
        self._conn.commit()
        logger.info("Database schema initialized")
    
    def cache_auth(self, card_uid: str, auth_result: Dict[str, Any], ttl: int = 3600):
        """Cache successful authentication."""
        expires = datetime.fromtimestamp(datetime.now().timestamp() + ttl)
        
        with self._conn:
            self._conn.execute('''
                INSERT OR REPLACE INTO auth_cache 
                (card_uid, user_id, user_name, cabinet_id, cached_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                card_uid,
                auth_result.get('user_id'),
                auth_result.get('user_name'),
                auth_result.get('cabinet_id'),
                datetime.now(),
                expires
            ))
    
    def get_cached_auth(self, card_uid: str) -> Optional[Dict[str, Any]]:
        """Get cached authentication if not expired."""
        row = self._conn.execute('''
            SELECT * FROM auth_cache 
            WHERE card_uid = ? AND expires_at > ?
        ''', (card_uid, datetime.now())).fetchone()
        
        if row:
            return {
                'authorized': True,
                'user_id': row['user_id'],
                'user_name': row['user_name'],
                'cabinet_id': row['cabinet_id'],
                'source': 'cache'
            }
        return None
    
    def save_rfid_snapshot(self, session_id: str, cabinet_id: int, rfids: List[str]):
        """Save RFID snapshot for session (marks end of session)."""
        with self._conn:
            # Save all present tags
            for tag in rfids:
                self._conn.execute('''
                    INSERT INTO rfid_snapshots (session_id, cabinet_id, rfid_tag, present)
                    VALUES (?, ?, ?, ?)
                ''', (session_id, cabinet_id, tag, True))
        logger.info(f"Saved RFID snapshot: {len(rfids)} tags for session {session_id[:8]}")
    
    def get_last_snapshot(self, cabinet_id: int, before_session: Optional[str] = None) -> Set[str]:
        """Get RFID tags from last session for this cabinet."""
        query = '''
            SELECT rfid_tag FROM rfid_snapshots 
            WHERE cabinet_id = ? AND session_id != ?
            AND captured_at = (
                SELECT MAX(captured_at) FROM rfid_snapshots 
                WHERE cabinet_id = ? AND session_id != ?
            )
        '''
        rows = self._conn.execute(query, (cabinet_id, before_session or '', cabinet_id, before_session or '')).fetchall()
        return set(row['rfid_tag'] for row in rows)
    
    def calculate_diff(self, current_rfids: List[str], cabinet_id: int, user_id: str) -> Tuple[List[Dict], List[Dict]]:
        """
        Calculate diff between current RFID scan and last snapshot.
        Returns: (borrowed_items, returned_items)
        """
        current_set = set(current_rfids)
        last_set = self.get_last_snapshot(cabinet_id)
        
        # Items that disappeared = borrowed by this user
        borrowed_tags = last_set - current_set
        # Items that appeared = returned by this user  
        returned_tags = current_set - last_set
        
        borrowed = []
        returned = []
        
        # Lookup item details from cache
        for tag in borrowed_tags:
            item = self._conn.execute(
                'SELECT * FROM item_cache WHERE rfid_tag = ?', (tag,)
            ).fetchone()
            if item:
                borrowed.append({
                    'rfid': tag,
                    'item_id': item['item_id'],
                    'name': item['name'] or f'Item {tag}'
                })
                # Update cache: now held by this user
                self.update_item_state(tag, 'BORROWED', user_id)
        
        for tag in returned_tags:
            item = self._conn.execute(
                'SELECT * FROM item_cache WHERE rfid_tag = ?', (tag,)
            ).fetchone()
            if item:
                returned.append({
                    'rfid': tag,
                    'item_id': item['item_id'],
                    'name': item['name'] or f'Item {tag}'
                })
                # Update cache: now available
                self.update_item_state(tag, 'AVAILABLE', None)
        
        return borrowed, returned
    
    def save_session_diff(self, session_id: str, user_id: str, borrowed: List[Dict], returned: List[Dict]):
        """Save calculated diff for immediate display."""
        with self._conn:
            self._conn.execute('''
                INSERT OR REPLACE INTO session_diffs 
                (session_id, user_id, borrowed, returned, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                session_id, user_id,
                json.dumps(borrowed), json.dumps(returned),
                datetime.now()
            ))
        logger.info(f"Saved session diff: {len(borrowed)} borrowed, {len(returned)} returned")
    
    def get_session_diff(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session diff for display."""
        row = self._conn.execute(
            'SELECT * FROM session_diffs WHERE session_id = ?', (session_id,)
        ).fetchone()
        
        if row:
            return {
                'session_id': row['session_id'],
                'user_id': row['user_id'],
                'borrowed': json.loads(row['borrowed']),
                'returned': json.loads(row['returned']),
                'created_at': row['created_at'],
                'synced': row['synced']
            }
        return None
    
    def mark_diff_synced(self, session_id: str):
        """Mark diff as synced after successful API call."""
        with self._conn:
            self._conn.execute('''
                UPDATE session_diffs 
                SET synced = TRUE, synced_at = ?
                WHERE session_id = ?
            ''', (datetime.now(), session_id))
    
    def queue_sync_session(self, session_id: str, user_id: str, rfids: List[str]) -> bool:
        """
        Queue session for later sync.
        Returns False if already queued (idempotency).
        """
        try:
            with self._conn:
                self._conn.execute('''
                    INSERT INTO pending_sync (session_id, user_id, rfids)
                    VALUES (?, ?, ?)
                ''', (session_id, user_id, json.dumps(rfids)))
            logger.info(f"Queued session {session_id[:8]} for sync")
            return True
        except sqlite3.IntegrityError:
            # Already queued - idempotency
            logger.info(f"Session {session_id[:8]} already queued, skipping")
            return False
    
    def get_pending_sync(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get pending sync sessions ordered by retry count (least retries first)."""
        rows = self._conn.execute('''
            SELECT * FROM pending_sync 
            ORDER BY retry_count ASC, created_at ASC
            LIMIT ?
        ''', (limit,)).fetchall()
        
        return [{
            'id': row['id'],
            'session_id': row['session_id'],
            'user_id': row['user_id'],
            'rfids': json.loads(row['rfids']),
            'retry_count': row['retry_count'],
            'created_at': row['created_at'],
            'last_attempt': row['last_attempt'],
        } for row in rows]
    
    def mark_sync_attempt(self, sync_id: int, error: Optional[str] = None):
        """Record sync attempt, increment retry count on failure."""
        with self._conn:
            if error:
                self._conn.execute('''
                    UPDATE pending_sync 
                    SET retry_count = retry_count + 1, 
                        last_error = ?, 
                        last_attempt = ?
                    WHERE id = ?
                ''', (error, datetime.now(), sync_id))
            else:
                self._conn.execute('''
                    UPDATE pending_sync 
                    SET last_attempt = ?
                    WHERE id = ?
                ''', (datetime.now(), sync_id))
    
    def remove_pending_sync(self, sync_id: int):
        """Remove processed sync from queue."""
        with self._conn:
            self._conn.execute('DELETE FROM pending_sync WHERE id = ?', (sync_id,))
    
    def is_session_synced(self, session_id: str) -> bool:
        """Check if session has already been synced."""
        row = self._conn.execute(
            'SELECT 1 FROM pending_sync WHERE session_id = ?', (session_id,)
        ).fetchone()
        return row is None
    
    def log_access(self, card_uid: Optional[str], user_id: Optional[str],
                   session_id: Optional[str] = None, tags_found: Optional[List[str]] = None):
        """Log access attempt."""
        with self._conn:
            self._conn.execute('''
                INSERT INTO access_logs (card_uid, user_id, session_id, tags_found)
                VALUES (?, ?, ?, ?)
            ''', (card_uid, user_id, session_id, json.dumps(tags_found or [])))
    
    def update_item_state(self, rfid_tag: str, status: str, holder_id: Optional[str]):
        """Update cached item state. Only updates if item exists."""
        with self._conn:
            # Check if item exists first
            existing = self._conn.execute(
                'SELECT 1 FROM item_cache WHERE rfid_tag = ?', (rfid_tag,)
            ).fetchone()
            
            if existing:
                # Update existing item
                self._conn.execute('''
                    UPDATE item_cache 
                    SET status = ?, holder_id = ?, updated_at = ?
                    WHERE rfid_tag = ?
                ''', (status, holder_id, datetime.now(), rfid_tag))
            # If not exists, don't insert (item_id and name would be NULL)
            # The item should be populated via local_sync or initial seed
    
    def get_item_cache(self, rfid_tag: str) -> Optional[Dict[str, Any]]:
        """Get cached item info."""
        row = self._conn.execute(
            'SELECT * FROM item_cache WHERE rfid_tag = ?', (rfid_tag,)
        ).fetchone()
        
        if row:
            return {
                'rfid_tag': row['rfid_tag'],
                'item_id': row['item_id'],
                'name': row['name'],
                'status': row['status'],
                'holder_id': row['holder_id']
            }
        return None
    
    def close(self):
        """Close database connection."""
        if self._conn:
            self._conn.close()
            logger.info("Database connection closed")
