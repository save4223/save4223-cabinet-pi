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
        pending = self.local_db.get_pending_sync_full(limit=10)

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

                # Get session details
                session_diff = self.local_db.get_session_full_diff(session_id)

                # Call API with start/end RFIDs
                result = self.api.sync_session(
                    session_id=session_id,
                    cabinet_id=1,  # TODO: get from config
                    user_id=item['user_id'],
                    start_rfids=item.get('start_rfids', []),
                    end_rfids=item.get('end_rfids', [])
                )

                # Success - remove from queue
                self.local_db.remove_pending_sync(item['id'])

                # Mark diff as synced and server confirmed
                self.local_db.mark_diff_synced(session_id)
                self.local_db.mark_session_server_confirmed(session_id)

                # Log transactions
                transactions = result.get('transactions', [])
                logger.info(f"Synced session {session_id[:8]}: {len(transactions)} transactions")

                # Record borrow/return history
                if session_diff:
                    for item_data in session_diff.get('borrowed', []):
                        self.local_db.record_borrow(
                            session_id=session_id,
                            user_id=item['user_id'],
                            user_name=session_diff.get('user_name', 'Unknown'),
                            rfid_tag=item_data['rfid'],
                            item_id=item_data.get('item_id'),
                            item_name=item_data.get('name', 'Unknown')
                        )
                    for item_data in session_diff.get('returned', []):
                        self.local_db.record_return(
                            session_id=session_id,
                            user_id=item['user_id'],
                            user_name=session_diff.get('user_name', 'Unknown'),
                            rfid_tag=item_data['rfid'],
                            item_id=item_data.get('item_id'),
                            item_name=item_data.get('name', 'Unknown')
                        )

            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Failed to sync {session_id[:8]}: {error_msg}")

                # Record failure for retry tracking
                self.local_db.mark_sync_attempt(item['id'], error=error_msg)

                # Stop processing more items if this one failed
                # (likely network issue, retry later)
                break

    def sync_inventory_cache(self) -> bool:
        """Sync item inventory cache with server."""
        if not self._online:
            return False

        try:
            # Fetch latest inventory from server
            result = self.api.local_sync(cabinet_id=1)  # TODO: from config

            # Update local cache
            items = result.get('items', [])
            for item in items:
                self.local_db.update_item_cache(
                    rfid_tag=item['rfid_tag'],
                    item_id=item['item_id'],
                    name=item['name'],
                    status=item.get('status', 'AVAILABLE'),
                    holder_id=item.get('holder_id'),
                    description=item.get('description'),
                    cabinet_id=item.get('cabinet_id')
                )

            # Cache auth data
            for user in result.get('users', []):
                self.local_db.cache_auth(
                    card_uid=user['card_uid'],
                    auth_result={
                        'user_id': user['user_id'],
                        'user_name': user['user_name'],
                        'email': user.get('email'),
                        'role': user.get('role', 'USER')
                    },
                    ttl=3600 * 24  # 24 hours
                )

            logger.info(f"Synced inventory: {len(items)} items, {len(result.get('users', []))} users")
            return True

        except Exception as e:
            logger.error(f"Failed to sync inventory: {e}")
            return False

    def check_and_sync(self) -> Dict[str, Any]:
        """Check connection and perform all sync operations."""
        results = {
            'online': False,
            'sessions_synced': 0,
            'inventory_synced': False,
            'pairings_synced': 0,
            'errors': []
        }

        # Check connection
        self._check_connection()
        results['online'] = self._online

        if not self._online:
            results['errors'].append('Server is offline')
            return results

        # Sync pending sessions
        try:
            pending = self.local_db.get_pending_sync_full()
            results['sessions_synced'] = len(pending)
            self._sync_pending()
        except Exception as e:
            results['errors'].append(f'Session sync error: {e}')

        # Sync inventory cache
        try:
            results['inventory_synced'] = self.sync_inventory_cache()
        except Exception as e:
            results['errors'].append(f'Inventory sync error: {e}')

        return results
    
    def is_online(self) -> bool:
        """Check if server is online."""
        return self._online
    
    def stop(self):
        """Stop the worker thread."""
        self._running = False
        self._stop_event.set()
        self.join(timeout=5)
