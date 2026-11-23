from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import RvLockEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([Connected(entry.runtime_data, "connected"), Reachable(entry.runtime_data, "reachable"),
                        Unconfirmed(entry.runtime_data, "unconfirmed")])


class Unconfirmed(RvLockEntity, BinarySensorEntity):
    """On when a lock/unlock was commanded (and acked) but the bolt never confirmed it moved.

    The ack only means the lock received the command; the only proof of movement is a DP 47
    report.  If a move was expected (the commanded state differs from the last confirmed bolt
    position) and no DP 47 arrives, the door's real state is unknown -- it may be jammed, or the
    report was simply lost.  A redundant command (e.g. locking an already-locked door) never
    turns this on.  Cleared as soon as any DP 47 report arrives.
    """
    _attr_name = "Last command unconfirmed"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self):
        return self.session.unconfirmed


class Connected(RvLockEntity, BinarySensorEntity):
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self):
        return self.session.connected

    @property
    def extra_state_attributes(self):
        return {"last_error": self.session.last_error, "sessions": self.session.connect_count,
                "connect_s": self.session.last_connect_s, "handshake_s": self.session.last_handshake_s}


class Reachable(RvLockEntity, BinarySensorEntity):
    """On while the lock is connected or was heard advertising in the last REACHABLE_WINDOW seconds.

    With idle-disconnect the link is down most of the time on purpose, so "Connected" is not a
    health signal.  Alert on this one instead.
    """
    _attr_name = "Reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self):
        return self.session.reachable

    @property
    def extra_state_attributes(self):
        return {"idle_parked": bool(self.session._idle_parked and not self.session.connected)}
