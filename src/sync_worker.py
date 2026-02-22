"""Background sync worker for offline/online sync."""

import threading
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SyncWorker(threading.Thread):
    """Background thread for syncing with server."""
    
    def __init__(self, local_db, api_client, interval: int = 60):
        super().__init__(daemon=True)
        self.local_db = local_db
        self.api = api_client
        self.interval = interval
        self._running = False
        self._online = False
        self._stop_event = threading.Event()
    
    def run(self):
        """Main worker loop."""
        self._running = True
        logger.info("Sync worker started")
        
        while not self._stop_event.is_set():
            try:
                self._check_connection()
                if self._online:
                    self._sync_pending()
            except Exception as e:
                logger.exception(f"Sync error: {e}")
            
            self._stop_event.wait(self.interval)
        
        logger.info("Sync worker stopped")
    
    def _check_connection(self):
        """Check server connectivity."""
        try:
            self._online = self.api.health_check()
            if self._online:
                logger.debug("Server is online")
        except Exception:
            self._online = False
    
    def _sync_pending(self):
        """Process pending sync queue with idempotency."""
        pending = self.local_db.get_pending_sync(limit=10)
        
        if not pending:
            return
        
        logger.info(f"Processing {len(pending)} pending syncs")
        
        for item in pending:
            session_id = item['session_id']
            
            try:
                # Idempotency check: skip if already synced via another path
                if self.local_db.is_session_synced(session_id):
                    logger.debug(f"Session {session_id[:8]} already synced, removing from queue")
                    self.local_db.remove_pending_sync(item['id'])
                    continue
                
                # Mark attempt
                self.local_db.mark_sync_attempt(item['id'])
                
                # Call API
                result = self.api.sync_session(
                    session_id=session_id,
                    cabinet_id=1,  # TODO: get from config
                    user_id=item['user_id'],
                    rfids=item['rfids']
                )
                
                # Success - remove from queue
                self.local_db.remove_pending_sync(item['id'])
                
                # Mark diff as synced too
                self.local_db.mark_diff_synced(session_id)
                
                logger.info(f"Synced session {session_id[:8]}: "
                           f"{len(result.get('borrowed', []))} borrowed, "
                           f"{len(result.get('returned', []))} returned")
                
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Failed to sync {session_id[:8]}: {error_msg}")
                
                # Record failure for retry tracking
                self.local_db.mark_sync_attempt(item['id'], error=error_msg)
                
                # Stop processing more items if this one failed
                # (likely network issue, retry later)
                break
    
    def is_online(self) -> bool:
        """Check if server is online."""
        return self._online
    
    def stop(self):
        """Stop the worker thread."""
        self._running = False
        self._stop_event.set()
        self.join(timeout=5)
