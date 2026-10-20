"""The PowerPico's USB serial port, with pyserial or, on Windows, without it.

The bootloader and the application both appear as a USB CDC virtual COM port
with ST's IDs, VID 0483 and PID 5740. When pyserial imports, this module uses
it. Without pyserial, on Windows, it opens the port through the Win32 API
(kernel32 through ctypes) and finds it in the registry, so the tools run on a
plain Python install; elsewhere it needs pyserial.

  backend()              "pyserial", "win32", or None when neither can be used
  list_ports(vid, pid)   the ports with those IDs present now, as
                         Port(device, description), sorted by name
  open_port(name, timeout, write_timeout)
                         a port with the methods the tools use from pyserial:
                         read(n), write(data), flush(), reset_input_buffer()
                         and close()

The Win32 port's read(n) returns as soon as anything has arrived, up to n
bytes; with nothing queued it waits up to `timeout` seconds and returns b"".
A port that has gone away (the unit unplugged or restarted) raises OSError,
as pyserial's SerialException does. write() raises OSError when the data is
not taken within `write_timeout` seconds.

It sets no baud rate or line settings: the bootloader's and the application's
CDC code (usbd_cdc_if.c) ignore them. It raises DTR on open, as pyserial does.

Without pyserial a port is found from two registry keys: each device instance
Windows has seen with the IDs is a key under
HKLM\\SYSTEM\\CurrentControlSet\\Enum\\USB\\VID_0483&PID_5740, whose
Device Parameters\\PortName is the COM name it was given, and
HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM lists the COM names present now. A port
is listed when both agree. A PowerPico instance that is unplugged, whose COM
name another device now has, would be listed too, so pico_flash.py and
pico_boot.py read a port and check what it sends before writing to it.
"""

import collections
import ctypes
import re
import sys

USB_VID = 0x0483  # ST's virtual COM port IDs, used by both the
USB_PID = 0x5740  # bootloader and the application
NEEDS_PYSERIAL = "This needs pyserial: python -m pip install pyserial"

Port = collections.namedtuple("Port", "device description")

_WINDOWS = sys.platform == "win32"


def _pyserial():
    """The pyserial package, or None."""
    try:
        import serial
        import serial.tools.list_ports  # noqa: F401
    except ImportError:
        return None
    return serial if hasattr(serial, "Serial") else None


def backend():
    if _pyserial() is not None:
        return "pyserial"
    if _WINDOWS:
        return "win32"
    return None


def list_ports(vid=USB_VID, pid=USB_PID):
    serial = _pyserial()
    if serial is not None:
        ports = [Port(p.device, p.description) for p in serial.tools.list_ports.comports()
                 if p.vid == vid and p.pid == pid]
    elif _WINDOWS:
        present = _present_names()
        ports = {}
        for name, description in _registry_instances(vid, pid):
            if name in present:
                ports.setdefault(name, Port(name, description))
        ports = list(ports.values())
    else:
        raise RuntimeError(NEEDS_PYSERIAL)
    return sorted(ports, key=_name_order)


def open_port(name, timeout=0.05, write_timeout=10.0):
    serial = _pyserial()
    if serial is not None:
        return serial.Serial(name, 115200, timeout=timeout, write_timeout=write_timeout)
    if _WINDOWS:
        return WinSerial(name, timeout, write_timeout)
    raise RuntimeError(NEEDS_PYSERIAL)


def _name_order(port):
    """COM2 before COM10."""
    match = re.match(r"(.*?)(\d*)$", port.device)
    return match.group(1), int(match.group(2) or 0), port.device


# --- the registry ------------------------------------------------------------

def _subkeys(key):
    import winreg
    index = 0
    while True:
        try:
            yield winreg.EnumKey(key, index)
        except OSError:
            return
        index += 1


def _registry_instances(vid, pid):
    """(COM name, description) of each device instance seen with these IDs."""
    import winreg
    path = r"SYSTEM\CurrentControlSet\Enum\USB\VID_%04X&PID_%04X" % (vid, pid)
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except OSError:
        return []
    found = []
    with key:
        for instance in list(_subkeys(key)):
            try:
                with winreg.OpenKey(key, instance + r"\Device Parameters") as params:
                    name = winreg.QueryValueEx(params, "PortName")[0]
            except OSError:
                continue
            found.append((name, _description(key, instance)))
    return found


def _description(key, instance):
    import winreg
    try:
        with winreg.OpenKey(key, instance) as handle:
            for value in ("FriendlyName", "DeviceDesc"):
                try:
                    # DeviceDesc reads "@oem12.inf,%desc%;USB Serial Device".
                    return winreg.QueryValueEx(handle, value)[0].split(";")[-1]
                except OSError:
                    pass
    except OSError:
        pass
    return "USB serial port"


def _present_names():
    """The COM names of the serial ports present now."""
    import winreg
    names = set()
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return names
    with key:
        index = 0
        while True:
            try:
                names.add(winreg.EnumValue(key, index)[1])
            except OSError:
                return names
            index += 1


# --- the Win32 port ----------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
SETDTR = 5
PURGE_RXABORT = 0x0002
PURGE_RXCLEAR = 0x0008
MAXDWORD = 0xFFFFFFFF

_kernel32_cache = []


def _kernel32():
    """kernel32 with the argument types set, loaded on first use."""
    if _kernel32_cache:
        return _kernel32_cache[0]
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                wt.DWORD, wt.DWORD, wt.HANDLE]
    for name in ("ReadFile", "WriteFile"):
        function = getattr(k32, name)
        function.restype = wt.BOOL
        function.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                             ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    k32.SetCommTimeouts.restype = wt.BOOL
    k32.SetCommTimeouts.argtypes = [wt.HANDLE, ctypes.c_void_p]
    for name in ("EscapeCommFunction", "PurgeComm"):
        function = getattr(k32, name)
        function.restype = wt.BOOL
        function.argtypes = [wt.HANDLE, wt.DWORD]
    for name in ("FlushFileBuffers", "CloseHandle"):
        function = getattr(k32, name)
        function.restype = wt.BOOL
        function.argtypes = [wt.HANDLE]
    _kernel32_cache.append(k32)
    return k32


def _windows_error(what):
    code = ctypes.get_last_error()
    return OSError("%s (Windows error %d: %s)" % (what, code, ctypes.FormatError(code).strip()))


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [(field, ctypes.c_uint32) for field in (
        "ReadIntervalTimeout", "ReadTotalTimeoutMultiplier", "ReadTotalTimeoutConstant",
        "WriteTotalTimeoutMultiplier", "WriteTotalTimeoutConstant")]


class WinSerial:
    """A COM port opened through kernel32."""

    def __init__(self, name, timeout=0.05, write_timeout=10.0):
        self._handle = None
        self.name = name
        import ctypes.wintypes as wt
        self._k32 = _kernel32()
        self._dword = wt.DWORD
        path = name if name.startswith("\\\\.\\") else "\\\\.\\" + name
        handle = self._k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                                       OPEN_EXISTING, 0, None)
        if handle is None or handle == wt.HANDLE(-1).value:
            raise _windows_error("cannot open %s" % name)
        self._handle = handle
        # MAXDWORD, MAXDWORD, T: return at once with what is queued; with
        # nothing queued, wait up to T ms for the first byte.
        timeouts = COMMTIMEOUTS(MAXDWORD, MAXDWORD, max(1, int(timeout * 1000)),
                                0, max(1, int(write_timeout * 1000)))
        if not self._k32.SetCommTimeouts(handle, ctypes.byref(timeouts)):
            error = _windows_error("cannot set the timeouts of %s" % name)
            self.close()
            raise error
        self._k32.EscapeCommFunction(handle, SETDTR)
        self._buffer = ctypes.create_string_buffer(65536)

    def read(self, n):
        got = self._dword(0)
        if not self._k32.ReadFile(self._open(), self._buffer, min(n, len(self._buffer)),
                                  ctypes.byref(got), None):
            raise _windows_error("reading %s failed" % self.name)
        return self._buffer.raw[:got.value]

    def write(self, data):
        data = bytes(data)
        done = 0
        while done < len(data):
            chunk = ctypes.create_string_buffer(data[done:], len(data) - done)
            wrote = self._dword(0)
            if not self._k32.WriteFile(self._open(), chunk, len(data) - done,
                                       ctypes.byref(wrote), None):
                raise _windows_error("writing to %s failed" % self.name)
            if wrote.value == 0:
                raise OSError("writing to %s timed out" % self.name)
            done += wrote.value
        return done

    def flush(self):
        """Wait until everything written has been sent."""
        self._k32.FlushFileBuffers(self._open())

    def reset_input_buffer(self):
        self._k32.PurgeComm(self._open(), PURGE_RXABORT | PURGE_RXCLEAR)

    def close(self):
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    def _open(self):
        if self._handle is None:
            raise OSError("%s is closed" % self.name)
        return self._handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        self.close()
