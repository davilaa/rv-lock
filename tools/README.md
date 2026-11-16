# Tools

Development and diagnostic helpers. None of them is needed to use the integration.

## Bluetooth diagnostics (run inside the Home Assistant container)

All open raw HCI sockets that coexist with bluetoothd. Copy a script to the Home Assistant host and
run it with `docker exec -i homeassistant python3 -I /dev/stdin [args] < script.py`. They stop when
Home Assistant core restarts.

Read-only:
- `hcimon.py SECONDS LOCK_ADDRESS` - passive HCI monitor (channel 2): LE connection complete/update/disconnect
  reasons, L2CAP connection-parameter requests, ATT opcodes, TX acks, fragments, ACL packets per
  handle per 30 s.
- `chmap.py HANDLE...` - LE Read Channel Map per connection handle, LE features, LE buffer size.
- `chlog.py` - logs the channel-map size of two handles every 5 s for 15 min.
- `btprobe.py SECONDS LOCK_ADDRESS` - per-handle ACL inter-arrival histogram (the connection interval
  of each link) and the lock's advertisements.
- `advlisten.py [hciN] [seconds] [address...]` - what the adapter's running scan hears.
- `start_hcimon_haos.sh` - wrapper that starts the monitor detached and writes `/tmp/hcimonN.out` (edit the address at its top).

These change controller state:
- `chmap_set.py 10,11,...|all` - LE Set Host Channel Classification for every LE link on the adapter
  (`all` restores 37 channels).
- `afh_assessment.py 0|1` - writes the AFH Channel Assessment Mode, then also limits the host channel
  classification to channels 2-21 and 36. Run `chmap_set.py all` afterwards.
- `hostscan.py [hciN] [seconds] [address...]` - runs its own passive scan: sets scan parameters, turns
  scanning on and turns it off when it ends, which interrupts Home Assistant's scan on that adapter.
  Use it on an adapter Home Assistant is not using.

`chlog.py`, `chmap_set.py` and `afh_assessment.py` use connection handles 16 and 17 (hard-coded; edit
them for your links).

## Loading changed code without restarting Home Assistant

`hotreload.sh` loads changed `rv_lock` Python files into the running Home Assistant (Python 3.14,
`sys.remote_exec`): it imports every module in a separate process first (a file that fails to import
stops the reload there), then unloads the config entry, reloads the modules and sets the entry up
again. It cannot load a module file that did not exist when Home Assistant started, and service
handlers registered at first setup keep their original code. The result is appended to
`/config/.rvl_hotreload.log` (`reloaded; setup ok=True`).

`reset_arm_inject.py` arms a one-shot factory reset of the lock over Bluetooth (erases pairing, PINs
and fingerprints). Read its header before using it.

## Site settings

`hotreload.sh` and everything in `bench/` assume Home Assistant OS running as a Proxmox VM with the
QEMU guest agent, reached with `qm guest exec`. Copy `local.env.example` to `local.env` (git-ignored)
and set the Proxmox ssh target, the VM id and the proxy's IP address.

## Bench tools for the proxy path (`bench/`)

`hq.sh FILE` runs a shell script as root on the Home Assistant OS host (400 s limit). The Python
tools run inside the `homeassistant` container and read a long-lived access token from
`/config/.ha_token` at run time. Create one in your Home Assistant profile for the test session,
store it there with mode 600, and delete the file and revoke the token when you are done; no token
is in these files. Copy a tool to `/config/.name.py` (for example with `push.sh`, then rename
the `.new` file) and start it from a small script given to `hq.sh`:
`docker exec -e RVL_PROXY_PREFIX=$PROXY_PREFIX homeassistant python3 -u -I /config/.name.py ARGS`
(`/config` is `/mnt/data/supervisor/homeassistant` on the host, which is the path `push.sh` needs).
Delete the copies afterwards. The proxy's entity-id prefix comes from `RVL_PROXY_PREFIX` (default
`bt_proxy`, the example YAML's device name); the lock's entity ids, the `bt-proxy.yaml` file name
and the ESPHome container name in the tools are the author's, change them to yours.

- `push.sh LOCAL REMOTE` - upload a file in 20 KB base64 chunks; writes `REMOTE.new` and prints its
  md5. Move it into place yourself (keep a copy of the old file), then `../hotreload.sh`.
- `lk.py` - one-line helper, commands chain: `state`, `wait on|off SECONDS`, `sleep S`, `lock`,
  `unlock`, `reconnect`, `interval MS`, `idle on|off`, `timeout S`, `debug LEVEL`, `watch S STEP`,
  `tune hold|follow HONOUR_MS LOGIN_MS`, `radio BOOST WIFI_PCT HOLD INIT_UNITS`, `skipver on|off`.
  Timing of a real command after the link was dropped:
  `state wait off 70 sleep 12 unlock sleep 4 lock sleep 2 state` (leaves the lock locked).
- `trial.py N [arms]` - N reconnects from the idle state through the Reconnect button, one line each
  (advert wait, bring-up, connect requests, connect, login) and a summary. Stops at the first
  handshake error on an open link. Arms `on,off` toggle the proxy's version-exchange skip between runs.
- `factorial.py ROUNDS` - randomized 2x2 (cached connect x version skip), one reconnect per arm per
  round; flips `PROXY_CACHED_CONNECT` in the running integration with `sys.remote_exec` and the skip
  with the proxy's `lock_skip_version` action. It ends by putting back the shipped values (cached
  connect on with a 0.10 s settle, version skip on); if you stop it early, finish with
  `../hotreload.sh` and reboot the proxy. `trial.py` with arms leaves the skip at the last arm's
  value until the proxy reboots.
- `compile.sh`, `flash.sh`, `proxy_log.sh` - give to `hq.sh`: build the proxy firmware in the ESPHome
  add-on container, upload it to the proxy (the proxy reboots, any lock link drops), read 14 s of its
  log (the config dump lines of `lock_fastlink`).
- `elf_wrap_check.py` - run inside the ESPHome add-on container after a compile: lists who calls each
  of the five wrapped internal Bluedroid functions in `firmware.elf`, to confirm a `--wrap` took effect (a wrap only
  catches calls from other source files).

Rules while testing: one connect at a time with pauses (the scripts wait for the idle disconnect
between runs); never attach a second ESPHome API client that subscribes to Bluetooth advertisements
(ESPHome serves one subscriber, so Home Assistant would lose its feed; `esphome logs` is fine).
