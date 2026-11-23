"""BLE session with the RV lock, modelled on the Smart Life app (thingclips SDK).

What the app does, and this does too:
- connects with up to 4 attempts 1 s apart;
- after link-up: enable notify, use the negotiated MTU, send device info carrying
  MTU-3, pair, answer the time request, then one empty queryDps;
- writes every packet with response, one frame at a time, a whole frame per
  write when the MTU allows;
- acknowledges every DP report (0x8006/0x8007) that asks for it.

Where it differs from the app: a lost held link is re-made when the lock is heard again
(5 s dedupe), but a failed connect round waits ROUND_RETRY first and repeated login
wedges wait WEDGE_BACKOFF_MAX; a held link is kept warm with a queryDps every
KEEPALIVE_INTERVAL and a harmless DP write every DP_KEEPALIVE_INTERVAL (the lock's
firmware hangs up a link that sees no DP write); on the host adapter the supervision
timeout is raised (CONN_PIN_TIMEOUT) while the interval stays the lock's own.

Paths to the lock: a connectable ESPHome Bluetooth proxy that hears it is preferred
(strongest signal first); the Home Assistant host adapter (hci0) is the fallback.
Guards against hammering the lock, whose firmware can stop answering logins after a
burst of connects: CONNECT_MIN_GAP between any two connect attempts, one connect in
flight, a login-wedge detector that backs off hard and raises a notification instead
of retrying, and a failure breaker on the host adapter.  With idle-disconnect on (the
default) the link is dropped after a short idle time and re-made by the next command;
with it off the link is held and kept warm.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
import logging
import time

from bleak import BleakClient
from bleak.exc import BleakError

from homeassistant.components import bluetooth, persistent_notification
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import protocol as P
from .const import (
    ADVERT_RECONNECT_DEDUPE,
    ACK_TIMEOUT,
    COMMAND_DEADLINE,
    SHARED_DEVICE_BREAKER_SECONDS,
    SHARED_DEVICE_STATUS_ENTITY,
    CONN_PIN_MIN,
    CONN_PIN_MAX,
    CONN_PIN_LATENCY,
    CONN_PIN_TIMEOUT,
    CONNECT_ATTEMPT_TIMEOUTS,
    CONNECT_MIN_GAP,
    LOCAL_ATTEMPT_GAP,
    LOCAL_BREAKER_SECONDS,
    LOCAL_FAIL_LIMIT,
    ROUND_RETRY,
    ROUND_IDLE_RECHECK,
    CONNECT_ATTEMPTS,
    CONNECT_RETRY_GAP,
    DP_BATTERY,
    DP_BLE_UNLOCK,
    DP_LOCK,
    DP_MOTOR_STATE,
    DP_UNLOCK_BLE_COUNT,
    IDLE_RECHECK,
    KEEPALIVE_INTERVAL,
    IDLE_DISCONNECT,
    REACHABLE_WINDOW,
    DP_KEEPALIVE_INTERVAL,
    PREEMPT_RECONNECT,
    PREEMPT_READVERT_WAIT,
    PREEMPT_STRONG_RSSI,
    PIN_SETTLE,
    PROXY_SETTLE,
    PROXY_CACHED_CONNECT,
    PROXY_SETTLE_CACHED,
    HANDSHAKE_STEP_TIMEOUT,
    BATTERY_SYNC_INTERVAL,
    STALE_LINK_RECHECK,
    STALE_LINK_GRACE,
    NOTIFY_CHAR,
    READBACK_TIMEOUT,
    STEP_TIMEOUT,
    WRITE_CHAR,
)

_LOGGER = logging.getLogger(__name__)


class AnswerMissing(HomeAssistantError):
    """The DP write was acknowledged but the lock's answer never came (outcome unknown)."""

# DPs that report which credential opened the lock (value = hw id / member id)
UNLOCK_DPS = {
    12: "fingerprint", 13: "pin", 14: "dynamic_pin", 15: "card", 16: "key",
    19: "app", 55: "temporary_pin", 62: "remote", 63: "voice",
}


# Unused: the proxy is the preferred path now (see _lock_client).
PROXY_FALLBACK_ATTEMPT = 3
# Login wedge: the lock holds the BLE link + MTU but never answers 0x0000
# (firmware app-layer hang that nothing on the Home Assistant side clears).
# Reconnecting cannot fix it, so after a couple of wedge rounds stop hammering and
# sit quiet for WEDGE_BACKOFF s (the lock's only shot at self-recovery).
WEDGE_BACKOFF = 600.0          # s: quiet window after WEDGE_STREAK_FOR_LONG wedge rounds
WEDGE_BACKOFF_MAX = 600.0      # s: quiet window once the wedge is clearly sustained
WEDGE_STREAK_FOR_LONG = 6      # 6 x 60 s first: on a very lossy link a "no reply on an open link"
WEDGE_STREAK_FOR_NOTIFY = 12   # round is usually RF (reply never finished assembling), not a wedge
                               # (2026-11-06); CONNECT_MIN_GAP already keeps every round gentle.
# A clean self-drop (the lock hung up its OWN healthy, paired link) is the safe case: the
# lock just proved it is up and is re-advertising, so the first reconnect may use a shorter
# floor.  After a FAILED/aborted connect (never paired) we keep the full CONNECT_MIN_GAP --
# that is the churn case that risks the wedge.
CLEAN_DROP_GAP = 1.0


# BlueZ/kernel strings for a controller that is out of connection slots or busy.  Only
# these trip the hci0 breaker; everything else (timeouts, 0x3E, link lost during service
# discovery) is the weak radio link and must not bench the adapter.
_SLOT_ERRORS = ("le-connection-concurrent-limit", "le-connection-busy",
                "le-connection-memory-allocation", "le-connection-limit", "connection limit")


def _is_slot_error(err: BaseException) -> bool:
    text = repr(err).lower()
    return any(token in text for token in _SLOT_ERRORS)


def _cached_connect_factory(factory):
    """Wrap an ESPHome proxy client factory so connect() asks for the proxy's cached path.

    Home Assistant's wrapper builds the backend client itself and calls
    ``backend.connect(pair, **kwargs)``; bleak-esphome only requests "v3 with cache" when
    ``dangerous_use_bleak_cache`` is passed (bleak-retry-connector does that, this
    integration connects directly).  The flag is added here, for the proxy backend only.
    Relies on three library details (checked on bleak 3.x / habluetooth 7.x / bleak-esphome
    4.x): the wrapped backend's ``client`` field is assignable, the client instance accepts
    an instance attribute, and connect() takes that keyword.  If any of them is gone the
    plain client is used and one warning is logged.
    """
    def make(*args, **kwargs):
        client = factory(*args, **kwargs)
        try:
            orig = client.connect
            if "dangerous_use_bleak_cache" not in inspect.signature(orig).parameters:
                _LOGGER.warning("RV lock: this proxy client has no cached connect option; using the normal path")
                return client

            async def connect(pair, **ckw):
                ckw.setdefault("dangerous_use_bleak_cache", True)
                return await orig(pair, **ckw)

            client.connect = connect
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("RV lock: cached proxy connect not applied (%r); using the normal path", err)
        return client
    return make


def _lock_client(allow_local: Callable[[], bool], chosen: dict,
                 want_proxy: Callable[[], bool] = lambda: False,
                 cache: dict | None = None,
                 use_cached: Callable[[], bool] = lambda: False) -> type[BleakClient]:
    """BleakClient subclass that picks the scanner itself.

    With want_proxy() true, a connectable remote scanner (ESPHome proxy) that has heard
    the lock is used first, strongest signal first; use_cached() also asks that proxy
    for its cached connect path.  Otherwise, or when no proxy has heard the lock, the
    local adapter (hci0) is used, gated by allow_local() (attempt spacing + failure
    breaker); when hci0 has no fresh advert the last known device is tried anyway.
    """
    from habluetooth.wrappers import HaBleakClientWrapper  # noqa: PLC0415

    if not issubclass(BleakClient, HaBleakClientWrapper):
        raise RuntimeError("Home Assistant's Bluetooth wrapper is not installed")

    class _LockClient(BleakClient):  # type: ignore[misc,valid-type]
        def _load_conn_params(self, scanner, device, params, debug_logging, description):
            # HA's wrapper loads FAST (7.5 ms) before connect and MEDIUM (8.75-11.25 ms)
            # the instant connect() returns.  The lock asks for its own 200-400 ms ~0.7 s
            # after link-up (BlueZ honours it), and its weak link does not survive ~11 ms
            # (HCI trace 2026-10-31).  Skip the post-connect MEDIUM load at the source:
            # there is then no override to cancel and no second LE Connection Update to
            # collide with (HCI trace 2026-11-06: HA's MEDIUM and the integration's pin
            # went out 1 ms apart, the pin was rejected 0x0C "Command Disallowed", the
            # link was forced to 11 ms and the handshake died).  The pre-connect FAST
            # load stays: it only shapes the first ~0.7 s (MTU + notify subscribe run at
            # 7.5 ms) until the lock's own request lands.
            from habluetooth.const import ConnectParams  # noqa: PLC0415
            if params is ConnectParams.MEDIUM:
                _LOGGER.debug("RV lock: skipped HA's post-connect MEDIUM conn-param load")
                return
            super()._load_conn_params(scanner, device, params, debug_logging, description)

        def _async_get_best_available_backend_and_device(self, manager):
            address = self._HaBleakClientWrapper__address
            local_ok = allow_local()
            devices = sorted(
                manager.async_scanner_devices_by_address(address, True),
                key=lambda d: -(d.advertisement.rssi or -127),  # strongest signal first
            )
            # 1) proxy (remote scanner) first on the early attempts
            if want_proxy():
                for dev in devices:
                    if getattr(dev.scanner, "adapter_idx", None) is None:  # remote = proxy
                        if backend := self._async_get_backend_for_ble_device(manager, dev.scanner, dev.ble_device):
                            chosen["source"] = dev.scanner.source
                            chosen["local"] = False
                            chosen["rssi"] = dev.advertisement.rssi
                            if use_cached():
                                try:
                                    backend.client = _cached_connect_factory(backend.client)
                                    chosen["cached"] = True
                                except Exception as err:  # noqa: BLE001
                                    _LOGGER.warning("RV lock: cached proxy connect not applied (%r)", err)
                            return backend
            # 2) hci0 (local adapter) fallback
            if local_ok:
                for dev in devices:
                    if getattr(dev.scanner, "adapter_idx", None) is not None:  # local = hci0
                        if backend := self._async_get_backend_for_ble_device(manager, dev.scanner, dev.ble_device):
                            chosen["source"] = dev.scanner.source
                            chosen["local"] = True
                            chosen["rssi"] = dev.advertisement.rssi
                            if cache is not None:
                                cache["device"] = dev.ble_device
                                cache["scanner"] = dev.scanner
                            return backend
                # 3) hci0 has no FRESH advert from the lock.  Its scanner catches only a few
                # percent of them, and until 2026-11-12 that meant no connect was even tried
                # (an 18-min outage while two other Bluetooth scanners heard the lock every second).
                # The controller's own connect listens for the advert itself, so issue the
                # connect anyway through the last known hci0 device (BlueZ keeps the device
                # object because the integration marks it Trusted).
                if cache and cache.get("trusted") and cache.get("device") is not None:
                    scanner = cache["scanner"]
                    if manager.async_scanner_by_source(scanner.source) is scanner:
                        if backend := self._async_get_backend_for_ble_device(manager, scanner, cache["device"]):
                            chosen["source"] = scanner.source
                            chosen["local"] = True
                            chosen["rssi"] = None
                            chosen["blind"] = True
                            return backend
            raise BleakError(f"{address}: no scanner can reach the lock yet")

    return _LockClient


class LockSession:
    """One held, authenticated session with the lock."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        local_key: str,
        uuid: str,
        dev_id: str,
        passcode: str,
        proxy: str,
    ) -> None:
        self.hass = hass
        self.address = address.upper()
        self._local_key = local_key
        self._uuid = uuid
        self._dev_id = dev_id
        self._passcode = passcode
        self._proxy = proxy

        self._client: BleakClient | None = None
        self._connect_attempt = 0       # index within the current connect round (hci0-first)
        self._cmd_seq = 0               # bumps on every lock/unlock request (coalescing)
        self._cmd_target: bool | None = None  # latest requested state: True=lock, False=unlock
        self._keys: dict[int, bytes] = {P.SEC_LOGIN: P.login_key(local_key)}
        self._sn = 1
        self._dps_sn = 0
        self._chunk = 20
        self._with_response = True
        self._asm = P.Assembler()
        self._tx = asyncio.Lock()
        self._waiters: dict[int, asyncio.Future] = {}  # keyed by our frame sn (the lock echoes it as ack_sn)
        self._cmd_lock = asyncio.Lock()
        self._cred_lock = asyncio.Lock()
        self._state_event = asyncio.Event()
        self._dp47_gen = 0
        self._dp_queues: dict[int, asyncio.Queue] = {}   # credential DP replies
        self.last_unlock: dict | None = None             # {"method", "id", "time"}
        self.unlock_listeners: list[Callable[[dict], None]] = []
        self.creds = None  # CredentialManager, set by __init__
        self.locking = False
        self._internal_unlock = False
        self._suppress_app_until = 0.0
        self._unlock_written = False
        self._verify_task: asyncio.Task | None = None
        self._link_lost: asyncio.Future | None = None
        self._unlock_seen: dict[tuple[int, int], float] = {}
        self._paired = asyncio.Event()
        self._wake = asyncio.Event()
        self._last_attempt = 0.0
        self._next_round_at = 0.0     # backoff between failed connect rounds
        # Local adapter (hci0, shared with other BLE devices) safeguards
        self._chosen: dict = {}
        self._local_next = 0.0           # earliest next lock attempt through hci0
        self._local_blocked_until = 0.0  # circuit breaker
        self._local_fail_streak = 0
        self._last_local_use = -1e9
        self._shared_down_since: float | None = None
        self._last_hci_reset = -1e9
        self._last_connect_started = -1e9  # hard floor between connect attempts (CONNECT_MIN_GAP)
        self._connected_at = -1e9          # monotonic time the current link was established (for PREEMPT_RECONNECT)
        self.connected_since = None        # wall-clock datetime the current link came up (diagnostic "Connected since" sensor)
        self._round_backoff = 0.0
        self._settle_credit = 0.0          # settle time saved on the proxy path this round; a failed round adds it back to its backoff
        self.last_heard_mono = time.monotonic()   # last advert heard by a connectable scanner (assume heard at start)
        self._cached_ok = True             # cached proxy connect allowed; cleared by a failed round, set by a good login
        self._last_reachable = True
        self._round_attempted = False      # this round issued at least one real connect
        self._last_dp_write = -1e9         # monotonic time of the last DP write the lock acknowledged
        self._local_cache: dict = {}       # last hci0 BLEDevice + scanner, and whether BlueZ keeps it (Trusted)
        self._task: asyncio.Task | None = None
        self._unsub_adv: CALLBACK_TYPE | None = None
        self._unsub_poll: CALLBACK_TYPE | None = None
        self._no_reply_streak = 0
        self._wedge_streak = 0          # consecutive login-wedge rounds (lock ignores 0x0000)
        self._wedge_notified = False
        self._last_round_wedged = False
        self._clean_drop = False         # last disconnect was a healthy paired link dropping itself
        self._listeners: list[Callable[[], None]] = []

        self.want_connected = True
        self.released_until = 0.0
        self._idle_parked = False          # True = stood down after idle; wakes only on a command
        self._park_fresh = False           # True until the first stale-link check after a park
        self._last_cmd_mono = time.monotonic()  # last user command (lock/unlock/cred); drives idle-disconnect
        self.idle_enabled = True           # idle-disconnect on/off (switch entity; default from IDLE_DISCONNECT)
        self.idle_seconds = float(IDLE_DISCONNECT)  # idle grace before disconnect (number entity)
        self.locked: bool | None = None
        self.battery: int | None = None
        self._last_battery_sync = -1e9   # monotonic; dp69 check + battery query at most once per BATTERY_SYNC_INTERVAL
        self.last_error: str | None = None
        self.rssi: int | None = None
        self.connect_count = 0
        self.last_command_s: float | None = None
        self.last_connect_s: float | None = None    # BLE link-setup time of the last reconnect
        self.last_reconnect_s: float | None = None   # wall time from losing a session to the next one coming up
        self._last_drop_mono: float | None = None    # monotonic time the current outage began (None once consumed)
        self._detached: set = set()                  # links we dropped ourselves (pre-emptive); their late callback is ignored
        self.last_handshake_s: float | None = None   # device_info+pair time of the last reconnect
        self.jammed = False

    # ------------------------------------------------------------ public API
    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected and self._paired.is_set()

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def async_start(self) -> None:
        self._unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            {"address": self.address, "connectable": False},
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
        # HA dispatches advertisement callbacks only when the content changes and
        # the lock's advertisement is static, so poll its last-seen info too.
        self._unsub_poll = async_track_time_interval(self.hass, self._poll, timedelta(seconds=3))
        self._task = self.hass.async_create_background_task(self._run(), f"rv_lock_{self.address}")

    async def async_stop(self) -> None:
        self.want_connected = False
        for unsub in (self._unsub_adv, self._unsub_poll):
            if unsub:
                unsub()
        self._unsub_adv = self._unsub_poll = None
        if self._task:
            task, self._task = self._task, None
            task.cancel()
            with suppress(BaseException):
                await asyncio.wait_for(task, 15)
        await self._disconnect()

    @property
    def reachable(self) -> bool:
        """Connected, or heard advertising recently (the honest liveness signal while idle-parked)."""
        return self.connected or (time.monotonic() - self.last_heard_mono) < REACHABLE_WINDOW

    @callback
    def _poll(self, _now) -> None:
        self._shared_adapter_watchdog()
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        if info is not None and info.time > self.last_heard_mono:
            self.last_heard_mono = info.time
        if self.connected:
            self.last_heard_mono = time.monotonic()   # a live link is proof enough; start the window at link loss
        reach = self.reachable
        if reach != self._last_reachable:
            self._last_reachable = reach
            self._notify()
        if info is None or time.monotonic() - info.time > 15:
            return  # not heard recently (out of range, or held by the phone app)
        if info.rssi != self.rssi:
            self.rssi = info.rssi
        now = time.monotonic()
        if (not self.connected and not self._idle_parked and now >= self.released_until
                and now >= self._next_round_at
                and now - self._last_attempt > ADVERT_RECONNECT_DEDUPE):
            self._wake.set()

    async def async_release(self, seconds: float) -> None:
        """Free the lock for the Smart Life app."""
        self.released_until = time.monotonic() + seconds
        await self._disconnect()
        if self._proxy_holds_lock():
            await self._clear_proxy_link()
        self._notify()

    def resume(self) -> None:
        self.released_until = 0.0
        self._idle_parked = False
        self._wake.set()

    def set_idle_enabled(self, enabled: bool) -> None:
        """Turn idle-disconnect (battery saver) on/off (switch entity)."""
        self.idle_enabled = bool(enabled)
        if not self.idle_enabled:
            self._idle_parked = False   # stop standing down; resume holding the link
        self._wake.set()
        self._notify()

    def set_idle_seconds(self, seconds: float) -> None:
        """Set the idle grace before the link is dropped (number entity)."""
        self.idle_seconds = max(15.0, float(seconds))
        self._wake.set()
        self._notify()

    async def async_reconnect(self) -> None:
        """Drop the current link (even if it looks up) and start a fresh session.

        Also clears the wedge/backoff state: a manual Reconnect must retry now and start
        any fresh backoff from the short floor -- otherwise an escalated wedge backoff
        (WEDGE_BACKOFF_MAX) would keep it parked even
        though the lock is back.
        """
        self.released_until = 0.0
        self._idle_parked = False
        self._round_backoff = 0.0
        self._settle_credit = 0.0
        self._next_round_at = 0.0
        self._wedge_streak = 0
        self._last_round_wedged = False
        await self._disconnect()
        self._notify()
        self._wake.set()

    async def async_lock(self) -> None:
        await self._request_state(True)

    async def async_unlock(self) -> None:
        await self._request_state(False)

    async def _request_state(self, want_locked: bool) -> None:
        """Run lock/unlock, collapsing a burst of presses to the latest one.

        Each press records the desired end state and bumps a sequence number.
        Presses queue on _cmd_lock; when one reaches the front it checks whether
        a newer press has arrived and, if so, skips (that newer press will set
        the final state).  Mashing Lock/Unlock therefore runs at most the command
        already in flight plus the final one, instead of every press in turn.
        """
        self._cmd_target = want_locked
        seq = self._cmd_seq = self._cmd_seq + 1
        self._idle_parked = False
        if not self.connected:
            self._wake.set()
        async with self._cmd_lock:
            if seq != self._cmd_seq:
                return  # superseded by a newer press
            if self._cmd_target:
                await self._do_lock()
            else:
                await self._do_unlock()

    async def _do_lock(self) -> None:
        """Lock the door.  Caller holds _cmd_lock.

        A DP 46 write runs the motor whatever state the lock believes it is in
        (verified 2026-11-23: a bare lock threw the bolt after a hand-turn the
        lock could not sense), so a lock is a single motor command with no
        pre-unlock.  It is sent twice to be sure: the first write is
        authoritative and a failure is reported loudly (the door may be
        unlocked); the second is a best-effort re-assert so a dropped ack on the
        first can't quietly leave the door open.
        """
        start = time.monotonic()
        self.locking = True
        self._notify()
        try:
            try:
                await self._command_locked(DP_LOCK, P.DP_TYPE_BOOL, lambda: b"\x01", "lock",
                                           locked=True, start=start)
            except Exception as err:
                persistent_notification.async_create(
                    self.hass,
                    "The RV door lock did not confirm LOCK. The door may be UNLOCKED.",
                    "RV lock", "rv_lock_failed_lock")
                raise BleakError(f"lock failed, door may be UNLOCKED ({err})") from err
            # Send it again to be sure; the door is already locked so a failure
            # on this second write is not reported (the first is authoritative).
            try:
                await self._command_locked(DP_LOCK, P.DP_TYPE_BOOL, lambda: b"\x01", "lock (confirm)",
                                           locked=True, start=time.monotonic())
            except Exception as err:  # noqa: BLE001
                _LOGGER.info("RV lock: confirming second lock did not ack (%r); first lock succeeded", err)
        finally:
            self.locking = False
            self._notify()

    def _unlock_value(self) -> bytes:
        return P.ble_unlock_value(self._passcode, int(time.time()))

    async def _do_unlock(self) -> None:
        """Unlock.  Caller holds _cmd_lock."""
        start = time.monotonic()
        await self._command_locked(DP_BLE_UNLOCK, P.DP_TYPE_RAW, self._unlock_value, "unlock",
                                   locked=False, start=start)

    # --------------------------------------------------------- connect loop
    @callback
    def _on_advertisement(self, info: bluetooth.BluetoothServiceInfoBleak, _change) -> None:
        self.rssi = info.rssi
        now = time.monotonic()
        if (not self.connected and not self._idle_parked and now >= self.released_until
                and now >= self._next_round_at
                and now - self._last_attempt > ADVERT_RECONNECT_DEDUPE):
            self._wake.set()

    async def _run(self) -> None:
        while self.want_connected:
            try:
                if time.monotonic() < self.released_until:
                    if self._client is not None:
                        await self._disconnect()
                        self._notify()
                    await self._sleep_or_wake(self.released_until - time.monotonic())
                    continue
                if self._idle_parked and not self.connected:
                    # Idle-disconnect: the link was dropped after IDLE_DISCONNECT to save the
                    # lock's battery.  Stay quiet (radio asleep on the lock side) and do NOT
                    # reconnect on adverts -- only a user command clears _idle_parked + wakes us.
                    # But a session the lock's own supervision timeout dropped can leave the proxy
                    # still holding the lock's slot (a zombie that then refuses the next connect --
                    # HA's or the phone app's -- until something forces it closed; a 3 h-old one
                    # blocked two unlock commands 2026-11-16).  While parked we want no link, so
                    # drop any the proxy still lists: first STALE_LINK_GRACE after parking (our own
                    # disconnect needs a moment to finish on the proxy; looking at once reported it
                    # as stale after every park), then every STALE_LINK_RECHECK.  No connect, no bolt
                    # movement; this branch is skipped during Release, so the app keeps its link.
                    self._wake.clear()
                    await self._sleep_or_wake(STALE_LINK_GRACE if self._park_fresh else STALE_LINK_RECHECK)
                    self._park_fresh = False
                    if (self._idle_parked and not self.connected and self._client is None
                            and time.monotonic() >= self.released_until and self._proxy_holds_lock()):
                        await self._clear_proxy_link()
                    continue
                if not self.connected:
                    self._last_attempt = time.monotonic()
                    self._round_attempted = False
                    self._settle_credit = 0.0
                    await self._connect_and_pair()
                    if self.connected:
                        self._connected_at = time.monotonic()
                        self._last_dp_write = self._connected_at   # the lock's inactivity timer starts at bonding
                        self.connected_since = dt_util.utcnow()
                        self._round_backoff = 0.0
                        self._next_round_at = 0.0
                        if self._wedge_streak or self._wedge_notified:
                            persistent_notification.async_dismiss(self.hass, "rv_lock_wedged")
                        self._wedge_streak = 0
                        self._wedge_notified = False
                    else:
                        # Back-to-back connects wear the lock down until it stops serving
                        # links at all (2026-10-25). Like the app, stay quiet while idle:
                        # retry every ROUND_RETRY seconds. A command still wakes us at once.
                        if not self._round_attempted:
                            # Nothing was sent to the lock (not heard by hci0 yet, or the
                            # breaker is open): no connect pressure happened and nothing was
                            # learned, so leave the backoff and wedge state alone and look
                            # again shortly; the next advert wakes us.
                            self._next_round_at = time.monotonic() + ROUND_IDLE_RECHECK
                            self._wake.clear()
                            await self._sleep_or_wake(ROUND_IDLE_RECHECK)
                            continue
                        if self._last_round_wedged:
                            # Login wedge: no reconnect clears a firmware hang, so stop
                            # hammering -- keep the ROUND_RETRY pace until WEDGE_STREAK_FOR_NOTIFY wedge
                            # rounds in a row, then probe only every WEDGE_BACKOFF_MAX and notify once.
                            self._wedge_streak += 1
                            if self._wedge_streak >= WEDGE_STREAK_FOR_NOTIFY:
                                self._round_backoff = WEDGE_BACKOFF_MAX
                                self._notify_wedged()
                            else:
                                self._round_backoff = ROUND_RETRY + self._settle_credit
                        else:
                            self._wedge_streak = 0
                            self._round_backoff = ROUND_RETRY + self._settle_credit
                        self._next_round_at = time.monotonic() + self._round_backoff
                        # Adverts arriving during the failed round set _wake before
                        # _next_round_at moved into the future; drop that stale wake so
                        # the backoff actually holds (otherwise the sleep returns at once).
                        self._wake.clear()
                        await self._sleep_or_wake(self._round_backoff)
                        continue
                if self.connected:
                    # Held link: keep it open indefinitely so commands stay fast.
                    # A light read every KEEPALIVE_INTERVAL exercises the link and
                    # surfaces a silent drop at once (otherwise the next command
                    # would be the one to discover it and pay the reconnect cost).
                    while self.connected and self.want_connected and time.monotonic() >= self.released_until:
                        # (4) Keep the keepalive cadence, but never sleep PAST the pre-empt deadline:
                        # a plain 25 s cadence fired the pre-empt anywhere in 270-295 s.  Sleep only
                        # the remaining time once the deadline is closer than one interval (2 s floor
                        # so a command in flight at the deadline doesn't spin this loop).
                        remaining = PREEMPT_RECONNECT - (time.monotonic() - self._connected_at)
                        await self._sleep_or_wake(min(KEEPALIVE_INTERVAL, max(2.0, remaining)))
                        # Idle-disconnect (battery): after IDLE_DISCONNECT with no user command,
                        # cleanly drop the link so the lock's radio sleeps.  Skip while a command
                        # or credential op is in flight.  A command reconnects on demand.
                        if (self.idle_enabled and self.idle_seconds
                                and self.connected
                                and not self._cmd_lock.locked()
                                and not self._cred_lock.locked()
                                and time.monotonic() - self._last_cmd_mono >= self.idle_seconds
                                and time.monotonic() >= self.released_until):
                            # Piggyback a battery read on this about-to-close link if the gauge
                            # has not been refreshed in a day: in on-demand mode every connect is
                            # command-driven, so _sync never gets to poll it (it defers to the
                            # command that is holding the link).  This is the once-a-day reading.
                            await self._battery_poll_if_stale()
                            # A command may have arrived during that read; only park if still idle.
                            if not (self.connected and not self._cmd_lock.locked()
                                    and not self._cred_lock.locked()
                                    and time.monotonic() - self._last_cmd_mono >= self.idle_seconds
                                    and time.monotonic() >= self.released_until):
                                continue
                            _LOGGER.info("RV lock: idle %.0f s -> disconnecting to save battery "
                                         "(reconnects on the next command)",
                                         time.monotonic() - self._last_cmd_mono)
                            self._idle_parked = True
                            self._park_fresh = True
                            await self._disconnect()
                            self._notify()
                            break
                        # Pre-empt the lock's ~5-min self-hangup (0x13).  On hci0 the firmware
                        # drops the link on its own because BlueZ won't honour its conn-interval;
                        # if a reply is mid-flight at that drop, the firmware's GATT send-queue
                        # latches shut and the lock stops answering.
                        # Beat it with a CLEAN disconnect while the queue is drained -- no command
                        # in flight (_cmd_lock free) and right after a completed keepalive -- then
                        # the loop reconnects.  This replaces the dangerous mid-reply self-drop.
                        if (self.connected
                                and not self._cmd_lock.locked()
                                and time.monotonic() - self._connected_at >= PREEMPT_RECONNECT
                                and time.monotonic() >= self.released_until):
                            _LOGGER.info(
                                "RV lock: pre-emptive clean reconnect after %.0f s (beats the ~5-min self-hangup)",
                                time.monotonic() - self._connected_at)
                            # (3) Non-blocking teardown: detach the client now and let BlueZ finish the
                            # disconnect in the background instead of blocking ~2 s on its confirmation.
                            # The re-advertise wait below inherently waits for the lock to actually drop
                            # (it only advertises again once the link is gone), so nothing is lost; the
                            # old client's disconnected-callback is ignored via _detached.
                            old_client = self._detach_client()
                            bg = None
                            if old_client is not None:
                                bg = self.hass.async_create_background_task(
                                    self._bg_disconnect(old_client), f"rv_lock_bg_disconnect_{self.address}")
                            self._clean_drop = True   # healthy link we dropped ourselves: allow the short regap
                            self._notify()
                            # Wait for the lock to resume connectable advertising before the loop
                            # reconnects.  Reconnecting on the very first advert often races the
                            # re-advertise -> first connect fails -> the retry costs extra seconds,
                            # the old 20-45 s tail.  (2) RSSI-gated: if the first fresh advert is STRONG
                            # (>= PREEMPT_STRONG_RSSI) connect at once -- a strong advert lands; if it is
                            # weak, wait for a SECOND fresh advert (steady advertising) first.  Capped by
                            # PREEMPT_READVERT_WAIT; fall through and try anyway after that.
                            t_drop = time.monotonic()
                            waited = 0.0
                            first_adv = None
                            while waited < PREEMPT_READVERT_WAIT:
                                info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
                                if info is not None and info.time > t_drop:
                                    if (info.rssi or -127) >= PREEMPT_STRONG_RSSI:
                                        break                          # strong fresh advert -> connect now
                                    if first_adv is None:
                                        first_adv = info.time          # weak: advertising again, wait for a 2nd
                                    elif info.time > first_adv:
                                        break                          # steadily advertising -> connect now
                                await asyncio.sleep(0.25)
                                waited += 0.25
                            # Never reconnect while BlueZ may still hold the old link.  The lock can
                            # drop its side and re-advertise BEFORE BlueZ finishes ours (at weak signal
                            # the terminate ack / supervision timeout can take 10+ s), and a connect then
                            # bounces off the still-held handle -> churn (a 74 s outage, 2026-10-28).
                            # So the teardown overlaps the re-advertise wait, but we connect only once
                            # BOTH are done.
                            if bg is not None:
                                with suppress(Exception):
                                    async with asyncio.timeout(20):
                                        await bg
                            break
                        if (self.connected and self._paired.is_set()
                                and not self._cmd_lock.locked()
                                and time.monotonic() >= self.released_until):
                            try:
                                if time.monotonic() - self._last_dp_write >= DP_KEEPALIVE_INTERVAL:
                                    await self._dp_keepalive()
                                else:
                                    await self._send(P.CMD_QUERY_DPS, b"", P.SEC_SESSION)
                            except Exception as err:  # noqa: BLE001
                                _LOGGER.debug("RV lock: keepalive write failed: %r", err)
                    continue
                await self._sleep_or_wake(IDLE_RECHECK)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if not self.want_connected or (task is not None and task.cancelling()):
                    raise  # unload or HA shutdown
                _LOGGER.debug("RV lock: inner cancellation, continuing")
                await asyncio.sleep(1)
            except Exception as err:  # noqa: BLE001
                self.last_error = repr(err)
                _LOGGER.debug("RV lock: session loop: %s", err)
                await self._sleep_or_wake(IDLE_RECHECK)

    async def _dp_keepalive(self) -> None:
        """Restart the lock's own inactivity timer with a harmless DP write.

        The lock firmware hangs up a bonded link when its "conn_to_monitor" timer
        expires; only a DP write (or OTA / report ack) restarts it, a queryDps does
        not.  The dp69 check is the write the Smart Life panel sends on every connect:
        it moves nothing and changes nothing.  Runs under _cmd_lock so it never
        overlaps a user command; a press waits at most the short timeout below.
        """
        if self._cmd_lock.locked():
            return
        async with self._cmd_lock:
            if not self.connected:
                return
            self._dps_sn += 1
            check = b"\xff\xff\x00\x01" + self._passcode.encode() + b"\x00"
            await self._request(P.CMD_DP_WRITE, P.dp_write_data(self._dps_sn, 69, P.DP_TYPE_RAW, check),
                                P.SEC_SESSION, timeout=8)
            self._last_dp_write = time.monotonic()
            _LOGGER.debug("RV lock: DP-write keepalive acknowledged (link held %.0f s)",
                          time.monotonic() - self._connected_at)

    async def _battery_poll_if_stale(self) -> None:
        """Read the battery if it has not been read for BATTERY_SYNC_INTERVAL.

        Called from the idle loop right before the link is dropped, so it reuses the
        connection a command just opened instead of waking a dedicated connect.  In
        on-demand mode every connect is command-driven, so _sync never gets to poll
        the battery; this is where the daily reading comes from.  Runs under _cmd_lock
        (like the DP-write keepalive) so it never overlaps a user command.
        """
        if self._cmd_lock.locked() or self._cred_lock.locked():
            return
        if self.battery is not None and time.monotonic() - self._last_battery_sync < BATTERY_SYNC_INTERVAL:
            return
        async with self._cmd_lock:
            if not self.connected:
                return
            try:
                self._dps_sn += 1
                check = b"\xff\xff\x00\x01" + self._passcode.encode() + b"\x00"
                await self._request(P.CMD_DP_WRITE, P.dp_write_data(self._dps_sn, 69, P.DP_TYPE_RAW, check),
                                    P.SEC_SESSION, timeout=8)
                if not self.connected:
                    return
                await self._send(P.CMD_QUERY_DPS, b"\x55", P.SEC_SESSION)
                self._last_battery_sync = time.monotonic()
                _LOGGER.info("RV lock: battery read before idle park (battery %s%%)", self.battery)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("RV lock: battery read before idle park failed: %r", err)

    async def _sleep_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), max(0.1, seconds))
        except (TimeoutError, asyncio.TimeoutError):
            pass
        self._wake.clear()

    def _blocked(self) -> bool:
        return not self.want_connected or time.monotonic() < self.released_until

    def _notify_wedged(self) -> None:
        """Surface a sustained login wedge once (lock accepts the link but never
        answers login -- a firmware hang no reconnect can clear).  Dismissed when a
        session next comes up."""
        if self._wedge_notified:
            return
        self._wedge_notified = True
        _LOGGER.error("RV lock: sustained login wedge (%d rounds); probing only every %d min now",
                      self._wedge_streak, int(WEDGE_BACKOFF_MAX // 60))
        persistent_notification.async_create(
            self.hass,
            "The RV lock keeps accepting the Bluetooth connection but never answers "
            "login (a firmware-level hang). Home Assistant has stopped frequent retries "
            "and now probes only occasionally; it will pick the lock back up on its own "
            "if the lock recovers.",
            "RV lock", "rv_lock_wedged")

    async def _pin_conn_params(self, client: BleakClient) -> None:
        """Re-pin the live link to the lock's own 200-400 ms interval.

        HA's bluetooth stack auto-loads FAST/MEDIUM (~7.5-11 ms) conn params on
        every connect; at that interval this lock's weak, fragile link drops
        within ~15 s (HCI trace 2026-10-31: connect -> lock requests 200-400 ms
        (honoured, link at 398 ms) -> HA forces ~11 ms -> drop).  Setting the
        lock's own requested params cancels that override so the link holds, the
        way phones and the ESP32 (which leave it slow) do.  Best-effort: a failure
        here must never break an otherwise-good session.
        """
        if not self._chosen.get("local"):
            return  # only the local (hci0/mgmt) path gets the auto FAST/MEDIUM load
        setter = getattr(client, "set_connection_params", None)
        if setter is None:
            _LOGGER.debug("RV lock: client exposes no set_connection_params; conn-param pin skipped")
            return
        try:
            await setter(CONN_PIN_MIN, CONN_PIN_MAX, CONN_PIN_LATENCY, CONN_PIN_TIMEOUT)
            _LOGGER.info("RV lock: pinned conn params to %d-%d ms, supervision %d ms",
                         CONN_PIN_MIN * 1.25, CONN_PIN_MAX * 1.25, CONN_PIN_TIMEOUT * 10)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("RV lock: conn-param pin failed (non-fatal): %r", err)

    async def _connect_and_pair(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None and self._local_cache.get("trusted"):
            ble_device = self._local_cache.get("device")   # not heard lately: connect anyway (see _lock_client)
        if ble_device is None:
            self.last_error = "no connectable scanner has seen the lock recently"
            return
        # Proxy first, hci0 as the fallback (see _lock_client).  CONNECT_MIN_GAP + the
        # wedge backoff keep reconnects gentle; never hammer this lock.
        self._last_round_wedged = False
        cls = _lock_client(self._local_allowed, self._chosen, want_proxy=lambda: True, cache=self._local_cache,
                           use_cached=lambda: PROXY_CACHED_CONNECT and self._cached_ok)
        for handshake_try in range(2):
            if self._blocked():
                return
            client = await self._open_link(cls, ble_device)
            if client is None:
                return
            self._client = client
            if self._blocked():
                await self._disconnect()
                return
            # Pin BEFORE the handshake, after a short settle: the lock's own L2CAP
            # conn-param request lands ~0.3 s after connect() returns and the kernel's
            # LE Connection Update for it completes ~0.1 s later; a pin issued in that
            # window is rejected 0x0C (HCI trace 2026-11-06).  _LockClient already
            # skips HA's post-connect MEDIUM load, so after the settle nothing else is in
            # flight.  The pin keeps the lock's 200-400 ms interval and only raises the
            # supervision timeout (CONN_PIN_TIMEOUT) so the lossy -84..-90 dBm link
            # survives the 6-60 s fades that killed every session at the lock's 6 s.
            # The long settle (and both pins) exist for the hci0/BlueZ path only.  Through the
            # bt-proxy _pin_conn_params returns at once (the proxy firmware owns the link
            # parameters) and the lock's own request is triggered by OUR CCCD write in
            # _handshake, not by a timer, so there is nothing to wait out: use PROXY_SETTLE.
            # Only a link positively known to be remote takes it.  The time saved is kept in
            # _settle_credit and added to the backoff if this round fails, so failed and
            # wedge-looking rounds come no closer together than before.
            on_proxy = self._chosen.get("local") is False
            settle = PIN_SETTLE
            if on_proxy:
                # A cached connect returns right at link-up, an uncached one ~0.25 s later.
                settle = PROXY_SETTLE_CACHED if self._chosen.get("cached") else PROXY_SETTLE
                self._settle_credit = max(0.0, PIN_SETTLE - settle)
            await asyncio.sleep(settle)
            if self._blocked() or self._client is not client or not client.is_connected:
                await self._disconnect()
                return
            await self._pin_conn_params(client)
            try:
                hs_start = time.monotonic()
                await self._handshake(client)
                self.last_handshake_s = round(time.monotonic() - hs_start, 2)
                if self._blocked():
                    await self._disconnect()
                    return
                await self._pin_conn_params(client)
                self.connect_count += 1
                self.last_error = None
                self._no_reply_streak = 0
                self._cached_ok = True
                self._last_round_wedged = False
                if self._last_drop_mono is not None:
                    # Full outage: from losing the last session to this one being up (includes
                    # any failed attempts + back-off), i.e. how long the reconnect actually took.
                    self.last_reconnect_s = round(time.monotonic() - self._last_drop_mono, 2)
                    self._last_drop_mono = None
                _LOGGER.info("RV lock: session up in %.2f s (connect %.2f s, MTU payload %d, %s)",
                             self.last_handshake_s, self.last_connect_s or -1, self._chunk,
                             "write with response" if self._with_response else "write without response")
                self._notify()
                await self._after_pair()
                return
            except Exception as err:  # noqa: BLE001
                self.last_error = f"handshake: {err!r}"
                _LOGGER.info("RV lock: %s", self.last_error)
                if self._chosen.get("cached"):
                    self._cached_ok = False   # the next round takes the proven uncached path
                if self._client is client and client.is_connected:
                    # Handshake failed on an OPEN link = login wedge (lock ignores 0x0000).
                    # Do NOT retry on this round: another connect+handshake now just churns a
                    # wedged lock.  Flag it so _run backs off hard, and bail.
                    self._last_round_wedged = True
                else:
                    # The LINK itself dropped mid-handshake (weak signal -> supervision
                    # timeout; the GATT op then fails or the reply never lands).  The lock
                    # was answering until the radio gave out, so this is RF, not a firmware
                    # wedge.  Counting it as a wedge escalated the quiet window to 30 min
                    # and kept a reachable lock "down" for days (2026-11-01..06).  Take the
                    # normal failed-round backoff (ROUND_RETRY) and wake on the next advert.
                    self._last_round_wedged = False
                    _LOGGER.info("RV lock: link dropped during the handshake (RF, not a wedge)")
                await self._disconnect()
                return

    async def _open_link(self, cls, ble_device) -> BleakClient | None:
        """Connect through the proxy with a per-attempt limit the proxy can meet.

        bleak_retry_connector hard-codes 20 s per attempt; this proxy often needs
        15-20 s (connect + discovery at the lock's slow interval).  Giving up just
        before the proxy finishes leaves it holding an unowned link, which the
        lock then reserves for it: every later connect bounces off at once.  So
        allow CONNECT_ATTEMPT_TIMEOUT, and if attempts bounce off instantly,
        tell the proxy to drop whatever link it still holds to the lock.
        """
        quick_fails = 0
        for attempt in range(CONNECT_ATTEMPTS):
            if self._blocked():
                return None
            self._connect_attempt = attempt
            # HARD floor between ANY two real connect attempts.  Back-to-back connects
            # wedged this lock's firmware on 2026-10-26 (5 in 18 s), so this is a plain
            # sleep a user command cannot shorten: better to answer late than hammer the
            # lock into a state only a reboot recovers.  The ONLY shortened case is the
            # first attempt right after a clean self-drop of a healthy link (CLEAN_DROP_GAP);
            # any failed/aborted connect keeps the full CONNECT_MIN_GAP.
            floor = CLEAN_DROP_GAP if (attempt == 0 and self._clean_drop) else CONNECT_MIN_GAP
            self._clean_drop = False
            wait = floor - (time.monotonic() - self._last_connect_started)
            # Also wait out the hci0 spacing (LOCAL_ATTEMPT_GAP after the previous attempt
            # ended).  Without this the next attempt fired 1 s after a failure, found hci0
            # "not allowed yet", failed instantly with "no scanner can reach the lock" and
            # burned one of the round's attempts (2 of every 4 were such no-ops).
            wait = max(wait, self._local_next - time.monotonic())
            if wait > 0:
                await asyncio.sleep(wait)
                if self._blocked():
                    return None
            if time.monotonic() < self._local_blocked_until:
                return None  # breaker open: nothing can be attempted this round
            prev_connect_started = self._last_connect_started
            self._last_connect_started = time.monotonic()
            await self._ensure_no_stale_link()
            device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True) or ble_device
            client = cls(device, disconnected_callback=self._on_disconnected, _is_retry_client=True)
            self._chosen.clear()
            started = time.monotonic()
            try:
                await client.connect(timeout=CONNECT_ATTEMPT_TIMEOUTS[min(attempt, len(CONNECT_ATTEMPT_TIMEOUTS) - 1)])
                self.last_connect_s = round(time.monotonic() - started, 2)
                self._round_attempted = True
                if self._chosen.get("local"):
                    self._local_fail_streak = 0
                    self._last_local_use = time.monotonic()
                _LOGGER.info("RV lock: connected via %s (rssi %s) in %.2f s%s", self._chosen.get("source"),
                             self._chosen.get("rssi"), self.last_connect_s,
                             " (cached path)" if self._chosen.get("cached") else "")
                return client
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.last_error = f"connect attempt {attempt + 1} via {self._chosen.get('source', '?')}: {err!r}"
                _LOGGER.debug("RV lock: %s", self.last_error)
                if self._chosen.get("cached"):
                    self._cached_ok = False   # the next attempt takes the proven uncached path
                if not self._chosen:
                    # No scanner was picked (hci0 has not heard the lock in its current
                    # window): nothing went on the air.  Do not charge the connect floor or
                    # one of the round's attempts for it; end the round and let the next
                    # advert start a fresh one.
                    self._last_connect_started = prev_connect_started
                    return None
                self._round_attempted = True
                if self._chosen.get("local"):
                    self._last_local_use = time.monotonic()
                    if _is_slot_error(err):
                        self._local_fail_streak += 1
                    else:
                        self._local_fail_streak = 0   # radio failure: never benches the adapter
                    if self._local_fail_streak >= LOCAL_FAIL_LIMIT:
                        self._local_blocked_until = time.monotonic() + LOCAL_BREAKER_SECONDS
                        self._local_fail_streak = 0
                        _LOGGER.warning("RV lock: %d connects in a row refused for lack of adapter slots; "
                                        "not using the local adapter for %d min",
                                        LOCAL_FAIL_LIMIT, LOCAL_BREAKER_SECONDS // 60)
                elif time.monotonic() - started < 2:
                    quick_fails += 1
                if quick_fails >= 2:
                    await self._clear_proxy_link()
                    quick_fails = 0
                await asyncio.sleep(CONNECT_RETRY_GAP)
            finally:
                if self._chosen.get("local"):
                    self._local_next = time.monotonic() + LOCAL_ATTEMPT_GAP
        return None

    def _local_allowed(self) -> bool:
        now = time.monotonic()
        return now >= self._local_next and now >= self._local_blocked_until

    def _shared_adapter_watchdog(self) -> None:
        """If another BLE device on hci0 drops while the lock has been using hci0: stop using hci0, then repair hci0.

        Off (early return below): it benched the lock off hci0 for 30 min on a transient blip of the
        other device and did not lift the bench when that device came back.  To use it, set
        SHARED_DEVICE_STATUS_ENTITY and remove the early return.
        """
        self._shared_down_since = None
        return
        st = self.hass.states.get(SHARED_DEVICE_STATUS_ENTITY)
        now = time.monotonic()
        if st is None or st.state == "Connected":
            self._shared_down_since = None
            return
        if now - self._last_local_use > 600:
            return  # not related to the lock's use of hci0
        if self._shared_down_since is None:
            self._shared_down_since = now
            self._local_blocked_until = now + SHARED_DEVICE_BREAKER_SECONDS
            _LOGGER.warning("RV lock: the watched BLE device disconnected while the lock used hci0; lock stays off hci0 for 30 min")
            persistent_notification.async_create(
                self.hass, "Another Bluetooth device disconnected while the RV lock was using Home Assistant's own Bluetooth adapter. "
                "The lock stopped using that adapter for 30 minutes.", "RV lock", "rv_lock_adapter_guard")
        elif now - self._shared_down_since > 180 and now - self._last_hci_reset > 1800:
            self._last_hci_reset = now
            self.hass.async_create_background_task(self._reset_hci0(), "rv_lock_hci0_reset")

    async def _reset_hci0(self) -> None:
        """Documented recovery for leaked controller slots: power-cycle hci0, pairable on."""
        _LOGGER.warning("RV lock: the watched BLE device is still down; power-cycling hci0 (slot-leak recovery)")
        for args, pause in ((("power", "off"), 3), (("power", "on"), 2), (("pairable", "on"), 0)):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bluetoothctl", *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                async with asyncio.timeout(20):
                    await proc.wait()
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("RV lock: bluetoothctl %s failed: %r", " ".join(args), err)
            await asyncio.sleep(pause)

    def _proxy_holds_lock(self) -> list[str]:
        """Sources of remote scanners that still have a slot allocated to the lock."""
        held = []
        for scanner in bluetooth.async_current_scanners(self.hass):
            if getattr(scanner, "adapter_idx", None) is not None:
                continue
            alloc = scanner.get_allocations()
            if alloc and self.address in [a.upper() for a in alloc.allocated]:
                held.append(scanner.source)
        return held

    async def _ensure_no_stale_link(self) -> None:
        """A link left open on a proxy makes the lock refuse everyone (HA and the phone app)."""
        if self.connected:
            return
        if self._proxy_holds_lock():
            await self._clear_proxy_link()
            await asyncio.sleep(1)

    async def _clear_proxy_link(self) -> None:
        """Ask the ESPHome proxy (through HA's own API connection) to drop its link to the lock."""
        holding = {s.upper() for s in self._proxy_holds_lock()}
        want = (self._proxy or "").upper()
        addr = int(self.address.replace(":", ""), 16)
        for entry in self.hass.config_entries.async_loaded_entries("esphome"):
            rd = getattr(entry, "runtime_data", None)
            info = getattr(rd, "device_info", None)
            mac = (getattr(info, "bluetooth_mac_address", "") or "").upper()
            if not mac or (mac not in holding and mac != want):
                continue
            try:
                await rd.client.bluetooth_device_disconnect(addr, timeout=10)
                if mac in holding:
                    _LOGGER.warning("RV lock: cleared a stale proxy link to the lock on %s", mac)
                else:
                    # Precautionary drop on the configured proxy (its allocations did NOT list
                    # the lock).  Reached via quick "no scanner can reach the lock yet" fails,
                    # i.e. the lock is simply out of reach -- debug, not the 2026-10-26 signature.
                    _LOGGER.debug("RV lock: precautionary proxy drop on %s (it held no link)", mac)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("RV lock: clearing proxy link on %s: %r", mac, err)

    async def _acquire_local_mtu(self, client: BleakClient) -> int:
        """Force the ATT MTU exchange on the BlueZ/hci0 path.

        Bleak's BlueZ backend exposes `_acquire_mtu()`, which runs a single
        D-Bus AcquireWrite on a write-without-response characteristic purely to
        read back the negotiated MTU (it closes the fd immediately; normal
        writes keep working).  The ESPHome proxy backend has no such method and
        negotiates 246 on its own, so this runs only for the local adapter.
        """
        backend = getattr(client, "_backend", None)
        acquire = getattr(backend, "_acquire_mtu", None)
        if acquire is None or getattr(backend, "services", None) is None:
            return 23
        try:
            await acquire()
            mtu = getattr(backend, "mtu_size", 23) or 23
            _LOGGER.info("RV lock: negotiated MTU %d on the local adapter", mtu)
            return mtu
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("RV lock: local MTU negotiation failed: %r", err)
            return 23

    async def _handshake(self, client: BleakClient) -> None:
        self._reset_session()
        char = client.services.get_characteristic(WRITE_CHAR)
        if char is None:
            raise BleakError("lock write characteristic not found")
        self._with_response = "write" in char.properties
        mtu = getattr(client, "mtu_size", 23) or 23
        # Only the ESPHome proxy auto-negotiates MTU 246 shortly after connect, so
        # wait for it there.  The local (hci0) path never does; skip the poll and
        # force the exchange at once instead of burning ~5 s before every command.
        if not self._chosen.get("local"):
            for _ in range(25):  # ESPHome proxies negotiate MTU 246 just after connect
                if mtu > 23:
                    break
                await asyncio.sleep(0.2)
                mtu = getattr(client, "mtu_size", 23) or 23
        notify_started = False
        if mtu <= 23:
            # The local (BlueZ/hci0) path does not negotiate the ATT MTU on its own,
            # so it is stuck at 23 (20-byte packets, ~4-6 s commands) while the proxy
            # path reaches 246.  Force the exchange via BlueZ so hci0 is fast too.
            # (1) Do it CONCURRENTLY with the notify subscribe: independent D-Bus ops on
            # different characteristics, each a round trip.  Overlapping them takes one
            # round trip off the login critical path, so the handshake more often finishes
            # inside the lock's ~1 s fast-interval window before it slows the link to
            # 400 ms (where each round trip costs ~1 s and the handshake takes 3-5 s).
            # The MTU is still known BEFORE device-info, so the lock is told the full
            # payload size (deferring it past login would lock the session to 20-byte
            # replies -- the old 4-6 s commands).
            if self._chosen.get("local"):
                acquired, _ = await asyncio.gather(
                    self._acquire_local_mtu(client),
                    client.start_notify(NOTIFY_CHAR, self._on_notify))
                notify_started = True
            else:
                acquired = await self._acquire_local_mtu(client)
            if acquired > 23:
                mtu = acquired
        self._chunk = max(20, min(mtu - 3, 243))
        if mtu <= 23:
            _LOGGER.warning("RV lock: MTU still 23 after 5 s; using 20-byte packets (slower commands)")
        else:
            _LOGGER.debug("RV lock: MTU %d (payload %d)", mtu, self._chunk)
        if not notify_started:
            await client.start_notify(NOTIFY_CHAR, self._on_notify)
        _LOGGER.debug("RV lock: notify enabled")   # timing mark: the lock's own conn-param request follows this
        info = await self._request_retry(P.CMD_DEVICE_INFO, P.device_info_data(self._chunk), P.SEC_LOGIN)
        parsed = P.parse_device_info(info[3])
        if parsed is None:
            raise BleakError("bad device info")
        self._keys[P.SEC_SESSION] = P.session_key(self._local_key, parsed["srand"])
        pair = await self._request_retry(P.CMD_PAIR, P.pair_data(self._uuid, self._local_key, self._dev_id), P.SEC_SESSION)
        result = pair[3][:1]
        if result not in (b"\x00", b"\x02"):
            raise BleakError(f"pair refused ({result.hex()})")
        self._dps_sn = 0
        self._paired.set()

    async def _ensure_trusted(self) -> None:
        """Mark the lock Trusted in BlueZ so its device object is kept.

        BlueZ drops an untrusted, unbonded device TemporaryTimeout (195 s on HAOS) after
        the adapter last heard it; with no device object nothing can connect until the
        scanner happens to catch another advert.  Trusted devices are stored and stay.
        Undo: `bluetoothctl untrust <address>` in the homeassistant container.
        """
        if self._local_cache.get("trusted"):
            return
        device = self._local_cache.get("device")
        path = (getattr(device, "details", None) or {}).get("path")
        if not path:
            return
        try:
            from dbus_fast import BusType, Message, MessageType, Variant  # noqa: PLC0415
            from dbus_fast.aio import MessageBus  # noqa: PLC0415
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                reply = await bus.call(Message(
                    destination="org.bluez", path=path, interface="org.freedesktop.DBus.Properties",
                    member="Set", signature="ssv", body=["org.bluez.Device1", "Trusted", Variant("b", True)]))
            finally:
                bus.disconnect()
            if reply is not None and reply.message_type == MessageType.METHOD_RETURN:
                self._local_cache["trusted"] = True
                _LOGGER.info("RV lock: marked %s Trusted in BlueZ (device object is kept)", path)
            else:
                _LOGGER.debug("RV lock: could not mark the lock Trusted: %s", getattr(reply, "body", None))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("RV lock: could not mark the lock Trusted: %r", err)

    async def _after_pair(self) -> None:
        self.hass.async_create_background_task(self._ensure_trusted(), f"rv_lock_trust_{self.address}")
        self.hass.async_create_background_task(self._sync(self.connect_count), f"rv_lock_sync_{self.address}")
        if self.creds is not None and self.creds.pending_enrolment:
            self.hass.async_create_background_task(self.creds.recover_pending(), f"rv_lock_recover_{self.address}")

    async def _sync(self, session: int) -> None:
        """SDK: one empty queryDps.  Lock panel: dp69 check + queryDps(0x55),
        which makes the lock report its battery (dp8).  Never holds the command
        lock and gives way to a pending user command or a newer session."""
        try:
            if self._cmd_lock.locked():
                return  # a user command is waiting: it will read state itself
            await self._send(P.CMD_QUERY_DPS, b"", P.SEC_SESSION)
            if self.connect_count != session or self._cmd_lock.locked():
                return
            # The dp69 check + queryDps(0x55) battery read only needs to run once a day:
            # battery drifts slowly and the empty queryDps above already syncs bolt state.
            # Doing it on every ~4.5 min pre-emptive reconnect was 3 extra frames (incl. a
            # write) per login for no new information.
            if self.battery is not None and time.monotonic() - self._last_battery_sync < BATTERY_SYNC_INTERVAL:
                return
            self._dps_sn += 1
            check = b"\xff\xff\x00\x01" + self._passcode.encode() + b"\x00"
            await self._request(P.CMD_DP_WRITE, P.dp_write_data(self._dps_sn, 69, P.DP_TYPE_RAW, check),
                                P.SEC_SESSION, timeout=6)
            if self.connect_count != session:
                return
            await self._send(P.CMD_QUERY_DPS, b"\x55", P.SEC_SESSION)
            self._last_battery_sync = time.monotonic()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("RV lock: sync: %r", err)

    def _fail_link_lost(self) -> None:
        if self._link_lost is not None and not self._link_lost.done():
            self._link_lost.set_result(None)
        self._link_lost = None

    def _reset_session(self) -> None:
        self._fail_link_lost()
        self._link_lost = self.hass.loop.create_future()
        self._sn = 1
        self._asm.reset()
        self._paired.clear()
        self._keys.pop(P.SEC_SESSION, None)
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(BleakError("disconnected"))
        self._waiters.clear()

    @callback
    def _on_disconnected(self, client: BleakClient) -> None:
        if client in self._detached:
            self._detached.discard(client)
            return  # we detached this link ourselves (pre-emptive reconnect): already handled
        if self._client is not None and client is not self._client:
            return  # stale callback from a previous client
        _LOGGER.info("RV lock: disconnected")
        if self._paired.is_set() and self._last_drop_mono is None:
            self._last_drop_mono = time.monotonic()  # start of an outage (lock dropped a real session)
        self._clean_drop = self._paired.is_set()  # healthy link dropped -> first reconnect may be quicker
        self._fail_link_lost()
        self._client = None
        self._paired.clear()
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(BleakError("disconnected"))
        self._waiters.clear()
        self._notify()
        # Don't pre-empt a wedge backoff: a wedged round disconnects itself, and
        # waking here would skip the long quiet window the backoff just scheduled.
        # Only a healthy-session drop (not wedged) reconnects right away. 2026-10-27.
        if not self._last_round_wedged:
            self._wake.set()  # app behaviour: reconnect right away

    def _detach_client(self) -> BleakClient | None:
        """Drop our reference to the current link WITHOUT waiting for BlueZ to confirm the
        disconnect (the pre-emptive reconnect's non-blocking teardown).  Stamps the outage
        start, clears session state and fails pending waiters so nothing can use the old
        link, and remembers the client so its late disconnected-callback is ignored."""
        if self._paired.is_set() and self._last_drop_mono is None:
            self._last_drop_mono = time.monotonic()
        client, self._client = self._client, None
        self._paired.clear()
        if client is not None:
            self._detached.add(client)
        self._fail_link_lost()
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(BleakError("disconnected"))
        self._waiters.clear()
        return client

    async def _bg_disconnect(self, client: BleakClient) -> None:
        """Finish a detached client's disconnect off the critical path.

        Returns once BlueZ has released the link (or after a 20 s bound -- at weak signal
        the terminate ack / supervision timeout can take 10+ s).  The client then stays in
        _detached for a further 60 s so its late disconnected-callback is still recognised
        and ignored (a 5 s grace was too short on 2026-10-28: the callback escaped the guard).
        """
        try:
            async with asyncio.timeout(20):
                await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.hass.async_create_background_task(self._forget_detached(client), f"rv_lock_forget_{self.address}")

    async def _forget_detached(self, client: BleakClient) -> None:
        await asyncio.sleep(300)
        self._detached.discard(client)

    async def _disconnect(self) -> None:
        if self._paired.is_set() and self._last_drop_mono is None:
            self._last_drop_mono = time.monotonic()  # we are ending a real session (e.g. pre-emptive reconnect)
        client, self._client = self._client, None
        self._paired.clear()
        if client is not None:
            try:
                async with asyncio.timeout(10):
                    await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- framing
    async def _send(self, cmd: int, data: bytes, flag: int, ack_sn: int = 0, fut: asyncio.Future | None = None) -> int:
        async with self._tx:
            client = self._client
            if client is None or not client.is_connected or flag not in self._keys:
                raise BleakError("not connected")
            key = self._keys[flag]
            sn = self._sn
            self._sn += 1
            if fut is not None:
                self._waiters[sn] = fut  # registered before the write: replies can be fast
            for packet in P.build_frame(sn, ack_sn, cmd, data, key, flag, self._chunk):
                async with asyncio.timeout(3):
                    await client.write_gatt_char(WRITE_CHAR, packet, response=self._with_response)
        return sn

    async def _request(self, cmd: int, data: bytes, flag: int, timeout: float = STEP_TIMEOUT):
        fut = self.hass.loop.create_future()
        sn = None
        try:
            sn = await self._send(cmd, data, flag, fut=fut)
            return await asyncio.wait_for(fut, timeout)
        finally:
            for k, v in list(self._waiters.items()):
                if v is fut:
                    self._waiters.pop(k, None)

    async def _request_retry(self, cmd: int, data: bytes, flag: int):
        """Handshake step: one send, HANDSHAKE_STEP_TIMEOUT, no resend (what the Smart Life app does);
        a timeout fails the handshake and the link is dropped."""
        return await self._request(cmd, data, flag, timeout=HANDSHAKE_STEP_TIMEOUT)

    def _on_notify(self, _char, data: bytearray) -> None:
        payload = self._asm.feed(bytes(data))
        if payload is None:
            return
        frame = P.decrypt_frame(payload, self._keys)
        if frame is None:
            _LOGGER.debug("RV lock: undecodable frame")
            return
        sn, ack_sn, cmd, body = frame
        _LOGGER.debug("RV lock: rx sn=%d ack=%d cmd=0x%04x %s", sn, ack_sn, cmd, body.hex())
        if ack_sn and (fut := self._waiters.get(ack_sn)) is not None and not fut.done():
            fut.set_result(frame)  # matched by our sn; command type checked by the caller
        if cmd in (P.CMD_TIME1, P.CMD_TIME2, P.CMD_TIME_LOCAL):
            data = {P.CMD_TIME1: P.time1_data, P.CMD_TIME2: P.time2_data, P.CMD_TIME_LOCAL: P.time_local_data}[cmd]()
            self.hass.async_create_task(self._reply(cmd, data, 0 if cmd == P.CMD_TIME_LOCAL else sn))
        elif cmd in (P.CMD_DP_REPORT, P.CMD_DP_REPORT_TIME):
            head, need_ack, dps = P.parse_report(body, time_prefix=cmd == P.CMD_DP_REPORT_TIME)
            if not head:
                self.hass.async_create_task(self._reply(cmd, b"\x00\x00\x00\x00\x00\x00\x00\x01", sn))
            elif need_ack:
                self.hass.async_create_task(self._reply(cmd, P.report_ack_data(head), sn))
            self._apply_dps(dps, offline=cmd == P.CMD_DP_REPORT_TIME)

    async def _reply(self, cmd: int, data: bytes, ack_sn: int) -> None:
        try:
            await self._send(cmd, data, P.SEC_SESSION, ack_sn=ack_sn)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("RV lock: reply 0x%04x failed: %s", cmd, err)

    def _apply_dps(self, dps: list[tuple[int, int, bytes]], offline: bool = False) -> None:
        changed = False
        for dp, _typ, val in dps:
            if (q := self._dp_queues.get(dp)) is not None:
                q.put_nowait(val)
            if dp in UNLOCK_DPS and val:
                ident = int.from_bytes(val, "big")
                if ident and not (dp == DP_UNLOCK_BLE_COUNT and (
                        self._internal_unlock or time.monotonic() < self._suppress_app_until)):
                    self._unlock_event(dp, ident, offline)
            if not val:
                continue
            ival = int.from_bytes(val, "big")
            if dp == DP_MOTOR_STATE:
                self.locked = ival == 0
                self.jammed = False
                self._dp47_gen += 1
                self._state_event.set()
                changed = changed or not self._internal_unlock
            elif dp == DP_BATTERY:
                self.battery, changed = ival, True
        if changed:
            self._notify()

    def _unlock_event(self, dp: int, ident: int, offline: bool) -> None:
        now = time.monotonic()
        key = (dp, ident)
        if now - self._unlock_seen.get(key, -1e9) < 5:
            return  # the same record reported twice (report + queryDps answer)
        self._unlock_seen[key] = now
        method = UNLOCK_DPS[dp]
        name = self.creds.name_for(method, ident) if self.creds else None
        label = {"fingerprint": "Fingerprint", "pin": "PIN", "temporary_pin": "Temporary PIN", "card": "Card",
                 "key": "Key", "app": "App/HA", "dynamic_pin": "Offline code", "remote": "Remote",
                 "voice": "Voice"}.get(method, method)
        text = name or (f"{label} #{ident}" if method not in ("key", "app") else label)
        self.last_unlock = {"method": method, "slot": ident, "name": name, "label": text, "offline_record": offline}
        self.hass.bus.async_fire("rv_lock_unlocked", dict(self.last_unlock))
        for cb in list(self.unlock_listeners):
            cb(self.last_unlock)

    # --------------------------------------------------------- credentials
    async def dp_transaction(self, dp: int, value: bytes, reply_dp: int, accept: Callable[[bytes], bool],
                             timeout: float, progress: Callable[[bytes], None] | None = None) -> bytes:
        """Write a raw DP and wait for the lock's report on `reply_dp` that `accept` returns True for.

        The command lock is held only for the write and its ack, so lock/unlock
        presses are not stuck behind a long fingerprint enrolment.  Raises
        HomeAssistantError with a readable message on refusal, drop or timeout.
        """
        async with self._cred_lock:
            self._idle_parked = False      # a credential op also cancels idle-disconnect + wakes a connect
            self._last_cmd_mono = time.monotonic()
            q: asyncio.Queue = asyncio.Queue()
            try:
                if not self.connected:
                    self._wake.set()
                    with suppress(TimeoutError, asyncio.TimeoutError):
                        await asyncio.wait_for(self._paired.wait(), COMMAND_DEADLINE)
                async with self._cmd_lock:
                    if not self.connected:
                        self._wake.set()
                        try:
                            await asyncio.wait_for(self._paired.wait(), COMMAND_DEADLINE)
                        except (TimeoutError, asyncio.TimeoutError) as err:
                            raise HomeAssistantError(
                                f"RV lock not reachable ({self.last_error or 'connecting'})") from err
                    self._dp_queues[reply_dp] = q
                    link_lost = self._link_lost
                    self._dps_sn += 1
                    try:
                        frame = await self._request(
                            P.CMD_DP_WRITE, P.dp_write_data(self._dps_sn, dp, P.DP_TYPE_RAW, value),
                            P.SEC_SESSION, timeout=ACK_TIMEOUT)
                    except (BleakError, TimeoutError, asyncio.TimeoutError) as err:
                        raise HomeAssistantError(f"RV lock: no reply to DP {dp} write ({err!r})") from err
                    if frame[2] != P.CMD_DP_WRITE or (len(frame[3]) >= 6 and frame[3][5] != 0):
                        raise HomeAssistantError(f"RV lock refused DP {dp} write ({frame[3].hex()})")
                try:
                    async with asyncio.timeout(timeout):
                        while True:
                            getter = asyncio.ensure_future(q.get())
                            try:
                                waits = {getter} | ({link_lost} if link_lost is not None else set())
                                done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                                if getter not in done:
                                    raise AnswerMissing("RV lock: link dropped before the lock answered")
                                val = getter.result()
                            finally:
                                getter.cancel()
                            if progress is not None:
                                progress(val)
                            if accept(val):
                                return val
                except (TimeoutError, asyncio.TimeoutError) as err:
                    raise AnswerMissing(f"RV lock: no answer on DP {reply_dp} within {timeout:.0f} s") from err
            finally:
                if self._dp_queues.get(reply_dp) is q:
                    self._dp_queues.pop(reply_dp, None)

    # ------------------------------------------------------------ commands
    async def _command_locked(self, dp: int, typ: int, value_fn, desc: str, locked: bool, start: float,
                              publish: bool = True) -> None:
        """Send one lock/unlock until confirmed.  Caller holds _cmd_lock.

        Confirmed by the lock's ack to this write (status 0), or by a NEW bolt
        report (dp47) in the wanted state when that is a change from before the
        command (an old report can't confirm a command that hasn't run).  With no
        confirmation the bolt is read back before anything is reported as failed.
        If the link drops first, the command is resent once (fresh unlock
        timestamp).  Everything counts against COMMAND_DEADLINE from the press.
        """
        deadline = start + COMMAND_DEADLINE
        self.released_until = 0.0
        self._idle_parked = False          # a command cancels idle-disconnect and wakes a connect
        self._last_cmd_mono = time.monotonic()
        if self._verify_task is not None:
            self._verify_task.cancel()
            self._verify_task = None
        last = None
        for _attempt in range(2):
            if not self.connected:
                self._wake.set()
                try:
                    await asyncio.wait_for(self._paired.wait(), max(0.1, deadline - time.monotonic()))
                except (TimeoutError, asyncio.TimeoutError):
                    last = last or f"lock not reachable ({self.last_error or 'connecting'})"
                    break
            if time.monotonic() >= deadline:
                last = last or "timed out waiting for earlier commands"
                break
            pre_state = self.locked
            gen0 = self._dp47_gen
            self._dps_sn += 1
            dps_sn = self._dps_sn
            fut = self.hass.loop.create_future()
            moved = None
            dropped = False
            try:
                try:
                    await self._send(P.CMD_DP_WRITE, P.dp_write_data(dps_sn, dp, typ, value_fn()),
                                     P.SEC_SESSION, fut=fut)
                    if not publish:
                        self._unlock_written = True
                except Exception as err:  # noqa: BLE001
                    last = f"write: {err!r}"
                    dropped = "not connected" in str(err)
                waits = []
                if not dropped:
                    waits.append(fut)
                if pre_state is not locked:  # a bolt report can only confirm an actual change
                    moved = asyncio.ensure_future(self._wait_report(gen0, locked))
                    waits.append(moved)
                done: set = set()
                if waits:
                    done, _ = await asyncio.wait(
                        waits, timeout=max(0.1, min(ACK_TIMEOUT, deadline - time.monotonic())),
                        return_when=asyncio.FIRST_COMPLETED)
            finally:
                if moved is not None:
                    moved.cancel()
                for k, v in list(self._waiters.items()):
                    if v is fut:
                        self._waiters.pop(k, None)
            if moved is not None and moved in done:
                self._no_reply_streak = 0
                return self._confirmed(desc, start, "bolt report", publish)
            if fut in done and not fut.cancelled():
                if fut.exception() is not None:
                    last = f"link dropped ({fut.exception()!r})"
                else:
                    frame = fut.result()
                    body = frame[3]
                    if frame[2] == P.CMD_DP_WRITE:
                        status = body[5] if len(body) >= 6 else 0
                        if status != 0:
                            raise BleakError(f"lock refused the {desc} (status {status})")
                        self._no_reply_streak = 0
                        self.locked = locked
                        if publish:
                            self._verify_later(locked)
                        return self._confirmed(desc, start, "ack", publish)
                    last = f"unexpected reply 0x{frame[2]:04x}"
            if self.connected and not dropped:
                if pre_state is not locked and await self._read_back(locked, deadline):
                    self._no_reply_streak = 0
                    return self._confirmed(desc, start, "state read-back", publish)
                self._no_reply_streak += 1
                last = last or f"no reply within {ACK_TIMEOUT:.0f} s"
                if self._no_reply_streak >= 2:
                    _LOGGER.warning("RV lock: link up but lock not answering; reconnecting")
                    self.hass.async_create_task(self.async_reconnect())
                break
        raise BleakError(f"{desc} failed: {last}")

    async def _read_back(self, locked: bool, deadline: float) -> bool:
        gen1 = self._dp47_gen
        try:
            await self._send(P.CMD_QUERY_DPS, b"", P.SEC_SESSION)
            async with asyncio.timeout(max(0.1, min(READBACK_TIMEOUT, deadline - time.monotonic() + 5))):
                await self._wait_report(gen1, locked)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _verify_later(self, locked: bool) -> None:
        """After an ack, make sure the bolt really moved (jam detection)."""
        gen0 = self._dp47_gen

        async def _wait_one() -> None:
            while self._dp47_gen == gen0:
                self._state_event.clear()
                await self._state_event.wait()

        async def _check() -> None:
            try:
                async with asyncio.timeout(10):
                    await _wait_one()
            except (TimeoutError, asyncio.TimeoutError):
                try:
                    await self._send(P.CMD_QUERY_DPS, b"", P.SEC_SESSION)
                    async with asyncio.timeout(8):
                        await _wait_one()
                except Exception:  # noqa: BLE001
                    return
            jammed = self.locked is not locked
            if jammed != self.jammed:
                self.jammed = jammed
                if jammed:
                    _LOGGER.warning("RV lock: bolt did not reach %s after the command",
                                    "locked" if locked else "unlocked")
                self._notify()

        self._verify_task = self.hass.async_create_background_task(_check(), f"rv_lock_verify_{self.address}")

    def _confirmed(self, desc: str, start: float, how: str, publish: bool = True) -> None:
        self.last_command_s = round(time.monotonic() - start, 2)
        if desc == "lock":
            persistent_notification.async_dismiss(self.hass, "rv_lock_failed_lock")
        _LOGGER.info("RV lock: %s confirmed by %s in %.2f s", desc, how, self.last_command_s)
        if publish:
            self._notify()

    async def _wait_report(self, gen0: int, locked: bool) -> None:
        while not (self._dp47_gen > gen0 and self.locked is locked):
            self._state_event.clear()
            await self._state_event.wait()
