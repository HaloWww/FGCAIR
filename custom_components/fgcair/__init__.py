from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store

from .api import FGCAirClient, FGCAirSession
from .const import CONF_SELECTED_DIDS, DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


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
        _LOGGER.info("FGCAir token refreshed uid=%s expire_at=%s", new_session.uid, new_session.expire_at)

    async def test_control(call: ServiceCall) -> None:
        attrs = {}
        pk_index = int(call.data.get("pk_index", 4))
        if "power" in call.data:
            attrs[f"Power_indoor_PK{pk_index}"] = bool(call.data["power"])
        if "mode" in call.data:
            attrs[f"Mode_indoor_PK{pk_index}"] = int(call.data["mode"])
        if "temperature" in call.data:
            attrs[f"Temp_indoor_PK{pk_index}"] = float(call.data["temperature"])
        if "speed" in call.data:
            attrs[f"Speed_indoor_PK{pk_index}"] = int(call.data["speed"])
        if not attrs:
            raise ValueError("至少需要提供 power、mode、temperature 或 speed 中的一个控制属性")
        result = await client.control_sequence(str(call.data["did"]), attrs)
        _LOGGER.info("FGCAir test_control did=%s attrs=%s result=%s", call.data["did"], attrs, result)

    if not hass.services.has_service(DOMAIN, "refresh_token"):
        hass.services.async_register(DOMAIN, "refresh_token", refresh_token)
    if not hass.services.has_service(DOMAIN, "test_control"):
        hass.services.async_register(
            DOMAIN,
            "test_control",
            test_control,
            schema=vol.Schema(
                {
                    vol.Required("did"): str,
                    vol.Optional("pk_index", default=4): int,
                    vol.Optional("power"): bool,
                    vol.Optional("mode"): int,
                    vol.Optional("temperature"): vol.Coerce(float),
                    vol.Optional("speed"): int,
                }
            ),
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
