# lock_fastlink: ESPHome component for the proxy that carries the lock's link

An ESPHome external component for a **classic ESP32** Bluetooth proxy (ESP-IDF framework,
Bluedroid). It manages the BLE link from the proxy to one device, the lock, by its Bluetooth
address, and leaves the link settings of every other device the proxy serves alone. Two things
apply to the whole proxy: BLE TX power is raised to +9 dBm for every link, scanning and
advertising, and Wi-Fi gets a smaller share of the radio for up to 12 s while a connect to the
lock is waiting. The `rv_lock` integration does not require the component (the Home Assistant
host adapter also works), but on the author's setup a stock proxy was not dependable with this
lock.

Tested with ESPHome 2026.10.2 on an `esp32dev` board, built in the Home Assistant ESPHome add-on.

## What it does

- **Connect boost** (`connect_boost: 1`, `boost_wifi_share: 20`). The ESP32 has one radio, shared
  with Wi-Fi: in every beacon period (102.4 ms) Wi-Fi gets the first 60 % and Bluetooth
  scanning/connecting the last 40 %. The lock advertises about every 0.8 s, so its advertisements
  drift through that window: a few are heard, then none for 4-5 s. While a connect is waiting for
  the lock's advertisement the component sets the BLE "mesh config" coexistence status bit and
  supplies its own share table (`coex_schm_ble_mesh_config_wifi_conn`, a weak symbol in
  `libcoexist.a`) so Bluetooth gets 80 %. Both are undone when the link is up, the connect is
  abandoned, or after 12 s. (`esp_coex_preference_set()`, which ESPHome calls, is an empty stub on
  the classic ESP32.) Measured wait for the advertisement, 6 interleaved connects per arm: mean
  1.76 s without the boost, 0.96 s with it.
- **Link created at 30 ms** (`connect_interval_min/max: 30ms`) instead of ESPHome's 7.5 ms, which
  this lock does not survive; +9 dBm TX power (whole proxy); 20 s supervision timeout.
- **No version exchange** (`skip_version_exchange: true`). As central, Bluedroid reads the remote
  features after the link starts, then the remote version, and reports the link up only after
  both. For the lock the link is reported up right after a successful feature read (peer without
  Data Length Extension, link still waiting to be reported) and the version request is not sent;
  if that does not bring the link up, the normal request goes out. Bring-up 197 ms with the
  exchange, 78 ms without (12 connects per arm, randomized).
- **Login window** (`login_fast`, 15 s): every connection runs at 30 ms from link-up, whatever the
  hold interval is.
- **The lock's own 200-400 ms request** (sent once per connection after notifications are
  enabled; Bluedroid always answers OK): with `peer_request: hold` the component keeps its own
  interval, so the login never slows down. `follow` grants the request at 200 ms and reverts
  after `honour`.
- **Hold interval**: the link moves to it after the login window. In the example the "Lock link
  interval" select decides it: its stored value (30 ms on a fresh flash) is applied at boot and
  replaces `interval`.
- **Connect listening** (`initiate_full_duty`): the first LE Create Connection of each connect
  listens without gaps. When the lock does not answer a connect request (about 1 connect in 10),
  Bluedroid re-issues it by itself; with `retry_full_duty` the first of those retries listens
  without gaps too, instead of the stock 30 of 60 ms that hears only half of the lock's
  advertisements. Any further retry keeps the stock duty, so a lock that is not answering at all
  is not sent requests in quick succession.
- **Connect timings**: `take_phases()` hands out, once per connection, the wait for the
  advertisement, the link bring-up time and the number of connect requests. The example YAML
  publishes them as three diagnostic sensors 5 s after link-up.

## Use

Copy [`bt-proxy-example.yaml`](bt-proxy-example.yaml) and [`bt_tx_power.h`](bt_tx_power.h), set
the lock's address and your secrets, and flash. The component is pulled with:

```yaml
external_components:
  - source: github://davilaa/rv-lock@main
    components: [lock_fastlink]
```

The component's include paths point at the ESP-IDF copy inside the ESPHome container
(`/root/.cache/esphome/idf/frameworks/<version>/components/bt`, `IDF_ROOT` in `__init__.py`).
Change `IDF_ROOT` if your build environment keeps ESP-IDF elsewhere: copy
`esphome/components/lock_fastlink` into a `components/` folder next to your YAML, edit `IDF_ROOT`
in its `__init__.py`, and replace the `github://` line with `- source: {type: local, path:
components}`. If the path is wrong, the build stops at the include of `l2c_int.h`.

| Option | Default | Meaning |
| --- | --- | --- |
| `mac_address` | required | The lock's Bluetooth address |
| `enabled` | `true` | Off = the component stops holding its own interval after link-up, so the lock's 200-400 ms request wins. Link setup at `connect_interval_min/max`, connect listening, the connect boost, the version-exchange skip and TX power stay active |
| `interval` | `30ms` | Hold interval after the login window |
| `connect_interval_min` / `connect_interval_max` | `30ms` / `50ms` | Interval the link is created with |
| `login_fast` | `15s` | How long every new connection stays at 30 ms |
| `peer_request` | `follow` | `hold` or `follow`, for the lock's own interval request (the example uses `hold`) |
| `honour` | `200ms` | With `follow`: how long the lock's interval stands before the revert |
| `initiate_full_duty` | `true` | Gap-free listening on the first connect request |
| `connect_boost` | `2` | 0 off, 1 larger Bluetooth radio share, 2 also Wi-Fi max modem sleep (the example uses 1) |
| `boost_wifi_share` | `45` | Wi-Fi's percent of each beacon period while boosted, 5-60 (the example uses 20) |
| `skip_version_exchange` | `false` | Skip the LL version exchange with the lock (the example uses `true`) |
| `retry_full_duty` | `false` | Gap-free listening on the first retry after an unanswered connect request (the example uses `true`) |

API actions in the example, for experiments (values return to the YAML ones on reboot):
`lock_link_tune` (`peer_hold`, `honour_ms`, `login_fast_ms`), `lock_radio_test` (`boost`,
`wifi_pct`, `hold`, `init_units`; `hold: 1` keeps the larger Bluetooth share on while only
scanning and ends by itself after 120 s), `lock_skip_version` (`skip`) and `lock_retry_listen`
(`full`).

The config dump in the proxy log shows it working:
`Link setup for the lock: 24-24 x1.25 ms (overrides used: N, updates held: N)`,
`Version exchange with the lock: skipped (skipped N times)`,
`First retry of a connect to the lock: continuous (used N)`.

## How it works

Seven linker wraps (`-Wl,--wrap`): `esp_ble_gap_set_prefer_conn_params`,
`esp_ble_gap_update_conn_params`, `btsnd_hcic_ble_upd_ll_conn_params`,
`btsnd_hcic_ble_create_ll_conn`, `l2cble_conn_comp`, `btsnd_hcic_rmt_ver_req`,
`btm_ble_read_remote_features_complete`; a strong definition of one coexistence table; and a
`loop()` that reads the link's L2CAP control block through private Bluedroid headers. Per-link
state is void as soon as a new connect is issued (generation counter) or the control block is
seen in any state but connected.

## Known issues and notes

- **Classic ESP32 only.** The wraps and the coexistence table are specific to the Bluedroid host
  and the ESP32's Wi-Fi/Bluetooth coexistence library. Other chips are untested.
- **It depends on ESP-IDF internals.** After any ESPHome / ESP-IDF upgrade check: the build log
  for the seven wraps; that `btm_ble_read_remote_features_complete(UINT8 *p)` still receives
  status, handle, features in that order and still calls `btsnd_hcic_rmt_ver_req` for a central
  link; that `coex_schm_ble_mesh_config_wifi_conn` is still a 10-byte table
  `{2, 1, 45, 0x03, 0, 0, 55, 0, 0, 0}` in the new `libcoexist.a` (if the layout changed, set
  `connect_boost: 0` until the table is re-derived); and the proxy log lines above.
- **ESPHome's Bluetooth proxy serves one advertisement subscriber.** Do not attach a second
  ESPHome API client that subscribes to Bluetooth advertisements for measurements: Home Assistant
  loses its feed until its ESPHome entry is reloaded. `esphome logs` is fine.
- While boosted, Wi-Fi gets 20 % of the air time for up to 12 s per connect. The proxy stayed
  reachable through 80 s test holds at that share.
