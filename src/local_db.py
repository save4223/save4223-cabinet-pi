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
            email TEXT,
            role TEXT DEFAULT 'USER',
            cabinet_id INTEGER,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        );

        -- Item state cache (knows which user holds each item)
        CREATE TABLE IF NOT EXISTS item_cache (
            rfid_tag TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            name TEXT,
            description TEXT,
            status TEXT DEFAULT 'AVAILABLE',
            holder_id TEXT,
            holder_name TEXT,
            due_at TIMESTAMP,
            cabinet_id INTEGER,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- RFID snapshots (for local diff calculation)
        CREATE TABLE IF NOT EXISTS rfid_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            cabinet_id INTEGER NOT NULL,
            rfid_tag TEXT NOT NULL,
            snapshot_type TEXT DEFAULT 'end',  -- 'start' or 'end'
            present BOOLEAN NOT NULL DEFAULT 1,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Session diff results (for immediate display)
        CREATE TABLE IF NOT EXISTS session_diffs (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            user_name TEXT,
            borrowed TEXT,  -- JSON array
            returned TEXT,  -- JSON array
            start_rfids TEXT,  -- JSON array
            end_rfids TEXT,  -- JSON array
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            synced BOOLEAN DEFAULT FALSE,
            synced_at TIMESTAMP,
            server_confirmed BOOLEAN DEFAULT FALSE
        );

        -- Pending sync sessions (with idempotency check)
        CREATE TABLE IF NOT EXISTS pending_sync (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,  -- UNIQUE prevents duplicates
            user_id TEXT NOT NULL,
            start_rfids TEXT,  -- JSON array
            end_rfids TEXT,  -- JSON array
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_attempt TIMESTAMP
        );

        -- Pending pairings (for offline mode)
        CREATE TABLE IF NOT EXISTS pending_pairings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_uid TEXT NOT NULL,
            pairing_code TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_attempt TIMESTAMP
        );

        -- Borrow history (local record)
        CREATE TABLE IF NOT EXISTS borrow_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id TEXT,
            session_id TEXT,
            user_id TEXT NOT NULL,
            user_name TEXT,
            rfid_tag TEXT NOT NULL,
            item_id TEXT,
            item_name TEXT,
            action TEXT NOT NULL,  -- 'BORROW' or 'RETURN'
            synced BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Access logs
        CREATE TABLE IF NOT EXISTS access_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_uid TEXT,
            user_id TEXT,
            user_name TEXT,
            session_id TEXT,
            action TEXT,  -- 'AUTH_SUCCESS', 'AUTH_FAILURE', 'PAIRING', 'DOOR_OPEN', 'DOOR_CLOSE'
            tags_found TEXT,  -- JSON array
            details TEXT,  -- JSON object for additional details
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Offline actions queue (generic queue for all offline operations)
        CREATE TABLE IF NOT EXISTS offline_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,  -- 'session_sync', 'pairing', 'inventory_update'
            payload TEXT NOT NULL,  -- JSON payload
            priority INTEGER DEFAULT 5,  -- Lower = higher priority
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_attempt TIMESTAMP
        );

        -- Create indexes
        CREATE INDEX IF NOT EXISTS idx_auth_expires ON auth_cache(expires_at);
        CREATE INDEX IF NOT EXISTS idx_auth_user ON auth_cache(user_id);
        CREATE INDEX IF NOT EXISTS idx_item_status ON item_cache(status);
        CREATE INDEX IF NOT EXISTS idx_item_holder ON item_cache(holder_id);
        CREATE INDEX IF NOT EXISTS idx_item_cabinet ON item_cache(cabinet_id);
        CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_sync(created_at);
        CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_sync(session_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_session ON rfid_snapshots(session_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_cabinet ON rfid_snapshots(cabinet_id);
        CREATE INDEX IF NOT EXISTS idx_diffs_session ON session_diffs(session_id);
        CREATE INDEX IF NOT EXISTS idx_diffs_user ON session_diffs(user_id);
        CREATE INDEX IF NOT EXISTS idx_borrow_history_user ON borrow_history(user_id);
        CREATE INDEX IF NOT EXISTS idx_borrow_history_rfid ON borrow_history(rfid_tag);
        CREATE INDEX IF NOT EXISTS idx_borrow_history_session ON borrow_history(session_id);
        CREATE INDEX IF NOT EXISTS idx_access_logs_session ON access_logs(session_id);
        CREATE INDEX IF NOT EXISTS idx_access_logs_user ON access_logs(user_id);
        CREATE INDEX IF NOT EXISTS idx_offline_queue_type ON offline_queue(action_type);
        CREATE INDEX IF NOT EXISTS idx_offline_queue_priority ON offline_queue(priority);
        CREATE INDEX IF NOT EXISTS idx_pending_pairings ON pending_pairings(created_at);
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
        """Get RFID tags from most recent session snapshot."""
        # Get the most recent session (excluding current one if specified)
        query = '''
            SELECT DISTINCT session_id FROM rfid_snapshots 
            WHERE cabinet_id = ? AND session_id != ?
            ORDER BY captured_at DESC
            LIMIT 1
        '''
        row = self._conn.execute(query, (cabinet_id, before_session or '')).fetchone()
        
        if not row:
            return set()
        
        last_session_id = row['session_id']
        
        # Get all tags from that session
        rows = self._conn.execute(
            'SELECT rfid_tag FROM rfid_snapshots WHERE session_id = ?',
            (last_session_id,)
        ).fetchall()
        
        return set(row['rfid_tag'] for row in rows)
    
    def calculate_diff(self, current_rfids: List[str], cabinet_id: int, user_id: str) -> Tuple[List[Dict], List[Dict]]:
        """
        Calculate diff between current RFID scan and last snapshot.
        Returns: (borrowed_items, returned_items)
        """
        current_set = set(current_rfids)
        last_set = self.get_last_snapshot(cabinet_id)
        
        logger.info(f"Diff calc: current={len(current_set)} tags, last={len(last_set)} tags")
        logger.debug(f"Current: {current_set}")
        logger.debug(f"Last: {last_set}")
        
        # Items that disappeared = borrowed by this user
        borrowed_tags = last_set - current_set
        # Items that appeared = returned by this user  
        returned_tags = current_set - last_set
        
        logger.info(f"Borrowed tags: {borrowed_tags}, Returned tags: {returned_tags}")
        
        borrowed = []
        returned = []
        
        # Lookup item details from cache
        for tag in borrowed_tags:
            item = self._conn.execute(
                'SELECT * FROM item_cache WHERE rfid_tag = ?', (tag,)
            ).fetchone()
            if item:
                # Verify this item was held by this user (or available for first borrow)
                # For simulation, we assume any missing item is borrowed by current user
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
                # Verify this item was borrowed by this user
                # For simulation, we assume any new item is returned by current user
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

    # =========================================================================
    # Enhanced RFID Snapshot Methods
    # =========================================================================

    def save_rfid_snapshot(self, session_id: str, cabinet_id: int,
                          rfid_tags: List[str], snapshot_type: str = 'end'):
        """
        Save RFID snapshot with type (start or end).

        Args:
            session_id: Session identifier
            cabinet_id: Cabinet identifier
            rfid_tags: List of RFID tags present
            snapshot_type: 'start' or 'end'
        """
        with self._conn:
            # Clear any existing snapshots of this type for this session
            self._conn.execute('''
                DELETE FROM rfid_snapshots
                WHERE session_id = ? AND snapshot_type = ?
            ''', (session_id, snapshot_type))

            # Save new snapshot
            for tag in rfid_tags:
                self._conn.execute('''
                    INSERT INTO rfid_snapshots
                    (session_id, cabinet_id, rfid_tag, snapshot_type, present)
                    VALUES (?, ?, ?, ?, ?)
                ''', (session_id, cabinet_id, tag, snapshot_type, True))

        logger.info(f"Saved {snapshot_type} snapshot: {len(rfid_tags)} tags for session {session_id[:8]}")

    def get_snapshot(self, session_id: str, snapshot_type: str = 'end') -> List[str]:
        """Get RFID tags from a specific snapshot."""
        rows = self._conn.execute('''
            SELECT rfid_tag FROM rfid_snapshots
            WHERE session_id = ? AND snapshot_type = ?
        ''', (session_id, snapshot_type)).fetchall()
        return [row['rfid_tag'] for row in rows]

    def get_last_snapshot(self, cabinet_id: int, before_session: Optional[str] = None) -> List[str]:
        """Get RFID tags from most recent end snapshot."""
        query = '''
            SELECT DISTINCT session_id, captured_at
            FROM rfid_snapshots
            WHERE cabinet_id = ? AND snapshot_type = 'end'
        '''
        params = [cabinet_id]

        if before_session:
            query += ' AND session_id != ?'
            params.append(before_session)

        query += ' ORDER BY captured_at DESC LIMIT 1'

        row = self._conn.execute(query, params).fetchone()

        if not row:
            return []

        # Get all tags from that session
        rows = self._conn.execute('''
            SELECT rfid_tag FROM rfid_snapshots
            WHERE session_id = ? AND snapshot_type = 'end'
        ''', (row['session_id'],)).fetchall()

        return [r['rfid_tag'] for r in rows]

    # =========================================================================
    # Item Cache Methods
    # =========================================================================

    def update_item_cache(self, rfid_tag: str, item_id: str, name: str,
                         status: str = 'AVAILABLE', holder_id: Optional[str] = None,
                         description: Optional[str] = None, cabinet_id: Optional[int] = None):
        """Update or insert item cache."""
        with self._conn:
            self._conn.execute('''
                INSERT INTO item_cache
                (rfid_tag, item_id, name, description, status, holder_id, cabinet_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rfid_tag) DO UPDATE SET
                    item_id = excluded.item_id,
                    name = excluded.name,
                    description = excluded.description,
                    status = excluded.status,
                    holder_id = excluded.holder_id,
                    cabinet_id = excluded.cabinet_id,
                    updated_at = excluded.updated_at
            ''', (rfid_tag, item_id, name, description, status, holder_id, cabinet_id, datetime.now()))

    def get_all_items_in_cabinet(self, cabinet_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all items currently in cabinet (AVAILABLE status)."""
        query = 'SELECT * FROM item_cache WHERE status = ?'
        params = ['AVAILABLE']

        if cabinet_id:
            query += ' AND cabinet_id = ?'
            params.append(cabinet_id)

        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_borrowed_items(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get items currently borrowed."""
        query = 'SELECT * FROM item_cache WHERE status = ?'
        params = ['BORROWED']

        if user_id:
            query += ' AND holder_id = ?'
            params.append(user_id)

        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_item_cache_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Get cached item by item_id (not RFID)."""
        row = self._conn.execute(
            'SELECT * FROM item_cache WHERE item_id = ?', (item_id,)
        ).fetchone()
        return dict(row) if row else None

    # =========================================================================
    # Borrow History Methods
    # =========================================================================

    def record_borrow(self, session_id: str, user_id: str, user_name: str,
                     rfid_tag: str, item_id: Optional[str], item_name: str):
        """Record a borrow action."""
        with self._conn:
            self._conn.execute('''
                INSERT INTO borrow_history
                (session_id, user_id, user_name, rfid_tag, item_id, item_name, action)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (session_id, user_id, user_name, rfid_tag, item_id, item_name, 'BORROW'))

    def record_return(self, session_id: str, user_id: str, user_name: str,
                     rfid_tag: str, item_id: Optional[str], item_name: str):
        """Record a return action."""
        with self._conn:
            self._conn.execute('''
                INSERT INTO borrow_history
                (session_id, user_id, user_name, rfid_tag, item_id, item_name, action)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (session_id, user_id, user_name, rfid_tag, item_id, item_name, 'RETURN'))

    def get_user_borrow_history(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Get borrow/return history for a user."""
        rows = self._conn.execute('''
            SELECT * FROM borrow_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        ''', (user_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def get_item_history(self, rfid_tag: str) -> List[Dict[str, Any]]:
        """Get full history for a specific item."""
        rows = self._conn.execute('''
            SELECT * FROM borrow_history
            WHERE rfid_tag = ?
            ORDER BY created_at DESC
        ''', (rfid_tag,)).fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # Pending Pairing Methods
    # =========================================================================

    def queue_pending_pairing(self, card_uid: str, pairing_code: str,
                              created_at: Optional[datetime] = None):
        """Queue a pairing for later sync."""
        with self._conn:
            self._conn.execute('''
                INSERT INTO pending_pairings (card_uid, pairing_code, created_at)
                VALUES (?, ?, ?)
            ''', (card_uid, pairing_code, created_at or datetime.now()))

    def get_pending_pairings(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get pending pairings."""
        rows = self._conn.execute('''
            SELECT * FROM pending_pairings
            ORDER BY created_at ASC
            LIMIT ?
        ''', (limit,)).fetchall()
        return [dict(row) for row in rows]

    def remove_pending_pairing(self, pairing_id: int):
        """Remove a pending pairing."""
        with self._conn:
            self._conn.execute('DELETE FROM pending_pairings WHERE id = ?', (pairing_id,))

    def mark_pairing_attempt(self, pairing_id: int, error: Optional[str] = None):
        """Record pairing attempt."""
        with self._conn:
            if error:
                self._conn.execute('''
                    UPDATE pending_pairings
                    SET retry_count = retry_count + 1, last_error = ?, last_attempt = ?
                    WHERE id = ?
                ''', (error, datetime.now(), pairing_id))

    # =========================================================================
    # Enhanced Session Diff Methods
    # =========================================================================

    def save_session_diff(self, session_id: str, user_id: str, user_name: str,
                         borrowed: List[Dict], returned: List[Dict],
                         start_rfids: List[str], end_rfids: List[str]):
        """Save session diff with full details."""
        with self._conn:
            self._conn.execute('''
                INSERT OR REPLACE INTO session_diffs
                (session_id, user_id, user_name, borrowed, returned,
                 start_rfids, end_rfids, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session_id, user_id, user_name,
                json.dumps(borrowed), json.dumps(returned),
                json.dumps(start_rfids), json.dumps(end_rfids),
                datetime.now()
            ))

    def get_session_full_diff(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get complete session diff including RFID lists."""
        row = self._conn.execute(
            'SELECT * FROM session_diffs WHERE session_id = ?', (session_id,)
        ).fetchone()

        if not row:
            return None

        return {
            'session_id': row['session_id'],
            'user_id': row['user_id'],
            'user_name': row['user_name'],
            'borrowed': json.loads(row['borrowed']),
            'returned': json.loads(row['returned']),
            'start_rfids': json.loads(row['start_rfids']) if row['start_rfids'] else [],
            'end_rfids': json.loads(row['end_rfids']) if row['end_rfids'] else [],
            'created_at': row['created_at'],
            'synced': row['synced'],
            'server_confirmed': row['server_confirmed']
        }

    def queue_session_sync(self, session_id: str, user_id: str,
                          start_rfids: List[str], end_rfids: List[str]) -> bool:
        """Queue session for sync with start/end RFID lists."""
        try:
            with self._conn:
                self._conn.execute('''
                    INSERT INTO pending_sync (session_id, user_id, start_rfids, end_rfids)
                    VALUES (?, ?, ?, ?)
                ''', (session_id, user_id, json.dumps(start_rfids), json.dumps(end_rfids)))
            return True
        except sqlite3.IntegrityError:
            return False

    def get_pending_sync_full(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get pending sync with full RFID lists."""
        rows = self._conn.execute('''
            SELECT * FROM pending_sync
            ORDER BY retry_count ASC, created_at ASC
            LIMIT ?
        ''', (limit,)).fetchall()

        return [{
            'id': row['id'],
            'session_id': row['session_id'],
            'user_id': row['user_id'],
            'start_rfids': json.loads(row['start_rfids']) if row['start_rfids'] else [],
            'end_rfids': json.loads(row['end_rfids']) if row['end_rfids'] else [],
            'retry_count': row['retry_count'],
            'created_at': row['created_at'],
            'last_attempt': row['last_attempt'],
        } for row in rows]

    def mark_session_server_confirmed(self, session_id: str):
        """Mark session as confirmed by server."""
        with self._conn:
            self._conn.execute('''
                UPDATE session_diffs
                SET server_confirmed = TRUE
                WHERE session_id = ?
            ''', (session_id,))

    # =========================================================================
    # Enhanced Access Logging
    # =========================================================================

    def log_access(self, card_uid: Optional[str], user_id: Optional[str],
                   user_name: Optional[str] = None, session_id: Optional[str] = None,
                   action: str = 'ACCESS', tags_found: Optional[List[str]] = None,
                   details: Optional[Dict] = None):
        """Enhanced access logging with action type."""
        with self._conn:
            self._conn.execute('''
                INSERT INTO access_logs
                (card_uid, user_id, user_name, session_id, action, tags_found, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (card_uid, user_id, user_name, session_id, action,
                 json.dumps(tags_found or []), json.dumps(details or {})))

    def get_access_logs(self, user_id: Optional[str] = None,
                       session_id: Optional[str] = None,
                       limit: int = 100) -> List[Dict[str, Any]]:
        """Query access logs."""
        query = 'SELECT * FROM access_logs WHERE 1=1'
        params = []

        if user_id:
            query += ' AND user_id = ?'
            params.append(user_id)
        if session_id:
            query += ' AND session_id = ?'
            params.append(session_id)

        query += ' ORDER BY created_at DESC LIMIT ?'
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # =========================================================================
    # Offline Queue Methods
    # =========================================================================

    def queue_offline_action(self, action_type: str, payload: Dict[str, Any],
                            priority: int = 5, max_retries: int = 3):
        """Queue an action for offline processing."""
        with self._conn:
            self._conn.execute('''
                INSERT INTO offline_queue (action_type, payload, priority, max_retries)
                VALUES (?, ?, ?, ?)
            ''', (action_type, json.dumps(payload), priority, max_retries))

    def get_offline_queue(self, action_type: Optional[str] = None,
                         limit: int = 10) -> List[Dict[str, Any]]:
        """Get pending offline actions."""
        query = 'SELECT * FROM offline_queue WHERE retry_count < max_retries'
        params = []

        if action_type:
            query += ' AND action_type = ?'
            params.append(action_type)

        query += ' ORDER BY priority ASC, created_at ASC LIMIT ?'
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        return [{
            'id': row['id'],
            'action_type': row['action_type'],
            'payload': json.loads(row['payload']),
            'priority': row['priority'],
            'retry_count': row['retry_count'],
            'created_at': row['created_at'],
        } for row in rows]

    def mark_offline_action_complete(self, action_id: int):
        """Remove completed offline action."""
        with self._conn:
            self._conn.execute('DELETE FROM offline_queue WHERE id = ?', (action_id,))

    def mark_offline_action_failed(self, action_id: int, error: str):
        """Record offline action failure."""
        with self._conn:
            self._conn.execute('''
                UPDATE offline_queue
                SET retry_count = retry_count + 1, last_error = ?, last_attempt = ?
                WHERE id = ?
            ''', (error, datetime.now(), action_id))

    # =========================================================================
    # Statistics Methods
    # =========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        stats = {}

        # Pending sync count
        row = self._conn.execute('SELECT COUNT(*) as count FROM pending_sync').fetchone()
        stats['pending_syncs'] = row['count']

        # Pending pairings
        row = self._conn.execute('SELECT COUNT(*) as count FROM pending_pairings').fetchone()
        stats['pending_pairings'] = row['count']

        # Offline queue
        row = self._conn.execute('SELECT COUNT(*) as count FROM offline_queue').fetchone()
        stats['offline_queue'] = row['count']

        # Items in cabinet
        row = self._conn.execute(
            "SELECT COUNT(*) as count FROM item_cache WHERE status = 'AVAILABLE'"
        ).fetchone()
        stats['items_in_cabinet'] = row['count']

        # Borrowed items
        row = self._conn.execute(
            "SELECT COUNT(*) as count FROM item_cache WHERE status = 'BORROWED'"
        ).fetchone()
        stats['items_borrowed'] = row['count']

        # Total access logs
        row = self._conn.execute('SELECT COUNT(*) as count FROM access_logs').fetchone()
        stats['total_access_logs'] = row['count']

        # Cached users
        row = self._conn.execute('SELECT COUNT(*) as count FROM auth_cache').fetchone()
        stats['cached_users'] = row['count']

        return stats
