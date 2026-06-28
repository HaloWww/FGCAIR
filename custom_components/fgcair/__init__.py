from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store

from .api import FGCAirClient, FGCAirSession
from .const import CONF_SELECTED_DIDS, DOMAIN, PLATFORMS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = None
    if entry.data.get("uid") and entry.data.get("token"):
        session = FGCAirSession(entry.data["uid"], entry.data["token"], entry.data.get("expire_at"))
    client = FGCAirClient(entry.data["username"], entry.data["password"], session)
    store: Store[dict] = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_state")
    state_cache = await store.async_load() or {}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"client": client, "store": store, "state_cache": state_cache}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def refresh_token(call: ServiceCall) -> None:
        new_session = await client.ensure_session(force=True)
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "uid": new_session.uid, "token": new_session.token, "expire_at": new_session.expire_at},
        )

    if not hass.services.has_service(DOMAIN, "refresh_token"):
        hass.services.async_register(DOMAIN, "refresh_token", refresh_token)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
