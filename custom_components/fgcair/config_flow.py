from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import selector

from .api import FGCAirAuthError, FGCAirClient, FGCAirError, indoor_devices, indoor_index
from .const import CONF_AUTO_BIND_CAPTURED, CONF_DEVICES, CONF_SELECTED_DIDS, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN


class FGCAirConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._client: FGCAirClient | None = None
        self._devices: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()
            client = FGCAirClient(username, user_input[CONF_PASSWORD])
            try:
                session = await client.login()
                if user_input.get(CONF_AUTO_BIND_CAPTURED):
                    await client.bind_captured_gateway()
                devices = indoor_devices(await client.list_bindings())
            except FGCAirAuthError:
                errors["base"] = "invalid_auth"
            except FGCAirError:
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._client = client
                    self._devices = devices
                    self._data = {
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        "uid": session.uid,
                        "token": session.token,
                        "expire_at": session.expire_at,
                    }
                    return await self.async_step_select_devices()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_AUTO_BIND_CAPTURED, default=True): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_select_devices(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        options = []
        for device in self._devices:
            index = indoor_index(device)
            name = device.get("dev_alias") or device.get("product_name") or device.get("did")
            options.append({"value": device["did"], "label": f"室内机 {index} - {name} - {device.get('mac')}"})

        if user_input is not None:
            selected = user_input.get(CONF_SELECTED_DIDS) or []
            if not selected:
                errors["base"] = "no_devices"
            else:
                return self.async_create_entry(
                    title=f"FGCAir {self._data['username']}",
                    data={**self._data, CONF_DEVICES: self._devices, CONF_SELECTED_DIDS: selected},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SELECTED_DIDS, default=[item["value"] for item in options]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN)
                )
            }
        )
        return self.async_show_form(step_id="select_devices", data_schema=schema, errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return FGCAirOptionsFlow(config_entry)


class FGCAirOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=self._config_entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=3600,
                        step=5,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
