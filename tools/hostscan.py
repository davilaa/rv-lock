#!/usr/bin/env python3
"""Passive LE scan on a raw HCI socket (no bluetoothd needed). Prints RSSI stats per address.
usage: hostscan.py [hciN=0] [seconds=30] [addr-filter ...]"""
import socket, struct, sys, time, fcntl, collections
dev = int(sys.argv[1]) if len(sys.argv) > 1 else 0
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 30
want = [a.upper() for a in sys.argv[3:]]
HCIDEVUP, HCIDEVDOWN = 0x400448C9, 0x400448CA
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
was_down = False
try:
    fcntl.ioctl(s.fileno(), HCIDEVUP, dev); was_down = True
except OSError as e:
    if e.errno != 114: raise  # EALREADY
s.bind((dev,))
s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, struct.pack("<LLLHxx", 1 << 4, 0xffffffff, 0xffffffff, 0))
def cmd(ogf, ocf, data=b""):
    op = (ogf << 10) | ocf
    s.send(struct.pack("<BHB", 1, op, len(data)) + data)
    t = time.time() + 2
    while time.time() < t:
        p = s.recv(260)
        if p[1] == 0x0e and struct.unpack("<H", p[4:6])[0] == op: return p[6:]
        if p[1] == 0x0f and struct.unpack("<H", p[5:7])[0] == op: return p[3:4]
    return None
ext = False
r = cmd(8, 0x0b, struct.pack("<BHHBB", 0, 0x60, 0x60, 0, 0))
if r and r[0] == 0x0c:  # controller is in extended mode
    ext = True
    print("ext scan params", cmd(8, 0x41, struct.pack("<BBBBHH", 0, 0, 1, 0, 0x60, 0x60)))
    print("ext scan on", cmd(8, 0x42, struct.pack("<BBHH", 1, 0, 0, 0)))
else:
    print("scan on", cmd(8, 0x0c, b"\x01\x00"))
seen = collections.defaultdict(list); end = time.time() + secs
s.settimeout(1)
while time.time() < end:
    try: p = s.recv(300)
    except socket.timeout: continue
    if p[1] == 0x3e and p[3] == 0x0d:
        n = p[4]; o = 5
        for _ in range(n):
            et = struct.unpack("<H", p[o:o+2])[0]; addr = ":".join("%02X" % b for b in p[o+3:o+9][::-1])
            rssi = struct.unpack("b", p[o+13:o+14])[0]; ln = p[o+23]; o += 24 + ln
            seen[addr].append((time.time(), rssi, et))
        continue
    if p[1] != 0x3e or p[3] != 0x02: continue
    n = p[4]; o = 5
    for _ in range(n):
        et, at = p[o], p[o+1]; addr = ":".join("%02X" % b for b in p[o+2:o+8][::-1]); ln = p[o+8]
        rssi = struct.unpack("b", p[o+9+ln:o+10+ln])[0]; o += 10 + ln
        seen[addr].append((time.time(), rssi, et))
s.settimeout(None)
print("scan off", cmd(8, 0x42, struct.pack("<BBHH", 0, 0, 0, 0)) if ext else cmd(8, 0x0c, b"\x00\x00"))
for a, v in sorted(seen.items(), key=lambda kv: -len(kv[1])):
    if want and a not in want: continue
    r = sorted(x[1] for x in v)
    print(a, "n=%d" % len(v), "rssi min/med/max %d/%d/%d" % (r[0], r[len(r)//2], r[-1]))
if not want: print(len(seen), "addresses")
if was_down: fcntl.ioctl(s.fileno(), HCIDEVDOWN, dev)
