"""Set up from the existing ha_kohree entry (same lock keys), or by hand."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow

from .const import CONF_ADDRESS, CONF_DEV_ID, CONF_LOCAL_KEY, CONF_PASSCODE, CONF_PROXY, CONF_UUID, DEFAULT_PROXY, DOMAIN


class RvLockFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        for e in self.hass.config_entries.async_entries("ha_kohree"):
            d = e.data
            if d.get("tuya_local_key") and d.get("address"):
                await self.async_set_unique_id(d["address"].upper())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="RV Door Lock",
                    data={
                        CONF_ADDRESS: d["address"].upper(),
                        CONF_LOCAL_KEY: d["tuya_local_key"],
                        CONF_UUID: d["tuya_uuid"],
                        CONF_DEV_ID: d["tuya_dev_id"],
                        CONF_PASSCODE: d.get("tuya_unlock_passcode", "00000000"),
                        CONF_PROXY: DEFAULT_PROXY,
                    },
                )
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ADDRESS].upper())
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="RV Door Lock", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ADDRESS): str,
                vol.Required(CONF_LOCAL_KEY): str,
                vol.Required(CONF_UUID): str,
                vol.Required(CONF_DEV_ID): str,
                vol.Required(CONF_PASSCODE, default="00000000"): str,
                vol.Optional(CONF_PROXY, default=DEFAULT_PROXY): str,
            }),
        )
