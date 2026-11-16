# LE Set Host Channel Classification (OGF 0x08 OCF 0x0014): hop only on the BLE data channels given on argv
# (or 'all' to restore all 37).  Then read back the map of handle 17 after the update instant.
import socket, struct, sys, time
arg=sys.argv[1] if len(sys.argv)>1 else "all"
chans=list(range(37)) if arg=="all" else [int(x) for x in arg.split(",")]
bits=0
for c in chans: bits|=1<<c
cm=bits.to_bytes(5,"little")
s=socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI); s.bind((0,))
flt=struct.pack("<IIIH", 1<<4, ((1<<0x0E)|(1<<0x0F)) & 0xffffffff, 0, 0)+b"\x00\x00"
s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, flt); s.settimeout(3)
def cmd(opcode, params):
    s.send(bytes([0x01])+struct.pack("<HB", opcode, len(params))+params)
    t0=time.time()
    while time.time()-t0<3:
        r=s.recv(260)
        if r[0]==4 and r[1]==0x0E and struct.unpack_from("<H", r, 4)[0]==opcode: return r[6:]
        if r[0]==4 and r[1]==0x0F and struct.unpack_from("<H", r, 5)[0]==opcode: return bytes([r[3]])
    return None
r=cmd(0x2014, cm)
print("set host channel classification %s (%d channels) -> status %s" % (cm.hex(), len(chans), None if r is None else "0x%02x" % r[0]))
time.sleep(3)
for h in (17,16):
    r=cmd(0x2015, struct.pack("<H", h))
    if r and len(r)>=8:
        bits=int.from_bytes(r[3:8],"little"); print("handle %d map=%s used=%s" % (h, r[3:8].hex(), [i for i in range(37) if bits>>i&1]))
    elif r: print("handle %d status 0x%02x" % (h, r[0]))
