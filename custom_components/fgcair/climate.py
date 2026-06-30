from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .api import indoor_index, merge_state_cache, state_attrs_from_cache
from .const import CONF_DEVICES, CONF_SELECTED_DIDS, CONF_TEMP_SOURCE_ENTITY_ID, DOMAIN, FAN_TO_SPEED, MODE_TO_HVAC, SIGNAL_STATE_UPDATED, SPEED_TO_FAN

POWER_PREFIX = "Power_indoor_PK"
MODE_PREFIX = "Mode_indoor_PK"
SPEED_PREFIX = "Speed_indoor_PK"
TEMP_PREFIX = "Temp_indoor_PK"
ROOM_TEMP_PREFIX = "Roomtemp_indoor_PK"
DEFAULT_POWER = False
DEFAULT_MODE = 1
DEFAULT_SPEED = 0
DEFAULT_TEMP = 26
TEMP_STEP = 0.5
_LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
if hasattr(ClimateEntityFeature, "TURN_ON"):
    SUPPORTED_FEATURES |= ClimateEntityFeature.TURN_ON
if hasattr(ClimateEntityFeature, "TURN_OFF"):
    SUPPORTED_FEATURES |= ClimateEntityFeature.TURN_OFF


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    devices = _configured_devices(entry)
    entities = [FGCAirClimate(hass, entry, device) for device in devices if device.get("did") in selected]
    async_add_entities(entities, True)


def _configured_devices(entry: ConfigEntry) -> list[dict[str, Any]]:
    devices = entry.data.get(CONF_DEVICES)
    if isinstance(devices, list) and devices:
        return [device for device in devices if isinstance(device, dict)]
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    return [{"did": did, "product_name": "FGCAir 室内机", "mac": "", "dev_alias": ""} for did in selected]


def _first_key(attrs: dict[str, Any], prefix: str, pk_index: int) -> str:
    expected = f"{prefix}{pk_index}"
    if expected in attrs:
        return expected
    return next((key for key in attrs if key.startswith(prefix)), expected)


class FGCAirClimate(ClimateEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:air-conditioner"
    _attr_force_update = True
    _attr_supported_features = SUPPORTED_FEATURES
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.DRY]
    _attr_fan_modes = list(FAN_TO_SPEED.keys())
    _attr_min_temp = 18
    _attr_max_temp = 30
    _attr_target_temperature_step = TEMP_STEP
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.device = device
        self.did = str(device["did"])
        self.index = indoor_index(device)
        self.pk_index = 4
        self._attrs: dict[str, Any] = self._default_attrs()
        self._tracked_temp_source: str | None = None
        self._remove_temp_source_listener = None
        self._attr_unique_id = f"fgcair_{self.did}"
        self._attr_name = _device_name(device, self.index)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.did)},
            "name": self._attr_name,
            "manufacturer": "FGCAir",
            "model": device.get("product_name"),
        }

    def _default_attrs(self) -> dict[str, Any]:
        return {
            f"{POWER_PREFIX}{self.pk_index}": DEFAULT_POWER,
            f"{MODE_PREFIX}{self.pk_index}": DEFAULT_MODE,
            f"{SPEED_PREFIX}{self.pk_index}": DEFAULT_SPEED,
            f"{TEMP_PREFIX}{self.pk_index}": DEFAULT_TEMP,
        }

    @property
    def _client(self) -> FGCAirClient:
        return self.hass.data[DOMAIN][self.entry.entry_id]["client"]

    @property
    def _cache(self) -> dict[str, Any]:
        return self.hass.data[DOMAIN][self.entry.entry_id]["state_cache"]

    async def _save_attrs(self, attrs: dict[str, Any]) -> None:
        data = self.hass.data[DOMAIN][self.entry.entry_id]
        data["state_cache"] = merge_state_cache(data["state_cache"], self.device, attrs)
        await data["store"].async_save(data["state_cache"])
        self._attrs.update(attrs)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_updated)
        )
        self._track_temp_source()
        cached = state_attrs_from_cache(self._cache, self.did)
        if cached:
            self._attrs.update(cached)
            return
        await self._save_attrs(self._default_attrs())

    def _handle_state_updated(self, did: str) -> None:
        if did != self.did:
            return
        cached = state_attrs_from_cache(self._cache, self.did)
        merged = self._default_attrs()
        merged.update(cached)
        self._attrs = merged
        self._track_temp_source()
        self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    async def async_update(self) -> None:
        cached = state_attrs_from_cache(self._cache, self.did)
        merged = self._default_attrs()
        merged.update(cached)
        self._attrs = merged

    @property
    def _temp_source_entity_id(self) -> str | None:
        entry = self._cache.get(self.did, {}) if isinstance(self._cache, dict) else {}
        source = entry.get(CONF_TEMP_SOURCE_ENTITY_ID) if isinstance(entry, dict) else None
        return str(source) if source else None

    @property
    def hvac_mode(self) -> HVACMode:
        power = self._attrs.get(_first_key(self._attrs, POWER_PREFIX, self.pk_index))
        if power is False:
            return HVACMode.OFF
        mode = self._attrs.get(_first_key(self._attrs, MODE_PREFIX, self.pk_index))
        hvac_mode = HVACMode(MODE_TO_HVAC.get(mode, HVACMode.COOL))
        return hvac_mode if hvac_mode in self.hvac_modes else HVACMode.COOL

    @property
    def target_temperature(self) -> float | None:
        value = self._attrs.get(_first_key(self._attrs, TEMP_PREFIX, self.pk_index))
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def current_temperature(self) -> float | None:
        source_entity_id = self._temp_source_entity_id
        if source_entity_id:
            state = self.hass.states.get(source_entity_id)
            if state:
                raw_value = state.attributes.get("current_temperature") if source_entity_id.startswith("climate.") else state.state
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    _LOGGER.debug("Temperature source %s has non-numeric temperature %s", source_entity_id, raw_value)
                else:
                    if state.attributes.get("unit_of_measurement") == UnitOfTemperature.FAHRENHEIT:
                        return round((value - 32) * 5 / 9, 1)
                    return value
        value = self._attrs.get(_first_key(self._attrs, ROOM_TEMP_PREFIX, self.pk_index))
        return round(value * 0.5 - 75, 1) if isinstance(value, (int, float)) else None

    def _track_temp_source(self) -> None:
        source_entity_id = self._temp_source_entity_id
        if source_entity_id == self._tracked_temp_source:
            return
        if self._remove_temp_source_listener:
            self._remove_temp_source_listener()
            self._remove_temp_source_listener = None
        self._tracked_temp_source = source_entity_id
        if source_entity_id:
            self._remove_temp_source_listener = async_track_state_change_event(
                self.hass,
                [source_entity_id],
                self._handle_temp_source_changed,
            )

    def _handle_temp_source_changed(self, event: Any) -> None:
        self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_temp_source_listener:
            self._remove_temp_source_listener()
            self._remove_temp_source_listener = None

    @property
    def fan_mode(self) -> str | None:
        value = self._attrs.get(_first_key(self._attrs, SPEED_PREFIX, self.pk_index))
        return SPEED_TO_FAN.get(value) if isinstance(value, int) else None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode | str) -> None:
        await self._send_hvac_mode(self._coerce_hvac_mode(hvac_mode))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        updates: dict[str, Any] = {}
        hvac_mode = kwargs.get("hvac_mode")
        if hvac_mode and self._coerce_hvac_mode(hvac_mode) != HVACMode.OFF:
            hvac_to_mode = {HVACMode.COOL: 1, HVACMode.DRY: 2, HVACMode.HEAT: 4}
            hvac_mode = self._coerce_hvac_mode(hvac_mode)
            hvac_mode = hvac_mode if hvac_mode in hvac_to_mode else HVACMode.COOL
            updates[f"{POWER_PREFIX}{self.pk_index}"] = True
            updates[f"{MODE_PREFIX}{self.pk_index}"] = hvac_to_mode[hvac_mode]
        if ATTR_TEMPERATURE not in kwargs:
            if updates:
                await self._send_attrs(updates)
            return
        temperature = max(18, min(30, round(float(kwargs[ATTR_TEMPERATURE]) / TEMP_STEP) * TEMP_STEP))
        updates[f"{TEMP_PREFIX}{self.pk_index}"] = temperature
        await self._send_attrs(updates)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        attrs = {
            f"{SPEED_PREFIX}{self.pk_index}": FAN_TO_SPEED[fan_mode],
        }
        await self._send_attrs(attrs)

    async def async_turn_on(self) -> None:
        attrs = {
            f"{POWER_PREFIX}{self.pk_index}": True,
        }
        await self._send_attrs(attrs)

    async def async_turn_off(self) -> None:
        await self._send_attrs({f"{POWER_PREFIX}{self.pk_index}": False})

    def _coerce_hvac_mode(self, hvac_mode: HVACMode | str) -> HVACMode:
        return hvac_mode if isinstance(hvac_mode, HVACMode) else HVACMode(hvac_mode)

    async def _send_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        hvac_to_mode = {HVACMode.COOL: 1, HVACMode.DRY: 2, HVACMode.HEAT: 4}
        hvac_mode = hvac_mode if hvac_mode in hvac_to_mode else HVACMode.COOL
        attrs = {
            f"{POWER_PREFIX}{self.pk_index}": True,
            f"{MODE_PREFIX}{self.pk_index}": hvac_to_mode[hvac_mode],
        }
        await self._send_attrs(attrs)

    async def _send_attrs(self, attrs: dict[str, Any]) -> None:
        _LOGGER.info("Sending FGCAir climate control sequence did=%s attrs=%s", self.did, attrs)
        await self._client.control_sequence(self.did, attrs)
        await self._save_attrs(attrs)
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self.did)
        self.async_write_ha_state()


def _device_name(device: dict[str, Any], index: int | None) -> str:
    if index is not None:
        return f"室内机 {index}"
    return str(device.get("dev_alias") or device.get("product_name") or "FGCAir 室内机")
