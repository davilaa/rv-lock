from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .entity import RvLockEntity

RELEASE_SECONDS = 15 * 60


async def async_setup_entry(hass, entry, async_add_entities):
    s = entry.runtime_data
    async_add_entities([
        LockButton(s, "lock_button"), UnlockButton(s, "unlock_button"),
        Release(s, "release"), Reconnect(s, "reconnect"),
    ])


class LockButton(RvLockEntity, ButtonEntity):
    """Plain button (e.g. for HomeKit/Siri without the secure-lock prompt)."""

    _attr_name = "Lock"
    _attr_icon = "mdi:lock"

    async def async_press(self) -> None:
        try:
            await self.session.async_lock()
        except Exception as err:
            raise HomeAssistantError(f"RV lock: {err}" if str(err) else f"RV lock: {err!r}") from err


class UnlockButton(RvLockEntity, ButtonEntity):
    """Plain button (e.g. for HomeKit/Siri without the secure-lock prompt)."""

    _attr_name = "Unlock"
    _attr_icon = "mdi:lock-open-variant"

    async def async_press(self) -> None:
        try:
            await self.session.async_unlock()
        except Exception as err:
            raise HomeAssistantError(f"RV lock: {err}" if str(err) else f"RV lock: {err!r}") from err


class Release(RvLockEntity, ButtonEntity):
    _attr_name = "Release for app (15 min)"
    _attr_icon = "mdi:cellphone-link"
    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.session.async_release(RELEASE_SECONDS)


class Reconnect(RvLockEntity, ButtonEntity):
    _attr_name = "Reconnect"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        await self.session.async_reconnect()
