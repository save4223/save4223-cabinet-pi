#!/usr/bin/env python3
"""
Test API Server for Smart Cabinet Pi
Provides HTTP endpoints for test panel to interact with Local DB
"""

import json
import sys
import uuid
import logging
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from local_db import LocalDB
from api_client import APIClient
from config import CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Local DB
DB_PATH = "/tmp/cabinet_test.db"
db = LocalDB(DB_PATH)

# Current state (in-memory for simulation)
state = {
    "current_state": "LOCKED",
    "current_session": None,
    "current_user": None,
    "last_scan": None
}

# Mock item data for initial cache
MOCK_ITEMS = [
    {"rfid_tag": "RFID-OSC-001", "item_id": "item-1", "name": "Oscilloscope #1", "status": "BORROWED", "holder_id": "550e8400-e29b-41d4-a716-446655440000"},
    {"rfid_tag": "RFID-OSC-002", "item_id": "item-2", "name": "Oscilloscope #2", "status": "AVAILABLE", "holder_id": None},
    {"rfid_tag": "RFID-OSC-003", "item_id": "item-3", "name": "Oscilloscope #3", "status": "AVAILABLE", "holder_id": None},
    {"rfid_tag": "RFID-TOOL-001", "item_id": "item-4", "name": "Screwdriver Set", "status": "AVAILABLE", "holder_id": None},
    {"rfid_tag": "RFID-MUL-001", "item_id": "item-5", "name": "Multimeter #1", "status": "BORROWED", "holder_id": "550e8400-e29b-41d4-a716-446655440000"},
    {"rfid_tag": "RFID-MUL-002", "item_id": "item-6", "name": "Multimeter #2", "status": "AVAILABLE", "holder_id": None},
]

# Initialize cache with full data (including item_id and name)
for item in MOCK_ITEMS:
    db._conn.execute('''
        INSERT OR REPLACE INTO item_cache (rfid_tag, item_id, name, status, holder_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (item["rfid_tag"], item["item_id"], item["name"], item["status"], item["holder_id"], datetime.now()))
db._conn.commit()

# Create initial baseline snapshot for diff calculation
initial_session = "initial-baseline"
initial_tags = [item["rfid_tag"] for item in MOCK_ITEMS]
for tag in initial_tags:
    db._conn.execute('''
        INSERT INTO rfid_snapshots (session_id, cabinet_id, rfid_tag, present, captured_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (initial_session, 1, tag, True, datetime.now()))
db._conn.commit()
logger.info(f"✅ Created initial baseline snapshot with {len(initial_tags)} items")

# Mock cards
MOCK_CARDS = {
    "TEST123": {"user_id": "550e8400-e29b-41d4-a716-446655440000", "user_name": "Test User", "email": "test@example.com"},
    "CARD456": {"user_id": "b6961f07-e43a-4304-ab75-dc51c0b58ce5", "user_name": "Vicky", "email": "vicky@example.com"}
}


class TestAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for test API."""
    
    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")
    
    def _set_headers(self, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_OPTIONS(self):
        self._set_headers()
    
    def _read_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length:
            return json.loads(self.rfile.read(content_length))
        return {}
    
    def do_GET(self):
        """Handle GET requests."""
        # Serve static files (test-panel.html)
        if self.path == '/' or self.path == '/test-panel.html':
            try:
                display_dir = Path(__file__).parent.parent / "display"
                html_file = display_dir / "test-panel.html"
                
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                
                with open(html_file, 'rb') as f:
                    self.wfile.write(f.read())
                return
            except Exception as e:
                self._set_headers(500)
                self.wfile.write(json.dumps({"error": f"Failed to load HTML: {str(e)}"}).encode())
                return
        
        self._set_headers()
        
        if self.path == '/api/state':
            # Get current system state
            response = {
                "current_state": state["current_state"],
                "current_session": state["current_session"],
                "current_user": state["current_user"]
            }
            self.wfile.write(json.dumps(response).encode())
        
        elif self.path == '/api/db/item-cache':
            # Get all cached items
            items = []
            # Query directly from SQLite
            rows = db._conn.execute('SELECT * FROM item_cache').fetchall()
            for row in rows:
                items.append({
                    "rfid_tag": row['rfid_tag'],
                    "item_id": row['item_id'],
                    "name": row['name'],
                    "status": row['status'],
                    "holder_id": row['holder_id']
                })
            self.wfile.write(json.dumps(items).encode())
        
        elif self.path == '/api/db/session-diffs':
            # Get all session diffs
            diffs = []
            rows = db._conn.execute('SELECT * FROM session_diffs ORDER BY created_at DESC').fetchall()
            for row in rows:
                diffs.append({
                    "session_id": row['session_id'],
                    "user_id": row['user_id'],
                    "borrowed": json.loads(row['borrowed']),
                    "returned": json.loads(row['returned']),
                    "created_at": row['created_at'],
                    "synced": row['synced']
                })
            self.wfile.write(json.dumps(diffs).encode())
        
        elif self.path == '/api/db/pending-sync':
            # Get pending sync queue
            pending = db.get_pending_sync(limit=100)
            self.wfile.write(json.dumps(pending).encode())
        
        elif self.path == '/api/db/snapshots':
            # Get RFID snapshots
            snapshots = []
            rows = db._conn.execute('''
                SELECT session_id, COUNT(*) as tag_count, captured_at 
                FROM rfid_snapshots 
                GROUP BY session_id 
                ORDER BY captured_at DESC
                LIMIT 10
            ''').fetchall()
            for row in rows:
                snapshots.append({
                    "session_id": row['session_id'],
                    "tag_count": row['tag_count'],
                    "captured_at": row['captured_at']
                })
            self.wfile.write(json.dumps(snapshots).encode())
        
        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
    
    def do_POST(self):
        """Handle POST requests."""
        body = self._read_body()
        
        if self.path == '/api/simulate/auth':
            # Simulate card authentication
            card_uid = body.get('card_uid', '')
            
            if card_uid in MOCK_CARDS:
                user = MOCK_CARDS[card_uid]
                state["current_session"] = str(uuid.uuid4())
                state["current_user"] = user
                state["current_state"] = "AUTHENTICATING"
                
                # Cache auth in local DB
                db.cache_auth(card_uid, {
                    "user_id": user["user_id"],
                    "user_name": user["user_name"],
                    "cabinet_id": 1
                })
                
                self._set_headers()
                response = {
                    "success": True,
                    "authorized": True,
                    "session_id": state["current_session"],
                    "user": user
                }
                logger.info(f"✅ Auth success: {user['user_name']} ({card_uid})")
            else:
                self._set_headers(403)
                response = {
                    "success": False,
                    "authorized": False,
                    "reason": "Card not registered"
                }
                logger.warning(f"❌ Auth failed: {card_uid}")
            
            self.wfile.write(json.dumps(response).encode())
        
        elif self.path == '/api/simulate/unlock':
            # Simulate unlock
            state["current_state"] = "UNLOCKED"
            self._set_headers()
            self.wfile.write(json.dumps({"success": True, "state": "UNLOCKED"}).encode())
            logger.info("🔓 Cabinet unlocked")
        
        elif self.path == '/api/simulate/lock':
            # Simulate lock - but KEEP session for potential scan
            # In real cabinet, lock happens after scan, but for testing
            # we allow lock -> scan flow where scan uses existing session
            state["current_state"] = "LOCKED"
            # Don't clear session/user here - scan needs them
            # They will be cleared when new auth happens
            self._set_headers()
            self.wfile.write(json.dumps({"success": True, "state": "LOCKED"}).encode())
            logger.info("🔒 Cabinet locked (session preserved for scan)")
        
        elif self.path == '/api/simulate/scan':
            # Simulate RFID scan
            user_id = state["current_user"]["user_id"] if state["current_user"] else None
            session_id = state["current_session"]
            
            if not session_id:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "No active session. Please authenticate first."}).encode())
                return
            
            # Get detected tags from request or use default scenario
            # Scenario: User takes RFID-OSC-002 (borrow) and returns RFID-MUL-001 (return)
            # So OSC-002 is GONE, MUL-001 is PRESENT
            detected_tags = body.get('rfids', [
                "RFID-OSC-001",  # Still here
                # "RFID-OSC-002",  # BORROWED - not present
                "RFID-OSC-003",  # Still here
                "RFID-TOOL-001", # Still here
                "RFID-MUL-001",  # RETURNED - now present!
                "RFID-MUL-002",  # Still here
            ])
            
            # Save snapshot
            db.save_rfid_snapshot(session_id, 1, detected_tags)
            
            # Calculate diff
            borrowed, returned = db.calculate_diff(detected_tags, 1, user_id)
            
            # Save diff
            db.save_session_diff(session_id, user_id, borrowed, returned)
            
            # Update state
            state["current_state"] = "SCANNING"
            state["last_scan"] = {
                "session_id": session_id,
                "tags": detected_tags,
                "borrowed": borrowed,
                "returned": returned
            }
            
            self._set_headers()
            response = {
                "success": True,
                "session_id": session_id,
                "detected_tags": detected_tags,
                "borrowed": borrowed,
                "returned": returned
            }
            self.wfile.write(json.dumps(response).encode())
            logger.info(f"📡 Scan complete: {len(borrowed)} borrowed, {len(returned)} returned")
        
        elif self.path == '/api/simulate/sync':
            # Simulate sync with server
            session_id = body.get('session_id') or state["current_session"]
            
            if not session_id:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "No session to sync"}).encode())
                return
            
            # Mark as synced
            db.mark_diff_synced(session_id)
            
            # Remove from pending if exists
            db._conn.execute('DELETE FROM pending_sync WHERE session_id = ?', (session_id,))
            db._conn.commit()
            
            self._set_headers()
            self.wfile.write(json.dumps({"success": True, "synced": True}).encode())
            logger.info(f"✅ Session {session_id[:8]} synced")
        
        elif self.path == '/api/simulate/queue-sync':
            # Queue session for sync
            session_id = body.get('session_id') or state["current_session"]
            user_id = state["current_user"]["user_id"] if state["current_user"] else "unknown"
            rfids = body.get('rfids', [])
            
            added = db.queue_sync_session(session_id, user_id, rfids)
            
            self._set_headers()
            self.wfile.write(json.dumps({
                "success": True,
                "queued": added,
                "session_id": session_id
            }).encode())
        
        elif self.path == '/api/db/clear':
            # Clear all test data
            db._conn.execute('DELETE FROM session_diffs')
            db._conn.execute('DELETE FROM pending_sync')
            db._conn.execute('DELETE FROM rfid_snapshots')
            db._conn.execute('DELETE FROM access_logs')
            db._conn.commit()
            
            # Reset item cache
            for item in MOCK_ITEMS:
                db.update_item_state(item["rfid_tag"], item["status"], item["holder_id"])
            
            state["current_state"] = "LOCKED"
            state["current_session"] = None
            state["current_user"] = None
            
            self._set_headers()
            self.wfile.write(json.dumps({"success": True, "message": "Database cleared"}).encode())
            logger.info("🗑 Database cleared")
        
        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode())


def run_test_server(port=8765, host='0.0.0.0'):
    """Run the test API server."""
    server = HTTPServer((host, port), TestAPIHandler)
    logger.info(f"🚀 Test API Server running on http://{host}:{port}")
    logger.info(f"")
    logger.info(f"Endpoints:")
    logger.info(f"  GET  /api/state              - Get current system state")
    logger.info(f"  GET  /api/db/item-cache      - Get item cache from Local DB")
    logger.info(f"  GET  /api/db/session-diffs   - Get session diffs")
    logger.info(f"  GET  /api/db/pending-sync    - Get pending sync queue")
    logger.info(f"  GET  /api/db/snapshots       - Get RFID snapshots")
    logger.info(f"")
    logger.info(f"  POST /api/simulate/auth      - Simulate card auth")
    logger.info(f"  POST /api/simulate/unlock    - Simulate unlock")
    logger.info(f"  POST /api/simulate/lock      - Simulate lock")
    logger.info(f"  POST /api/simulate/scan      - Simulate RFID scan")
    logger.info(f"  POST /api/simulate/sync      - Simulate sync")
    logger.info(f"  POST /api/db/clear           - Clear all test data")
    logger.info(f"")
    logger.info(f"Test panel: http://localhost:{port}/test-panel.html")
    logger.info(f"Press Ctrl+C to stop")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("\n👋 Shutting down")
        server.shutdown()
        db.close()


if __name__ == "__main__":
    run_test_server()
