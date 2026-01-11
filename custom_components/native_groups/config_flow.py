"""Config flow for Native Group Orchestration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ENABLE_AREAS,
    CONF_ENABLE_FLOORS,
    CONF_ENABLE_GROUPS,
    CONF_ENABLE_LABELS,
    CONF_ENABLE_SCENES,
    CONF_ENABLED_PROTOCOLS,
    DOMAIN,
    PROTOCOL_ZIGBEE2MQTT,
    PROTOCOL_ZHA,
    PROTOCOL_ZWAVE_JS,
)

PROTOCOL_OPTIONS = [
    {"value": PROTOCOL_ZWAVE_JS, "label": "Z-Wave JS"},
    {"value": PROTOCOL_ZIGBEE2MQTT, "label": "Zigbee2MQTT"},
    {"value": PROTOCOL_ZHA, "label": "ZHA"},
]


class NativeGroupsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Native Group Orchestration."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        # Only allow a single config entry
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Native Group Orchestration",
                data={},
                options={
                    CONF_ENABLED_PROTOCOLS: user_input.get(
                        CONF_ENABLED_PROTOCOLS,
                        [PROTOCOL_ZWAVE_JS, PROTOCOL_ZIGBEE2MQTT, PROTOCOL_ZHA],
                    ),
                    CONF_ENABLE_GROUPS: user_input.get(CONF_ENABLE_GROUPS, True),
                    CONF_ENABLE_SCENES: user_input.get(CONF_ENABLE_SCENES, True),
                    CONF_ENABLE_AREAS: user_input.get(CONF_ENABLE_AREAS, True),
                    CONF_ENABLE_FLOORS: user_input.get(CONF_ENABLE_FLOORS, True),
                    CONF_ENABLE_LABELS: user_input.get(CONF_ENABLE_LABELS, True),
                },
            )

        # Detect available protocols
        available_protocols = await self._async_detect_protocols()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED_PROTOCOLS,
                        default=available_protocols,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=PROTOCOL_OPTIONS,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(CONF_ENABLE_GROUPS, default=True): BooleanSelector(),
                    vol.Required(CONF_ENABLE_SCENES, default=True): BooleanSelector(),
                    vol.Required(CONF_ENABLE_AREAS, default=True): BooleanSelector(),
                    vol.Required(CONF_ENABLE_FLOORS, default=True): BooleanSelector(),
                    vol.Required(CONF_ENABLE_LABELS, default=True): BooleanSelector(),
                }
            ),
        )

    async def _async_detect_protocols(self) -> list[str]:
        """Detect which protocols are available."""
        available = []
        if PROTOCOL_ZWAVE_JS in self.hass.config.components:
            available.append(PROTOCOL_ZWAVE_JS)
        if "mqtt" in self.hass.config.components:
            available.append(PROTOCOL_ZIGBEE2MQTT)
        if PROTOCOL_ZHA in self.hass.config.components:
            available.append(PROTOCOL_ZHA)
        return available

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigFlow,
    ) -> OptionsFlow:
        """Get the options flow for this handler."""
        return NativeGroupsOptionsFlow()


class NativeGroupsOptionsFlow(OptionsFlow):
    """Handle options flow for Native Group Orchestration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED_PROTOCOLS,
                        default=self.config_entry.options.get(
                            CONF_ENABLED_PROTOCOLS,
                            [PROTOCOL_ZWAVE_JS, PROTOCOL_ZIGBEE2MQTT, PROTOCOL_ZHA],
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=PROTOCOL_OPTIONS,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Required(
                        CONF_ENABLE_GROUPS,
                        default=self.config_entry.options.get(CONF_ENABLE_GROUPS, True),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_ENABLE_SCENES,
                        default=self.config_entry.options.get(CONF_ENABLE_SCENES, True),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_ENABLE_AREAS,
                        default=self.config_entry.options.get(CONF_ENABLE_AREAS, True),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_ENABLE_FLOORS,
                        default=self.config_entry.options.get(CONF_ENABLE_FLOORS, True),
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_ENABLE_LABELS,
                        default=self.config_entry.options.get(CONF_ENABLE_LABELS, True),
                    ): BooleanSelector(),
                }
            ),
        )
