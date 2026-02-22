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
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from state_machine import StateMachine, SystemState
from hardware.controller import HardwareController
from api_client import APIClient, APIError
from local_db import LocalDB
from sync_worker import SyncWorker
from config import CONFIG

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
    """Main cabinet controller class."""
    
    def __init__(self):
        self.running = False
        self.current_user_id = None
        self.current_card_uid = None
        self.session_id = None
        
        # Initialize components
        logger.info("Initializing Smart Cabinet...")
        self.state_machine = StateMachine()
        self.hardware = HardwareController()
        self.api = APIClient(CONFIG['server_url'], CONFIG['edge_secret'])
        self.local_db = LocalDB(CONFIG['db_path'])
        self.sync_worker = SyncWorker(self.local_db, self.api)
        
        # Setup handlers
        self._setup_signal_handlers()
        self._setup_state_handlers()
        
        # Start sync worker
        self.sync_worker.start()
        logger.info("Smart Cabinet initialized")
    
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
        """Send update to local dashboard (placeholder for WebSocket)."""
        # TODO: Implement WebSocket to Electron display
        logger.info(f"[DISPLAY] {message['type']}: {message}")
    
    def _on_locked(self):
        """Handle LOCKED state entry."""
        logger.info("State: LOCKED")
        self.hardware.set_all_leds('red')
        self.hardware.lock_all()
        self.current_user_id = None
        self.current_card_uid = None
        self.session_id = None
        
        self._send_to_display({
            'type': 'STATE_CHANGE',
            'state': 'LOCKED'
        })
    
    def _on_authenticating(self):
        """Handle AUTHENTICATING state entry."""
        logger.info("State: AUTHENTICATING")
        self.hardware.set_all_leds('yellow')
        
        self._send_to_display({
            'type': 'STATE_CHANGE',
            'state': 'AUTHENTICATING'
        })
        
        # Read NFC/QR
        card_uid = self.hardware.read_nfc_or_qr(timeout=30)
        
        if not card_uid:
            logger.warning("Authentication timeout")
            self.state_machine.transition(SystemState.LOCKED)
            return
        
        # Validate with API (with local cache fallback)
        result = self._authenticate(card_uid)
        
        if result['authorized']:
            self.current_card_uid = card_uid
            self.current_user_id = result['user_id']
            self.session_id = str(uuid.uuid4())  # Generate session ID now
            logger.info(f"Authenticated: {result['user_name']} ({self.current_user_id})")
            
            self._send_to_display({
                'type': 'AUTH_SUCCESS',
                'user': {
                    'id': self.current_user_id,
                    'email': result.get('email', ''),
                    'full_name': result.get('user_name', '')
                }
            })
            
            self.state_machine.transition(SystemState.UNLOCKED)
        else:
            logger.warning(f"Authentication failed: {result.get('reason')}")
            self.hardware.set_all_leds('red')
            self.hardware.beep_error()
            
            self._send_to_display({
                'type': 'AUTH_FAILURE',
                'error': result.get('reason', 'Access denied')
            })
            
            time.sleep(2)
            self.state_machine.transition(SystemState.LOCKED)
    
    def _authenticate(self, card_uid: str) -> dict:
        """Authenticate card with API or local cache."""
        # Try API first
        if self.sync_worker.is_online():
            try:
                result = self.api.authorize(card_uid, CONFIG['cabinet_id'])
                # Cache successful auth
                self.local_db.cache_auth(card_uid, result)
                return result
            except Exception as e:
                logger.warning(f"API auth failed: {e}, falling back to cache")
        
        # Fallback to local cache
        cached = self.local_db.get_cached_auth(card_uid)
        if cached:
            logger.info("Using cached authentication")
            return cached
        
        return {'authorized': False, 'reason': 'Offline and no cache'}
    
    def _on_unlocked(self):
        """Handle UNLOCKED state entry."""
        logger.info("State: UNLOCKED")
        self.hardware.set_all_leds('green')
        self.hardware.unlock_all()
        self.hardware.beep_success()
        
        unlock_time = time.time()
        
        self._send_to_display({
            'type': 'STATE_CHANGE',
            'state': 'UNLOCKED',
            'session_id': self.session_id
        })
        
        # Wait for user to close cabinet
        while self.running and (time.time() - unlock_time) < CONFIG['session_timeout']:
            # Check if same card scanned (close command)
            card = self.hardware.read_nfc_or_qr(timeout=0.5)
            
            if card == self.current_card_uid:
                if self.hardware.are_all_drawers_closed():
                    logger.info("Close command received, all drawers closed")
                    self.state_machine.transition(SystemState.SCANNING)
                    return
                else:
                    logger.info("Please close all drawers first")
                    self.hardware.beep_warning()
            
            # Update LED per drawer
            for i in range(4):
                if self.hardware.is_drawer_open(i):
                    self.hardware.set_led(i, 'red')
                else:
                    self.hardware.set_led(i, 'green')
            
            time.sleep(0.1)
        
        # Timeout
        logger.warning("Session timeout")
        self.hardware.beep_error()
        self.state_machine.transition(SystemState.SCANNING)
    
    def _on_scanning(self):
        """Handle SCANNING state entry."""
        logger.info("State: SCANNING")
        self.hardware.set_all_leds('yellow')
        self.hardware.lock_all()
        
        self._send_to_display({
            'type': 'STATE_CHANGE',
            'state': 'SCANNING'
        })
        
        # Perform RFID scan
        tags = self._scan_rfid()
        logger.info(f"RFID scan complete: {len(tags)} tags found")
        
        # Save snapshot for this session
        self.local_db.save_rfid_snapshot(
            self.session_id, CONFIG['cabinet_id'], tags
        )
        
        # Calculate diff locally (immediate feedback)
        borrowed, returned = self.local_db.calculate_diff(
            tags, CONFIG['cabinet_id'], self.current_user_id
        )
        
        logger.info(f"Local diff: {len(borrowed)} borrowed, {len(returned)} returned")
        
        # Save diff for display
        self.local_db.save_session_diff(
            self.session_id, self.current_user_id, borrowed, returned
        )
        
        # IMMEDIATE feedback to user (before sync)
        self._send_to_display({
            'type': 'ITEM_SUMMARY',
            'itemSummary': {
                'borrowed': borrowed,
                'returned': returned
            }
        })
        
        # Try to sync with server (best effort)
        sync_success = self._try_sync_session(tags, borrowed, returned)
        
        if not sync_success:
            # Queue for later if sync failed
            self.local_db.queue_sync_session(
                self.session_id, self.current_user_id, tags
            )
            logger.info(f"Session {self.session_id[:8]} queued for later sync")
        
        # Log access
        self.local_db.log_access(
            card_uid=self.current_card_uid,
            user_id=self.current_user_id,
            session_id=self.session_id,
            tags_found=tags
        )
        
        # Wait a moment for user to see summary, then return to locked
        time.sleep(10)  # Show summary for 10 seconds
        self.state_machine.transition(SystemState.LOCKED)
    
    def _scan_rfid(self) -> list:
        """Perform multiple RFID scans and return unique tags."""
        all_tags = set()
        
        for i in range(CONFIG['rfid_scan_count']):
            tags = self.hardware.read_rfid_tags()
            all_tags.update(tags)
            logger.debug(f"Scan {i+1}: {len(tags)} tags")
            time.sleep(0.1)
        
        return sorted(list(all_tags))
    
    def _try_sync_session(self, tags: list, borrowed: list, returned: list) -> bool:
        """
        Try to sync session with server.
        Returns True if successful, False if should queue for later.
        """
        if not self.sync_worker.is_online():
            logger.warning("Offline, will retry later")
            return False
        
        try:
            result = self.api.sync_session(
                session_id=self.session_id,
                cabinet_id=CONFIG['cabinet_id'],
                user_id=self.current_user_id,
                rfids=tags
            )
            
            # Validate server result matches local calculation
            server_borrowed = len(result.get('borrowed', []))
            server_returned = len(result.get('returned', []))
            local_borrowed = len(borrowed)
            local_returned = len(returned)
            
            if server_borrowed == local_borrowed and server_returned == local_returned:
                logger.info(f"Sync confirmed: {server_borrowed} borrowed, {server_returned} returned")
            else:
                logger.warning(f"Sync mismatch - Server: {server_borrowed}/{server_returned}, Local: {local_borrowed}/{local_returned}")
            
            # Mark as synced
            self.local_db.mark_diff_synced(self.session_id)
            return True
            
        except APIError as e:
            logger.error(f"Sync failed: {e}")
            return False
        except Exception as e:
            logger.exception(f"Unexpected sync error: {e}")
            return False
    
    def run(self):
        """Main loop."""
        logger.info("Starting Smart Cabinet main loop")
        self.running = True
        
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
        """Poll for NFC/QR in LOCKED state."""
        card = self.hardware.read_nfc_or_qr(timeout=0.5)
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


if __name__ == "__main__":
    cabinet = SmartCabinet()
    cabinet.run()
