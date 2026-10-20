"""Parse what the PowerPico sends on its USB port.

The firmware sends three kinds of frame, back to back. All fields are
little-endian.

AA 55, a measurement packet every 1 ms, 81 bytes (USB_ADC_Packet_t in
  User/user_AdcDataStrategy.h): the header, a 64-bit timestamp in
  microseconds, a sample count (10), then 10 samples taken at 10 kHz, each a
  range byte (1-3) and three 12-bit ADC readings as u16: vol_adc, cur_adc and
  ref_adc. vol_adc is the input VBUS through a 1 M / 100 k divider:
  V = vol_adc * 3.0 / 4095 * 11, the firmware's own conversion.

A5 5A, a command frame (User/user_CmdStrategy.c), from the PC or as the
  firmware's answer: the header, version (1), type (the answer's type has
  0x80 set), sequence u16, payload length u16 (48 at most), the payload, then
  a CRC-16/CCITT (start 0xFFFF) over version through payload. An answer's
  payload starts with a status byte.

AA 5D, a debug frame, sent only by the debug build (branch pps-debug-trace,
  User/user_DebugTrace.c): the header, the frame's whole length u8 (5-58), the
  frame type, a sequence byte, then the payload. Type 1 carries up to four
  12-byte events: TIM5 time in microseconds u32, kind u8, a u8, b u16, c u32.
  Types 2-6 are the boot summary and the record of the previous crash.

parse(buf) yields one item per complete frame:

  ("adc", timestamp_us, [VBUS volts, ...])
  ("cmd", type, sequence, payload)
  ("dbg", frame_type, sequence, payload)

and returns (as the generator's StopIteration value) the offset of the first
byte it did not consume: the start of a frame not complete yet. Bytes that
start no valid frame are skipped one at a time: an AA 55 with a count
outside 1-10, a range byte outside 1-3 or a reading over 12 bits; an AA 5D
with a length outside 5-58; an A5 5A with a payload over 48 bytes or a wrong
CRC. The tests on the AA 55 fields also drop most packets cut short by a lost
USB transfer, whose tail would otherwise be read from the next packet's first
bytes; the debug frames have no such check. Stream wraps parse for data that
arrives in pieces.
"""

import struct

V_PER_LSB = 3.0 / 4095.0 * 11.0

ADC_SIZE = 81
ADC_SAMPLES = 10
_TIMESTAMP = struct.Struct("<Q")
_VOL_ADC = struct.Struct("<" + "H5x" * (ADC_SAMPLES - 1) + "H")  # from the first vol_adc
DBG_MIN, DBG_MAX = 5, 58
CMD_OVERHEAD, CMD_MAX_PAYLOAD = 10, 48
CMD_VERSION = 1


def parse(buf):
    i = 0
    n = len(buf)
    while i < n - 1:
        b0, b1 = buf[i], buf[i + 1]
        if b0 == 0xAA and b1 == 0x55:
            if n - i < ADC_SIZE:
                break
            count = buf[i + 10]
            if 1 <= count <= ADC_SAMPLES and _plausible(buf, i + 11, i + 11 + 7 * count):
                yield ("adc", _TIMESTAMP.unpack_from(buf, i + 2)[0],
                       [raw * V_PER_LSB for raw in _VOL_ADC.unpack_from(buf, i + 12)[:count]])
                i += ADC_SIZE
                continue
        elif b0 == 0xAA and b1 == 0x5D:
            if n - i < 3:
                break
            length = buf[i + 2]
            if DBG_MIN <= length <= DBG_MAX:
                if n - i < length:
                    break
                yield ("dbg", buf[i + 3], buf[i + 4], bytes(buf[i + 5:i + length]))
                i += length
                continue
        elif b0 == 0xA5 and b1 == 0x5A:
            if n - i < 8:
                break
            size = buf[i + 6] | buf[i + 7] << 8
            if size <= CMD_MAX_PAYLOAD:
                end = i + CMD_OVERHEAD + size
                if n < end:
                    break
                if crc16_ccitt(buf[i + 2:end - 2]) == (buf[end - 2] | buf[end - 1] << 8):
                    yield ("cmd", buf[i + 3], buf[i + 4] | buf[i + 5] << 8,
                           bytes(buf[i + 8:end - 2]))
                    i = end
                    continue
        i += 1
    return i


def _plausible(buf, start, end):
    """The samples in buf[start:end] (7 bytes each) have a range of 1-3 and
    three readings of 12 bits."""
    ranges = buf[start:end:7]
    return (min(ranges) >= 1 and max(ranges) <= 3 and max(buf[start + 2:end:7]) < 0x10
            and max(buf[start + 4:end:7]) < 0x10 and max(buf[start + 6:end:7]) < 0x10)


class Stream:
    """Keeps the bytes of a frame not complete yet between feeds."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        """The items completed by `data`."""
        self.buf += data
        items = []
        frames = parse(self.buf)
        while True:
            try:
                items.append(next(frames))
            except StopIteration as stop:
                del self.buf[:stop.value]
                return items


# --- command frames ------------------------------------------------------------

CMD_NAMES = {0x10: "CAL_GET", 0x11: "RANGE_GET", 0x12: "RANGE_SET", 0x13: "CAL_SET",
             0x14: "CAL_RESET", 0x15: "FW_UPDATE"}
CMD_STATUS = ["OK", "unsupported version", "unsupported command", "invalid length",
              "invalid parameter", "EEPROM error", "busy", "CRC error"]
CMD_FW_UPDATE = 0x15
CMD_KEY = b"\xc3\x3c"  # what FW_UPDATE and CAL_RESET take as their payload


def crc16_ccitt(data):
    """CRC-16/CCITT-FALSE: polynomial 0x1021, start 0xFFFF, as CmdStrategy_Crc16."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def command_frame(ftype, sequence, payload=b""):
    if len(payload) > CMD_MAX_PAYLOAD:
        raise ValueError("a command payload is %d bytes at most" % CMD_MAX_PAYLOAD)
    body = struct.pack("<BBHH", CMD_VERSION, ftype, sequence, len(payload)) + bytes(payload)
    return b"\xa5\x5a" + body + struct.pack("<H", crc16_ccitt(body))


def describe_command(ftype, sequence, payload):
    name = CMD_NAMES.get(ftype & 0x7F, "0x%02X" % (ftype & 0x7F))
    if ftype & 0x80:
        status = payload[0] if payload else None
        text = CMD_STATUS[status] if status is not None and status < len(CMD_STATUS) else status
        return "CMD answer to %s #%d: %s %s" % (name, sequence, text, payload[1:].hex())
    return "CMD %s #%d %s" % (name, sequence, payload.hex())


# --- debug frames (the pps-debug-trace build) ----------------------------------

CTRL = {1: "GoodCRC", 2: "GotoMin", 3: "Accept", 4: "Reject", 5: "Ping", 6: "PS_RDY",
        7: "Get_Src_Cap", 8: "Get_Sink_Cap", 9: "DR_Swap", 10: "PR_Swap", 11: "VCONN_Swap",
        12: "Wait", 13: "Soft_Reset", 14: "Data_Reset", 15: "Data_Reset_Cpl",
        16: "Not_Supported", 17: "Get_Src_Cap_Ext", 18: "Get_Status", 19: "FR_Swap",
        20: "Get_PPS_Status", 21: "Get_Country_Codes", 22: "Get_Sink_Cap_Ext"}
DATA = {1: "Src_Cap", 2: "Request", 3: "BIST", 4: "Sink_Cap", 5: "Battery_Status",
        6: "Alert", 7: "Get_Country_Info", 8: "Enter_USB", 15: "VDM"}
EXT = {1: "Src_Cap_Ext", 2: "Status", 12: "PPS_Status", 15: "Sink_Cap_Ext"}

# DBG_EV_* in user_DebugTrace.h
KIND = {1: "SET_PPS", 2: "PD_START", 3: "PD_STOP", 4: "TX_REQUEST", 5: "TX_OTHER", 6: "RX",
        7: "PSRDY_TIMEOUT", 8: "PSRDY_DONE", 9: "HARDRST_RX", 10: "HARDRST_TX", 11: "ATTACH",
        12: "DETACH", 13: "FUSB", 14: "KEY", 15: "STACK_FREE", 16: "BOOT", 17: "LIVE_DROPPED"}
CRASH = {0: "none", 1: "stack overflow", 2: "assert", 3: "fault", 4: "Error_Handler",
         5: "RTOS tick stalled 1 s", 6: "stream task stalled 2 s"}
TASKS = ["MsgRec", "MsgSend", "Key", "PD", "LVGL", "Default"]
KEYS = ["B", "Y", "L", "R", "N"]
KEYEV = ["none", "down", "up", "click", "long", "repeat"]
INTA = ["HARDRST", "SOFTRST", "TXSENT", "HARDSENT", "RETRYFAIL", "SOFTFAIL", "TOGDONE", "OCP_TEMP"]
INTR = ["BC_LVL", "COLLISION", "WAKE", "ALERT", "CRC_CHK", "COMP_CHNG", "ACTIVITY", "VBUSOK"]
DBG_EVENTS = 1
_EVENT = struct.Struct("<IBBHI")


def pd_header(h):
    """(message name, number of data objects, message id) of a PD header."""
    t = h & 0x1F
    n = (h >> 12) & 7
    mid = (h >> 9) & 7
    if h & 0x8000:
        name = EXT.get(t, "Ext%d" % t)
    elif n:
        name = DATA.get(t, "Data%d" % t)
    else:
        name = CTRL.get(t, "Ctrl%d" % t)
    return name, n, mid


def rdo(c):
    """A PPS request data object."""
    return "pos %d, %d mV, %d mA" % ((c >> 28) & 7, ((c >> 9) & 0x7FF) * 20, (c & 0x7F) * 50)


def bits(v, names):
    return "|".join(names[i] for i in range(8) if v & (1 << i)) or "-"


def _name(names, index):
    return names[index] if index < len(names) else index


def describe(kind, a, b, c):
    """One debug event as text."""
    k = KIND.get(kind, "K%d" % kind)
    if kind == 1:
        return "%s %d mV %d mA" % (k, b * 20, a * 50)
    if kind in (4, 5, 6):
        name, n, mid = pd_header(b)
        s = "%s %s id%d" % (k, name, mid)
        if kind == 4:
            s += " [%s] (%s)" % (_name(["flagged", "keep-alive", "answer to caps"], a), rdo(c))
        elif n:
            s += " obj0 %08x" % c
        return s
    if kind == 7:
        return "%s after %d ms" % (k, b)
    if kind == 8:
        return "%s type %d, PPS %d mV, PDO %d" % (k, a, b * 20, c)
    if kind == 11:
        return "%s cc %d" % (k, a)
    if kind == 13:
        return "%s INTA %s INTB %s INT %s st %08x" % (
            k, bits(a, INTA), "GCRCSENT" if (b >> 8) & 1 else "-", bits(b & 0xFF, INTR), c)
    if kind == 14:
        return "%s %s %s (btn %d)" % (k, _name(KEYS, a), _name(KEYEV, b), c)
    if kind == 15:
        return "%s %s %d words" % (k, _name(TASKS, a), b)
    if kind == 16:
        return "%s #%d, previous crash: %s, RCC CSR %08x" % (k, b, CRASH.get(a, a), c)
    return "%s a=%d b=%d c=%d" % (k, a, b, c)


def events(payload):
    """The (t_us, kind, a, b, c) events of a type 1 frame."""
    count = min(payload[0], (len(payload) - 1) // _EVENT.size) if payload else 0
    for k in range(count):
        yield _EVENT.unpack_from(payload, 1 + _EVENT.size * k)


def _cstr(raw):
    return raw.split(b"\0")[0].decode("ascii", "replace")


def describe_frame(ftype, payload):
    """A debug frame other than type 1 as text (one line, or several for type 6)."""
    try:
        return _describe_frame(ftype, payload)
    except (struct.error, IndexError):
        return "frame type %d, %d bytes, too short to read" % (ftype, len(payload))


def _describe_frame(ftype, payload):
    if ftype == 2:
        boot, csr, pv, pk, _, _, pboot, tim5, head, dropped, ms = struct.unpack_from(
            "<IIBBBBIIIII", payload)
        heap = ""
        if len(payload) >= 40:
            heap = ", heap free %d B (lowest %d B)" % struct.unpack_from("<II", payload, 32)
        return ("SUMMARY boot #%d, RCC CSR %08x, previous crash: %s, TIM5 %d us, events %d, "
                "dropped %d, uptime %d ms%s" % (boot, csr, CRASH.get(pk, pk) if pv else "none",
                                               tim5, head, dropped, ms, heap))
    if ftype == 3:
        kind, t, task, line, basepri, primask, head = struct.unpack_from("<II16sIIII", payload)
        return ("CRASH %s at TIM5 %d us in task '%s', line %d, BASEPRI %02x PRIMASK %d, "
                "%d events before" % (CRASH.get(kind, kind), t, _cstr(task), line, basepri,
                                      primask, head))
    if ftype == 4:
        return ("CRASH regs r0 %08x r1 %08x r2 %08x r3 %08x r12 %08x lr %08x pc %08x xpsr %08x "
                "CFSR %08x HFSR %08x MMFAR %08x BFAR %08x" % struct.unpack_from("<12I", payload))
    if ftype == 5:
        exc, sp, name = struct.unpack_from("<II24s", payload)
        return "CRASH exc_return %08x frame at %08x file '%s'" % (exc, sp, _cstr(name))
    if ftype == 6:
        first = payload[0] | (payload[1] << 8)
        lines = []
        for k in range(payload[2]):
            t, kind, a, b, c = _EVENT.unpack_from(payload, 3 + _EVENT.size * k)
            lines.append("CRASHLOG %3d  %10d us  %s" % (first + k, t, describe(kind, a, b, c)))
        return "\n".join(lines)
    return "frame type %d, %d bytes" % (ftype, len(payload))
