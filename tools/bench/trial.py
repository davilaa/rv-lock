import urllib.request, json, time, sys, statistics, os
t=open("/config/.ha_token").read().strip(); H={"Authorization":"Bearer "+t,"Content-Type":"application/json"}
def g(e):
    try: return json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8123/api/states/"+e,headers=H),timeout=10))
    except Exception as x: return {"state":"ERR","attributes":{}}
def call(dom,svc,body): urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8123/api/services/%s/%s"%(dom,svc),data=json.dumps(body).encode(),headers=H),timeout=45)
PX=os.environ.get("RVL_PROXY_PREFIX","bt_proxy")   # entity-id prefix of the proxy's lock entities
W="sensor.%s_lock_advert_wait"%PX; B="sensor.%s_lock_link_bring_up"%PX; R="sensor.%s_lock_connect_requests"%PX
CONN="binary_sensor.rv_door_lock_connected"
N=int(sys.argv[1]); arms=sys.argv[2].split(",") if len(sys.argv)>2 else ["x"]   # arms: "x" = no toggle, "off"/"on" = version skip
rows=[]; stop=None
for i in range(N):
    for arm in arms:
        if arm in ("on","off"):
            t0=time.time()
            while time.time()-t0<70 and g(CONN)["state"]!="off": time.sleep(2)
            call("esphome","bt_proxy_lock_skip_version",{"skip": arm=="on"})
        t0=time.time()
        while time.time()-t0<70 and g(CONN)["state"]!="off": time.sleep(2)
        time.sleep(3+(i*1.7)%4)
        before=g(W).get("last_updated"); err0=g(CONN)["attributes"].get("last_error")
        tp=time.time(); call("button","press",{"entity_id":"button.rv_door_lock_reconnect"})
        while time.time()-tp<45 and g(CONN)["state"]!="on": time.sleep(0.4)
        up=time.time()-tp
        st=g(CONN); err=st["attributes"].get("last_error")
        if st["state"]!="on" or (err and "handshake" in str(err)):
            # could be the instant idle-park artifact after a slow connect; a handshake error is the real stop
            if err and "handshake" in str(err):
                stop="STOP: %s"%err; print(time.strftime("%H:%M:%S"),arm,stop,flush=True); break
        t1=time.time()
        while time.time()-t1<12 and g(W).get("last_updated")==before: time.sleep(0.5)
        w=g(W); b=g(B); r=g(R); c=g(CONN)["attributes"]; fresh=w.get("last_updated")!=before
        try:
            wv=float(w["state"]); bv=float(b["state"]); rv=float(r["state"]); cs=float(c.get("connect_s")); hs=float(c.get("handshake_s"))
        except Exception: wv=bv=rv=cs=hs=None
        rem = None if (not fresh or cs is None) else cs*1000-wv-bv
        rows.append((arm,wv if fresh else None,bv if fresh else None,rv if fresh else None,cs,hs,rem))
        print("%s %-4s wait %6s  bring-up %5s  req %s  connect %s s  login %s s  after-open %s ms  (ready %.1f s)"%(time.strftime("%H:%M:%S"),arm,wv if fresh else "?",bv if fresh else "?",rv if fresh else "?",cs,hs,("%.0f"%rem) if rem is not None else "?",up),flush=True)
    if stop: break
print("--- summary")
for arm in arms:
    rr=[x for x in rows if x[0]==arm and x[1] is not None]
    if rr:
        print("%-4s n=%d  bring-up mean %.0f (min %.0f max %.0f)  after-open mean %.0f  login mean %.2f  connect mean %.2f  wait mean %.0f  requests>1: %d"%(arm,len(rr),statistics.mean(x[2] for x in rr),min(x[2] for x in rr),max(x[2] for x in rr),statistics.mean(x[6] for x in rr),statistics.mean(x[5] for x in rr),statistics.mean(x[4] for x in rr),statistics.mean(x[1] for x in rr),sum(1 for x in rr if x[3]>1)))
print(stop or "DONE")
