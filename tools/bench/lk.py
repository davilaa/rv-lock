import urllib.request,json,time,sys,os
PX=os.environ.get("RVL_PROXY_PREFIX","bt_proxy")   # entity-id prefix of the proxy's lock entities
t=open("/config/.ha_token").read().strip(); H={"Authorization":"Bearer "+t,"Content-Type":"application/json"}
def g(e):
    try: return json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8123/api/states/"+e,headers=H),timeout=10)).get("state")
    except Exception as x: return "ERR"
def call(dom,svc,body,to=45): urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8123/api/services/%s/%s"%(dom,svc),data=json.dumps(body).encode(),headers=H),timeout=to)
SEL="select.%s_lock_link_interval"%PX; IDLE="switch.rv_door_lock_idle_disconnect_battery_saver"; CONN="binary_sensor.rv_door_lock_connected"; NUM="number.rv_door_lock_idle_timeout"
def now(): return time.strftime("%H:%M:%S")
def state():
    print(now(),"conn=%s lock=%s interval=%s idle=%s timeout=%s fast=%s signal=%s batt=%s since=%s"%(g(CONN),g("lock.rv_door_lock"),g(SEL),g(IDLE),g(NUM),g("switch.%s_lock_fast_link"%PX),g("sensor.rv_door_lock_signal"),g("sensor.rv_door_lock_battery"),g("sensor.rv_door_lock_connected_since")))
def wait_conn(want,timeout):
    t0=time.time()
    while time.time()-t0<timeout:
        if g(CONN)==want: print(now(),"conn=%s after %.0fs"%(want,time.time()-t0)); return True
        time.sleep(2)
    print(now(),"TIMEOUT waiting conn=%s (%ds), is %s"%(want,timeout,g(CONN))); return False
def cmd(svc):
    tt=time.time(); print(now(),svc,"sent")
    try: call("lock",svc,{"entity_id":"lock.rv_door_lock"}); r="%.2fs"%(time.time()-tt)
    except Exception as e: r="FAIL(%s) %.2fs"%(getattr(e,'code',e),time.time()-tt)
    print(now(),svc,"->",r,"state="+str(g("lock.rv_door_lock")))
a=sys.argv[1:]; i=0
while i<len(a):
    c=a[i]
    if c=="state": state()
    elif c=="debug": call("logger","set_level",{"custom_components.rv_lock":a[i+1]}); print("rv_lock log level",a[i+1]); i+=1
    elif c=="interval": call("select","select_option",{"entity_id":SEL,"option":a[i+1]+" ms"}); time.sleep(1); print(now(),"interval ->",g(SEL)); i+=1
    elif c=="idle": call("switch","turn_"+a[i+1],{"entity_id":IDLE}); time.sleep(1); print(now(),"idle ->",g(IDLE)); i+=1
    elif c=="timeout": call("number","set_value",{"entity_id":NUM,"value":float(a[i+1])}); time.sleep(1); print(now(),"idle timeout ->",g(NUM)); i+=1
    elif c=="tune": call("esphome","bt_proxy_lock_link_tune",{"peer_hold":a[i+1]=="hold","honour_ms":int(a[i+2]),"login_fast_ms":int(a[i+3])}); print(now(),"tune peer=%s honour=%s login=%s"%(a[i+1],a[i+2],a[i+3])); i+=3
    elif c=="radio":   # radio <boost 0-2> <wifi_pct> <hold 0|1> <init_units>   (until the proxy reboots)
        call("esphome","bt_proxy_lock_radio_test",{"boost":int(a[i+1]),"wifi_pct":int(a[i+2]),"hold":int(a[i+3]),"init_units":int(a[i+4])}); print(now(),"radio test",a[i+1:i+5]); i+=4
    elif c=="skipver": call("esphome","bt_proxy_lock_skip_version",{"skip":a[i+1]=="on"}); print(now(),"version skip",a[i+1]); i+=1   # until the proxy reboots
    elif c=="reconnect": print(now(),"reconnect pressed"); call("button","press",{"entity_id":"button.rv_door_lock_reconnect"})
    elif c=="wait": ok=wait_conn(a[i+1],int(a[i+2])); i+=2
    elif c=="sleep": time.sleep(float(a[i+1])); i+=1
    elif c=="watch":   # watch <seconds> <step>: print when conn or connected_since changes
        end=time.time()+float(a[i+1]); step=float(a[i+2]); last=None; n=0
        while time.time()<end:
            cur=(g(CONN),g("sensor.rv_door_lock_connected_since")); n+=1
            if cur!=last: print(now(),"conn=%s since=%s"%cur); last=cur
            time.sleep(step)
        print(now(),"watch done, %d samples, final conn=%s"%(n,g(CONN))); i+=2
    elif c in("lock","unlock"): cmd(c)
    i+=1
