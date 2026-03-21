#!/usr/bin/env python3
"""Raspberry Pi hardware implementation for Smart Cabinet.

Real hardware control for production deployment.
Based on legacy implementation, adapted for new architecture.
"""

import time
import logging
import threading
import socket
import glob
from typing import List, Optional, Dict, Any
from pathlib import Path

try:
    import RPi.GPIO as GPIO
    from rpi_ws281x import PixelStrip, Color
    import serial
    RPI_AVAILABLE = True
except ImportError as e:
    RPI_AVAILABLE = False
    logging.warning(f"RPi libraries not available ({e}) - running in simulation mode")

from .base import HardwareInterface, DrawerState, LEDColor

logger = logging.getLogger(__name__)


# Configuration constants
SOLENOID_PINS = [27, 22, 10, 9]  # Lock A -> D
DRAWER_SWITCH_PINS = [26, 19, 13, 6]  # Switches 1 -> 4

# WS2812B configuration
LED_COUNT = 8        # Number of LED pixels
LED_PIN = 18         # GPIO pin connected to the pixels (18 uses PWM!)
LED_FREQ_HZ = 800000 # LED signal frequency in hertz (usually 800khz)
LED_DMA = 10         # DMA channel to use for generating signal (try 10)
LED_BRIGHTNESS = 255 # Set to 0 for darkest and 255 for brightest
LED_INVERT = False   # True to invert the signal (when using NPN transistor level shift)
LED_CHANNEL = 0      # set to '1' for GPIOs 13, 19, 41, 45 or 53

# RFID configuration
RFID_HOST = '192.168.0.178'
RFID_PORT = 4001
RFID_READ_CYCLES = 5
RFID_READ_INTERVAL = 0.5
RFID_ADDRESS = 0xFF
IGNORED_TAGS = {"00B07A15306008EFF68E8F54"}

# NFC/QR configuration
NFC_BAUD_RATE = 115200


class RaspberryPiHardware(HardwareInterface):
    """Raspberry Pi hardware implementation."""

    def __init__(self, num_drawers: int = 4, num_leds: int = 8, nfc_mode: str = "auto"):
        self.num_drawers = num_drawers
        self.num_leds = num_leds
        self._initialized = False
        self._drawer_states = {i: DrawerState.CLOSED for i in range(num_drawers)}
        self._nfc_reader = None
        self._rfid_reader = None
        self._hid_reader = None
        self._nfc_mode = nfc_mode
        self._strip = None

    def initialize(self) -> None:
        """Initialize hardware components."""
        if not RPI_AVAILABLE:
            logger.warning("RPi libraries not available - running in simulation mode")
            self._initialized = True
            return

        # Initialize GPIO
        GPIO.setmode(GPIO.BCM)

        # Setup solenoid pins
        for pin in SOLENOID_PINS:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW) # Ensure locked on start

        # Setup drawer switch pins (External Pull-up to 3.3V)
        for pin in DRAWER_SWITCH_PINS:
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_OFF)

        # Initialize WS2812B Strip
        try:
            self._strip = PixelStrip(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA, LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL)
            self._strip.begin()
            logger.info("WS2812B Strip initialized")
        except Exception as e:
            logger.error(f"Failed to initialize WS2812B strip: {e}")

        # Initialize NFC/QR reader
        self._init_nfc_reader()

        # Initialize RFID reader
        try:
            self._rfid_reader = RFIDReader()
        except Exception as e:
            logger.error(f"Failed to initialize RFID reader: {e}")

        self._initialized = True
        logger.info("Raspberry Pi hardware initialized (Solenoids + WS2812B)")

    def _init_nfc_reader(self):
        """Initialize NFC reader - tries serial first, then HID keyboard."""
        if self._nfc_mode == "none":
            logger.info("NFC reader disabled")
            return

        # Try serial reader first
        if self._nfc_mode in ("auto", "serial"):
            try:
                self._nfc_reader = NFCQRReader()
                if self._nfc_reader._test_connection():
                    logger.info("Serial NFC reader initialized")
                    return
            except Exception as e:
                logger.debug(f"Serial NFC reader not available: {e}")

        # Try HID keyboard reader
        if self._nfc_mode in ("auto", "hid"):
            try:
                from .hid_keyboard_reader import HIDKeyboardReader
                self._hid_reader = HIDKeyboardReader()
                if self._hid_reader.is_available():
                    logger.info("HID keyboard NFC reader initialized")
                    return
            except Exception as e:
                logger.debug(f"HID keyboard reader not available: {e}")

        logger.warning("No NFC reader available")

    def read_nfc(self, timeout: float = 30.0) -> Optional[str]:
        """Read NFC card UID."""
        if self._hid_reader and self._hid_reader.is_available():
            return self._hid_reader.read_card(timeout=timeout)

        if not self._nfc_reader:
            return None

        start_time = time.time()
        while time.time() - start_time < timeout:
            uid = self._nfc_reader.read_nfc_card()
            if uid:
                return uid
            time.sleep(0.1)

        return None

    def read_qr(self, timeout: float = 30.0) -> Optional[str]:
        """Read QR code."""
        if self._hid_reader and self._hid_reader.is_available():
            result = self._hid_reader.read_card(timeout=timeout)
            if result:
                return result

        if not self._nfc_reader:
            return None

        start_time = time.time()
        while time.time() - start_time < timeout:
            qr = self._nfc_reader.read_qr_code()
            if qr:
                return qr
            time.sleep(0.1)

        return None

    def read_rfid_tags(self, drawer_id: Optional[int] = None) -> List[str]:
        """Read RFID tags."""
        if not self._rfid_reader:
            return []

        return self._rfid_reader.read_rfid_tags_multiple()

    def unlock_drawer(self, drawer_id: int) -> bool:
        """Unlock a specific solenoid."""
        if drawer_id < 0 or drawer_id >= len(SOLENOID_PINS):
            return False

        try:
            pin = SOLENOID_PINS[drawer_id]
            GPIO.output(pin, GPIO.HIGH) # Energize solenoid
            self._drawer_states[drawer_id] = DrawerState.OPEN
            logger.info(f"Solenoid {drawer_id} (GPIO {pin}) energized (unlocked)")
            return True
        except Exception as e:
            logger.error(f"Failed to unlock drawer {drawer_id}: {e}")
            return False

    def lock_drawer(self, drawer_id: int) -> bool:
        """Lock a specific solenoid."""
        if drawer_id < 0 or drawer_id >= len(SOLENOID_PINS):
            return False

        try:
            pin = SOLENOID_PINS[drawer_id]
            GPIO.output(pin, GPIO.LOW) # De-energize solenoid
            self._drawer_states[drawer_id] = DrawerState.CLOSED
            logger.info(f"Solenoid {drawer_id} (GPIO {pin}) de-energized (locked)")
            return True
        except Exception as e:
            logger.error(f"Failed to lock drawer {drawer_id}: {e}")
            return False

    def unlock_all(self) -> bool:
        """Unlock all solenoids."""
        try:
            for i in range(self.num_drawers):
                self.unlock_drawer(i)
            return True
        except Exception as e:
            logger.error(f"Failed to unlock all drawers: {e}")
            return False

    def lock_all(self) -> bool:
        """Lock all solenoids."""
        try:
            for i in range(self.num_drawers):
                self.lock_drawer(i)
            return True
        except Exception as e:
            logger.error(f"Failed to lock all drawers: {e}")
            return False

    def get_drawer_state(self, drawer_id: int) -> DrawerState:
        """Get drawer state from switch."""
        if not RPI_AVAILABLE or drawer_id < 0 or drawer_id >= len(DRAWER_SWITCH_PINS):
            return self._drawer_states.get(drawer_id, DrawerState.UNKNOWN)

        try:
            pin = DRAWER_SWITCH_PINS[drawer_id]
            # HIGH means open (switch not pressed, pulled up externally)
            if GPIO.input(pin) == GPIO.HIGH:
                return DrawerState.OPEN
            else:
                return DrawerState.CLOSED
        except Exception as e:
            logger.error(f"Failed to read drawer {drawer_id} state: {e}")
            return DrawerState.UNKNOWN

    def are_all_drawers_closed(self) -> bool:
        """Check if all drawers are closed."""
        if not RPI_AVAILABLE:
            return all(s == DrawerState.CLOSED for s in self._drawer_states.values())

        try:
            for pin in DRAWER_SWITCH_PINS[:self.num_drawers]:
                if GPIO.input(pin) == GPIO.HIGH:
                    return False
            return True
        except Exception as e:
            logger.error(f"Failed to check drawer states: {e}")
            return False

    def set_led(self, index: int, color: LEDColor, brightness: float = 1.0) -> None:
        """Set WS2812B LED color."""
        if not self._strip or index < 0 or index >= LED_COUNT:
            return

        # Map LEDColor to rpi_ws281x Color
        ws_color = Color(0, 0, 0)
        if color == LEDColor.GREEN:
            ws_color = Color(0, 255, 0)
        elif color == LEDColor.RED:
            ws_color = Color(255, 0, 0)
        elif color == LEDColor.YELLOW:
            ws_color = Color(255, 255, 0)
        elif color == LEDColor.BLUE:
            ws_color = Color(0, 0, 255)

        try:
            self._strip.setPixelColor(index, ws_color)
            self._strip.show()
        except Exception as e:
            logger.error(f"Failed to set WS2812B LED {index}: {e}")

    def set_all_leds(self, color: LEDColor, brightness: float = 1.0) -> None:
        """Set all WS2812B LEDs to same color."""
        if not self._strip:
            return

        ws_color = Color(0, 0, 0)
        if color == LEDColor.GREEN:
            ws_color = Color(0, 255, 0)
        elif color == LEDColor.RED:
            ws_color = Color(255, 0, 0)
        elif color == LEDColor.YELLOW:
            ws_color = Color(255, 255, 0)

        for i in range(LED_COUNT):
            self._strip.setPixelColor(i, ws_color)
        self._strip.show()

    def cleanup(self) -> None:
        """Cleanup resources."""
        if self._nfc_reader:
            self._nfc_reader.close()

        if self._rfid_reader:
            self._rfid_reader.disconnect()

        if self._strip:
            self.set_all_leds(LEDColor.OFF)

        if RPI_AVAILABLE:
            GPIO.cleanup()

        self._initialized = False
        logger.info("Hardware cleanup complete")


    def health_check(self) -> Dict[str, Any]:
        """Check hardware health."""
        return {
            "status": "healthy" if self._initialized else "not_initialized",
            "mode": "raspberry_pi",
            "rpi_available": RPI_AVAILABLE,
            "servo": "ok" if self._servo_manager else "error",
            "nfc": "ok" if self._nfc_reader and self._nfc_reader._test_connection() else "error",
            "rfid": "ok" if self._rfid_reader else "error",
            "drawers": self.num_drawers,
            "leds": self.num_leds,
        }
