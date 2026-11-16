from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import RvLockEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([IdleTimeout(entry.runtime_data, "idle_timeout")])


class IdleTimeout(RvLockEntity, NumberEntity, RestoreEntity):
    """Seconds the link is held with no command before idle-disconnect drops it."""

    _attr_name = "Idle timeout"
    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 15
    _attr_native_max_value = 1800
    _attr_native_step = 15
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            try:
                self.session.set_idle_seconds(float(last.state))
            except (TypeError, ValueError):
                pass

    @property
    def native_value(self) -> float:
        return self.session.idle_seconds

    async def async_set_native_value(self, value: float) -> None:
        self.session.set_idle_seconds(value)
