#!/usr/bin/env python3
"""Ask the running PowerPico firmware to restart into its USB bootloader.

Usage:
  py tools/pico_boot.py [--port PORT]

Sends the firmware's own firmware-update command (type 0x15 with the key
C3 3C, in User/user_CmdStrategy.c), which restarts the unit into its
bootloader without the top button, then waits up to 20 s for the
bootloader's banner on the USB port. pico_flash.py can then flash it.

It writes nothing until it has read the port for 1.5 s: it sends the
command only to a port that carries the firmware's measurement packets. A
port that already shows the bootloader's banner is left alone.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I leaves it out
import pico_flash  # noqa: E402
import pico_serial  # noqa: E402
import pico_stream  # noqa: E402

FRAME = pico_stream.command_frame(pico_stream.CMD_FW_UPDATE, 1, pico_stream.CMD_KEY)


class BootError(Exception):
    pass


def look(port, seconds):
    """What the port sends in `seconds`."""
    seen = bytearray()
    stop = time.monotonic() + seconds
    while time.monotonic() < stop:
        seen += port.read(4096)
    return bytes(seen)


def request_bootloader(port, read_time=1.5):
    """Send the command if the port is the running firmware. Returns "sent",
    or "bootloader" when the port already shows the bootloader's banner."""
    seen = look(port, read_time)
    if pico_flash.BANNER in seen:
        return "bootloader"
    if pico_flash.count_app_frames(seen) < 3:
        raise BootError("no measurement packets in %.1f s: this is not the running PowerPico "
                        "firmware. Nothing was sent." % read_time)
    port.write(FRAME)
    if hasattr(port, "flush"):
        port.flush()
    return "sent"


def wait_for_banner(timeout=20.0, poll=1.0, read_time=1.5):
    """The name of a PowerPico port showing the bootloader's banner, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll)
        for found in pico_serial.list_ports():
            try:
                port = pico_serial.open_port(found.device, timeout=0.05)
            except OSError:
                continue  # still enumerating
            try:
                seen = look(port, read_time)
            except OSError:
                continue
            finally:
                try:
                    port.close()
                except OSError:
                    pass
            if pico_flash.BANNER in seen:
                return found.device
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help="the serial port, e.g. COM3 (default: the only "
                                       "port with the PowerPico's USB IDs)")
    args = parser.parse_args(argv)
    if pico_serial.backend() is None:
        print(pico_serial.NEEDS_PYSERIAL)
        return 1
    name = args.port
    if not name:
        ports = pico_serial.list_ports()
        if len(ports) != 1:
            print("Found %d ports with the PowerPico's USB IDs%s. Name one with --port."
                  % (len(ports), (": " + ", ".join(p.device for p in ports)) if ports else ""))
            return 1
        name = ports[0].device

    port = pico_serial.open_port(name, timeout=0.05)
    try:
        state = request_bootloader(port)
    except BootError as error:
        print("%s: %s" % (name, error))
        return 1
    finally:
        try:
            port.close()
        except OSError:
            pass  # the unit may already be restarting
    if state == "bootloader":
        print("%s already shows the bootloader's banner; nothing sent." % name)
        return 0
    print("Sent %s to %s; waiting for the bootloader ..." % (FRAME.hex(" "), name))
    found = wait_for_banner()
    if found is None:
        print("No bootloader banner within 20 s. Look at the screen; the top button at "
              "plug-in also starts the bootloader.")
        return 1
    print("The bootloader is on %s. Flash with:\n  py tools/pico_flash.py flash IMAGE --port %s"
          % (found, found))
    return 0


if __name__ == "__main__":
    sys.exit(main())
