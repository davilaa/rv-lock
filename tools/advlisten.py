#!/usr/bin/env python3
"""Listen-only: count LE advertising reports the kernel is already receiving on hciN (sends nothing).
usage: advlisten.py [hciN=0] [seconds=60] [addr ...]"""
import socket, struct, sys, time, collections
dev = int(sys.argv[1]) if len(sys.argv) > 1 else 0
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60
want = [a.upper() for a in sys.argv[3:]]
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
s.bind((dev,))
s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, struct.pack("<LLLHxx", 1 << 4, 0xffffffff, 0xffffffff, 0))
seen = collections.defaultdict(list); end = time.time() + secs; s.settimeout(1)
while time.time() < end:
    try: p = s.recv(300)
    except socket.timeout: continue
    if p[1] != 0x3e: continue
    if p[3] == 0x0d:
        o = 5
        for _ in range(p[4]):
            addr = ":".join("%02X" % b for b in p[o+3:o+9][::-1]); rssi = struct.unpack("b", p[o+13:o+14])[0]; et = struct.unpack("<H", p[o:o+2])[0]
            ln = p[o+23]; o += 24 + ln
            if not et & 0x08: seen[addr].append(rssi)  # skip scan responses
    elif p[3] == 0x02:
        o = 5
        for _ in range(p[4]):
            addr = ":".join("%02X" % b for b in p[o+2:o+8][::-1]); ln = p[o+8]
            rssi = struct.unpack("b", p[o+9+ln:o+10+ln])[0]; et = p[o]; o += 10 + ln
            if et != 4: seen[addr].append(rssi)  # skip scan responses
for a, v in sorted(seen.items(), key=lambda kv: -len(kv[1])):
    if want and a not in want: continue
    r = sorted(v); print(a, "n=%d" % len(v), "rssi min/med/max %d/%d/%d" % (r[0], r[len(r)//2], r[-1]))
print(len(seen), "addresses total")
