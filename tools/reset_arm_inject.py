# One-shot, armed inside the running HA process (sys.remote_exec): the next time rv_lock has an
# authenticated session, send the Tuya factory-reset frame (FRM_DEVICE_RESET 0x0006, session key,
# empty payload).  The lock answers 0x0006 [00], erases its pairing and every PIN/fingerprint and
# reboots.  HA is then parked for 24 h (the stored local key no longer works until the lock is
# re-added in Smart Life and the new key is put in the config entry).
# Destructive and cannot be undone from Home Assistant.  Dies with an HA restart; gives up after 24 h.
# Cancel: inject a script that does  hass.data.pop("rv_lock_reset_armed").cancel()
import asyncio, time, traceback
OUT = "/config/.rvl_reset.log"
def _log(msg):
    with open(OUT, "a") as f:
        f.write(time.strftime("%F %T ") + msg + "\n")
try:
    from homeassistant.core import async_get_hass
    hass = async_get_hass()
    async def _arm():
        from custom_components.rv_lock import protocol as P
        from homeassistant.components import persistent_notification
        deadline = time.monotonic() + 24 * 3600
        _log("armed: waiting for an authenticated session")
        try:
            while time.monotonic() < deadline:
                entries = hass.config_entries.async_loaded_entries("rv_lock")
                sess = entries[0].runtime_data if entries else None
                if sess is not None and sess.connected and not sess._cmd_lock.locked():
                    try:
                        async with sess._cmd_lock:
                            reply = await sess._request(0x0006, b"", P.SEC_SESSION, timeout=15)
                        _log(f"factory reset frame sent; lock replied {reply[3].hex() if reply else reply!r}")
                        sess.released_until = time.monotonic() + 86400
                        await asyncio.sleep(3)
                        await sess._disconnect()
                        sess._notify()
                        persistent_notification.async_create(
                            hass, "The RV lock accepted the factory reset. Its PINs, fingerprints and pairing "
                            "are erased. Re-add it in Smart Life; Home Assistant stays parked until its key is "
                            "updated.", "RV lock", "rv_lock_factory_reset")
                        return
                    except Exception as err:  # noqa: BLE001
                        _log(f"send attempt failed ({err!r}); will retry on the next session")
                        await asyncio.sleep(5)
                await asyncio.sleep(1)
            _log("gave up after 24 h without a usable session")
        except asyncio.CancelledError:
            _log("cancelled"); raise
        except Exception:
            _log("FAILED\n" + traceback.format_exc())
        finally:
            hass.data.pop("rv_lock_reset_armed", None)
    def _start():
        old = hass.data.get("rv_lock_reset_armed")
        if old is not None and not old.done():
            _log("already armed; not starting a second one"); return
        hass.data["rv_lock_reset_armed"] = hass.async_create_background_task(_arm(), "rv_lock_reset_armed")
    hass.loop.call_soon_threadsafe(_start)
except Exception:
    _log("inject FAILED\n" + traceback.format_exc())
