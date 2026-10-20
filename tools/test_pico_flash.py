"""Tests for pico_flash.py against a simulated bootloader.

FakeBootloader follows software/bootloader: Bootloader_Main's banner loop,
Main_Menu, USB_Download and Ymodem_Receive (no packet CRC check, the size read
up to a space, the erase on packet 0, ACK on the first EOT, the empty header
that ends the session, CA CA as a cancel), and the report with the STM32 CRC.

Run: py tools/test_pico_flash.py
"""

import contextlib
import io
import os
import random
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pico_flash as pf  # noqa: E402
import pico_serial  # noqa: E402

MENU = (b"\r\n================== Main Menu ============================\r\n\n"
        b"  Download Image To the STM32F4xx Internal Flash ------- 1\r\n\n"
        b"  Upload Image From the STM32F4xx Internal Flash ------- 2\r\n\n"
        b"  Execute The New Program ------------------------------ 3\r\n\n"
        b"==========================================================\r\n\n")
HEADER = (b"\r\n======================================================================"
          b"\r\n=  STM32F4xx In-Application Programming Application  (Version 1.0.0) ="
          b"\r\n======================================================================\r\n\r\n")


class FakeBootloader:
    """A port: write() feeds the bootloader, read() returns what it sent.

    When nothing is queued, read() lets time pass in the bootloader: the banner
    loop prints its banner, and Ymodem_Receive times out and sends 'C'.
    """

    def __init__(self, corrupt_at=None, nak_packets=(), app_frames=False):
        self.out = bytearray()
        self.inp = bytearray()
        self.state = "app" if app_frames else "banner"
        self.flash = bytearray(b"\xff" * pf.APP_SIZE)
        self.record = None  # (size, crc) as Write_Upgrade_Info stores it
        self.corrupt_at = corrupt_at
        self.nak_packets = set(nak_packets)
        self.received = bytearray()  # everything written to the port
        self.header_size_field = None
        self.closed = False
        self.timeouts_in_ymodem = 0

    # --- the port interface ---
    def write(self, data):
        self.received += data
        self.inp += data
        self.step()
        return len(data)

    def read(self, n):
        if not self.out:
            self.idle()
        chunk = bytes(self.out[:n])
        del self.out[:n]
        return chunk

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        self.closed = True

    # --- the bootloader ---
    def idle(self):
        if self.state == "banner":
            self.out += b"##PICO_BOOT##\r\n"
        elif self.state == "ymodem":
            self.timeouts_in_ymodem += 1
            self.out += b"C"
        elif self.state == "app":
            self.out += app_frame()

    def step(self):
        while self.inp:
            if self.state == "banner":
                key = self.inp.pop(0)
                if key == ord("0"):
                    self.out += HEADER + MENU
                    self.state = "menu"
            elif self.state == "menu":
                key = self.inp.pop(0)
                if key == ord("1"):
                    self.out += (b"Waiting for the file to be sent ... (press 'a' to abort)\n\r"
                                 b"Erasing Application Area...\r\nErase Complete.\r\n"
                                 b"Starting Ymodem Receive\n\r")
                    self.flash[:] = b"\xff" * pf.APP_SIZE
                    self.state, self.packets, self.dest, self.size = "ymodem", 0, 0, 0
                    self.name = b""
                elif key == ord("3"):
                    self.out += b"Start to execute the user application...\r\n"
                    self.state = "jumped"
                else:
                    self.out += b"Invalid Number ! ==> The number should be either 1, 2 or 3\r"
                    self.out += MENU
            elif self.state == "ymodem":
                if not self.receive():
                    return
            else:
                self.inp.clear()

    def receive(self):
        """One Receive_Packet. False when more bytes are needed."""
        first = self.inp[0]
        if first == pf.EOT:
            del self.inp[0]
            self.out += bytes([pf.ACK])
            self.packets = 0  # file_done: the outer loop waits for the next packet 0
            return True
        if first == pf.CA:
            if len(self.inp) < 2:
                return False
            del self.inp[:2]
            self.out += bytes([pf.ACK])
            self.finish(0)
            return True
        if first not in (pf.SOH, pf.STX):
            del self.inp[0]
            return True
        size = 128 if first == pf.SOH else 1024
        if len(self.inp) < size + 5:
            return False
        packet = bytes(self.inp[:size + 5])
        del self.inp[:size + 5]
        seq, comp, data = packet[1], packet[2], packet[3:3 + size]
        if seq != (comp ^ 0xFF):
            self.out += b"C"
            return True
        if seq != self.packets & 0xFF or (self.packets in self.nak_packets):
            self.nak_packets.discard(self.packets)
            self.out += bytes([pf.NAK])
            return True
        if self.packets == 0:
            if data[0] == 0:
                self.out += bytes([pf.ACK])
                self.finish(self.size)
                return True
            name_end = data.index(0)
            self.name = data[:name_end]
            field = bytearray()
            for byte in data[name_end + 1:]:
                if byte == ord(" ") or len(field) == 16:
                    break
                field.append(byte)
            self.header_size_field = bytes(field)
            self.size = int(bytes(field).split(b"\0")[0])
            if self.size > pf.APP_SIZE + 1:
                self.out += bytes([pf.CA, pf.CA])
                self.finish(-1)
                return True
            self.flash[:] = b"\xff" * pf.APP_SIZE
            self.out += bytes([pf.ACK, pf.CRC_REQUEST])
        else:
            data = bytearray(data)
            if self.corrupt_at is not None and self.dest <= self.corrupt_at < self.dest + size:
                data[self.corrupt_at - self.dest] ^= 0x01
            self.flash[self.dest:self.dest + size] = data
            self.dest += size
            self.out += bytes([pf.ACK])
        self.packets += 1
        return True

    def finish(self, size):
        if size > 0:
            crc = pf.stm32_crc32(bytes(self.flash[:size]))
            self.record = (size, crc)
            self.out += (b"\n\n\r File reception complete. Verifying...\n\r"
                         b"Verification and Flag Set successful!\r\n"
                         b"--------------------------------\r\n Name: " + self.name +
                         b"\n\r Size: " + str(size).ljust(10).encode() + b" Bytes\r\n"
                         b" CRC32: 0x" + ("%08X" % crc).encode() +
                         b"\r\n-------------------\nPlease reboot the device.\r\n")
        else:
            self.record = None  # FLASH_If_Erase_One_Sector(UPGRADE_INFO_SECTOR_NUM)
            self.out += b"\n\rUpgrade failed!\r\n"
            self.out += {0: b"\n\rFailed to receive the file!\n\r",
                         -1: b"\n\n\rThe image size is higher than the allowed space memory!\n\r"
                         }.get(size, b"")
        self.out += MENU
        self.state = "menu"


def app_frame():
    samples = b"".join(struct.pack("<BHHH", 1, 1000, 2100, 2048) for _ in range(10))
    return b"\xaa\x55" + struct.pack("<Q", 123456) + bytes([10]) + samples


def make_image(size=5000, seed=1):
    """An image that passes check_image: stack in RAM, handlers in the image."""
    rng = random.Random(seed)
    body = bytearray(rng.getrandbits(8) for _ in range(size))
    vectors = [0x2001D820] + [pf.APP_ADDRESS + 0x200 + 8 * i + 1 for i in range(15)]
    for i in (7, 8, 9, 10, 13):
        vectors[i] = 0
    body[:64] = struct.pack("<16I", *vectors)
    return bytes(body)


def fast_timeouts():
    t = pf.Timeouts()
    for name in ("banner", "menu", "erase", "crc_request", "header_ack", "packet_ack",
                 "report", "reenumerate", "stream"):
        setattr(t, name, 0.5)
    return t


def flasher_for(fake):
    return pf.Flasher(fake, say=lambda *_: None, timeouts=fast_timeouts())


class Checksums(unittest.TestCase):
    def test_crc16_check_value(self):
        self.assertEqual(pf.crc16_xmodem(b"123456789"), 0x31C3)

    def test_stm32_crc_known_word(self):
        # The STM32 CRC unit's widely published check: DR = 0x12345678 gives 0xDF8A8A2B.
        self.assertEqual(pf.stm32_crc32(struct.pack("<I", 0x12345678)), 0xDF8A8A2B)

    def test_stm32_crc_table_matches_bitwise(self):
        data = bytes(random.Random(7).getrandbits(8) for _ in range(4096))
        crc = 0xFFFFFFFF
        for (word,) in struct.iter_unpack("<I", data):
            crc ^= word
            for _ in range(32):
                crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
                crc &= 0xFFFFFFFF
        self.assertEqual(pf.stm32_crc32(data), crc)

    def test_stm32_crc_ignores_a_partial_word(self):
        self.assertEqual(pf.stm32_crc32(b"abcdefgh" + b"xy"), pf.stm32_crc32(b"abcdefgh"))


class ImageChecks(unittest.TestCase):
    def test_good_image_passes(self):
        info, problems = pf.check_image(make_image())
        self.assertEqual(problems, [])
        self.assertEqual(info.stack_pointer, 0x2001D820)

    def test_elf_and_hex_are_refused(self):
        self.assertTrue(pf.check_image(b"\x7fELF" + bytes(200))[1])
        self.assertTrue(pf.check_image(b":020000040801F1\n" * 10)[1])

    def test_too_big(self):
        self.assertTrue(any("do not fit" in p for p in
                            pf.check_image(make_image(pf.APP_SIZE + 4))[1]))

    def test_stack_pointer_outside_ram(self):
        image = bytearray(make_image())
        image[0:4] = struct.pack("<I", 0x20030000)
        self.assertTrue(any("stack pointer" in p for p in pf.check_image(bytes(image))[1]))

    def test_image_linked_for_0x08000000(self):
        image = bytearray(make_image())
        image[4:8] = struct.pack("<I", 0x08000201)
        self.assertTrue(any("reset handler" in p for p in pf.check_image(bytes(image))[1]))

    def test_even_reset_handler(self):
        image = bytearray(make_image())
        image[4:8] = struct.pack("<I", pf.APP_ADDRESS + 0x200)
        self.assertTrue(any("reset handler" in p for p in pf.check_image(bytes(image))[1]))

    def test_partial_word_refused(self):
        self.assertTrue(any("32-bit words" in p for p in pf.check_image(make_image(5001))[1]))

    def test_sha256_must_match(self):
        image = make_image()
        info, problems = pf.check_image(image, "00" * 32)
        self.assertTrue(any("SHA256" in p for p in problems))
        self.assertEqual(pf.check_image(image, info.sha256.upper())[1], [])

    def test_the_built_and_stock_images_pass(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for relative in ("software/Power_Pico/build/MinSizeRel/output/power_pico.bin",
                         pf.STOCK_IMAGE):
            path = os.path.join(root, relative)
            if not os.path.exists(path):
                continue
            with open(path, "rb") as handle:
                self.assertEqual(pf.check_image(handle.read())[1], [], relative)


class Packets(unittest.TestCase):
    def test_header_has_space_after_size(self):
        packet = pf.header_packet("power_pico.bin", 402948)
        self.assertEqual(packet[:3], bytes([pf.SOH, 0, 0xFF]))
        self.assertEqual(len(packet), 133)
        self.assertTrue(packet[3:].startswith(b"power_pico.bin\x00402948 \x00"))

    def test_last_block_padded_with_erased_flash(self):
        packet = pf.data_packet(256, b"\x01\x02")
        self.assertEqual(packet[:3], bytes([pf.STX, 0, 0xFF]))
        self.assertEqual(packet[5:1027], b"\xff" * 1022)

    def test_frames_counted(self):
        noise = b"\x00\xaa\x55junk"
        self.assertEqual(pf.count_app_frames(noise + app_frame() * 3 + noise), 3)
        self.assertEqual(pf.count_app_frames(b"##PICO_BOOT##\r\n" * 10), 0)


class Flashing(unittest.TestCase):
    def test_flash_writes_the_image_and_verifies(self):
        image = make_image(5000)
        fake = FakeBootloader()
        info, _ = pf.check_image(image)
        size, crc = flasher_for(fake).flash(image, "test.bin", info)
        self.assertEqual((size, crc), (len(image), info.crc32))
        self.assertEqual(bytes(fake.flash[:len(image)]), image)
        self.assertEqual(fake.flash[len(image):len(image) + 100], b"\xff" * 100)
        self.assertEqual(fake.header_size_field, b"5000")
        self.assertEqual(fake.record, (len(image), info.crc32))

    def test_more_than_256_blocks_wrap_the_sequence(self):
        image = make_image(300 * 1024)
        fake = FakeBootloader()
        info, _ = pf.check_image(image)
        flasher_for(fake).flash(image, "big.bin", info)
        self.assertEqual(bytes(fake.flash[:len(image)]), image)

    def test_nak_is_retried(self):
        image = make_image(4096)
        fake = FakeBootloader(nak_packets={2})
        info, _ = pf.check_image(image)
        flasher_for(fake).flash(image, "test.bin", info)
        self.assertEqual(bytes(fake.flash[:len(image)]), image)

    def test_corruption_is_caught_and_the_image_erased(self):
        image = make_image(5000)
        fake = FakeBootloader(corrupt_at=3000)
        info, _ = pf.check_image(image)
        with self.assertRaises(pf.VerifyError):
            flasher_for(fake).flash(image, "test.bin", info)
        self.assertIsNone(fake.record)  # the bootloader's record is gone: it cannot start
        self.assertEqual(fake.flash[:64], b"\xff" * 64)

    def test_running_firmware_is_never_written(self):
        fake = FakeBootloader(app_frames=True)
        image = make_image()
        info, _ = pf.check_image(image)
        with self.assertRaises(pf.FlashError) as caught:
            flasher_for(fake).flash(image, "test.bin", info)
        self.assertIn("running firmware", str(caught.exception))
        self.assertEqual(fake.received, b"")

    def test_silent_port_is_never_written(self):
        class Silent(FakeBootloader):
            def idle(self):
                pass
        fake = Silent()
        image = make_image()
        info, _ = pf.check_image(image)
        with self.assertRaises(pf.FlashError):
            flasher_for(fake).flash(image, "test.bin", info)
        self.assertEqual(fake.received, b"")

    def test_declined_confirmation_writes_nothing(self):
        fake = FakeBootloader()
        image = make_image()
        info, _ = pf.check_image(image)
        flasher = flasher_for(fake)
        with self.assertRaises(pf.FlashError):
            flasher.flash(image, "test.bin", info, confirm=lambda: False)
        self.assertEqual(fake.received, b"")
        self.assertFalse(flasher.app_erased)

    def test_run_starts_the_application_and_reads_its_stream(self):
        image = make_image()
        fake = FakeBootloader()
        info, _ = pf.check_image(image)
        flasher = flasher_for(fake)
        flasher.flash(image, "test.bin", info)
        flasher.start_application()
        self.assertEqual(fake.state, "jumped")
        attempts = []

        def open_port(name):
            attempts.append(name)
            if len(attempts) == 1:
                raise OSError("not there yet")
            return FakeBootloader(app_frames=True)

        frames = pf.check_running(open_port, lambda: None, "COM9", fast_timeouts(), settle=0)
        self.assertGreaterEqual(frames, 3)

    def test_the_real_report_parses(self):
        # The bootloader's report from the first flash on the unit, 2026-10-20.
        report = (b"\n\n\r File reception complete. Verifying...\n\rVerification and Flag Set "
                  b"successful!\r\n--------------------------------\r\n Name: power_pico.bin\n\r"
                  b" Size: 402948     Bytes\r\n CRC32: 0xB3B20948\r\n-------------------\n"
                  b"Please reboot the device.\r\n" + MENU)

        class Replay(FakeBootloader):
            def idle(self):
                pass
        port = Replay()
        port.out += report
        self.assertEqual(flasher_for(port).read_report(), (402948, 0xB3B20948))

    def test_bootloader_back_after_run_is_reported(self):
        with self.assertRaises(pf.FlashError):
            pf.check_running(lambda name: FakeBootloader(), lambda: None, "COM9",
                             fast_timeouts(), settle=0)


class WithoutPyserial(unittest.TestCase):
    """The command line with no pyserial: Win32 on Windows, a message elsewhere."""

    def setUp(self):
        self.opened = []
        self.fake = FakeBootloader()

        def win_serial(name, timeout, write_timeout):
            self.opened.append((name, timeout, write_timeout))
            return self.fake

        patches = [mock.patch.object(pico_serial, "_pyserial", lambda: None),
                   mock.patch.object(pico_serial, "_WINDOWS", True),
                   mock.patch.object(pico_serial, "WinSerial", win_serial),
                   mock.patch.object(pico_serial, "_registry_instances",
                                     lambda vid, pid: [("COM3", "USB Serial Device (COM3)")]),
                   mock.patch.object(pico_serial, "_present_names", lambda: {"COM3"})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = pf.main(list(argv))
        return status, out.getvalue()

    def test_ports_lists_the_registry_port(self):
        status, out = self.run_main("ports")
        self.assertEqual(status, 0)
        self.assertIn("COM3  USB Serial Device (COM3)", out)

    def test_flash_goes_through_the_win32_port(self):
        image = make_image(5000)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "power_pico.bin")
            with open(path, "wb") as handle:
                handle.write(image)
            status, out = self.run_main("flash", path, "--yes")
        self.assertEqual(status, 0, out)
        self.assertEqual(self.opened, [("COM3", 0.05, 10)])
        self.assertEqual(bytes(self.fake.flash[:len(image)]), image)
        self.assertIn("Verified", out)

    def test_elsewhere_it_asks_for_pyserial(self):
        with mock.patch.object(pico_serial, "_WINDOWS", False):
            status, out = self.run_main("ports")
        self.assertEqual(status, 1)
        self.assertIn("pip install pyserial", out)
        self.assertEqual(self.opened, [])


if __name__ == "__main__":
    unittest.main()
