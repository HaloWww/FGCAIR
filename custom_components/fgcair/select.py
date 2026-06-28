from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import FGCAirClient, indoor_index, merge_state_cache, state_attrs_from_cache
from .const import (
    CONF_SELECTED_DIDS,
    CONF_TEMP_SOURCE_DID,
    DOMAIN,
    FAN_TO_SPEED,
    LABEL_TO_MODE,
    MODE_TO_LABEL,
    SIGNAL_STATE_UPDATED,
    SPEED_TO_FAN,
)

POWER_PREFIX = "Power_indoor_PK"
MODE_PREFIX = "Mode_indoor_PK"
SPEED_PREFIX = "Speed_indoor_PK"
TEMP_PREFIX = "Temp_indoor_PK"
DEFAULT_MODE = 1
DEFAULT_SPEED = 0
DEFAULT_TEMP = 26
PK_INDEX = 4
TEMP_SOURCE_SELF = "自身室温"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client: FGCAirClient = data["client"]
    devices = await client.list_bindings()
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    indoor_devices = [device for device in devices if device.get("did") in selected]
    entities: list[SelectEntity] = []
    for device in indoor_devices:
        entities.extend(
            [
                FGCAirFanSpeedSelect(hass, entry, device),
                FGCAirModeSelect(hass, entry, device),
                FGCAirTemperatureSourceSelect(hass, entry, device, indoor_devices),
            ]
        )
    async_add_entities(entities, True)


class FGCAirBaseSelect(SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.device = device
        self.did = str(device["did"])
        self.index = indoor_index(device) or 4
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.did)},
            "name": f"室内机 {self.index}",
            "manufacturer": "FGCAir",
            "model": device.get("product_name"),
        }

    @property
    def _client(self) -> FGCAirClient:
        return self.hass.data[DOMAIN][self.entry.entry_id]["client"]

    @property
    def _cache(self) -> dict[str, Any]:
        return self.hass.data[DOMAIN][self.entry.entry_id]["state_cache"]

    def _cached_attrs(self) -> dict[str, Any]:
        return state_attrs_from_cache(self._cache, self.did)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_updated))

    def _handle_state_updated(self, did: str) -> None:
        if did == self.did:
            self.async_write_ha_state()

    def _build_full_attrs(self, updates: dict[str, Any]) -> dict[str, Any]:
        attrs = self._cached_attrs()
        full_attrs = {
            f"{POWER_PREFIX}{PK_INDEX}": attrs.get(f"{POWER_PREFIX}{PK_INDEX}", False),
            f"{MODE_PREFIX}{PK_INDEX}": attrs.get(f"{MODE_PREFIX}{PK_INDEX}", DEFAULT_MODE),
            f"{TEMP_PREFIX}{PK_INDEX}": attrs.get(f"{TEMP_PREFIX}{PK_INDEX}", DEFAULT_TEMP),
            f"{SPEED_PREFIX}{PK_INDEX}": attrs.get(f"{SPEED_PREFIX}{PK_INDEX}", DEFAULT_SPEED),
        }
        full_attrs.update(updates)
        return full_attrs

    async def _send_attrs(self, attrs: dict[str, Any]) -> None:
        await self._client.control_sequence(self.did, attrs)
        data = self.hass.data[DOMAIN][self.entry.entry_id]
        data["state_cache"] = merge_state_cache(data["state_cache"], self.device, attrs)
        await data["store"].async_save(data["state_cache"])
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self.did)
        self.async_write_ha_state()


class FGCAirFanSpeedSelect(FGCAirBaseSelect):
    _attr_translation_key = "fan_speed"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
        super().__init__(hass, entry, device)
        self._attr_unique_id = f"fgcair_{self.did}_fan_speed"
        self._attr_name = "风速档位"
        self._attr_options = list(FAN_TO_SPEED.keys())

    @property
    def current_option(self) -> str | None:
        value = self._cached_attrs().get(f"{SPEED_PREFIX}{PK_INDEX}", DEFAULT_SPEED)
        return SPEED_TO_FAN.get(value, SPEED_TO_FAN[DEFAULT_SPEED])

    async def async_select_option(self, option: str) -> None:
        await self._send_attrs(self._build_full_attrs({f"{SPEED_PREFIX}{PK_INDEX}": FAN_TO_SPEED[option]}))


class FGCAirModeSelect(FGCAirBaseSelect):
    _attr_translation_key = "mode"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
        super().__init__(hass, entry, device)
        self._attr_unique_id = f"fgcair_{self.did}_mode"
        self._attr_name = "运行模式"
        self._attr_options = list(LABEL_TO_MODE.keys())

    @property
    def current_option(self) -> str | None:
        value = self._cached_attrs().get(f"{MODE_PREFIX}{PK_INDEX}", DEFAULT_MODE)
        return MODE_TO_LABEL.get(value, MODE_TO_LABEL[DEFAULT_MODE])

    async def async_select_option(self, option: str) -> None:
        await self._send_attrs(
            self._build_full_attrs(
                {
                    f"{POWER_PREFIX}{PK_INDEX}": True,
                    f"{MODE_PREFIX}{PK_INDEX}": LABEL_TO_MODE[option],
                }
            )
        )


class FGCAirTemperatureSourceSelect(FGCAirBaseSelect):
    _attr_translation_key = "temperature_source"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any], devices: list[dict[str, Any]]) -> None:
        super().__init__(hass, entry, device)
        self._attr_unique_id = f"fgcair_{self.did}_temperature_source"
        self._attr_name = "当前温度来源"
        self._source_by_option = {TEMP_SOURCE_SELF: self.did}
        for source_device in devices:
            if str(source_device["did"]) == self.did:
                continue
            source_index = indoor_index(source_device) or 4
            self._source_by_option[f"室内机 {source_index}"] = str(source_device["did"])
        self._option_by_source = {value: key for key, value in self._source_by_option.items()}
        self._attr_options = list(self._source_by_option.keys())

    @property
    def current_option(self) -> str | None:
        entry = self._cache.get(self.did, {}) if isinstance(self._cache, dict) else {}
        source_did = entry.get(CONF_TEMP_SOURCE_DID) if isinstance(entry, dict) else None
        return self._option_by_source.get(str(source_did or self.did), TEMP_SOURCE_SELF)

    async def async_select_option(self, option: str) -> None:
        data = self.hass.data[DOMAIN][self.entry.entry_id]
        cache = data["state_cache"]
        device_cache = cache.get(self.did, {}) if isinstance(cache.get(self.did), dict) else {}
        device_cache[CONF_TEMP_SOURCE_DID] = self._source_by_option[option]
        cache[self.did] = device_cache
        data["state_cache"] = cache
        await data["store"].async_save(cache)
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self.did)
        self.async_write_ha_state()
