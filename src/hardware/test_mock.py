#!/usr/bin/env python3
"""Test script for MockHardware implementation."""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hardware import MockHardware, LEDColor


def test_mock_hardware():
    """Run through mock hardware functionality."""
    print("=" * 60)
    print(" Mock Hardware Test Suite")
    print("=" * 60)

    # Initialize mock hardware
    hw = MockHardware(num_drawers=4, num_leds=8)

    with hw:
        # Test LED control
        print("\n--- LED Test ---")
        hw.set_all_leds(LEDColor.RED)
        hw.set_led(0, LEDColor.GREEN)
        hw.led_pattern("blink", LEDColor.BLUE, 1.0)

        # Test beeper
        print("\n--- Beeper Test ---")
        hw.beep_success()
        hw.beep_warning()
        hw.beep_error()

        # Test drawer locks
        print("\n--- Drawer Lock Test ---")
        hw.unlock_all()
        print(f"Drawer states: {hw.get_all_drawer_states()}")
        hw.lock_drawer(0)
        hw.unlock_drawer(1)
        print(f"All closed? {hw.are_all_drawers_closed()}")
        hw.lock_all()
        print(f"All closed? {hw.are_all_drawers_closed()}")

        # Test NFC reading (requires user input)
        print("\n--- NFC Test ---")
        print("Press Enter to skip NFC test, or select a card...")
        card = hw.read_nfc(timeout=3.0)
        if card:
            print(f"Card read: {card}")
        else:
            print("NFC timeout (expected)")

        # Test RFID reading (requires user input)
        print("\n--- RFID Test ---")
        print("Press Enter to use default tags, or 'a' to add all...")
        tags = hw.read_rfid_tags()
        print(f"Tags detected: {tags}")

        # Health check
        print("\n--- Health Check ---")
        health = hw.health_check()
        print(f"Status: {health}")

    print("\n" + "=" * 60)
    print(" Test Complete")
    print("=" * 60)


if __name__ == "__main__":
    test_mock_hardware()
