from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.storage import Store
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import FGCAirClient, FGCAirSession, merge_state_cache
from .const import CONF_DEVICES, CONF_SELECTED_DIDS, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN, PLATFORMS, SIGNAL_STATE_UPDATED, SPEED_TO_FAN

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    if _patch_homekit_climate():
        hass.async_create_task(_async_reload_homekit_after_patch(hass))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if _patch_homekit_climate():
        hass.async_create_task(_async_reload_homekit_after_patch(hass))
    session = None
    if entry.data.get("uid") and entry.data.get("token"):
        session = FGCAirSession(entry.data["uid"], entry.data["token"], entry.data.get("expire_at"))
    client = FGCAirClient(entry.data["username"], entry.data["password"], session)
    store: Store[dict] = Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_state")
    state_cache = await store.async_load() or {}
    update_interval = int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    remove_update_listener = entry.add_update_listener(_async_update_options)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "store": store,
        "state_cache": state_cache,
        "remove_update_listener": remove_update_listener,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    device_map = {str(device.get("did")): device for device in entry.data.get(CONF_DEVICES, []) if isinstance(device, dict)}

    async def handle_ws_update(did: str, attrs: dict[str, object]) -> None:
        device = device_map.get(did) or {"did": did}
        data = hass.data[DOMAIN][entry.entry_id]
        data["state_cache"] = merge_state_cache(data["state_cache"], device, attrs)  # type: ignore[arg-type]
        await data["store"].async_save(data["state_cache"])
        _LOGGER.debug("FGCAir MQTT state update did=%s attrs=%s", did, attrs)
        async_dispatcher_send(hass, SIGNAL_STATE_UPDATED, did)

    client.set_ws_message_callback(handle_ws_update)
    cached_devices = [device for device in entry.data.get(CONF_DEVICES, []) if isinstance(device, dict)]
    await client.start_ws_listener(list(entry.data.get(CONF_SELECTED_DIDS, [])), update_interval, cached_devices)

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
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data and callable(data.get("remove_update_listener")):
        data["remove_update_listener"]()
    if data and isinstance(data.get("client"), FGCAirClient):
        await data["client"].stop_ws_listener()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _patch_homekit_climate() -> bool:
    fan_modes_changed = _patch_homekit_climate_fan_modes()
    modes_changed = _patch_homekit_climate_modes()
    names_changed = _patch_homekit_climate_configured_names()
    return fan_modes_changed or modes_changed or names_changed


def _patch_homekit_climate_fan_modes() -> bool:
    try:
        from homeassistant.components.homekit import type_thermostats
    except (ImportError, RuntimeError) as exc:
        _LOGGER.debug("FGCAir HomeKit climate fan mode patch skipped: %s", exc)
        return False

    changed = False
    for speed, fan_mode in SPEED_TO_FAN.items():
        key = fan_mode.lower()
        if key not in type_thermostats.PRE_DEFINED_FAN_MODES:
            type_thermostats.PRE_DEFINED_FAN_MODES.add(key)
            changed = True
    ordered_speeds = [SPEED_TO_FAN[speed].lower() for speed in range(1, 7)] + [SPEED_TO_FAN[0].lower()]
    for key in ordered_speeds:
        if key in type_thermostats.ORDERED_FAN_SPEEDS:
            type_thermostats.ORDERED_FAN_SPEEDS.remove(key)
        type_thermostats.ORDERED_FAN_SPEEDS.append(key)
        changed = True
    if changed:
        _LOGGER.info("FGCAir HomeKit climate fan modes enabled: %s", list(SPEED_TO_FAN.values()))
    return changed


def _patch_homekit_climate_modes() -> bool:
    try:
        from homeassistant.components.homekit import type_thermostats
        from homeassistant.helpers import entity_registry as er
    except (ImportError, RuntimeError) as exc:
        _LOGGER.debug("FGCAir HomeKit climate mode patch skipped: %s", exc)
        return False

    thermostat_cls = type_thermostats.Thermostat
    if getattr(thermostat_cls, "_fgcair_dry_auto_patch", False):
        return False

    dry_auto_entities: set[str] = set()
    original_configure_hvac_modes = thermostat_cls._configure_hvac_modes
    original_hk_hvac_mode_from_state = type_thermostats._hk_hvac_mode_from_state

    def is_fgcair_entity(self: Any, entity_id: str) -> bool:
        entity_entry = er.async_get(self.hass).async_get(entity_id)
        return entity_entry is not None and entity_entry.platform == DOMAIN

    def fgcair_configure_hvac_modes(self: Any, state: Any) -> None:
        raw_modes = state.attributes.get(type_thermostats.ATTR_HVAC_MODES) or type_thermostats.DEFAULT_HVAC_MODES
        hvac_modes = set(raw_modes)
        if is_fgcair_entity(self, state.entity_id) and type_thermostats.HVACMode.DRY in hvac_modes:
            dry_auto_entities.add(state.entity_id)
            mapping = {}
            for homekit_mode, hass_mode in (
                (type_thermostats.HC_HEAT_COOL_OFF, type_thermostats.HVACMode.OFF),
                (type_thermostats.HC_HEAT_COOL_HEAT, type_thermostats.HVACMode.HEAT),
                (type_thermostats.HC_HEAT_COOL_COOL, type_thermostats.HVACMode.COOL),
                (type_thermostats.HC_HEAT_COOL_AUTO, type_thermostats.HVACMode.DRY),
            ):
                if hass_mode in hvac_modes:
                    mapping[homekit_mode] = hass_mode
            self.hc_homekit_to_hass = mapping
            self.hc_hass_to_homekit = {hass_mode: homekit_mode for homekit_mode, hass_mode in mapping.items()}
            return

        dry_auto_entities.discard(state.entity_id)
        original_configure_hvac_modes(self, state)

    def fgcair_hk_hvac_mode_from_state(state: Any) -> int | None:
        if state.entity_id in dry_auto_entities and state.state == type_thermostats.HVACMode.DRY.value:
            return type_thermostats.HC_HEAT_COOL_AUTO
        return original_hk_hvac_mode_from_state(state)

    thermostat_cls._configure_hvac_modes = fgcair_configure_hvac_modes
    thermostat_cls._fgcair_dry_auto_patch = True
    type_thermostats._hk_hvac_mode_from_state = fgcair_hk_hvac_mode_from_state
    _LOGGER.info("FGCAir HomeKit climate dry mode exposed as Auto")
    return True


def _patch_homekit_climate_configured_names() -> bool:
    try:
        from homeassistant.components.homekit import type_thermostats
        from homeassistant.components.homekit.const import CHAR_CONFIGURED_NAME
        from homeassistant.components.homekit.util import cleanup_name_for_homekit
        from homeassistant.helpers import entity_registry as er
    except (ImportError, RuntimeError) as exc:
        _LOGGER.debug("FGCAir HomeKit configured name patch skipped: %s", exc)
        return False

    thermostat_cls = type_thermostats.Thermostat
    if getattr(thermostat_cls, "_fgcair_configured_name_patch", False):
        return False

    original_init = thermostat_cls.__init__

    def is_fgcair_entity(self: Any) -> bool:
        entity_entry = er.async_get(self.hass).async_get(self.entity_id)
        return entity_entry is not None and entity_entry.platform == DOMAIN

    def fgcair_thermostat_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if not is_fgcair_entity(self):
            return

        name = cleanup_name_for_homekit(self.display_name)
        for service_name in (type_thermostats.SERV_THERMOSTAT, type_thermostats.SERV_FANV2):
            service = self.get_service(service_name)
            if service is None:
                continue
            char = self.driver.loader.get_char(CHAR_CONFIGURED_NAME)
            service.add_characteristic(char)
            if char.broker is None:
                char.broker = self
                self.iid_manager.assign(char)
            service.configure_char(
                CHAR_CONFIGURED_NAME,
                value=name,
            )

    thermostat_cls.__init__ = fgcair_thermostat_init
    thermostat_cls._fgcair_configured_name_patch = True
    _LOGGER.info("FGCAir HomeKit configured names enabled for climate services")
    return True


async def _async_reload_homekit_after_patch(hass: HomeAssistant) -> None:
    key = f"{DOMAIN}_homekit_reloaded"
    if hass.data.get(key):
        return
    hass.data[key] = True
    await asyncio.sleep(15)
    for entry in hass.config_entries.async_entries("homekit"):
        await hass.config_entries.async_reload(entry.entry_id)
        _LOGGER.info("FGCAir reloaded HomeKit bridge %s after enabling climate fan modes", entry.entry_id)
