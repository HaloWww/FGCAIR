from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import indoor_index, merge_state_cache, state_attrs_from_cache
from .const import CONF_DEVICES, CONF_SELECTED_DIDS, DOMAIN, FAN_TO_SPEED, SIGNAL_STATE_UPDATED, SPEED_TO_FAN

SPEED_PREFIX = "Speed_indoor_PK"
POWER_PREFIX = "Power_indoor_PK"
DEFAULT_SPEED = 0
PK_INDEX = 4
PRESET_MODES = list(FAN_TO_SPEED.keys())
SUPPORTED_FEATURES = FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE
if hasattr(FanEntityFeature, "TURN_ON"):
    SUPPORTED_FEATURES |= FanEntityFeature.TURN_ON
if hasattr(FanEntityFeature, "TURN_OFF"):
    SUPPORTED_FEATURES |= FanEntityFeature.TURN_OFF
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    devices = _configured_devices(entry)
    entities = [FGCAirFan(hass, entry, device) for device in devices if device.get("did") in selected]
    async_add_entities(entities, True)


def _configured_devices(entry: ConfigEntry) -> list[dict[str, Any]]:
    devices = entry.data.get(CONF_DEVICES)
    if isinstance(devices, list) and devices:
        return [device for device in devices if isinstance(device, dict)]
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    return [{"did": did, "product_name": "FGCAir 室内机", "mac": "", "dev_alias": ""} for did in selected]


class FGCAirFan(FanEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:fan"
    _attr_supported_features = SUPPORTED_FEATURES
    _attr_preset_modes = PRESET_MODES
    _attr_speed_count = 7

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.device = device
        self.did = str(device["did"])
        self.index = indoor_index(device)
        self._speed = DEFAULT_SPEED
        self._power = False
        self._attr_unique_id = f"fgcair_{self.did}_fan"
        device_name = _device_name(device, self.index)
        self._attr_name = f"{device_name} 风速"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self.did)},
            "name": device_name,
            "manufacturer": "FGCAir",
            "model": device.get("product_name"),
        }

    @property
    def _client(self):
        return self.hass.data[DOMAIN][self.entry.entry_id]["client"]

    @property
    def _cache(self) -> dict[str, Any]:
        return self.hass.data[DOMAIN][self.entry.entry_id]["state_cache"]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_updated))
        self._load_cached_state()

    def _handle_state_updated(self, did: str) -> None:
        if did != self.did:
            return
        self._load_cached_state()
        self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    def _load_cached_state(self) -> None:
        attrs = state_attrs_from_cache(self._cache, self.did)
        speed = attrs.get(f"{SPEED_PREFIX}{PK_INDEX}")
        power = attrs.get(f"{POWER_PREFIX}{PK_INDEX}")
        if isinstance(speed, int) and speed in SPEED_TO_FAN:
            self._speed = speed
        if isinstance(power, bool):
            self._power = power

    @property
    def is_on(self) -> bool:
        return self._power

    @property
    def preset_mode(self) -> str | None:
        return SPEED_TO_FAN.get(self._speed)

    @property
    def percentage(self) -> int | None:
        if not self._power:
            return 0
        return _percentage_from_speed(self._speed)

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        updates: dict[str, Any] = {f"{POWER_PREFIX}{PK_INDEX}": True}
        if preset_mode is not None:
            updates[f"{SPEED_PREFIX}{PK_INDEX}"] = FAN_TO_SPEED[preset_mode]
        elif percentage is not None:
            updates[f"{SPEED_PREFIX}{PK_INDEX}"] = _speed_from_percentage(percentage)
        await self._send_attrs(updates)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_attrs({f"{POWER_PREFIX}{PK_INDEX}": False})

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage <= 0:
            await self.async_turn_off()
            return
        await self._send_attrs({
            f"{POWER_PREFIX}{PK_INDEX}": True,
            f"{SPEED_PREFIX}{PK_INDEX}": _speed_from_percentage(percentage),
        })

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._send_attrs({
            f"{POWER_PREFIX}{PK_INDEX}": True,
            f"{SPEED_PREFIX}{PK_INDEX}": FAN_TO_SPEED[preset_mode],
        })

    async def _send_attrs(self, attrs: dict[str, Any]) -> None:
        _LOGGER.info("Sending FGCAir fan control sequence did=%s attrs=%s", self.did, attrs)
        await self._client.control_sequence(self.did, attrs)
        data = self.hass.data[DOMAIN][self.entry.entry_id]
        data["state_cache"] = merge_state_cache(data["state_cache"], self.device, attrs)
        await data["store"].async_save(data["state_cache"])
        self._load_cached_state()
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self.did)
        self.async_write_ha_state()


def _speed_from_percentage(percentage: int) -> int:
    if percentage <= 21:
        return 0
    return min(6, max(1, round((percentage - 14) / 86 * 6)))


def _percentage_from_speed(speed: int) -> int:
    if speed <= 0:
        return 14
    return round(14 + speed / 6 * 86)


def _device_name(device: dict[str, Any], index: int | None) -> str:
    if index is not None:
        return f"室内机 {index}"
    return str(device.get("dev_alias") or device.get("product_name") or "FGCAir 室内机")
