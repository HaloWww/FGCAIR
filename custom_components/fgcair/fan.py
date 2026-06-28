from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import FGCAirClient, indoor_index, merge_state_cache, state_attrs_from_cache
from .const import CONF_SELECTED_DIDS, DOMAIN, SIGNAL_STATE_UPDATED

POWER_PREFIX = "Power_indoor_PK"
MODE_PREFIX = "Mode_indoor_PK"
SPEED_PREFIX = "Speed_indoor_PK"
TEMP_PREFIX = "Temp_indoor_PK"
DEFAULT_MODE = 1
DEFAULT_SPEED = 0
DEFAULT_TEMP = 26
PK_INDEX = 4
PERCENTAGE_TO_SPEED = {14: 0, 29: 1, 43: 2, 57: 3, 71: 4, 86: 5, 100: 6}
SPEED_TO_PERCENTAGE = {value: key for key, value in PERCENTAGE_TO_SPEED.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    client: FGCAirClient = data["client"]
    devices = await client.list_bindings()
    selected = set(entry.data.get(CONF_SELECTED_DIDS, []))
    async_add_entities([FGCAirFan(hass, entry, device) for device in devices if device.get("did") in selected], True)


class FGCAirFan(FanEntity):
    _attr_has_entity_name = True
    _attr_supported_features = FanEntityFeature.SET_SPEED

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: dict[str, Any]) -> None:
        self.hass = hass
        self.entry = entry
        self.device = device
        self.did = str(device["did"])
        self.index = indoor_index(device) or 4
        self._attr_unique_id = f"fgcair_{self.did}_fan"
        self._attr_name = "风速"
        self._attr_speed_count = 7
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

    def _attrs(self) -> dict[str, Any]:
        return state_attrs_from_cache(self._cache, self.did)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, self._handle_state_updated))

    def _handle_state_updated(self, did: str) -> None:
        if did == self.did:
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        return bool(self._attrs().get(f"{POWER_PREFIX}{PK_INDEX}", False))

    @property
    def percentage(self) -> int | None:
        speed = self._attrs().get(f"{SPEED_PREFIX}{PK_INDEX}", DEFAULT_SPEED)
        return SPEED_TO_PERCENTAGE.get(speed, SPEED_TO_PERCENTAGE[DEFAULT_SPEED])

    def _build_full_attrs(self, updates: dict[str, Any]) -> dict[str, Any]:
        attrs = self._attrs()
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

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        updates = {f"{POWER_PREFIX}{PK_INDEX}": True}
        if percentage is not None:
            updates[f"{SPEED_PREFIX}{PK_INDEX}"] = _percentage_to_speed(percentage)
        else:
            updates[f"{SPEED_PREFIX}{PK_INDEX}"] = self._attrs().get(f"{SPEED_PREFIX}{PK_INDEX}", DEFAULT_SPEED)
        await self._send_attrs(self._build_full_attrs(updates))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_attrs(self._build_full_attrs({f"{POWER_PREFIX}{PK_INDEX}": False}))

    async def async_set_percentage(self, percentage: int) -> None:
        await self._send_attrs(
            self._build_full_attrs(
                {
                    f"{POWER_PREFIX}{PK_INDEX}": True,
                    f"{SPEED_PREFIX}{PK_INDEX}": _percentage_to_speed(percentage),
                }
            )
        )


def _percentage_to_speed(percentage: int) -> int:
    nearest = min(PERCENTAGE_TO_SPEED, key=lambda value: abs(value - percentage))
    return PERCENTAGE_TO_SPEED[nearest]
