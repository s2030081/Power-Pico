"""Tests for pico_boot.py against the simulated unit in test_pico_flash.py.

Run: py tools/test_pico_boot.py
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_boot  # noqa: E402
import pico_serial  # noqa: E402
import pico_stream  # noqa: E402
from test_pico_flash import FakeBootloader  # noqa: E402


class Request(unittest.TestCase):
    def test_the_running_firmware_gets_the_command(self):
        unit = FakeBootloader(app_frames=True)
        self.assertEqual(pico_boot.request_bootloader(unit, read_time=0.05), "sent")
        self.assertEqual(bytes(unit.received), pico_boot.FRAME)
        [item] = pico_stream.parse(bytes(unit.received))
        self.assertEqual(item, ("cmd", 0x15, 1, b"\xc3\x3c"))

    def test_the_bootloader_is_left_alone(self):
        unit = FakeBootloader()
        self.assertEqual(pico_boot.request_bootloader(unit, read_time=0.05), "bootloader")
        self.assertEqual(unit.received, b"")

    def test_a_silent_port_is_never_written(self):
        class Silent(FakeBootloader):
            def idle(self):
                pass
        unit = Silent()
        with self.assertRaises(pico_boot.BootError):
            pico_boot.request_bootloader(unit, read_time=0.05)
        self.assertEqual(unit.received, b"")


class Waiting(unittest.TestCase):
    def test_the_banner_is_found_after_the_port_comes_back(self):
        attempts = []

        def open_port(name, timeout):
            attempts.append(name)
            if len(attempts) == 1:
                raise OSError("not there yet")
            return FakeBootloader(app_frames=len(attempts) == 2)

        with mock.patch.object(pico_serial, "list_ports",
                               lambda: [pico_serial.Port("COM4", "USB Serial Device")]), \
                mock.patch.object(pico_serial, "open_port", open_port):
            self.assertEqual(pico_boot.wait_for_banner(timeout=2, poll=0.01, read_time=0.02),
                             "COM4")
        self.assertEqual(len(attempts), 3)

    def test_no_banner(self):
        with mock.patch.object(pico_serial, "list_ports", lambda: []):
            self.assertIsNone(pico_boot.wait_for_banner(timeout=0.05, poll=0.01))


if __name__ == "__main__":
    unittest.main()
