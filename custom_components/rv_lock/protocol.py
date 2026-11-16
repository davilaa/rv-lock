"""Tuya BLE protocol v4 framing, as implemented by the Smart Life app.

Plaintext frame: [sn:4][ack_sn:4][cmd:2][len:2][data][crc16 BE], zero-padded to 16,
AES-128-CBC with a random IV; on-air: [security flag][iv:16][ciphertext].
MTP packets: [varint packet no.][pkt 0: varint total len + (2<<4)][payload], each
packet up to the negotiated MTU-3 bytes (the app writes a whole frame at once).
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
from struct import pack, unpack

from Crypto.Cipher import AES

SEC_LOGIN = 4
SEC_SESSION = 5

CMD_DEVICE_INFO = 0x0000
CMD_PAIR = 0x0001
CMD_QUERY_DPS = 0x0003
CMD_DP_WRITE = 0x0027
CMD_DP_REPORT = 0x8006
CMD_DP_REPORT_TIME = 0x8007
CMD_TIME1 = 0x8011
CMD_TIME2 = 0x8012
CMD_TIME_LOCAL = 0x8013

DP_TYPE_RAW = 0x00
DP_TYPE_BOOL = 0x01
DP_TYPE_INT = 0x02

FRAME_VERSION_BYTE = 2 << 4


def login_key(local_key: str) -> bytes:
    return hashlib.md5(local_key[:6].encode()).digest()


def session_key(local_key: str, srand: bytes) -> bytes:
    return hashlib.md5(local_key[:6].encode() + srand).digest()


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _unvarint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for i in range(5):
        if pos + i >= len(data):
            raise ValueError("truncated varint")
        b = data[pos + i]
        result |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return result, pos + i + 1
    raise ValueError("varint overflow")


def build_frame(sn: int, ack_sn: int, cmd: int, data: bytes, key: bytes, flag: int, chunk: int) -> list[bytes]:
    raw = bytearray(pack(">IIHH", sn, ack_sn, cmd, len(data)) + data)
    raw += pack(">H", _crc16(raw))
    while len(raw) % 16:
        raw.append(0)
    iv = secrets.token_bytes(16)
    enc = bytes([flag]) + iv + AES.new(key, AES.MODE_CBC, iv).encrypt(bytes(raw))
    packets, pos, num = [], 0, 0
    while pos < len(enc):
        head = _varint(num)
        if num == 0:
            head += _varint(len(enc)) + bytes([FRAME_VERSION_BYTE])
        part = enc[pos: pos + max(1, chunk - len(head))]
        packets.append(head + part)
        pos += len(part)
        num += 1
    return packets


class Assembler:
    """Reassemble MTP notifications into one encrypted frame."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.buf = bytearray()
        self.expect = 0
        self.total = 0

    def feed(self, data: bytes) -> bytes | None:
        try:
            num, pos = _unvarint(data, 0)
            if num == 0:
                self.reset()
                self.total, pos = _unvarint(data, pos)
                pos += 1
            elif num != self.expect:
                self.reset()
                return None
        except ValueError:
            self.reset()
            return None
        self.buf += data[pos:]
        self.expect = num + 1
        if self.total and len(self.buf) >= self.total:
            out = bytes(self.buf[: self.total])
            self.reset()
            return out
        return None


def decrypt_frame(payload: bytes, keys: dict[int, bytes]) -> tuple[int, int, int, bytes] | None:
    if len(payload) < 17 or payload[0] not in keys:
        return None
    body = payload[17:]
    if not body or len(body) % 16:
        return None
    raw = AES.new(keys[payload[0]], AES.MODE_CBC, payload[1:17]).decrypt(body)
    if len(raw) < 14:
        return None
    sn, ack_sn, cmd, length = unpack(">IIHH", raw[:12])
    if 12 + length + 2 > len(raw):
        return None
    if _crc16(raw[: 12 + length]) != unpack(">H", raw[12 + length: 14 + length])[0]:
        return None
    return sn, ack_sn, cmd, raw[12: 12 + length]


def device_info_data(mtu_payload: int) -> bytes:
    """The app sends its max packet payload (MTU-3) as uint16 BE (00 f3 = 243)."""
    return pack(">H", mtu_payload)


def parse_device_info(data: bytes) -> dict | None:
    if len(data) < 12:
        return None
    return {
        "version": f"{data[0]}.{data[1]}",
        "protocol": (data[2], data[3]),
        "flags": data[4],
        "bound": data[5] != 0,
        "srand": bytes(data[6:12]),
    }


def pair_data(uuid: str, local_key: str, dev_id: str) -> bytes:
    payload = bytearray(uuid.encode()[:16].ljust(16, b"\x00"))
    payload += local_key[:6].encode()
    payload += dev_id.encode()[:22].ljust(22, b"\x00")
    payload += b"\x00\x01"
    return bytes(payload)


def dp_write_data(dps_sn: int, dp_id: int, dp_type: int, value: bytes) -> bytes:
    """[ver 00][dpsSn:4 BE][dp][type][len:2][value] (app layout)."""
    return b"\x00" + pack(">I", dps_sn) + bytes([dp_id, dp_type]) + pack(">H", len(value)) + value


def parse_report(data: bytes, time_prefix: bool = False) -> tuple[bytes, bool, list[tuple[int, int, bytes]]]:
    """v4 report: [ver][rep_sn:4][b_type][flag] then [dp][type][len:2][value]...

    0x8007 (time_prefix) has a time block after the header: 00 + 13 ascii ms,
    or 01 + 4-byte ts.  Returns (header7, need_ack, dps)."""
    if len(data) < 7:
        return b"", False, []
    head = bytes(data[:7])
    need_ack = (data[5] >> 7) == 0
    i = 7
    if time_prefix and len(data) > 7:
        i += 14 if data[7] == 0 else 5 if data[7] == 1 else 1
    out = []
    while i + 4 <= len(data):
        dp, typ, ln = data[i], data[i + 1], (data[i + 2] << 8) | data[i + 3]
        val = bytes(data[i + 4: i + 4 + ln])
        if len(val) != ln:
            break
        out.append((dp, typ, val))
        i += 4 + ln
    return head, need_ack, out


def report_ack_data(head: bytes) -> bytes:
    return head + b"\x00"


def _tz_hundredths() -> int:
    try:
        from homeassistant.util import dt as dt_util  # follows HA's configured time zone
        off = dt_util.now().utcoffset()
    except ImportError:
        off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() / 36) if off else 0


def time1_data() -> bytes:
    ms = int(time.time()) * 1000
    return str(ms).rjust(13, "0").encode() + pack(">h", _tz_hundredths())


def time2_data() -> bytes:
    t = datetime.now(timezone.utc)
    wday = (t.weekday() + 1) % 7  # Sunday = 0
    return bytes([t.year - 2000, t.month, t.day, t.hour, t.minute, t.second, wday]) + pack(">h", _tz_hundredths())


def time_local_data() -> bytes:
    try:
        from homeassistant.util import dt as dt_util
        t = dt_util.now()
    except ImportError:
        t = datetime.now().astimezone()
    wday = (t.weekday() + 1) % 7
    return bytes([t.year - 2000, t.month, t.day, t.hour, t.minute, t.second, wday]) + pack(">h", _tz_hundredths())


def ble_unlock_value(passcode: str, ts: int) -> bytes:
    return b"\xff\xff\x00\x01" + passcode.encode() + b"\x01" + pack(">I", ts) + b"\x00\x01"


# ---------------------------------------------------------------- credentials
# Tuya BLE lock DPs (Tuya lock SDK 6.10.2 LockV2Utils/OpModeUtils + Tuya "BLE
# lock data point reference").  All raw, written with DP write 0x0027; the lock
# answers with a 0x8006 report of the same DP id.
DP_METHOD_CREATE = 1
DP_METHOD_DELETE = 2
DP_METHOD_MODIFY = 3
DP_TEMP_CREATE = 51
DP_TEMP_DELETE = 52
DP_TEMP_MODIFY = 53

METHOD_PASSWORD = 0x01
METHOD_CARD = 0x02
METHOD_FINGERPRINT = 0x03

STAGE_START = 0x00
STAGE_PROGRESS = 0xFC
STAGE_FAILED = 0xFD
STAGE_CANCELLED = 0xFE
STAGE_DONE = 0xFF

# SDK PERMANENT_TIME: 2000-01-01 .. 2030-12-31, no recurrence
PERMANENT_VALIDITY = bytes.fromhex("386cd30072bc9b7f00000000000000000000")[:17]


def validity_block(start: int, end: int, weekdays: int = 0, from_hm: tuple[int, int] = (0, 0),
                   to_hm: tuple[int, int] = (0, 0)) -> bytes:
    """[start:4][end:4][pattern:1][recur:4][from_hh][from_mm][to_hh][to_mm]; weekly if weekdays set
    (bit 0 = Sunday .. bit 6 = Saturday)."""
    pattern, recur = (0x02, weekdays & 0x7F) if weekdays else (0x00, 0)
    return (pack(">II", start, end) + bytes([pattern]) + pack(">I", recur)
            + bytes([from_hm[0], from_hm[1], to_hm[0], to_hm[1]]))


def pin_digits(code: str) -> bytes:
    """One byte per digit, value 0-9 (not ASCII, not BCD)."""
    if not code.isdigit():
        raise ValueError("PIN must be digits only")
    return bytes(int(c) for c in code)


def temp_pin_create(code: str, validity: bytes, uses: int = 0) -> bytes:
    """DP 51: [type][validity:17][count][len][digits]; type 01 = unlimited in window, 00 = counted."""
    digits = pin_digits(code)
    kind = 0x01 if uses == 0 else 0x00
    return bytes([kind]) + validity + bytes([uses & 0xFF, len(digits)]) + digits


def temp_pin_delete(hw_id: int) -> bytes:
    return bytes([hw_id & 0xFF])


def method_create(method: int, member: int, admin: bool, validity: bytes, code: str | None = None,
                  stage: int = STAGE_START) -> bytes:
    """DP 1: [type][stage][admin][member][hw_id=FF][validity:17][count=00][len][pin]."""
    tail = (bytes([len(code)]) + pin_digits(code)) if code is not None else b"\x01\x01"
    return bytes([method, stage, 0x01 if admin else 0x00, member & 0xFF, 0xFF]) + validity + b"\x00" + tail


def method_delete(method: int, member: int, admin: bool, hw_id: int) -> bytes:
    """DP 2: [type][00][admin][member][hw_id][01]."""
    return bytes([method, 0x00, 0x01 if admin else 0x00, member & 0xFF, hw_id & 0xFF, 0x01])


def parse_method_reply(val: bytes) -> dict | None:
    """DP 1/2 reply: [type][stage][admin][member][hw_id][count][result]..."""
    if len(val) < 6:
        return None
    return {
        "type": val[0], "stage": val[1], "admin": val[2], "member": val[3], "hw_id": val[4],
        "count": val[5], "result": val[6] if len(val) > 6 else None,
    }


def method_cancel(method: int, member: int, admin: bool) -> bytes:
    """DP 1 cancel (SDK OpModeUtils): [type][FE][admin][member][FF][validity][00][01][01]."""
    return bytes([method, STAGE_CANCELLED, 0x01 if admin else 0x00, member & 0xFF, 0xFF]) + PERMANENT_VALIDITY + b"\x00\x01\x01"
