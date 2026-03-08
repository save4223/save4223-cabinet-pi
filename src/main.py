#!/usr/bin/env python3
"""
Smart Cabinet Pi - Main Entry Point
Raspberry Pi controller for Save4223 smart tool cabinet system.
"""

import time
import signal
import sys
import logging
import uuid
import json
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from state_machine import StateMachine, SystemState
from api_client import APIClient, APIError
from local_db import LocalDB
from sync_worker import SyncWorker
from pairing_handler import PairingHandler, PairingResult
from inventory_manager import InventoryManager
from config import CONFIG

# Hardware imports
HARDWARE_MODE = CONFIG.get('hardware', {}).get('mode', 'mock')
if HARDWARE_MODE == 'mock':
    from hardware import MockHardware as HardwareController
else:
    from hardware import RaspberryPiHardware as HardwareController

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/var/log/cabinet.log')
    ]
)
logger = logging.getLogger(__name__)


class SmartCabinet:
    """Main cabinet controller class with full offline support."""

    # Operation modes
    MODE_NORMAL = 'normal'
    MODE_PAIRING = 'pairing'

    def __init__(self):
        self.running = False
        self.mode = self.MODE_NORMAL

        # Session state
        self.current_user_id: Optional[str] = None
        self.current_user_name: Optional[str] = 'Unknown'
        self.current_card_uid: Optional[str] = None
        self.session_id: Optional[str] = None
        self.session_start_time: Optional[datetime] = None

        # Initialize components
        logger.info("Initializing Smart Cabinet...")
        logger.info(f"Hardware mode: {HARDWARE_MODE}")

        self.state_machine = StateMachine()
        self.hardware = HardwareController()

        # SSL configuration
        ssl_config = CONFIG.get('ssl', {})
        cert_path = ssl_config.get('cert_path') if ssl_config.get('verify') else None

        self.api = APIClient(
            base_url=CONFIG['server_url'],
            edge_secret=CONFIG['edge_secret'],
            timeout=CONFIG.get('api', {}).get('timeout', 5),
            cert_path=cert_path,
            max_retries=CONFIG.get('api', {}).get('max_retries', 3),
            retry_delay=CONFIG.get('api', {}).get('retry_delay', 1.0)
        )

        self.local_db = LocalDB(CONFIG['db_path'])
        self.sync_worker = SyncWorker(
            self.local_db, self.api,
            interval=CONFIG.get('sync_interval', 60)
        )
        self.pairing_handler = PairingHandler(self.api, self.local_db)
        self.inventory = InventoryManager(self.local_db)

        # Setup handlers
        self._setup_signal_handlers()
        self._setup_state_handlers()

        # Start sync worker
        self.sync_worker.start()

        # Initial sync
        self._initial_sync()

        logger.info("Smart Cabinet initialized successfully")

    def _initial_sync(self):
        """Perform initial sync on startup."""
        logger.info("Performing initial sync...")
        try:
            if self.api.health_check():
                self.sync_worker.sync_inventory_cache()
                logger.info("Initial sync completed")
            else:
                logger.warning("Server unavailable, operating in offline mode")
        except Exception as e:
            logger.warning(f"Initial sync failed: {e}, operating in offline mode")

    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    def _setup_state_handlers(self):
        """Register state machine handlers."""
        self.state_machine.on_enter(SystemState.LOCKED, self._on_locked)
        self.state_machine.on_enter(SystemState.AUTHENTICATING, self._on_authenticating)
        self.state_machine.on_enter(SystemState.UNLOCKED, self._on_unlocked)
        self.state_machine.on_enter(SystemState.SCANNING, self._on_scanning)

    def _send_to_display(self, message: dict):
        """Send update to local dashboard."""
        logger.info(f"[DISPLAY] {message['type']}: {json.dumps(message, default=str)}")
        # TODO: Implement WebSocket to Electron display

    # =================================================================================
    # State Handlers
    # =================================================================================

    def _on_locked(self):
        """Handle LOCKED state entry."""
        logger.info("=" * 50)
        logger.info("State: LOCKED - Ready for card/QR scan")
        logger.info("=" * 50)

        self.hardware.set_all_leds('red')
        self.hardware.lock_all()

        # Reset session state
        self.current_user_id = None
        self.current_user_name = 'Unknown'
        self.current_card_uid = None
        self.session_id = None
        self.session_start_time = None

        self._send_to_display({
            'type': 'STATE_CHANGE',
            'state': 'LOCKED',
            'message': 'Tap card or scan QR code to open'
        })

    def _on_authenticating(self):
        """Handle AUTHENTICATING state entry."""
        logger.info("State: AUTHENTICATING")
        self.hardware.set_all_leds('yellow')

        self._send_to_display({
            'type': 'STATE_CHANGE',
            'state': 'AUTHENTICATING',
            'message': 'Reading card...'
        })

        # Read NFC/QR
        card_uid = self.hardware.read_nfc(timeout=30)

        if not card_uid:
            logger.warning("Authentication timeout - no card detected")
            self.hardware.beep_error()
            self._send_to_display({
                'type': 'AUTH_FAILURE',
                'error': 'Timeout - no card detected'
            })
            time.sleep(2)
            self.state_machine.transition(SystemState.LOCKED)
            return

        self.current_card_uid = card_uid
        logger.info(f"Card detected: {card_uid[:10]}...")

        # Check if in pairing mode
        if self.mode == self.MODE_PAIRING:
            self._handle_pairing_scan(card_uid)
            return

        # Normal authentication flow
        result = self._authenticate(card_uid)

        if result.get('authorized'):
            self._handle_auth_success(result)
        else:
            self._handle_auth_failure(result)

    def _handle_auth_success(self, result: Dict[str, Any]):
        """Handle successful authentication."""
        self.current_user_id = result['user_id']
        self.current_user_name = result.get('user_name', 'Unknown')
        self.session_id = str(uuid.uuid4())
        self.session_start_time = datetime.now()

        logger.info(f"Authenticated: {self.current_user_name} ({self.current_user_id})")

        # Log access
        self.local_db.log_access(
            card_uid=self.current_card_uid,
            user_id=self.current_user_id,
            user_name=self.current_user_name,
            session_id=self.session_id,
            action='AUTH_SUCCESS'
        )

        self._send_to_display({
            'type': 'AUTH_SUCCESS',
            'user': {
                'id': self.current_user_id,
                'name': self.current_user_name,
                'email': result.get('email', '')
            },
            'session_id': self.session_id
        })

        # Start inventory session
        self.inventory.start_session(self.session_id, self.current_user_id)

        self.state_machine.transition(SystemState.UNLOCKED)

    def _handle_auth_failure(self, result: Dict[str, Any]):
        """Handle authentication failure."""
        reason = result.get('reason', 'Access denied')
        logger.warning(f"Authentication failed: {reason}")

        # Log access
        self.local_db.log_access(
            card_uid=self.current_card_uid,
            action='AUTH_FAILURE',
            details={'reason': reason}
        )

        # Check if card is unpaired (needs pairing)
        if 'not registered' in reason.lower() or 'card not found' in reason.lower():
            logger.info("Unpaired card detected, offering pairing mode")
            self._send_to_display({
                'type': 'PAIRING_MODE',
                'message': 'Card not registered. Please use web app to generate pairing code.'
            })

        self.hardware.set_all_leds('red')
        self.hardware.beep_error()

        self._send_to_display({
            'type': 'AUTH_FAILURE',
            'error': reason
        })

        time.sleep(3)
        self.state_machine.transition(SystemState.LOCKED)

    def _authenticate(self, card_uid: str) -> Dict[str, Any]:
        """
        Authenticate card with API or local cache.

        Returns:
            Authentication result dict
        """
        # Try API first if online
        if self.sync_worker.is_online():
            try:
                result = self.api.authorize(card_uid, CONFIG['cabinet_id'])
                # Cache successful auth
                self.local_db.cache_auth(
                    card_uid, result,
                    ttl=3600 * 24 * 7  # 7 days
                )
                result['source'] = 'server'
                return result
            except APIError as e:
                logger.warning(f"API auth failed: {e}, falling back to cache")

        # Fallback to local cache
        cached = self.local_db.get_cached_auth(card_uid)
        if cached:
            logger.info("Using cached authentication")
            cached['source'] = 'cache'
            return cached

        return {'authorized': False, 'reason': 'Card not registered'}

    def _on_unlocked(self):
        """Handle UNLOCKED state entry."""
        logger.info("State: UNLOCKED")
        self.hardware.set_all_leds('green')
        self.hardware.unlock_all()
        self.hardware.beep_success()

        # Capture start snapshot (RFID tags present when unlocked)
        logger.info("Capturing start RFID snapshot...")
        start_tags = self._scan_rfid()
        self.inventory.capture_start_snapshot(start_tags)

        self.local_db.log_access(
            card_uid=self.current_card_uid,
            user_id=self.current_user_id,
            user_name=self.current_user_name,
            session_id=self.session_id,
            action='DOOR_OPEN',
            tags_found=start_tags
        )

        self._send_to_display({
            'type': 'STATE_CHANGE',
            'state': 'UNLOCKED',
            'message': f'Welcome {self.current_user_name}! Take or return tools, then tap card again to close.',
            'session_id': self.session_id,
            'start_tags': len(start_tags)
        })

        # Wait for user to finish
        unlock_time = time.time()
        session_timeout = CONFIG.get('session_timeout', 300)  # 5 minutes default

        while self.running and (time.time() - unlock_time) < session_timeout:
            # Check if same card scanned (close command)
            card = self.hardware.read_nfc(timeout=0.5)

            if card == self.current_card_uid:
                if self.hardware.are_all_drawers_closed():
                    logger.info("Close command received, all drawers closed")
                    self.state_machine.transition(SystemState.SCANNING)
                    return
                else:
                    logger.info("Please close all drawers first")
                    self.hardware.beep_warning()
                    self._send_to_display({
                        'type': 'WARNING',
                        'message': 'Please close all drawers first'
                    })

            # Update LED per drawer status
            for i in range(CONFIG.get('num_drawers', 4)):
                drawer_state = self.hardware.get_drawer_state(i)
                if drawer_state.value == 'open':
                    self.hardware.set_led(i, 'red')
                else:
                    self.hardware.set_led(i, 'green')

            time.sleep(0.1)

        # Session timeout
        logger.warning("Session timeout")
        self.hardware.beep_error()
        self._send_to_display({
            'type': 'TIMEOUT',
            'message': 'Session timeout'
        })
        self.state_machine.transition(SystemState.SCANNING)

    def _on_scanning(self):
        """Handle SCANNING state entry (end of session)."""
        logger.info("State: SCANNING - Finalizing session")
        self.hardware.set_all_leds('yellow')
        self.hardware.lock_all()

        # Capture end snapshot
        logger.info("Capturing end RFID snapshot...")
        end_tags = self._scan_rfid()

        self.local_db.log_access(
            card_uid=self.current_card_uid,
            user_id=self.current_user_id,
            user_name=self.current_user_name,
            session_id=self.session_id,
            action='DOOR_CLOSE',
            tags_found=end_tags
        )

        # Calculate diff
        borrowed, returned = self.inventory.capture_end_snapshot(end_tags)

        logger.info(f"Session summary: {len(borrowed)} borrowed, {len(returned)} returned")

        # Save session diff
        start_rfids = list(self.inventory._session_start_rfids or [])
        self.local_db.save_session_diff(
            session_id=self.session_id,
            user_id=self.current_user_id,
            user_name=self.current_user_name,
            borrowed=borrowed,
            returned=returned,
            start_rfids=start_rfids,
            end_rfids=end_tags
        )

        # Record borrow/return history locally
        for item in borrowed:
            self.local_db.record_borrow(
                session_id=self.session_id,
                user_id=self.current_user_id,
                user_name=self.current_user_name,
                rfid_tag=item['rfid'],
                item_id=item.get('item_id'),
                item_name=item.get('name', 'Unknown')
            )

        for item in returned:
            self.local_db.record_return(
                session_id=self.session_id,
                user_id=self.current_user_id,
                user_name=self.current_user_name,
                rfid_tag=item['rfid'],
                item_id=item.get('item_id'),
                item_name=item.get('name', 'Unknown')
            )

        # Display summary
        self._send_to_display({
            'type': 'SESSION_SUMMARY',
            'summary': {
                'borrowed': borrowed,
                'returned': returned,
                'borrowed_count': len(borrowed),
                'returned_count': len(returned)
            },
            'user_name': self.current_user_name
        })

        # Try to sync with server
        sync_success = self._try_sync_session(start_rfids, end_tags, borrowed, returned)

        if not sync_success:
            # Queue for later if sync failed
            self.local_db.queue_session_sync(
                session_id=self.session_id,
                user_id=self.current_user_id,
                start_rfids=start_rfids,
                end_rfids=end_tags
            )
            logger.info(f"Session {self.session_id[:8]} queued for later sync")
            self._send_to_display({
                'type': 'SYNC_QUEUED',
                'message': 'Changes saved locally, will sync when online'
            })

        # Cleanup
        self.inventory.end_session()

        # Show summary for 10 seconds then return to locked
        time.sleep(10)
        self.state_machine.transition(SystemState.LOCKED)

    def _try_sync_session(self, start_rfids: list, end_rfids: list,
                         borrowed: list, returned: list) -> bool:
        """
        Try to sync session with server.

        Returns:
            True if successful, False if should queue for later
        """
        if not self.sync_worker.is_online():
            logger.warning("Offline, will retry later")
            return False

        try:
            result = self.api.sync_session(
                session_id=self.session_id,
                cabinet_id=CONFIG['cabinet_id'],
                user_id=self.current_user_id,
                start_rfids=start_rfids,
                end_rfids=end_rfids
            )

            # Validate server result matches local calculation
            server_txs = result.get('transactions', [])
            server_borrowed = len([t for t in server_txs if t.get('action') == 'BORROW'])
            server_returned = len([t for t in server_txs if t.get('action') == 'RETURN'])
            local_borrowed = len(borrowed)
            local_returned = len(returned)

            if server_borrowed == local_borrowed and server_returned == local_returned:
                logger.info(f"Sync confirmed: {server_borrowed} borrowed, {server_returned} returned")
                self.local_db.mark_session_server_confirmed(self.session_id)
            else:
                logger.warning(f"Sync mismatch - Server: {server_borrowed}/{server_returned}, Local: {local_borrowed}/{local_returned}")

            # Mark diff as synced
            self.local_db.mark_diff_synced(self.session_id)

            self._send_to_display({
                'type': 'SYNC_SUCCESS',
                'message': 'Changes synced with server'
            })

            return True

        except APIError as e:
            logger.error(f"Sync failed: {e}")
            return False
        except Exception as e:
            logger.exception(f"Unexpected sync error: {e}")
            return False

    # =================================================================================
    # Pairing Mode
    # =================================================================================

    def _handle_pairing_scan(self, card_uid: str):
        """Handle card scan in pairing mode."""
        logger.info(f"Pairing mode: Card {card_uid[:10]}... detected")

        self._send_to_display({
            'type': 'PAIRING_PROMPT',
            'message': 'Show pairing QR code or enter pairing code'
        })

        # Try to read QR code
        qr_content = self.hardware.read_qr(timeout=30)

        if qr_content:
            # Try QR pairing
            result = self.pairing_handler.pair_with_qr(
                qr_content, card_uid, CONFIG['cabinet_id']
            )
            self._handle_pairing_result(result)
        else:
            # Timeout - go back to normal mode
            logger.info("Pairing timeout, returning to normal mode")
            self.mode = self.MODE_NORMAL
            self.state_machine.transition(SystemState.LOCKED)

    def _handle_pairing_result(self, result: PairingResult):
        """Handle pairing result."""
        if result.success:
            logger.info(f"Pairing successful: {result.message}")
            self.hardware.set_all_leds('green')
            self.hardware.beep_success()

            self._send_to_display({
                'type': 'PAIRING_SUCCESS',
                'message': result.message,
                'user_id': result.user_id
            })

            time.sleep(3)
        else:
            logger.warning(f"Pairing failed: {result.message}")
            self.hardware.set_all_leds('red')
            self.hardware.beep_error()

            self._send_to_display({
                'type': 'PAIRING_FAILURE',
                'error': result.message,
                'code': result.error_code
            })

            time.sleep(3)

        # Return to normal mode
        self.mode = self.MODE_NORMAL
        self.state_machine.transition(SystemState.LOCKED)

    def enter_pairing_mode(self):
        """Manually enter pairing mode (for testing or admin use)."""
        logger.info("Entering pairing mode")
        self.mode = self.MODE_PAIRING
        self._send_to_display({
            'type': 'PAIRING_MODE',
            'message': 'Pairing mode active. Tap unpaired card to begin.'
        })

    # =================================================================================
    # RFID Scanning
    # =================================================================================

    def _scan_rfid(self) -> list:
        """Perform multiple RFID scans and return unique tags."""
        all_tags = set()
        scan_count = CONFIG.get('rfid_scan_count', 10)

        logger.debug(f"Starting RFID scan ({scan_count} iterations)")

        for i in range(scan_count):
            tags = self.hardware.read_rfid_tags()
            all_tags.update(tags)
            if tags:
                logger.debug(f"Scan {i+1}: {len(tags)} tags found")
            time.sleep(0.1)

        result = sorted(list(all_tags))
        logger.info(f"RFID scan complete: {len(result)} unique tags found")
        return result

    # =================================================================================
    # Main Loop
    # =================================================================================

    def run(self):
        """Main loop."""
        logger.info("Starting Smart Cabinet main loop")
        self.running = True

        # Initialize hardware
        self.hardware.initialize()

        # Initial state
        self.state_machine.transition(SystemState.LOCKED)

        try:
            while self.running:
                state = self.state_machine.current_state

                if state == SystemState.LOCKED:
                    self._handle_locked()
                elif state == SystemState.AUTHENTICATING:
                    pass  # Handled by on_enter
                elif state == SystemState.UNLOCKED:
                    pass  # Handled by on_enter
                elif state == SystemState.SCANNING:
                    pass  # Handled by on_enter

                time.sleep(0.1)

        except KeyboardInterrupt:
            logger.info("\nUser interrupt")
            self.running = False
        except Exception as e:
            logger.exception("Fatal error in main loop")
        finally:
            self.cleanup()

    def _handle_locked(self):
        """Poll for NFC in LOCKED state."""
        card = self.hardware.read_nfc(timeout=0.5)
        if card:
            logger.info(f"Card detected: {card[:10]}...")
            self.state_machine.transition(SystemState.AUTHENTICATING)

    def cleanup(self):
        """Cleanup resources."""
        logger.info("Cleaning up...")
        self.running = False
        self.sync_worker.stop()
        self.local_db.close()
        self.hardware.cleanup()
        logger.info("Cleanup complete")

    def get_stats(self) -> Dict[str, Any]:
        """Get system statistics."""
        return {
            'hardware_mode': HARDWARE_MODE,
            'online': self.sync_worker.is_online(),
            'current_state': self.state_machine.current_state.value,
            'current_user': self.current_user_name,
            'db_stats': self.local_db.get_stats()
        }


if __name__ == "__main__":
    cabinet = SmartCabinet()

    # Optional: Print stats on startup
    stats = cabinet.get_stats()
    logger.info(f"System stats: {json.dumps(stats, indent=2, default=str)}")

    cabinet.run()
