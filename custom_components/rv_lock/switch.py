from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import RvLockEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([IdleDisconnect(entry.runtime_data, "idle_disconnect")])


class IdleDisconnect(RvLockEntity, SwitchEntity, RestoreEntity):
    """Idle-disconnect (battery saver): drop the held link after an idle grace so the
    lock's radio sleeps; a command reconnects on demand.  Off = hold the link always."""

    _attr_name = "Idle disconnect (battery saver)"
    _attr_icon = "mdi:sleep"
    _attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self.session.set_idle_enabled(last.state == "on")

    @property
    def is_on(self) -> bool:
        return self.session.idle_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.session.set_idle_enabled(True)

    async def async_turn_off(self, **kwargs) -> None:
        self.session.set_idle_enabled(False)
