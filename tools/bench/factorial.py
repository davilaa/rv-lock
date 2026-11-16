import urllib.request, json, time, sys, statistics, os
t=open("/config/.ha_token").read().strip(); H={"Authorization":"Bearer "+t,"Content-Type":"application/json"}
def g(e):
    try: return json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8123/api/states/"+e,headers=H),timeout=10))
    except Exception as x: return {"state":"ERR","attributes":{}}
def call(dom,svc,body): urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8123/api/services/%s/%s"%(dom,svc),data=json.dumps(body).encode(),headers=H),timeout=45)
def ha_pid():
    for p in os.listdir("/proc"):
        if p.isdigit():
            try: c=open("/proc/"+p+"/cmdline","rb").read().split(b"\0")
            except Exception: continue
            if b"homeassistant" in c and c[0].endswith(b"python3"): return int(p)
PID=ha_pid()
def set_globals(cached, settle):
    open("/config/.rvl_set.py","w").write("import sys\nm=sys.modules.get('custom_components.rv_lock.session')\nm.PROXY_CACHED_CONNECT=%r\nm.PROXY_SETTLE_CACHED=%r\nopen('/config/.rvl_set.ok','w').write('ok')\n"%(cached,settle))
    try: os.remove("/config/.rvl_set.ok")
    except OSError: pass
    sys.remote_exec(PID,"/config/.rvl_set.py")
    t0=time.time()
    while time.time()-t0<8 and not os.path.exists("/config/.rvl_set.ok"): time.sleep(0.2)
    return os.path.exists("/config/.rvl_set.ok")
PX=os.environ.get("RVL_PROXY_PREFIX","bt_proxy")   # entity-id prefix of the proxy's lock entities
W="sensor.%s_lock_advert_wait"%PX; B="sensor.%s_lock_link_bring_up"%PX; R="sensor.%s_lock_connect_requests"%PX
CONN="binary_sensor.rv_door_lock_connected"
ARMS={"A":("normal","exch",False,False),"B":("normal","skip",False,True),"C":("cached","exch",True,False),"D":("cached","skip",True,True)}
ORDERS=["ABCD","DCBA","BDAC","CADB","ACDB","BDCA"]
rows=[]; stop=None
for rnd,order in enumerate(ORDERS[:int(sys.argv[1])]):
    for arm in order:
        cname,vname,cached,skip=ARMS[arm]
        t0=time.time()
        while time.time()-t0<70 and g(CONN)["state"]!="off": time.sleep(2)
        if not set_globals(cached,0.10): stop="STOP: could not set mode"; break
        call("esphome","bt_proxy_lock_skip_version",{"skip": skip})
        time.sleep(2.5+(rnd*1.3)%3)
        before=g(W).get("last_updated")
        tp=time.time(); call("button","press",{"entity_id":"button.rv_door_lock_reconnect"})
        while time.time()-tp<45 and g(CONN)["state"]!="on": time.sleep(0.4)
        err=g(CONN)["attributes"].get("last_error")
        if err and "handshake" in str(err):
            stop="STOP: %s (arm %s %s)"%(err,cname,vname); print(time.strftime("%H:%M:%S"),stop,flush=True); break
        t1=time.time()
        while time.time()-t1<12 and g(W).get("last_updated")==before: time.sleep(0.5)
        fresh=g(W).get("last_updated")!=before
        time.sleep(1.0)
        w=g(W); b=g(B); r=g(R); c=g(CONN)["attributes"]
        try: wv=float(w["state"]); bv=float(b["state"]); rv=float(r["state"]); cs=float(c.get("connect_s")); hs=float(c.get("handshake_s"))
        except Exception: fresh=False
        if not fresh: print(time.strftime("%H:%M:%S"),arm,"no fresh timing",flush=True); continue
        after_open=cs*1000-wv-bv
        total=bv+after_open+100+hs*1000      # LE connection complete -> session up (settle is 0.10 s in every arm)
        rows.append((arm,wv,bv,rv,after_open,hs*1000,total))
        print("%s %s %-6s %-4s wait %5.0f req %d | bring-up %4.0f  after-open %4.0f  login %4.0f | link-start->ready %5.0f ms"%(time.strftime("%H:%M:%S"),arm,cname,vname,wv,rv,bv,after_open,hs*1000,total),flush=True)
    if stop: break
set_globals(True,0.10); call("esphome","bt_proxy_lock_skip_version",{"skip": True})
def summ(label,rr):
    if rr: print("%-26s n=%2d  bring-up %4.0f  after-open %4.0f  login %4.0f  link-start->ready mean %5.0f median %5.0f (min %4.0f max %5.0f)"%(label,len(rr),statistics.mean(x[2] for x in rr),statistics.mean(x[4] for x in rr),statistics.mean(x[5] for x in rr),statistics.mean(x[6] for x in rr),statistics.median(x[6] for x in rr),min(x[6] for x in rr),max(x[6] for x in rr)))
print("--- per arm (ms)")
for a,(cn,vn,_,_) in ARMS.items(): summ("%s %s + version %s"%(a,cn,vn),[x for x in rows if x[0]==a])
print("--- main effects")
summ("version exchanged (A+C)",[x for x in rows if x[0] in "AC"]); summ("version skipped (B+D)",[x for x in rows if x[0] in "BD"])
summ("normal connect (A+B)",[x for x in rows if x[0] in "AB"]); summ("cached connect (C+D)",[x for x in rows if x[0] in "CD"])
print(stop or "DONE")
