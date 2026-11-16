from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .session import LockSession


class RvLockEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, session: LockSession, key: str) -> None:
        self.session = session
        self._attr_unique_id = f"{session.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, session.address)},
            connections={(CONNECTION_BLUETOOTH, session.address)},
            name="RV Door Lock",
            manufacturer="Kohree (Tuya BLE)",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.session.add_listener(self.async_write_ha_state))
