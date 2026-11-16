import socket, struct, sys, time, collections
# 1) mgmt Read Default System Configuration (0x004a) on hci0 -- read-only
try:
    c = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    c.bind((0xFFFF, 3))  # HCI_CHANNEL_CONTROL
    c.settimeout(3)
    c.send(struct.pack("<HHH", 0x004a, 0, 0))
    t0=time.time()
    while time.time()-t0 < 3:
        r = c.recv(1024)
        ev, idx, ln = struct.unpack_from("<HHH", r, 0)
        if ev == 0x0001:  # cmd complete
            opc, status = struct.unpack_from("<HB", r, 6)
            if opc == 0x004a:
                p = r[9:]
                o = 0
                names={0x0:'BR page scan type',0x1:'BR page scan int',0x2:'BR page scan win',0x3:'BR inq scan type',0x4:'BR inq scan int',0x5:'BR inq scan win',0x6:'BR link supv to',0x7:'LE adv min',0x8:'LE adv max',0x9:'LE multi adv rot',0xa:'LE scan int autoconn',0xb:'LE scan win autoconn',0xc:'LE scan int wake',0xd:'LE scan win wake',0xe:'LE scan int discovery',0xf:'LE scan win discovery',0x10:'LE scan int adv-mon',0x11:'LE scan win adv-mon',0x12:'LE scan int connect',0x13:'LE scan win connect',0x14:'LE conn min int',0x15:'LE conn max int',0x16:'LE conn latency',0x17:'LE supv timeout',0x18:'LE autoconn timeout',0x19:'LE adv mon rotation',0x1a:'LE enable adv mon',0x1b:'LE adv mon rot dur',0x1c:'LE adv mon rot wake'}
                while o+3 <= len(p):
                    typ, l = struct.unpack_from("<HB", p, o); val = p[o+3:o+3+l]
                    v = int.from_bytes(val, "little")
                    print("sysconfig 0x%04x %-24s = %d (0x%x)" % (typ, names.get(typ,'?'), v, v)); o += 3+l
                break
    c.close()
except Exception as e:
    print("mgmt read failed:", e)
# 2) passive monitor: per-handle ACL RX inter-arrival + conn events
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30
LOCK = (sys.argv[2] if len(sys.argv) > 2 else "").upper()   # the lock's Bluetooth address
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
s.bind((0xFFFF, 2)); s.settimeout(1.0)
t0=time.time(); last={}; deltas=collections.defaultdict(list); cnt=collections.Counter()
def u16(b,o): return struct.unpack_from("<H",b,o)[0]
while time.time()-t0 < secs:
    try: pkt=s.recv(2048)
    except socket.timeout: continue
    if len(pkt)<6: continue
    op, idx, ln = struct.unpack_from("<HHH", pkt, 0); p=pkt[6:6+ln]
    now=time.time()
    if op in (4,5) and len(p)>=4:
        h=u16(p,0)&0x0FFF; d="TX" if op==4 else "RX"
        cnt[(h,d)]+=1
        if d=="RX":
            if h in last: deltas[h].append(now-last[h])
            last[h]=now
    elif op==3 and len(p)>=2:
        evt=p[0]; d=p[2:]
        if evt==0x3E and d:
            sub=d[0]
            if sub in (0x01,0x0A) and len(d)>=19:
                o=12 if sub==0x01 else 24
                print("%7.2f LE_Conn_Complete handle=%d status=%d addr=%s int=%d to=%d"%(now-t0,u16(d,2),d[1],d[6:12][::-1].hex(':'),u16(d,o),u16(d,o+4)))
            elif sub==0x03 and len(d)>=10:
                print("%7.2f LE_Conn_Update_Complete handle=%d status=%d int=%d lat=%d to=%d"%(now-t0,u16(d,2),d[1],u16(d,4),u16(d,6),u16(d,8)))
            elif sub==0x02 and len(d)>=12:
                # advertising report: count adverts from the lock
                n=d[1]; 
                # addr at d[4:10] for first report
                addr=d[4:10][::-1].hex(':').upper()
                if addr==LOCK:
                    rssi=struct.unpack_from("<b", d, len(d)-1)[0]
                    print("%7.2f ADV from LOCK rssi=%d"%(now-t0,rssi))
            elif sub==0x0D and len(d)>=20:
                addr=d[4+2+1:4+2+1+6]
                # ext adv report: evt_type(2) addr_type(1) addr(6) ...
                addr=d[5:11][::-1].hex(':').upper()
                if addr==LOCK:
                    rssi=struct.unpack_from("<b", d, 16)[0]
                    print("%7.2f EXTADV from LOCK rssi=%d"%(now-t0,rssi))
        elif evt==0x05 and len(d)>=4:
            print("%7.2f Disconnect handle=%d reason=0x%02x"%(now-t0,u16(d,1),d[3]))
print("ACL counts over %.0fs:"%secs, dict(cnt))
for h,dl in deltas.items():
    dl=sorted(dl); n=len(dl)
    if n<5: continue
    import statistics
    # histogram in ms buckets
    hist=collections.Counter(round(x*1000/5)*5 for x in dl)
    top=hist.most_common(6)
    print("handle %d RX n=%d min=%.1fms p10=%.1fms med=%.1fms p90=%.1fms top5ms-buckets=%s"%(h,n,dl[0]*1000,dl[n//10]*1000,dl[n//2]*1000,dl[9*n//10]*1000,top))
