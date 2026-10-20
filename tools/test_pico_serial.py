"""Tests for pico_serial.py: which backend it picks and how it finds the port.

Run: py tools/test_pico_serial.py
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_serial as ps  # noqa: E402


def fake_pyserial(comports=()):
    opened = []

    def serial_class(*args, **kwargs):
        opened.append((args, kwargs))
        return "pyserial port"

    list_ports = types.SimpleNamespace(comports=lambda: list(comports))
    module = types.SimpleNamespace(Serial=serial_class,
                                   tools=types.SimpleNamespace(list_ports=list_ports))
    return module, opened


def comport(device, vid, pid, description="USB Serial Device"):
    return types.SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


class WithPyserial(unittest.TestCase):
    def test_pyserial_is_used_when_it_imports(self):
        module, opened = fake_pyserial()
        with mock.patch.object(ps, "_pyserial", lambda: module):
            self.assertEqual(ps.backend(), "pyserial")
            self.assertEqual(ps.open_port("COM7", 0.05, 10), "pyserial port")
        self.assertEqual(opened, [(("COM7", 115200), {"timeout": 0.05, "write_timeout": 10})])

    def test_only_the_powerpico_ids_are_listed(self):
        module, _ = fake_pyserial([comport("COM10", 0x0483, 0x5740),
                                   comport("COM4", 0x10C4, 0xEA60),
                                   comport("COM2", 0x0483, 0x5740, "STM32 VCP")])
        with mock.patch.object(ps, "_pyserial", lambda: module):
            self.assertEqual(ps.list_ports(), [ps.Port("COM2", "STM32 VCP"),
                                               ps.Port("COM10", "USB Serial Device")])


class WithoutPyserial(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(ps, "_pyserial", lambda: None),
                   mock.patch.object(ps, "_WINDOWS", True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_windows_falls_back_to_win32(self):
        opened = []

        class FakeWinSerial:
            def __init__(self, *args):
                opened.append(args)

        self.assertEqual(ps.backend(), "win32")
        with mock.patch.object(ps, "WinSerial", FakeWinSerial):
            self.assertIsInstance(ps.open_port("COM3", 0.2, 5), FakeWinSerial)
        self.assertEqual(opened, [("COM3", 0.2, 5)])

    def test_registry_ports_must_be_present(self):
        seen = [("COM10", "STMicroelectronics Virtual COM Port (COM10)"),
                ("COM3", "USB Serial Device"),          # unplugged
                ("COM9", "USB Serial Device"),
                ("COM10", "a second instance with the same name")]
        with mock.patch.object(ps, "_registry_instances", lambda vid, pid: seen), \
                mock.patch.object(ps, "_present_names", lambda: {"COM9", "COM10", "COM1"}):
            self.assertEqual(ps.list_ports(), [
                ps.Port("COM9", "USB Serial Device"),
                ps.Port("COM10", "STMicroelectronics Virtual COM Port (COM10)")])

    def test_neither_backend_says_what_to_install(self):
        with mock.patch.object(ps, "_WINDOWS", False):
            self.assertIsNone(ps.backend())
            with self.assertRaises(RuntimeError) as caught:
                ps.open_port("/dev/ttyACM0")
            self.assertIn("pyserial", str(caught.exception))
            with self.assertRaises(RuntimeError):
                ps.list_ports()


@unittest.skipUnless(sys.platform == "win32", "Win32 port")
class Win32(unittest.TestCase):
    def test_a_missing_port_raises_oserror_with_its_name(self):
        with self.assertRaises(OSError) as caught:
            ps.WinSerial("COM250")
        self.assertIn("COM250", str(caught.exception))

    def test_the_registry_can_be_read(self):
        # Whatever is plugged in: this exercises the registry calls.
        for name, _ in ps._registry_instances(ps.USB_VID, ps.USB_PID):
            self.assertTrue(name.startswith("COM"), name)
        self.assertIsInstance(ps._present_names(), set)


if __name__ == "__main__":
    unittest.main()
