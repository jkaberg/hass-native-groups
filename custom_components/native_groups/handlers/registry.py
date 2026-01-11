"""Handler registry for protocol handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..const import PROTOCOL_ZIGBEE2MQTT, PROTOCOL_ZHA, PROTOCOL_ZWAVE_JS
from .base import ProtocolHandler
from .zigbee2mqtt import Zigbee2MQTTHandler
from .zha import ZHAHandler
from .zwave_js import ZWaveJSHandler

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class HandlerRegistry:
    """Registry of available protocol handlers.

    Handlers self-register based on integration availability.
    This decouples the orchestrator from specific handler implementations.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the registry."""
        self.hass = hass
        self._handlers: dict[str, ProtocolHandler] = {}

    def get_available_handlers(self) -> list[tuple[str, ProtocolHandler]]:
        """Get list of available handlers based on loaded integrations.

        Returns:
            List of (protocol_id, handler) tuples for available protocols.
        """
        available: list[tuple[str, ProtocolHandler]] = []

        # Z-Wave JS
        if PROTOCOL_ZWAVE_JS in self.hass.config.components:
            handler = ZWaveJSHandler(self.hass)
            available.append((PROTOCOL_ZWAVE_JS, handler))

        # Zigbee2MQTT (uses MQTT)
        if "mqtt" in self.hass.config.components:
            handler = Zigbee2MQTTHandler(self.hass)
            available.append((PROTOCOL_ZIGBEE2MQTT, handler))

        # ZHA
        if PROTOCOL_ZHA in self.hass.config.components:
            handler = ZHAHandler(self.hass)
            available.append((PROTOCOL_ZHA, handler))

        return available

    def get_handler(self, protocol: str) -> ProtocolHandler | None:
        """Get a specific handler by protocol ID."""
        return self._handlers.get(protocol)

