# run with hq.sh: upload the already-compiled firmware (no rebuild) to the proxy, wait for the result
nohup sh -c "docker exec app_5c53de3b_esphome sh -lc 'cd /config/esphome && esphome upload bt-proxy.yaml --device $PROXY_IP > /config/esphome/.fw-upload.log 2>&1; echo EXIT=\$? >> /config/esphome/.fw-upload.log'" >/dev/null 2>&1 &
for i in $(seq 1 40); do sleep 5; grep -aq "EXIT=" /mnt/data/supervisor/homeassistant/esphome/.fw-upload.log && break; done
tail -c 500 /mnt/data/supervisor/homeassistant/esphome/.fw-upload.log | tr '\r' '\n' | tail -5
