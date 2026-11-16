# Hot-reload custom_components.rv_lock inside the running HA process (no core restart).
# Injected with sys.remote_exec; schedules the work on HA's event loop.
import sys, gc, importlib, traceback, time
OUT = "/config/.rvl_hotreload.log"
def _log(msg):
    with open(OUT, "a") as f:
        f.write(time.strftime("%F %T ") + msg + "\n")
try:
    hass = None
    try:
        from homeassistant.core import async_get_hass
        hass = async_get_hass()
    except Exception:
        for o in gc.get_objects():
            if type(o).__name__ == "LockSession":
                hass = o.hass; break
    async def _do():
        try:
            entry = hass.config_entries.async_entries("rv_lock")[0]
            _log(f"unloading entry (state {entry.state})")
            await hass.config_entries.async_unload(entry.entry_id)
            pkg = "custom_components.rv_lock"
            for name in ("const", "protocol", "session", "creds", "entity", "lock", "sensor",
                         "binary_sensor", "button", "switch", "number", "config_flow"):
                mod = sys.modules.get(f"{pkg}.{name}")
                if mod is not None:
                    importlib.reload(mod)
            importlib.reload(sys.modules[pkg])
            ok = await hass.config_entries.async_setup(entry.entry_id)
            c = sys.modules[f"{pkg}.const"]
            _log(f"reloaded; setup ok={ok} state={entry.state} LOCAL_BREAKER_SECONDS={c.LOCAL_BREAKER_SECONDS} "
                 f"has_slot_fn={hasattr(sys.modules[pkg + '.session'], '_is_slot_error')}")
        except Exception:
            _log("FAILED\n" + traceback.format_exc())
        # Also refresh what the loader cached at start-up: manifest (name, version), the list of
        # top-level files (a brand/ folder added later = the integration's icon) and the title
        # in the translation cache.  Separate try: a failure here leaves the reload as it is.
        try:
            import os, json
            from homeassistant import loader
            from homeassistant.helpers import translation
            integ = await loader.async_get_integration(hass, "rv_lock")
            def _read():
                return json.loads((integ.file_path / "manifest.json").read_text()), set(os.listdir(integ.file_path))
            manifest, top = await hass.async_add_executor_job(_read)
            integ.manifest.update(manifest)
            integ._top_level_files = top
            for key, attr in list(vars(type(integ)).items()):
                if type(attr).__name__ == "cached_property":
                    integ.__dict__.pop(key, None)
            translation._async_get_translations_cache(hass).async_invalidate({"rv_lock"})
            await translation.async_load_integrations(hass, {"rv_lock"})
            title = (await translation.async_get_translations(hass, hass.config.language, "title", {"rv_lock"}))
            _log(f"metadata refreshed: name={integ.name!r} version={integ.version} branding={integ.has_branding} title={title}")
        except Exception:
            _log("metadata refresh FAILED\n" + traceback.format_exc())
    hass.loop.call_soon_threadsafe(lambda: hass.async_create_task(_do()))
    _log("scheduled")
except Exception:
    _log("inject FAILED\n" + traceback.format_exc())
