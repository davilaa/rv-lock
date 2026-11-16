#!/bin/sh
# usage: push.sh <local file> <remote path on the Home Assistant OS host>
# Uploads in 20 KB base64 chunks through `qm guest exec`; writes <remote path>.new and prints its md5.
set -e
here=$(dirname "$0")
. "$here/../local.env"
src="$1"; dst="$2"; tmp="$dst.upload.b64"
ssh -o BatchMode=yes "$PVE_HOST" "qm guest exec $HAOS_VMID -- sh -c ': > $tmp'" >/dev/null
base64 -w0 "$src" | fold -w 20000 | while IFS= read -r chunk || [ -n "$chunk" ]; do
  ssh -n -o BatchMode=yes "$PVE_HOST" "qm guest exec $HAOS_VMID -- sh -c 'printf %s $chunk >> $tmp'" >/dev/null
done
ssh -o BatchMode=yes "$PVE_HOST" "qm guest exec $HAOS_VMID -- sh -c 'base64 -d $tmp > $dst.new && rm -f $tmp && md5sum $dst.new'" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("out-data","").strip())'
