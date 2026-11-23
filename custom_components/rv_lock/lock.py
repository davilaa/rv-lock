from __future__ import annotations

from homeassistant.components.lock import LockEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.exceptions import HomeAssistantError

from .entity import RvLockEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RvLock(entry.runtime_data, "lock")])


class RvLock(RvLockEntity, LockEntity, RestoreEntity):
    _attr_name = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.session.locked is None and (last := await self.async_get_last_state()) is not None:
            if last.state in ("locked", "unlocked"):
                self.session.locked = last.state == "locked"
                # Seed the confirmed-position baseline across a restart so jam/unconfirmed
                # detection is live immediately (refined by the next real DP 47 report).
                if self.session._bolt_confirmed is None:
                    self.session._bolt_confirmed = self.session.locked

    @property
    def is_locked(self):
        return self.session.locked

    @property
    def is_jammed(self):
        return self.session.jammed

    @property
    def is_locking(self):
        return self.session.locking

    @property
    def available(self) -> bool:
        return True

    async def async_lock(self, **kwargs) -> None:
        try:
            await self.session.async_lock()
        except Exception as err:
            raise HomeAssistantError(f"RV lock: {err}" if str(err) else f"RV lock: {err!r}") from err

    async def async_unlock(self, **kwargs) -> None:
        try:
            await self.session.async_unlock()
        except Exception as err:
            raise HomeAssistantError(f"RV lock: {err}" if str(err) else f"RV lock: {err!r}") from err
