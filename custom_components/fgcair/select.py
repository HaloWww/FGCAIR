from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import indoor_index
from .const import CONF_DEVICES, CONF_SELECTED_DIDS, CONF_TEMP_SOURCE_ENTITY_ID, DOMAIN, SIGNAL_STATE_UPDATED

SELF_OPTION = "自身室温"
TEMPERATURE_DEVICE_CLASSES = {"temperature"}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    devices = _configured_devices(entry)
    async_add_entities([FGCAirTemperatureSourceSelect(hass, entry, device) for device in devices if device.get("did") in selected], True)


def _configured_devices(entry: ConfigEntry) -> list[dict[str, Any]]:
    devices = entry.data.get(CONF_DEVICES)
    if isinstance(devices, list) and devices:
        return [device for device in devices if isinstance(device, dict)]
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    return [{"did": did, "product_name": "FGCAir 室内机", "mac": "", "dev_alias": ""} for did in selected]


class FGCAirTemperatureSourceSelect(SelectEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer"
    _attr_translation_key = "temperature_source"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.device = device
        self.did = str(device["did"])
        self.index = indoor_index(device)
        self._attr_unique_id = f"fgcair_{self.did}_temperature_source"
        self._attr_name = "当前室温来源"
        device_name = _device_name(device, self.index)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.did)},
            "name": device_name,
            "manufacturer": "FGCAir",
            "model": device.get("product_name"),
        }

    @property
    def _cache(self) -> dict[str, Any]:
        return self.hass.data[DOMAIN][self.entry.entry_id]["state_cache"]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_updated))

    def _handle_state_updated(self, did: str) -> None:
        if did == self.did:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    @property
    def options(self) -> list[str]:
        return [SELF_OPTION, *self._temperature_entity_options().keys()]

    @property
    def current_option(self) -> str | None:
        entry = self._cache.get(self.did, {}) if isinstance(self._cache, dict) else {}
        entity_id = entry.get(CONF_TEMP_SOURCE_ENTITY_ID) if isinstance(entry, dict) else None
        if not entity_id:
            return SELF_OPTION
        options = self._temperature_entity_options()
        return next((label for label, option_entity_id in options.items() if option_entity_id == entity_id), SELF_OPTION)

    async def async_select_option(self, option: str) -> None:
        data = self.hass.data[DOMAIN][self.entry.entry_id]
        cache = data["state_cache"]
        device_cache = cache.get(self.did, {}) if isinstance(cache.get(self.did), dict) else {}
        if option == SELF_OPTION:
            device_cache.pop(CONF_TEMP_SOURCE_ENTITY_ID, None)
        else:
            device_cache[CONF_TEMP_SOURCE_ENTITY_ID] = self._temperature_entity_options()[option]
            room_temp = _temperature_from_state(self.hass.states.get(device_cache[CONF_TEMP_SOURCE_ENTITY_ID]))
            if room_temp is not None:
                attrs = device_cache.get("attrs", {}) if isinstance(device_cache.get("attrs"), dict) else {}
                attrs["Roomtemp_indoor_PK4"] = round((room_temp + 75) * 2)
                device_cache["attrs"] = attrs
        cache[self.did] = device_cache
        data["state_cache"] = cache
        await data["store"].async_save(cache)
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self.did)
        self.async_write_ha_state()

    def _temperature_entity_options(self) -> dict[str, str]:
        options: dict[str, str] = {}
        for state in self.hass.states.async_all():
            if not _is_temperature_state(state):
                continue
            name = state.name or state.entity_id
            label = f"{name} ({state.entity_id})"
            options[label] = state.entity_id
        return dict(sorted(options.items(), key=lambda item: item[0]))


def _is_temperature_state(state: Any) -> bool:
    if state.entity_id.startswith("sensor."):
        if state.attributes.get("device_class") in TEMPERATURE_DEVICE_CLASSES:
            return _is_numeric_state(state.state)
        if state.attributes.get("unit_of_measurement") in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT):
            return _is_numeric_state(state.state)
    if state.entity_id.startswith("climate."):
        return _is_numeric_state(state.attributes.get("current_temperature"))
    return False


def _is_numeric_state(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _temperature_from_state(state: Any) -> float | None:
    if state is None:
        return None
    raw_value = state.attributes.get("current_temperature") if state.entity_id.startswith("climate.") else state.state
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if state.attributes.get("unit_of_measurement") == UnitOfTemperature.FAHRENHEIT:
        return round((value - 32) * 5 / 9, 1)
    return value


def _device_name(device: dict[str, Any], index: int | None) -> str:
    if index is not None:
        return f"室内机 {index}"
    return str(device.get("dev_alias") or device.get("product_name") or "FGCAir 室内机")
