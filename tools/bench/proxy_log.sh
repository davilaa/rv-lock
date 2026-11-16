# run with hq.sh: 14 s of proxy log (a log client does NOT take the Bluetooth feed from Home Assistant)
docker exec app_5c53de3b_esphome sh -lc "cd /config/esphome && timeout 14 esphome logs bt-proxy.yaml --device $PROXY_IP 2>&1" | sed -E 's/\x1b\[[0-9;]*m//g' | grep -aE "lock_fastlink|compiled on" | cut -c1-200 | head -20
