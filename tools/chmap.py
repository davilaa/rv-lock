# Read-only: LE Read Channel Map (OGF 0x08, OCF 0x0015) for the given connection handles via a RAW HCI socket.
import socket, struct, sys, time
handles=[int(x) for x in sys.argv[1:]] or [16]
s=socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
s.bind((0,))  # hci0, HCI_CHANNEL_RAW
# HCI filter: events only, Command Complete (0x0E) + Command Status (0x0F)
type_mask=1<<4; event_mask=(1<<0x0E)|(1<<0x0F)
flt=struct.pack("<IIIH", type_mask, event_mask & 0xffffffff, event_mask>>32, 0)+b"\x00\x00"
try:
    s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, flt)
except OSError as e:
    print("filter16 failed", e); s.setsockopt(socket.SOL_HCI, 2, flt[:14])
s.settimeout(3)
def cmd(opcode, params):
    s.send(bytes([0x01])+struct.pack("<HB", opcode, len(params))+params)
    t0=time.time()
    while time.time()-t0<3:
        r=s.recv(260)
        if r[0]==4 and r[1]==0x0E and struct.unpack_from("<H", r, 4)[0]==opcode:
            return r[6:]
    return None
for h in handles:
    r=cmd(0x2015, struct.pack("<H", h))
    if r is None: print("handle", h, "no reply"); continue
    status=r[0]
    if len(r) < 8:
        print("handle", h, "status 0x%02x (no map)" % status); continue
    hh=struct.unpack_from("<H", r, 1)[0]; cm=r[3:8]
    bits=int.from_bytes(cm, "little")
    used=[i for i in range(37) if bits>>i & 1]
    print("handle %d status 0x%02x channel_map=%s used=%d/37 unused=%s" % (hh, status, cm.hex(), len(used), [i for i in range(37) if not bits>>i & 1]))
# LE Read Supported States / features for info
r=cmd(0x2003, b"")  # LE Read Local Supported Features
if r: print("LE features:", r[1:9].hex())
r=cmd(0x2002, b"")  # LE Read Buffer Size
if r: print("LE buffer: status %d, ACL data pkt len %d, num pkts %d" % (r[0], struct.unpack_from("<H", r, 1)[0], r[3]))
