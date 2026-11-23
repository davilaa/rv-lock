"""Constants for the RV lock (Tuya BLE protocol v4, Smart Life app behaviour)."""
DOMAIN = "rv_lock"

CONF_ADDRESS = "address"
CONF_LOCAL_KEY = "local_key"
CONF_UUID = "uuid"
CONF_DEV_ID = "dev_id"
CONF_PASSCODE = "passcode"
CONF_PROXY = "proxy_source"

DEFAULT_PROXY = ""                       # Optional Bluetooth MAC of one ESPHome proxy ("proxy_source", entered at
                                         # setup, stored in the config entry).  It does not select the path: any
                                         # connectable proxy that hears the lock is preferred over the host
                                         # adapter (hci0).  When connects bounce off, this proxy is also told to
                                         # drop any link to the lock, even if its allocation table lists none.

SERVICE_UUID = "0000fd50-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "00000001-0000-1001-8001-00805f9b07d0"
NOTIFY_CHAR = "00000002-0000-1001-8001-00805f9b07d0"

# Datapoints (Kohree, Tuya product f9amgmrh).  Observed set from a full motor cycle
# (2026-11-23): the lock emits only DP 1, 19, 46, 47, 71 (plus 8).  There is NO jam
# datapoint and NO "motor running" datapoint -- DP 47 is the only motor property and it
# is a RESULT, not a live flag (see below).  A jam can only be inferred (DP 47 did not
# reach the commanded state); the integration does that in _verify_later.
DP_BATTERY = 8          # int, %
DP_UNLOCK_BLE_COUNT = 19  # int, running count of BLE unlocks
DP_LOCK = 46            # bool, 1 = lock.  Drives the motor UNCONDITIONALLY: a write runs the
                        # motor whatever the lock's own DP 47 state is (verified 2026-11-23 -- a
                        # bare lock re-threw the bolt after a hand-turn), so no pre-unlock is needed.
DP_MOTOR_STATE = 47     # bool, 0 = locked, 1 = unlocked.  The lock's self-reported state; PUSHED
                        # when the motor FINISHES (not while it runs).  It tracks the motor/bolt
                        # driver, NOT the thumb-turn, so after a manual turn it is stale -- this is
                        # the concrete meaning of "the lock still considers itself locked".
DP_BLE_UNLOCK = 71      # raw, ble_unlock_check (unlock command + the echoed unlock record)
DP_UNKNOWN_1 = 1        # bool, seen = 0 throughout lock and unlock; purpose unconfirmed, not a jam/motor flag

# Connection behaviour copied from the Smart Life app (thingclips SDK):
CONNECT_ATTEMPTS = 4                      # connectRetry=3 (bleak_retry_connector max_attempts)
CONNECT_ATTEMPT_TIMEOUTS = (28.0, 30.0, 36.0, 30.0)  # per attempt.  The app allows 8/10/16/10 s for the link plus
                                          # up to 20 s for service discovery; one BlueZ Connect covers both.
CONNECT_RETRY_GAP = 1.0                   # 1000 ms between attempts
CONNECT_MIN_GAP = 1.0                     # between any two connect attempts, like the app (was a hard 20 s floor;
                                          # now the app's pacing)
ADVERT_RECONNECT_DEDUPE = 5.0             # reconnect on advertisement, 5 s windows
IDLE_RECHECK = 30.0                       # if no advertisement arrives
KEEPALIVE_INTERVAL = 25.0                 # read while idle to keep the held link warm
IDLE_DISCONNECT = 120.0                    # s: drop the held link after this long with no user command,
                                          # so the lock's radio sleeps (battery).  A lock/unlock/credential
                                          # command wakes a fresh connect on demand (about 2 s through a
                                          # tuned proxy).  0 = hold the link forever (old behaviour).
                                          # Adverts do NOT auto-reconnect while idle-parked; only a command does.
PROXY_CACHED_CONNECT = True   # ask the ESPHome proxy for its cached connect path ("v3 with cache"): the proxy
                              # reports the link as soon as it is open instead of after the MTU reply, and uses
                              # its stored copy of the lock's (fixed) GATT table.  Home Assistant already keeps the
                              # table and the MTU (246) from the first connect after a restart or a proxy
                              # reconnect; that first connect takes the normal path by itself.  Proxy path only;
                              # hci0 is untouched.  After any failed round the next one uses the normal path
                              # (session._cached_ok) until a login has succeeded again.
PROXY_SETTLE_CACHED = 0.0     # s between connect() returning and the handshake on a cached proxy connect.
                              # connect() then returns right at link-up (not ~0.15 s after it, as uncached).
                              # No pause is needed: the proxy still runs the MTU exchange first and Bluedroid
                              # queues the first GATT write behind it.  Measured, randomized, link start ->
                              # ready: 996 ms uncached, 907 ms cached at 0.10, 1051 ms at 0.25 (2026-11-15,
                              # 12 connects per arm); 694 ms at 0 against 786 ms at 0.10 (2026-11-17, 6 per
                              # arm, 12 of 12 logins good at 0).
REACHABLE_WINDOW = 180.0   # s: "Reachable" stays on this long after the last advert a connectable scanner heard.
                           # While idle-parked the link is down by design, so "Connected = off" says nothing;
                           # the lock still advertises every 0.77 s and the proxy hears about 1 in 5 (gaps of
                           # up to 15 s seen), so 180 s without one means it really cannot be reached.
PREEMPT_RECONNECT = 1e9                   # s: effectively OFF. Was 1800 s = a clean
                                          # pre-emptive reconnect every 30 min to beat the lock's "~5 min
                                          # self-hangup" (firmware inactivity timer). Via the bt-proxy that
                                          # hangup no longer fires (ESP32 completes the LL conn-param
                                          # procedure + DP_KEEPALIVE_INTERVAL restarts the timer), so the
                                          # periodic reconnect is redundant and stays off; the
                                          # link now holds until a real RF drop. The >= compare never fires at
                                          # 1e9, and the keepalive cadence is unaffected (min with KEEPALIVE).
                                          # To restore the safety net, set this back to 1800.0.
PREEMPT_READVERT_WAIT = 10.0              # s: after the pre-emptive disconnect, wait (up to this) for the
                                          # lock to resume connectable advertising before reconnecting, so the
                                          # reconnect doesn't race the re-advertise (br-connection-canceled ->
                                          # back-off -> ~100 s recovery instead of ~9 s)
PREEMPT_STRONG_RSSI = -84                 # dBm: a fresh advert at/above this is strong enough to connect on
                                          # at once; weaker -> wait for a 2nd fresh advert (steady advertising)
DP_KEEPALIVE_INTERVAL = 75.0              # s: a harmless DP WRITE (the dp69 check the Smart Life panel sends on
                                          # connect) this often while the link is held.  Tuya's lock firmware
                                          # (tuya_ble_lock_sdk, lock_timer.c "conn_to_monitor") runs an inactivity
                                          # timer from bonding that UNCONDITIONALLY disconnects the link when it
                                          # expires, and only a DP write, an OTA frame or the ack of a lock report
                                          # restarts it -- a queryDps (the 25 s keepalive) does not.  That timer,
                                          # not the adapter, is the "~5 min self-hangup".  Seen in the
                                          # firmware of a sibling BK3431Q-based lock (2 min there, ~5 min on this lock).
BATTERY_SYNC_INTERVAL = 86400.0           # s: dp69 check + battery query (queryDps 0x55) at most once a day
STALE_LINK_GRACE = 8.0                    # s after parking before the first look at the proxy's slots (lets our own
                                          # disconnect finish there, so a normal teardown is not reported as stale)
STALE_LINK_RECHECK = 300.0                # s: while idle-parked, how often to drop a proxy slot the
                                          # lock still holds (a zombie link from an abnormal drop blocks the next
                                          # connect for HA and the phone app until it is forced closed)
STEP_TIMEOUT = 15.0                       # per handshake step, then one resend (same future keeps the first sn
                                          # alive).  Was 8 s; at -84..-90 dBm (~90 % of connection events lost,
                                          # 2026-11-06 HCI trace) a 2-fragment reply took 5-25 s to complete.
COMMAND_TIMEOUT = 30.0                    # wait for a session before sending a command
COMMAND_DEADLINE = 45.0                   # from the press: a queued command older than this is dropped
READBACK_TIMEOUT = 8.0                    # bolt read-back when no confirmation arrived
ACK_TIMEOUT = 15.0                        # lock's reply to a DP write (5-6 s seen at 400 ms interval)

# Pin the lock to its OWN slow connection interval once the link is up.  HA's
# bluetooth stack (habluetooth) automatically loads FAST (7.5 ms) then MEDIUM
# (8.75-11.25 ms) conn params on every connect via the kernel mgmt API; at that
# aggressive interval this lock's weak, fragile link drops within ~15 s (HCI
# trace 2026-10-31).  Re-loading the lock's own 200-400 ms request (exactly what
# it asks for over L2CAP, and what phones/the ESP32 leave it at) cancels that
# override so the link holds.  Units: interval = 1.25 ms, timeout = 10 ms.
CONN_PIN_MIN = 0x00A0      # 160 * 1.25 ms = 200 ms  (the lock's OWN requested min; never an override)
CONN_PIN_MAX = 0x0140      # 320 * 1.25 ms = 400 ms  (the lock's OWN requested max).  Pinning the lock's own
                           # interval (not a faster override) cannot be "too aggressive" and will not wedge it;
                           # the 20 s supervision below is what helps the weak link ride through RF fades.
CONN_PIN_LATENCY = 0
HANDSHAKE_STEP_TIMEOUT = 10.0             # device info and pair: 10 s each and NO resend, like the app; a timeout
                                          # drops the link and the next attempt starts over
PIN_SETTLE = 3.5           # s after connect() returns before the pre-handshake pin.  The lock sends its own
                           # L2CAP conn-param request 0.3-3.2 s after link-up (traces 2026-11-06); a pin
                           # inside that update is rejected 0x0C, and a pin BEFORE the request gets
                           # overridden by it (back to 6 s supervision).  So wait it out, then pin once.
PROXY_SETTLE = 0.1         # s after connect() returns before the handshake when the link runs through the
                           # ESPHome bt-proxy on the UNCACHED path (first connect after a Home Assistant restart
                           # or a proxy reconnect, and the round after any failed round).  There connect() returns
                           # about 0.1-0.2 s after link-up, after the proxy's MTU exchange.  PIN_SETTLE only
                           # serves the hci0 mgmt pin; _pin_conn_params is a no-op on the proxy path.  A failed
                           # round adds the saved time back to its backoff (session._settle_credit), so failed
                           # and wedge-looking rounds are spaced as before.  Roll back: set to 3.5.
CONN_PIN_TIMEOUT = 2000    # 2000 * 10 ms = 20 s supervision.  The lock asks for 6 s; at -84..-90 dBm
                           # (~90 % of connection events lost, HCI trace 2026-11-06) 15 misses in a row
                           # come every 6-60 s and killed every session.  Applied by the post-handshake
                           # pin only (interval stays the lock's own 200-400 ms).  EXPERIMENTAL unit;
                           # if sessions drop right after the pin, put this back to 600.

# The host adapter (hci0) is usually shared with other Bluetooth devices: failed connects
# through it can leak controller slots and take those devices offline.
LOCAL_ATTEMPT_GAP = 1.0           # spacing of lock connect attempts via hci0 (app pacing; was 20 s)
LOCAL_FAIL_LIMIT = 3              # this many SLOT-type hci0 failures in a row (controller out of connection
                                  # slots / busy) ...
LOCAL_BREAKER_SECONDS = 120       # ... stop using hci0 for 2 min.  Plain radio failures (connect timeout,
                                  # link lost during discovery) never count: until 2026-11-12 they did, and
                                  # the breaker benched a reachable lock for 10 min 15 times in 14 h.
ROUND_RETRY = 10.0                # a failed connect round is retried this soon, every time, no escalation
                                  # (the app retries every 10 s while the device is in range)
ROUND_IDLE_RECHECK = 5.0          # a round that attempted no connect (lock not heard yet) re-checks this soon
                                  # and never escalates the backoff
SHARED_DEVICE_STATUS_ENTITY = ""  # connection-status entity of another BLE device on hci0; read only by the
                                  # watchdog in session.py, which is off
SHARED_DEVICE_BREAKER_SECONDS = 1800    # that device dropped while the lock used hci0: 30 min off hci0
