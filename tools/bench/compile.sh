# run with hq.sh: start the ESPHome compile of bt-proxy detached; poll .fw-compile.log for EXIT=
nohup sh -c "docker exec app_5c53de3b_esphome sh -lc 'cd /config/esphome && esphome compile bt-proxy.yaml > /config/esphome/.fw-compile.log 2>&1; echo EXIT=\$? >> /config/esphome/.fw-compile.log'" >/dev/null 2>&1 &
sleep 3; echo started; tail -c 300 /mnt/data/supervisor/homeassistant/esphome/.fw-compile.log
