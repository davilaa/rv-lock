#!/bin/sh
# usage: hq.sh <script-file>
# Runs the script as root on the Home Assistant OS host (a Proxmox VM, through `qm guest exec`,
# 400 s limit) and prints its stdout/stderr.  Site values come from tools/local.env (see
# tools/local.env.example).  PROXY_IP and PROXY_PREFIX are defined at the top of the script that
# runs remotely.
here=$(dirname "$0")
. "$here/../local.env"
b=$( { printf 'PROXY_IP=%s\nPROXY_PREFIX=%s\n' "$PROXY_IP" "${PROXY_PREFIX:-bt_proxy}"; cat "$1"; } | base64 -w0 )
ssh -o BatchMode=yes "$PVE_HOST" "qm guest exec $HAOS_VMID --timeout 400 -- sh -c 'echo $b | base64 -d | sh'" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("out-data",""));print(d.get("err-data",""),file=sys.stderr)'
