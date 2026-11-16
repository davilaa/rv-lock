import struct,subprocess
E="/config/esphome/.esphome/build/bt-proxy/build/firmware.elf"
NM="/root/.cache/esphome/idf/tools/xtensa-esp-elf/esp-14.2.0_20260121/xtensa-esp-elf/bin/xtensa-esp32-elf-nm"
d=open(E,"rb").read()
phoff=struct.unpack_from("<I",d,0x1c)[0]; phentsize,phnum=struct.unpack_from("<HH",d,0x2a)
segs=[]
for i in range(phnum):
    t,off,va,pa,fsz,msz,fl,al=struct.unpack_from("<IIIIIIII",d,phoff+i*phentsize)
    if t==1 and fsz: segs.append((va,off,fsz,fl))
syms=[]
for l in subprocess.run([NM,"-n",E],capture_output=True,text=True).stdout.splitlines():
    p=l.split()
    if len(p)==3 and p[1] in "tT": syms.append((int(p[0],16),p[2]))
import bisect
addrs=[a for a,_ in syms]
def owner(a):
    i=bisect.bisect_right(addrs,a)-1
    return syms[i][1] if i>=0 else "?"
N={"__wrap_btsnd_hcic_ble_upd_ll_conn_params":"wrap_upd","btsnd_hcic_ble_upd_ll_conn_params":"real_upd","__wrap_btsnd_hcic_ble_create_ll_conn":"wrap_create","btsnd_hcic_ble_create_ll_conn":"real_create","__wrap_l2cble_conn_comp":"wrap_conncomp","l2cble_conn_comp":"real_conncomp","__wrap_btsnd_hcic_rmt_ver_req":"wrap_ver","btsnd_hcic_rmt_ver_req":"real_ver","__wrap_btm_ble_read_remote_features_complete":"wrap_feat","btm_ble_read_remote_features_complete":"real_feat"}
T={N[n]:a for a,n in syms if n in N}
inv={v:k for k,v in T.items()}
hits={k:set() for k in T}
for va,off,fsz,fl in segs:
    if not (fl & 1): continue          # executable segments only
    blob=d[off:off+fsz]
    for i in range(0,fsz-3):
        b0=blob[i]
        if (b0 & 0x3f)==0x25:           # call8
            w=b0|(blob[i+1]<<8)|(blob[i+2]<<16)
            o=w>>6
            if o & 0x20000: o-=0x40000
            pc=va+i
            tgt=((pc & ~3)+4+(o<<2)) & 0xffffffff
            if tgt in inv: hits[inv[tgt]].add("call8 in "+owner(pc))
    # literals (l32r + callx8)
    for i in range(0,fsz-3):
        v=struct.unpack_from("<I",blob,i)[0]
        if v in inv and (va+i)%4==0: hits[inv[v]].add("literal near "+owner(va+i))
for k in T: print(k,hex(T[k]),sorted(hits[k]))
