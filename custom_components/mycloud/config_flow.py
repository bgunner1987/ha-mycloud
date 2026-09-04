"""Config and options flows for My Cloud."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_DRIVE_DEVICES,
    CONF_SLEEP_AWARE_ENABLED,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_UPDATE_INTERVAL,
    DEFAULT_DRIVE_DEVICES,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    HOST,
    PASSWORD,
    USERNAME,
    VERSION,
)
from .power_probe import parse_drive_devices


def _validate_drive_devices(value: str) -> str:
    """Validate submitted text, never use this function in a frontend schema."""
    if not isinstance(value, str):
        raise TypeError("Drive devices must be comma-separated text")
    return ",".join(parse_drive_devices(value))


class MyCloudOptionsFlowHandler(config_entries.OptionsFlow):
    """Manage polling and optional SSH sleep-awareness."""

    async def async_step_init(self, user_input=None):
        errors = {}
        current = dict(self.config_entry.options)
        if user_input is not None:
            current.update(user_input)
            try:
                current[CONF_DRIVE_DEVICES] = _validate_drive_devices(
                    current.get(CONF_DRIVE_DEVICES, DEFAULT_DRIVE_DEVICES)
                )
            except (TypeError, ValueError):
                errors[CONF_DRIVE_DEVICES] = "invalid_drive_devices"
                if not isinstance(current.get(CONF_DRIVE_DEVICES), str):
                    current[CONF_DRIVE_DEVICES] = ""
            else:
                return self.async_create_entry(title="", data=current)

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=current.get(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=30)),
                vol.Optional(
                    CONF_SLEEP_AWARE_ENABLED,
                    default=current.get(CONF_SLEEP_AWARE_ENABLED, False),
                ): bool,
                vol.Optional(
                    CONF_SSH_PORT,
                    default=current.get(CONF_SSH_PORT, DEFAULT_SSH_PORT),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
                vol.Optional(
                    CONF_SSH_USERNAME,
                    default=current.get(CONF_SSH_USERNAME, DEFAULT_SSH_USERNAME),
                ): vol.All(str, vol.Length(min=1)),
                vol.Optional(
                    CONF_SSH_PASSWORD,
                    default=current.get(CONF_SSH_PASSWORD, ""),
                ): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
                vol.Optional(
                    CONF_DRIVE_DEVICES,
                    default=current.get(CONF_DRIVE_DEVICES, DEFAULT_DRIVE_DEVICES),
                ): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
            description_placeholders={"minimum_interval": "30"},
        )


class MyCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure the WD API connection."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MyCloudOptionsFlowHandler()

    async def async_step_user(self, user_input=None):
        errors = {}
        current = dict(user_input or {})
        if user_input is not None:
            try:
                current[CONF_DRIVE_DEVICES] = _validate_drive_devices(
                    current.get(CONF_DRIVE_DEVICES, DEFAULT_DRIVE_DEVICES)
                )
            except (TypeError, ValueError):
                errors[CONF_DRIVE_DEVICES] = "invalid_drive_devices"
                if not isinstance(current.get(CONF_DRIVE_DEVICES), str):
                    current[CONF_DRIVE_DEVICES] = ""
            else:
                data = {
                    key: current[key]
                    for key in (HOST, USERNAME, PASSWORD, VERSION)
                }
                options = {
                    key: current[key]
                    for key in (
                        CONF_UPDATE_INTERVAL,
                        CONF_SLEEP_AWARE_ENABLED,
                        CONF_SSH_PORT,
                        CONF_SSH_USERNAME,
                        CONF_SSH_PASSWORD,
                        CONF_DRIVE_DEVICES,
                    )
                }
                return self.async_create_entry(
                    title="WD My Cloud Integration", data=data, options=options
                )

        schema = vol.Schema(
            {
                vol.Required(HOST, default=current.get(HOST, vol.UNDEFINED)): str,
                vol.Required(USERNAME, default=current.get(USERNAME, vol.UNDEFINED)): str,
                vol.Required(
                    PASSWORD, default=current.get(PASSWORD, vol.UNDEFINED)
                ): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
                vol.Required(
                    VERSION, default=current.get(VERSION, vol.UNDEFINED)
                ): vol.All(vol.Coerce(int), vol.In([2, 5])),
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=current.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=30)),
                vol.Optional(
                    CONF_SLEEP_AWARE_ENABLED,
                    default=current.get(CONF_SLEEP_AWARE_ENABLED, False),
                ): bool,
                vol.Optional(
                    CONF_SSH_PORT, default=current.get(CONF_SSH_PORT, DEFAULT_SSH_PORT)
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Optional(
                    CONF_SSH_USERNAME,
                    default=current.get(CONF_SSH_USERNAME, DEFAULT_SSH_USERNAME),
                ): vol.All(str, vol.Length(min=1)),
                vol.Optional(
                    CONF_SSH_PASSWORD, default=current.get(CONF_SSH_PASSWORD, "")
                ): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
                vol.Optional(
                    CONF_DRIVE_DEVICES,
                    default=current.get(CONF_DRIVE_DEVICES, DEFAULT_DRIVE_DEVICES),
                ): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"minimum_interval": "30"},
        )
