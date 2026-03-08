"""Abstract hardware interface for Smart Cabinet Pi."""

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from enum import Enum


class DrawerState(Enum):
    """Drawer state enumeration."""
    CLOSED = "closed"
    OPEN = "open"
    UNKNOWN = "unknown"


class LEDColor(Enum):
    """LED color enumeration."""
    OFF = "off"
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"
    WHITE = "white"


class HardwareInterface(ABC):
    """
    Abstract hardware interface for Smart Cabinet Pi.

    This interface abstracts all hardware interactions, allowing for:
    - Mock implementations for development and testing
    - Real implementations for production deployment
    - Easy unit testing without physical hardware
    """

    @abstractmethod
    def initialize(self) -> None:
        """Initialize hardware components."""
        pass

    @abstractmethod
    def read_nfc(self, timeout: float = 30.0) -> Optional[str]:
        """
        Read NFC card UID.

        Args:
            timeout: Maximum time to wait for card read in seconds

        Returns:
            Card UID string, or None if timeout
        """
        pass

    @abstractmethod
    def read_qr(self, timeout: float = 30.0) -> Optional[str]:
        """
        Read QR code.

        Args:
            timeout: Maximum time to wait for QR scan in seconds

        Returns:
            QR code content string, or None if timeout
        """
        pass

    @abstractmethod
    def read_rfid_tags(self, drawer_id: Optional[int] = None) -> List[str]:
        """
        Read RFID tags from all drawers or a specific drawer.

        Args:
            drawer_id: Specific drawer to scan, or None for all drawers

        Returns:
            List of RFID tag UIDs
        """
        pass

    @abstractmethod
    def unlock_drawer(self, drawer_id: int) -> bool:
        """
        Unlock a specific drawer.

        Args:
            drawer_id: Drawer identifier (0-indexed)

        Returns:
            True if unlock command succeeded
        """
        pass

    @abstractmethod
    def lock_drawer(self, drawer_id: int) -> bool:
        """
        Lock a specific drawer.

        Args:
            drawer_id: Drawer identifier (0-indexed)

        Returns:
            True if lock command succeeded
        """
        pass

    @abstractmethod
    def unlock_all(self) -> bool:
        """Unlock all drawers."""
        pass

    @abstractmethod
    def lock_all(self) -> bool:
        """Lock all drawers."""
        pass

    @abstractmethod
    def get_drawer_state(self, drawer_id: int) -> DrawerState:
        """
        Get the current state of a drawer.

        Args:
            drawer_id: Drawer identifier (0-indexed)

        Returns:
            Current drawer state
        """
        pass

    @abstractmethod
    def get_all_drawer_states(self) -> Dict[int, DrawerState]:
        """
        Get states of all drawers.

        Returns:
            Dictionary mapping drawer_id to state
        """
        pass

    @abstractmethod
    def are_all_drawers_closed(self) -> bool:
        """
        Check if all drawers are closed.

        Returns:
            True if all drawers are confirmed closed
        """
        pass

    @abstractmethod
    def set_led(self, index: int, color: LEDColor, brightness: float = 1.0) -> None:
        """
        Set LED color.

        Args:
            index: LED index
            color: LED color
            brightness: Brightness level (0.0 to 1.0)
        """
        pass

    @abstractmethod
    def set_all_leds(self, color: LEDColor, brightness: float = 1.0) -> None:
        """
        Set all LEDs to the same color.

        Args:
            color: LED color
            brightness: Brightness level (0.0 to 1.0)
        """
        pass

    @abstractmethod
    def led_pattern(self, pattern: str, color: LEDColor, duration: float = 1.0) -> None:
        """
        Run an LED pattern.

        Args:
            pattern: Pattern name ('blink', 'pulse', 'chase', 'solid')
            color: LED color
            duration: Pattern duration in seconds
        """
        pass

    @abstractmethod
    def beep(self, duration: float = 0.1, frequency: Optional[int] = None) -> None:
        """
        Play a beep sound.

        Args:
            duration: Beep duration in seconds
            frequency: Frequency in Hz (None for default)
        """
        pass

    @abstractmethod
    def beep_success(self) -> None:
        """Play success beep pattern (short high beep)."""
        pass

    @abstractmethod
    def beep_error(self) -> None:
        """Play error beep pattern (two short low beeps)."""
        pass

    @abstractmethod
    def beep_warning(self) -> None:
        """Play warning beep pattern (medium beep)."""
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Cleanup resources (GPIO, SPI, etc.). Call on shutdown."""
        pass

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        """
        Check hardware health status.

        Returns:
            Dictionary with component status
        """
        pass

    def __enter__(self):
        """Context manager entry."""
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.cleanup()
        return False
