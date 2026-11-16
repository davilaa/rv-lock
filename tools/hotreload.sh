#!/bin/sh
# Load changed rv_lock .py files into the RUNNING Home Assistant without `ha core restart`
# (a restart interrupts every other integration; this only reloads the rv_lock entry).
# 1. copy the changed files to HAOS /config/custom_components/rv_lock/ (keep a .bak-* first)
# 2. run:  sh tools/hotreload.sh   (site values in tools/local.env, see local.env.example)
# It injects tools/hotreload_inject.py into the HA process with Python 3.14's sys.remote_exec:
# unload the rv_lock entry -> importlib.reload every rv_lock module -> set the entry up again.
# Result is appended to HAOS /config/.rvl_hotreload.log ("reloaded; setup ok=True").
# Services registered at first setup keep their original closures (they look the session up at
# call time, so they still work); a change to __init__.py's service code needs a real restart.
set -e
here=$(dirname "$0")
B=$(base64 -w0 "$here/hotreload_inject.py")
cat > /tmp/rvl_hotreload_run.sh <<EOS
# Pre-flight: import every rv_lock module in a SEPARATE python process first.  A module that
# fails at import would otherwise leave the entry
# unloaded and every lock entity unavailable until the file is fixed.
docker exec homeassistant python3 -c "
import sys, importlib, pkgutil
sys.path.insert(0, '/config')
import custom_components.rv_lock as pkg
for m in pkgutil.iter_modules(pkg.__path__):
    importlib.import_module('custom_components.rv_lock.' + m.name)
print('preflight import ok')
" || { echo 'PREFLIGHT FAILED - not reloading'; exit 1; }
echo $B | base64 -d > /mnt/data/supervisor/homeassistant/.rvl_hotreload.py
docker exec homeassistant python3 -c "
import os, sys
pid = None
for p in os.listdir('/proc'):
    if p.isdigit():
        try: c = open('/proc/' + p + '/cmdline', 'rb').read().split(b'\\\\0')
        except Exception: continue
        if b'homeassistant' in c and c[0].endswith(b'python3'):
            pid = int(p); break
if pid is None: sys.exit('HA process not found')
sys.remote_exec(pid, '/config/.rvl_hotreload.py'); print('injected into pid', pid)
"
sleep 20
tail -4 /mnt/data/supervisor/homeassistant/.rvl_hotreload.log
EOS
sh "$here/bench/hq.sh" /tmp/rvl_hotreload_run.sh
