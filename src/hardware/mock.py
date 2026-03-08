"""Mock hardware implementation for development and testing."""

import logging
import sys
import select
import threading
import time
from typing import List, Optional, Dict, Any

from .base import HardwareInterface, DrawerState, LEDColor

logger = logging.getLogger(__name__)


class MockHardware(HardwareInterface):
    """
    Mock hardware implementation for development without physical components.

    Simulates all hardware interactions via console input/output.
    Useful for:
    - Development without Raspberry Pi
    - Unit testing
    - CI/CD pipelines
    - Hardware troubleshooting
    """

    def __init__(self, num_drawers: int = 4, num_leds: int = 8):
        """
        Initialize mock hardware.

        Args:
            num_drawers: Number of drawers to simulate
            num_leds: Number of LEDs to simulate
        """
        self.num_drawers = num_drawers
        self.num_leds = num_leds

        # Internal state
        self._drawer_states = {i: DrawerState.CLOSED for i in range(num_drawers)}
        self._led_states = {i: (LEDColor.OFF, 1.0) for i in range(num_leds)}
        self._initialized = False

        # Simulated RFID tags (for testing)
        self._mock_tags = [
            "RFID-001",
            "RFID-002",
            "RFID-003",
            "RFID-004",
            "RFID-005",
        ]

        # Current "scanned" tags (subset of mock_tags)
        self._current_tags: List[str] = []

        logger.info(f"MockHardware initialized: {num_drawers} drawers, {num_leds} LEDs")

    def initialize(self) -> None:
        """Initialize mock hardware (logs only)."""
        self._initialized = True
        logger.info("Mock hardware initialized (no-op)")
        print("\n" + "=" * 50)
        print(" MOCK HARDWARE MODE ")
        print("=" * 50)
        print(f" Simulating {self.num_drawers} drawers, {self.num_leds} LEDs")
        print(" Use console commands to simulate hardware events")
        print("=" * 50 + "\n")

    def read_nfc(self, timeout: float = 30.0) -> Optional[str]:
        """
        Read NFC card UID from console input.

        Prompts user to enter a card UID or select from predefined cards.
        """
        print("\n[MOCK NFC] Waiting for card scan...")
        print("Options:")
        print("  1. Enter card UID manually")
        print("  2. Use test card: 'CARD-001'")
        print("  3. Use test card: 'CARD-002'")
        print("  4. Press Enter to simulate timeout")
        print(f"[Timeout: {timeout}s]")

        # Use select for timeout on stdin
        if sys.stdin in select.select([sys.stdin], [], [], timeout)[0]:
            choice = sys.stdin.readline().strip()

            if choice == "1":
                print("Enter card UID:")
                uid = sys.stdin.readline().strip()
                if uid:
                    print(f"[MOCK NFC] Card scanned: {uid}")
                    self.beep_success()
                    return uid
            elif choice == "2":
                print("[MOCK NFC] Test card scanned: CARD-001")
                self.beep_success()
                return "CARD-001"
            elif choice == "3":
                print("[MOCK NFC] Test card scanned: CARD-002")
                self.beep_success()
                return "CARD-002"
            else:
                print("[MOCK NFC] Timeout (Enter pressed)")
                return None
        else:
            print("[MOCK NFC] Timeout (no input)")
            return None

    def read_qr(self, timeout: float = 30.0) -> Optional[str]:
        """Read QR code from console input."""
        print("\n[MOCK QR] Waiting for QR scan...")
        print("Enter QR content (or press Enter to timeout):")

        if sys.stdin in select.select([sys.stdin], [], [], timeout)[0]:
            content = sys.stdin.readline().strip()
            if content:
                print(f"[MOCK QR] Scanned: {content}")
                self.beep_success()
                return content
        print("[MOCK QR] Timeout")
        return None

    def read_rfid_tags(self, drawer_id: Optional[int] = None) -> List[str]:
        """
        Simulate RFID tag reading.

        Prompts user to select which tags are present.
        """
        print("\n[MOCK RFID] Scanning tags...")

        if drawer_id is not None:
            print(f"Scanning drawer {drawer_id}")
        else:
            print("Scanning all drawers")

        print("\nCurrent mock tags:")
        for i, tag in enumerate(self._mock_tags, 1):
            status = "[PRESENT]" if tag in self._current_tags else "[ABSENT]"
            print(f"  {i}. {tag} {status}")

        print("\nCommands:")
        print("  a - Add all tags")
        print("  c - Clear all tags")
        print("  r - Random selection")
        print("  number(s) - Toggle specific tag(s), e.g., '1 3'")
        print("  Enter - Keep current selection")

        if sys.stdin in select.select([sys.stdin], [], [], 5)[0]:
            cmd = sys.stdin.readline().strip()

            if cmd.lower() == 'a':
                self._current_tags = self._mock_tags.copy()
            elif cmd.lower() == 'c':
                self._current_tags = []
            elif cmd.lower() == 'r':
                import random
                self._current_tags = random.sample(
                    self._mock_tags,
                    random.randint(0, len(self._mock_tags))
                )
            elif cmd:
                # Toggle specific tags
                for num in cmd.split():
                    try:
                        idx = int(num) - 1
                        if 0 <= idx < len(self._mock_tags):
                            tag = self._mock_tags[idx]
                            if tag in self._current_tags:
                                self._current_tags.remove(tag)
                            else:
                                self._current_tags.append(tag)
                    except ValueError:
                        pass

        print(f"\n[MOCK RFID] Detected {len(self._current_tags)} tags: {self._current_tags}")
        return self._current_tags.copy()

    def unlock_drawer(self, drawer_id: int) -> bool:
        """Simulate unlocking a drawer."""
        if 0 <= drawer_id < self.num_drawers:
            self._drawer_states[drawer_id] = DrawerState.OPEN
            print(f"[MOCK] Drawer {drawer_id} UNLOCKED")
            return True
        logger.error(f"Invalid drawer ID: {drawer_id}")
        return False

    def lock_drawer(self, drawer_id: int) -> bool:
        """Simulate locking a drawer."""
        if 0 <= drawer_id < self.num_drawers:
            self._drawer_states[drawer_id] = DrawerState.CLOSED
            print(f"[MOCK] Drawer {drawer_id} LOCKED")
            return True
        logger.error(f"Invalid drawer ID: {drawer_id}")
        return False

    def unlock_all(self) -> bool:
        """Simulate unlocking all drawers."""
        print(f"[MOCK] Unlocking all {self.num_drawers} drawers")
        for i in range(self.num_drawers):
            self._drawer_states[i] = DrawerState.OPEN
        self.led_pattern("pulse", LEDColor.GREEN, 2.0)
        return True

    def lock_all(self) -> bool:
        """Simulate locking all drawers."""
        print(f"[MOCK] Locking all {self.num_drawers} drawers")
        for i in range(self.num_drawers):
            self._drawer_states[i] = DrawerState.CLOSED
        self.led_pattern("solid", LEDColor.RED, 1.0)
        return True

    def get_drawer_state(self, drawer_id: int) -> DrawerState:
        """Get simulated drawer state."""
        if 0 <= drawer_id < self.num_drawers:
            return self._drawer_states[drawer_id]
        return DrawerState.UNKNOWN

    def get_all_drawer_states(self) -> Dict[int, DrawerState]:
        """Get all drawer states."""
        return self._drawer_states.copy()

    def are_all_drawers_closed(self) -> bool:
        """Check if all drawers are closed."""
        closed = all(
            state == DrawerState.CLOSED
            for state in self._drawer_states.values()
        )
        print(f"[MOCK] All drawers closed: {closed}")
        return closed

    def set_led(self, index: int, color: LEDColor, brightness: float = 1.0) -> None:
        """Simulate setting LED color."""
        if 0 <= index < self.num_leds:
            self._led_states[index] = (color, brightness)
            print(f"[MOCK LED {index}] {color.value} (brightness: {brightness:.0%})")

    def set_all_leds(self, color: LEDColor, brightness: float = 1.0) -> None:
        """Simulate setting all LEDs."""
        for i in range(self.num_leds):
            self._led_states[i] = (color, brightness)
        print(f"[MOCK LEDs] All set to {color.value} (brightness: {brightness:.0%})")

    def led_pattern(self, pattern: str, color: LEDColor, duration: float = 1.0) -> None:
        """Simulate LED pattern."""
        print(f"[MOCK LEDs] Pattern: {pattern}, Color: {color.value}, Duration: {duration}s")

    def beep(self, duration: float = 0.1, frequency: Optional[int] = None) -> None:
        """Simulate beep."""
        freq_str = f" {frequency}Hz" if frequency else ""
        print(f"[MOCK BEEP]{freq_str} ({duration}s)")

    def beep_success(self) -> None:
        """Simulate success beep."""
        print("[MOCK BEEP] Success: ♪")
        self.beep(0.1, 2000)

    def beep_error(self) -> None:
        """Simulate error beep."""
        print("[MOCK BEEP] Error: ♪♪")
        self.beep(0.2, 500)
        time.sleep(0.1)
        self.beep(0.2, 500)

    def beep_warning(self) -> None:
        """Simulate warning beep."""
        print("[MOCK BEEP] Warning: ♪~")
        self.beep(0.3, 1000)

    def cleanup(self) -> None:
        """Cleanup mock hardware."""
        self._initialized = False
        print("\n[MOCK] Hardware cleanup complete")

    def health_check(self) -> Dict[str, Any]:
        """Return mock health status."""
        return {
            "status": "healthy",
            "mode": "mock",
            "drawers": self.num_drawers,
            "leds": self.num_leds,
            "nfc": "simulated",
            "rfid": "simulated",
            "servo": "simulated",
        }

    def simulate_drawer_open(self, drawer_id: int) -> None:
        """Helper method to simulate drawer being opened externally."""
        if 0 <= drawer_id < self.num_drawers:
            self._drawer_states[drawer_id] = DrawerState.OPEN
            print(f"[MOCK] Drawer {drawer_id} opened (external)")

    def simulate_drawer_close(self, drawer_id: int) -> None:
        """Helper method to simulate drawer being closed externally."""
        if 0 <= drawer_id < self.num_drawers:
            self._drawer_states[drawer_id] = DrawerState.CLOSED
            print(f"[MOCK] Drawer {drawer_id} closed (external)")
