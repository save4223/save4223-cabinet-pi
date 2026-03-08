"""Inventory management for Smart Cabinet Pi.

Handles item tracking, borrow/return detection, and local inventory cache.
"""

import logging
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass
from datetime import datetime

from local_db import LocalDB

logger = logging.getLogger(__name__)


@dataclass
class InventoryChange:
    """Represents a single inventory change (borrow or return)."""
    rfid_tag: str
    item_id: Optional[str]
    item_name: str
    action: str  # 'BORROW' or 'RETURN'
    user_id: str
    timestamp: datetime


@dataclass
class SessionResult:
    """Result of a cabinet session."""
    session_id: str
    user_id: str
    user_name: str
    start_time: datetime
    end_time: datetime
    borrowed: List[Dict[str, Any]]
    returned: List[Dict[str, Any]]
    start_rfids: List[str]
    end_rfids: List[str]
    synced: bool = False


class InventoryManager:
    """
    Manages inventory state and tracks item movements.

    Key responsibilities:
    - Track which items are in the cabinet
    - Detect borrow/return events
    - Maintain local cache of item information
    - Support offline operation
    """

    def __init__(self, local_db: LocalDB):
        self.db = local_db
        self._session_start_rfids: Optional[Set[str]] = None
        self._current_session_id: Optional[str] = None

    def start_session(self, session_id: str, user_id: str) -> None:
        """
        Start a new session and capture initial RFID state.

        This should be called when the cabinet is unlocked.
        """
        self._current_session_id = session_id
        logger.info(f"Starting session {session_id[:8]} for user {user_id}")

    def capture_start_snapshot(self, rfid_tags: List[str]) -> None:
        """
        Capture RFID snapshot when cabinet is unlocked (start of session).

        Args:
            rfid_tags: List of RFID tags present at start
        """
        self._session_start_rfids = set(rfid_tags)
        logger.info(f"Start snapshot captured: {len(rfid_tags)} tags")

        # Save to database
        if self._current_session_id:
            self.db.save_rfid_snapshot(
                session_id=self._current_session_id,
                cabinet_id=1,  # TODO: from config
                rfid_tags=rfid_tags,
                snapshot_type='start'
            )

    def capture_end_snapshot(self, rfid_tags: List[str]) -> Tuple[List[Dict], List[Dict]]:
        """
        Capture RFID snapshot when cabinet is locked (end of session).
        Calculates borrow/return based on diff with start snapshot.

        Args:
            rfid_tags: List of RFID tags present at end

        Returns:
            Tuple of (borrowed_items, returned_items)
        """
        if self._session_start_rfids is None:
            logger.warning("No start snapshot available, using last known state")
            self._session_start_rfids = self._get_last_known_state()

        end_rfids = set(rfid_tags)
        start_rfids = self._session_start_rfids

        # Calculate diff
        borrowed_tags = start_rfids - end_rfids  # Was there, now gone
        returned_tags = end_rfids - start_rfids  # Wasn't there, now is

        logger.info(f"End snapshot: {len(end_rfids)} tags")
        logger.info(f"Diff: {len(borrowed_tags)} borrowed, {len(returned_tags)} returned")

        # Convert tags to item details
        borrowed_items = self._resolve_items(borrowed_tags, 'BORROW')
        returned_items = self._resolve_items(returned_tags, 'RETURN')

        # Save end snapshot
        if self._current_session_id:
            self.db.save_rfid_snapshot(
                session_id=self._current_session_id,
                cabinet_id=1,
                rfid_tags=rfid_tags,
                snapshot_type='end'
            )

        # Update local item cache
        self._update_item_cache(borrowed_items, returned_items)

        return borrowed_items, returned_items

    def _get_last_known_state(self) -> Set[str]:
        """Get the last known RFID state from database."""
        # Query the most recent end snapshot
        tags = self.db.get_last_snapshot(cabinet_id=1)
        return set(tags)

    def _resolve_items(self, rfid_tags: Set[str], action: str) -> List[Dict[str, Any]]:
        """
        Resolve RFID tags to item details.

        Args:
            rfid_tags: Set of RFID tags
            action: 'BORROW' or 'RETURN' (for logging)

        Returns:
            List of item dictionaries
        """
        items = []
        for tag in rfid_tags:
            item = self.db.get_item_cache(tag)
            if item:
                items.append({
                    'rfid': tag,
                    'item_id': item['item_id'],
                    'name': item['name'] or f'Item {tag}',
                    'action': action
                })
            else:
                # Unknown tag - still track it
                items.append({
                    'rfid': tag,
                    'item_id': None,
                    'name': f'Unknown Item ({tag})',
                    'action': action
                })
                logger.warning(f"Unknown RFID tag {tag} detected during {action}")

        return items

    def _update_item_cache(self, borrowed: List[Dict], returned: List[Dict]) -> None:
        """Update local item cache based on borrow/return events."""
        for item in borrowed:
            self.db.update_item_state(
                rfid_tag=item['rfid'],
                status='BORROWED',
                holder_id=self._current_session_id  # Will be updated with user_id
            )

        for item in returned:
            self.db.update_item_state(
                rfid_tag=item['rfid'],
                status='AVAILABLE',
                holder_id=None
            )

    def get_current_inventory(self) -> List[Dict[str, Any]]:
        """Get current inventory (items currently in cabinet)."""
        return self.db.get_all_items_in_cabinet()

    def get_borrowed_items(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get items currently borrowed.

        Args:
            user_id: If provided, filter by this user

        Returns:
            List of borrowed items
        """
        return self.db.get_borrowed_items(user_id)

    def sync_item_cache(self, server_items: List[Dict[str, Any]]) -> None:
        """
        Sync item cache with server data.

        This should be called periodically to keep local cache up to date.
        """
        for item in server_items:
            self.db.update_item_cache(
                rfid_tag=item['rfid_tag'],
                item_id=item['item_id'],
                name=item['name'],
                status=item['status'],
                holder_id=item.get('holder_id')
            )
        logger.info(f"Synced {len(server_items)} items from server")

    def end_session(self) -> None:
        """End current session and clear session state."""
        self._session_start_rfids = None
        self._current_session_id = None
        logger.info("Session ended")

    def validate_session_data(self, start_rfids: List[str], end_rfids: List[str]) -> Dict[str, Any]:
        """
        Validate session data before sending to server.

        Returns:
            Validation result with warnings if any
        """
        warnings = []

        # Check for suspicious patterns
        start_set = set(start_rfids)
        end_set = set(end_rfids)

        # Too many changes at once (possible sensor error)
        total_changes = len(start_set - end_set) + len(end_set - start_set)
        if total_changes > 10:
            warnings.append(f"Large number of changes detected ({total_changes}), possible sensor error")

        # All items removed (possible cabinet emptying)
        if len(end_set) == 0 and len(start_set) > 0:
            warnings.append("All items removed from cabinet")

        # Unknown tags in result
        unknown_borrowed = [tag for tag in (start_set - end_set)
                           if not self.db.get_item_cache(tag)]
        unknown_returned = [tag for tag in (end_set - start_set)
                           if not self.db.get_item_cache(tag)]

        if unknown_borrowed:
            warnings.append(f"Unknown tags borrowed: {unknown_borrowed}")
        if unknown_returned:
            warnings.append(f"Unknown tags returned: {unknown_returned}")

        return {
            'valid': len(warnings) == 0,
            'warnings': warnings,
            'start_count': len(start_set),
            'end_count': len(end_set),
            'borrowed_count': len(start_set - end_set),
            'returned_count': len(end_set - start_set),
        }
