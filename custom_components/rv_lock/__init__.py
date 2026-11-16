"""RV lock: Tuya BLE lock driven the way the Smart Life app drives it."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN, CONF_ADDRESS, CONF_DEV_ID, CONF_LOCAL_KEY, CONF_PASSCODE, CONF_PROXY, CONF_UUID, DEFAULT_PROXY
from .creds import CredentialManager
from .session import LockSession

PLATFORMS = [Platform.LOCK, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SWITCH, Platform.NUMBER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    d = entry.data
    session = LockSession(
        hass, d[CONF_ADDRESS], d[CONF_LOCAL_KEY], d[CONF_UUID], d[CONF_DEV_ID],
        d[CONF_PASSCODE], entry.options.get(CONF_PROXY, d.get(CONF_PROXY, DEFAULT_PROXY)),
    )
    creds = CredentialManager(hass, session)
    await creds.async_load()
    session.creds = creds
    entry.runtime_data = session
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await session.async_start()
    _register_services(hass)
    return True


def _creds(hass: HomeAssistant) -> CredentialManager:
    for e in hass.config_entries.async_loaded_entries(DOMAIN):
        return e.runtime_data.creds
    raise HomeAssistantError("RV lock is not set up")


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "create_pin"):
        return
    code = vol.All(cv.string, vol.Match(r"^[0-9]{6,10}$"))

    async def create_pin(call: ServiceCall):
        return await _creds(hass).create_pin(call.data["name"], call.data["code"], call.data.get("admin", False))

    async def create_temporary_pin(call: ServiceCall):
        start = dt_util.as_local(call.data.get("start") or dt_util.now())
        end = dt_util.as_local(call.data["end"])
        return await _creds(hass).create_temporary_pin(
            call.data["name"], call.data["code"], start, end, call.data.get("uses", 0),
            call.data.get("weekdays"), call.data.get("daily_from"), call.data.get("daily_to"))

    async def delete_pin(call: ServiceCall):
        c = _creds(hass)
        name = call.data["name"]
        kinds = [k for k in ("pin", "temporary_pin") if any(
            i["name"].lower() == name.lower() and i["kind"] == k for i in c.items)]
        if not kinds:
            raise HomeAssistantError(f"No PIN named '{name}'")
        return await c.delete(name, kinds[0])

    async def enroll_fingerprint(call: ServiceCall):
        return await _creds(hass).enroll_fingerprint(call.data["name"], call.data.get("admin", False))

    async def delete_fingerprint(call: ServiceCall):
        return await _creds(hass).delete(call.data["name"], "fingerprint")

    async def forget_credential(call: ServiceCall):
        return await _creds(hass).forget(call.data.get("name"))

    async def list_credentials(call: ServiceCall):
        return {"credentials": _creds(hass).snapshot()}

    def dt(value):
        value = cv.datetime(value)
        if value.tzinfo is None:  # UI datetimes are local wall-clock time
            value = value.replace(tzinfo=dt_util.get_default_time_zone())
        return dt_util.as_local(value)

    hass.services.async_register(DOMAIN, "create_pin", create_pin, vol.Schema({
        vol.Required("name"): cv.string, vol.Required("code"): code, vol.Optional("admin"): cv.boolean,
    }), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "create_temporary_pin", create_temporary_pin, vol.Schema({
        vol.Required("name"): cv.string, vol.Required("code"): code,
        vol.Optional("start"): dt, vol.Required("end"): dt,
        vol.Optional("uses", default=0): vol.All(vol.Coerce(int), vol.Range(0, 254)),
        vol.Optional("weekdays"): vol.All(cv.ensure_list, [vol.In(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])]),
        vol.Optional("daily_from"): cv.time, vol.Optional("daily_to"): cv.time,
    }), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "delete_pin", delete_pin, vol.Schema({vol.Required("name"): cv.string}),
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "enroll_fingerprint", enroll_fingerprint, vol.Schema({
        vol.Required("name"): cv.string, vol.Optional("admin"): cv.boolean,
    }), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "delete_fingerprint", delete_fingerprint,
                                 vol.Schema({vol.Required("name"): cv.string}),
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "forget_credential", forget_credential,
                                 vol.Schema({vol.Optional("name"): cv.string}),
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "list_credentials", list_credentials, vol.Schema({}),
                                 supports_response=SupportsResponse.ONLY)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_stop()
    return ok
