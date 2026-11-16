import socket, struct, time
s=socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI); s.bind((0,))
flt=struct.pack("<IIIH", 1<<4, ((1<<0x0E)|(1<<0x0F)) & 0xffffffff, 0, 0)+b"\x00\x00"
s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, flt); s.settimeout(2)
def cmd(opcode, params=b""):
    s.send(bytes([0x01])+struct.pack("<HB", opcode, len(params))+params)
    t0=time.time()
    while time.time()-t0<2:
        try: r=s.recv(260)
        except socket.timeout: return None
        if r[0]==4 and r[1]==0x0E and struct.unpack_from("<H", r, 4)[0]==opcode: return r[6:]
    return None
t_end=time.time()+900
while time.time()<t_end:
    out=[]
    for h in (17,16):
        r=cmd(0x2015, struct.pack("<H", h))
        if r and len(r)>=8:
            b=int.from_bytes(r[3:8],"little"); out.append("h%d=%d" % (h, bin(b).count("1")))
        else: out.append("h%d=-" % h)
    print(time.strftime("%H:%M:%S"), " ".join(out), flush=True)
    time.sleep(5)
