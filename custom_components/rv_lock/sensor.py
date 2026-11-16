from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory, UnitOfTime
from homeassistant.util import dt as dt_util

from .entity import RvLockEntity

# How often the live "connected for" sensor re-writes while the link is held, so the
# recorder samples the climb (not just the drop-to-0 at each reconnect).
CONNECTED_TICK = timedelta(seconds=30)


async def async_setup_entry(hass, entry, async_add_entities):
    s = entry.runtime_data
    async_add_entities([Battery(s, "battery"), LastCommand(s, "last_command"), Rssi(s, "rssi"),
                        LastReconnect(s, "last_reconnect"), ConnectedSince(s, "connected_since"),
                        LastConnected(s, "last_connected"),
                        LastUnlockedBy(s, "last_unlocked_by"), Enrollment(s, "enrollment"),
                        Credentials(s, "credentials")])


class Credentials(RvLockEntity, SensorEntity):
    """PINs and fingerprints stored by this integration (not the app's own)."""

    _attr_name = "Credentials"
    _attr_icon = "mdi:form-textbox-password"

    @property
    def native_value(self):
        return len(self.session.creds.items) if self.session.creds else 0

    @property
    def extra_state_attributes(self):
        if not self.session.creds:
            return {}
        rows = []
        for it in self.session.creds.snapshot():
            row = {"name": it.get("name"), "kind": it.get("kind"), "state": it.get("state")}
            if it.get("kind") == "temporary_pin":
                row["end"] = it.get("end")
                row["uses"] = it.get("uses")
            if it.get("admin"):
                row["admin"] = True
            rows.append(row)
        return {"items": rows}


class LastUnlockedBy(RvLockEntity, SensorEntity, RestoreEntity):
    """Which credential last opened the lock (fingerprint/PIN/temp PIN/key/app).

    The rv_lock_unlocked event is fired by the session, so it works even if this
    sensor is disabled."""

    _attr_name = "Last unlocked by"
    _attr_icon = "mdi:account-key"

    def __init__(self, session, key) -> None:
        super().__init__(session, key)
        self._value: str | None = None
        self._attrs: dict = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state not in ("unknown", "unavailable"):
            self._value = last.state
            self._attrs = {k: last.attributes.get(k) for k in ("method", "slot", "name", "offline_record")}
        self.session.unlock_listeners.append(self._on_unlock)
        self.async_on_remove(lambda: self.session.unlock_listeners.remove(self._on_unlock))

    def _on_unlock(self, ev: dict) -> None:
        self._value = ev["label"]
        self._attrs = {k: ev.get(k) for k in ("method", "slot", "name", "offline_record")}
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self._value

    @property
    def extra_state_attributes(self):
        return self._attrs


class Enrollment(RvLockEntity, SensorEntity):
    _attr_name = "Fingerprint enrolment"
    _attr_icon = "mdi:fingerprint"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        e = self.session.creds.enrollment if self.session.creds else None
        return e["stage"] if e else "idle"

    @property
    def extra_state_attributes(self):
        e = self.session.creds.enrollment if self.session.creds else None
        return e or {}


class Battery(RvLockEntity, SensorEntity, RestoreEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.session.battery is None and (last := await self.async_get_last_state()) is not None:
            try:
                self.session.battery = int(float(last.state))
            except ValueError:
                pass

    @property
    def native_value(self):
        return self.session.battery


class LastCommand(RvLockEntity, SensorEntity):
    _attr_name = "Last command time"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self.session.last_command_s


class LastReconnect(RvLockEntity, SensorEntity, RestoreEntity):
    """How long the last reconnect took: from losing a session to the next one coming
    up (includes any failed attempts and back-off). Graphs in entity history."""

    _attr_name = "Last reconnect time"
    _attr_icon = "mdi:timer-sync-outline"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.session.last_reconnect_s is None and (last := await self.async_get_last_state()) is not None:
            try:
                self.session.last_reconnect_s = float(last.state)
            except (ValueError, TypeError):
                pass

    @property
    def native_value(self):
        return self.session.last_reconnect_s


class ConnectedSince(RvLockEntity, SensorEntity):
    """When the current BLE link came up; None while disconnected. HA renders a
    timestamp sensor as a live relative age ('3 minutes ago'), so this shows how
    long the lock has stayed connected this session, and its history shows the
    hold/reconnect pattern."""

    _attr_name = "Connected since"
    _attr_icon = "mdi:timer-play-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self.session.connected_since if self.session.connected else None


class LastConnected(RvLockEntity, SensorEntity):
    """How long the current BLE link has been held, in seconds: climbs while connected
    and drops to 0 at each reconnect. A numeric DURATION sensor, so tapping it shows a
    line graph (like 'Last reconnect time'); the sawtooth shows how long each link lasted
    and surfaces any early drops. Re-writes every CONNECTED_TICK while up so the
    recorder captures the climb, not just the resets (which the connect/disconnect
    notifications already record)."""

    _attr_name = "Last connected time"
    _attr_icon = "mdi:timer-check-outline"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, CONNECTED_TICK))

    @callback
    def _tick(self, _now) -> None:
        if self.session.connected:
            self.async_write_ha_state()

    @property
    def native_value(self):
        cs = self.session.connected_since
        if self.session.connected and cs is not None:
            return max(0, round((dt_util.utcnow() - cs).total_seconds()))
        return 0


class Rssi(RvLockEntity, SensorEntity):
    _attr_name = "Signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self.session.rssi
