"""Entity protocol classification for Native Group Orchestration."""

from __future__ import annotations

from collections import defaultdict
import logging
import re
from typing import TYPE_CHECKING

from homeassistant.components.light.const import COLOR_MODES_BRIGHTNESS, COLOR_MODES_COLOR
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    PROTOCOL_UNKNOWN,
    PROTOCOL_ZIGBEE2MQTT,
    PROTOCOL_ZHA,
    PROTOCOL_ZWAVE_JS,
    ZWAVE_CAP_BINARY,
    ZWAVE_CAP_COLOR,
    ZWAVE_CAP_DIMMER,
    ZWAVE_GROUPABLE_DOMAINS,
)
from .mapping import ProtocolInfo

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class EntityClassifier:
    """Classifies entities by their underlying protocol."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the classifier."""
        self.hass = hass

    def classify_entity(self, entity_id: str) -> ProtocolInfo:
        """Classify a single entity by its protocol.

        Returns ProtocolInfo with protocol type and native identifier.
        """
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        entry = ent_reg.async_get(entity_id)
        if not entry:
            return ProtocolInfo(
                protocol=PROTOCOL_UNKNOWN,
                native_id=None,
                entity_id=entity_id,
            )

        # Determine device capability (for Z-Wave capability-based grouping)
        capability = self._detect_capability(entity_id)

        # Check Z-Wave JS
        if entry.platform == "zwave_js":
            native_id = self._extract_zwave_node_id(entry)
            return ProtocolInfo(
                protocol=PROTOCOL_ZWAVE_JS,
                native_id=native_id,
                entity_id=entity_id,
                node_id=native_id,
                capability=capability,
            )

        # Check ZHA
        if entry.platform == "zha":
            ieee = self._extract_zha_ieee(entry)
            return ProtocolInfo(
                protocol=PROTOCOL_ZHA,
                native_id=ieee,
                entity_id=entity_id,
                ieee_address=ieee,
                capability=capability,
            )

        # Check Zigbee2MQTT (MQTT platform with Z2M device)
        if entry.platform == "mqtt" and entry.device_id:
            device = dev_reg.async_get(entry.device_id)
            if device:
                z2m_id = self._extract_z2m_identifier(device)
                if z2m_id:
                    return ProtocolInfo(
                        protocol=PROTOCOL_ZIGBEE2MQTT,
                        native_id=z2m_id,
                        entity_id=entity_id,
                        ieee_address=z2m_id,
                        friendly_name=device.name,
                        capability=capability,
                    )

        # Unknown protocol
        return ProtocolInfo(
            protocol=PROTOCOL_UNKNOWN,
            native_id=None,
            entity_id=entity_id,
            capability=capability,
        )

    def classify_entities(
        self, entity_ids: list[str]
    ) -> dict[str, list[ProtocolInfo]]:
        """Classify multiple entities and group by protocol.

        Returns dict mapping protocol name to list of ProtocolInfo objects.
        """
        by_protocol: dict[str, list[ProtocolInfo]] = defaultdict(list)

        for entity_id in entity_ids:
            info = self.classify_entity(entity_id)
            by_protocol[info.protocol].append(info)

        return dict(by_protocol)

    def _extract_zwave_node_id(self, entry: er.RegistryEntry) -> int | None:
        """Extract Z-Wave node ID from entity registry entry.

        Z-Wave JS unique_id format: "config_entry_id-node_id-endpoint-..."
        """
        if not entry.unique_id:
            return None

        try:
            parts = entry.unique_id.split("-")
            if len(parts) >= 2:
                return int(parts[1])
        except (IndexError, ValueError):
            _LOGGER.debug(
                "Could not extract node ID from Z-Wave entity %s", entry.entity_id
            )

        return None

    def _extract_zha_ieee(self, entry: er.RegistryEntry) -> str | None:
        """Extract IEEE address from ZHA entity registry entry.

        ZHA unique_id format: "aa:bb:cc:dd:ee:ff:00:11-1-6"
        """
        if not entry.unique_id:
            return None

        # IEEE address is the first part before the first dash
        parts = entry.unique_id.split("-")
        if parts:
            return parts[0]

        return None

    def _extract_z2m_identifier(self, device: dr.DeviceEntry) -> str | None:
        """Extract Zigbee2MQTT device identifier from device registry.

        Z2M devices have identifiers like ("mqtt", "zigbee2mqtt_0x00158d...")
        """
        for domain, identifier in device.identifiers:
            if domain == "mqtt" and "zigbee2mqtt" in identifier:
                # Extract IEEE address (0x...) or friendly name
                match = re.search(r"(0x[0-9a-fA-F]+)", identifier)
                if match:
                    return match.group(1)
                # Fall back to full identifier without prefix
                if identifier.startswith("zigbee2mqtt_"):
                    return identifier[12:]  # Remove "zigbee2mqtt_" prefix

        return None

    def _detect_capability(self, entity_id: str) -> str | None:
        """Detect device capability based on entity state and attributes.

        Returns:
            "color" - Supports color (RGB, RGBW, etc.)
            "dimmer" - Supports brightness but not color
            "binary" - On/off only (switches, binary lights)
            None - Domain not groupable (climate, lock, fan, etc.)
        """
        domain = entity_id.split(".")[0]

        # Only certain domains support Z-Wave multicast grouping
        # Other domains (climate, lock, fan) use different CCs
        if domain not in ZWAVE_GROUPABLE_DOMAINS:
            return None

        # Switches are always binary
        if domain == "switch":
            return ZWAVE_CAP_BINARY

        # For lights, check color modes from state attributes
        if domain == "light":
            state = self.hass.states.get(entity_id)
            if state:
                supported_modes = state.attributes.get("supported_color_modes", [])
                if supported_modes:
                    supported_set = set(supported_modes)

                    # Check for color support
                    if supported_set & COLOR_MODES_COLOR:
                        return ZWAVE_CAP_COLOR

                    # Check for brightness/dimming support
                    if supported_set & COLOR_MODES_BRIGHTNESS:
                        return ZWAVE_CAP_DIMMER

            # Default lights without info to dimmer (most common)
            return ZWAVE_CAP_DIMMER

        # Covers with position are like dimmers, otherwise binary
        if domain == "cover":
            state = self.hass.states.get(entity_id)
            if state:
                # Check if cover supports position
                supported_features = state.attributes.get("supported_features", 0)
                # CoverEntityFeature.SET_POSITION = 4
                if supported_features & 4:
                    return ZWAVE_CAP_DIMMER
            return ZWAVE_CAP_BINARY

        # Should not reach here due to ZWAVE_GROUPABLE_DOMAINS check
        return ZWAVE_CAP_BINARY

