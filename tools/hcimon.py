#!/usr/bin/env python3
"""Passive HCI monitor (read-only) for one peer: decodes connect/update/disconnect,
L2CAP conn-param signalling and ATT opcodes on the lock's link.  Usage: hcimon.py SECONDS LOCK_ADDRESS"""
import socket, struct, sys, time
import os
PEER = bytes.fromhex((sys.argv[2] if len(sys.argv) > 2 else os.environ.get("LOCK_MAC", "00:00:00:00:00:00")).replace(":", ""))[::-1]  # the lock, little-endian on the wire
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 300
s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
s.bind((0xFFFF, 2))  # HCI_DEV_NONE, HCI_CHANNEL_MONITOR
s.settimeout(1.0)
t0 = time.time(); handles = set(); pending_cmd = {}; acl = {}; tlast = time.time()
ATT = {0x01:"ErrorRsp",0x02:"MtuReq",0x03:"MtuRsp",0x08:"ReadByTypeReq",0x09:"ReadByTypeRsp",0x10:"ReadByGroupReq",0x11:"ReadByGroupRsp",0x12:"WriteReq",0x13:"WriteRsp",0x52:"WriteCmd",0x1b:"Notify",0x0a:"ReadReq",0x0b:"ReadRsp",0x04:"FindInfoReq",0x05:"FindInfoRsp"}
def log(msg): print("%8.3f %s" % (time.time()-t0, msg), flush=True)
def u16(b, o): return struct.unpack_from("<H", b, o)[0]
while time.time() - t0 < secs:
    try: pkt = s.recv(2048)
    except socket.timeout: continue
    if len(pkt) < 6: continue
    op, idx, ln = struct.unpack_from("<HHH", pkt, 0); p = pkt[6:6+ln]
    if op == 2 and len(p) >= 3:  # command
        opc = u16(p, 0)
        if opc in (0x2013, 0x0406, 0x200d, 0x2043):
            if opc == 0x2013: log("CMD LE_Conn_Update handle=%d int=%d-%d lat=%d to=%d" % (u16(p,3), u16(p,5), u16(p,7), u16(p,9), u16(p,11)))
            elif opc == 0x0406: log("CMD Disconnect handle=%d reason=0x%02x" % (u16(p,3), p[5]))
            else: log("CMD opcode=0x%04x" % opc)
    elif op == 3 and len(p) >= 2:  # event
        evt, plen, d = p[0], p[1], p[2:]
        if evt == 0x05 and len(d) >= 4:
            h = u16(d, 1)
            if h in handles: log("EVT Disconnect handle=%d reason=0x%02x" % (h, d[3])); handles.discard(h)
        elif evt == 0x3E and d:
            sub = d[0]
            if sub in (0x01, 0x0A) and len(d) >= 19:
                h = u16(d, 2); addr = d[6:12]; o = 12 if sub == 0x01 else 24  # 0x0A carries 2 extra RPAs
                who = "LOCK" if addr == PEER else addr[::-1].hex(":")
                if addr == PEER: handles.add(h)
                log("EVT LE_Conn_Complete %s handle=%d status=%d int=%d lat=%d to=%d" % (who, h, d[1], u16(d,o), u16(d,o+2), u16(d,o+4)))
            elif sub == 0x03 and len(d) >= 10:
                h = u16(d, 2)
                if h in handles: log("EVT LE_Conn_Update_Complete handle=%d status=%d int=%d lat=%d to=%d" % (h, d[1], u16(d,4), u16(d,6), u16(d,8)))
            elif sub == 0x07 and len(d) >= 11:
                h = u16(d, 1)
                if h in handles: log("EVT LE_Data_Length_Change handle=%d maxTx=%d maxRx=%d" % (h, u16(d,3), u16(d,7)))
            elif sub == 0x06 and len(d) >= 11:
                h = u16(d, 1)
                if h in handles: log("EVT LE_Remote_Conn_Param_Request handle=%d int=%d-%d lat=%d to=%d" % (h, u16(d,3), u16(d,5), u16(d,7), u16(d,9)))
        elif evt == 0x13 and len(d) >= 5:  # Number of Completed Packets = peer ACKed our TX
            n = d[0]
            for i in range(n):
                h = u16(d, 1 + 4*i); c = u16(d, 3 + 4*i)
                if h in handles: log("EVT TX_acked handle=%d n=%d" % (h, c))
        elif evt == 0x0E and len(d) >= 4:
            opc = u16(d, 1)
            if opc in (0x2013, 0x2020): log("EVT CmdComplete opcode=0x%04x status=0x%02x" % (opc, d[3]))
        elif evt == 0x0F and len(d) >= 4:
            opc = u16(d, 2)
            if opc in (0x2013, 0x0406): log("EVT CmdStatus opcode=0x%04x status=0x%02x" % (opc, d[0]))
    elif op in (4, 5) and len(p) >= 8:  # ACL tx/rx
        h = u16(p, 0) & 0x0FFF
        acl[h] = acl.get(h, 0) + 1
        if time.time() - tlast >= 30:
            tlast = time.time(); log("ACL pkts/30s per handle: %s" % acl); acl.clear()
        if h not in handles: continue
        pb = (u16(p, 0) >> 12) & 3; cid = u16(p, 6); body = p[8:]
        dirn = "TX" if op == 4 else "RX"
        if pb == 1:  # continuation fragment of an L2CAP PDU
            log("ACL %s cont-fragment len=%d" % (dirn, u16(p, 2))); continue
        if cid == 4 and body:
            log("ACL %s ATT %s %s" % (dirn, ATT.get(body[0], "0x%02x" % body[0]), body[:8].hex()))
        elif cid == 5 and len(body) >= 4:
            code = body[0]
            if code == 0x12: log("ACL %s L2CAP ConnParamUpdateReq int=%d-%d lat=%d to=%d" % (dirn, u16(body,4), u16(body,6), u16(body,8), u16(body,10)))
            elif code == 0x13: log("ACL %s L2CAP ConnParamUpdateRsp result=%d" % (dirn, u16(body,4)))
            else: log("ACL %s L2CAP sig code=0x%02x" % (dirn, code))
log("END")
