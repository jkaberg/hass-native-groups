"""Z-Wave JS protocol handler with capability-based grouping."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import (
    CC_BINARY_SWITCH,
    CC_COLOR_SWITCH,
    CC_MULTILEVEL_SWITCH,
    CC_SCENE_ACTIVATION,
    CC_SCENE_ACTUATOR_CONFIGURATION,
    PROTOCOL_ZWAVE_JS,
    ZWAVE_CAP_BINARY,
    ZWAVE_CAP_COLOR,
    ZWAVE_CAP_DIMMER,
)
from .base import ProtocolHandler

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class ZWaveJSHandler(ProtocolHandler):
    """Handler for Z-Wave JS integration with capability-based grouping.

    Z-Wave multicast sends a single Command Class to all nodes. Different
    device types support different CCs:
    - Binary Switch CC (0x25): On/off switches
    - Multilevel Switch CC (0x26): Dimmers
    - Color Switch CC (0x33): Color lights

    This handler creates separate groups per capability and sends appropriate
    commands to each group for seamless operation.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize Z-Wave JS handler."""
        super().__init__(hass)
        # Track groups as node ID lists per capability
        # Key format: "group_name" or "group_name.capability"
        self._groups: dict[str, list[int]] = {}
        # Track which capabilities exist for each base group
        self._group_capabilities: dict[str, set[str]] = {}
        # Map node_id -> device_id for service calls
        self._node_to_device: dict[int, str] = {}

    @property
    def protocol_id(self) -> str:
        """Return protocol identifier."""
        return PROTOCOL_ZWAVE_JS

    async def async_is_available(self) -> bool:
        """Check if Z-Wave JS integration is loaded."""
        return "zwave_js" in self.hass.config.components

    async def async_cleanup(self) -> None:
        """Clean up handler resources."""
        self._groups.clear()
        self._group_capabilities.clear()
        self._node_to_device.clear()

    def _get_node_from_node_id(self, node_id: int) -> Any | None:
        """Get Z-Wave node object from node ID.

        Returns the ZwaveNode object or None if not found.
        """
        try:
            # Find device by node_id - iterate through zwave_js devices
            for entry in self.hass.config_entries.async_entries("zwave_js"):
                if not hasattr(entry, "runtime_data") or entry.runtime_data is None:
                    continue
                client = entry.runtime_data.client
                if client.driver and node_id in client.driver.controller.nodes:
                    return client.driver.controller.nodes[node_id]

        except Exception as err:
            _LOGGER.debug("Could not get node %d: %s", node_id, err)

        return None

    def _get_device_id_from_node_id(self, node_id: int) -> str | None:
        """Get Home Assistant device ID from Z-Wave node ID."""
        # Check cache first
        if node_id in self._node_to_device:
            return self._node_to_device[node_id]

        dev_reg = dr.async_get(self.hass)

        # Find the device by looking for zwave_js devices with matching node ID
        for entry in self.hass.config_entries.async_entries("zwave_js"):
            if not hasattr(entry, "runtime_data") or entry.runtime_data is None:
                continue

            client = entry.runtime_data.client
            if not client.driver:
                continue

            home_id = client.driver.controller.home_id
            # Z-Wave JS device identifier is (DOMAIN, f"{home_id}-{node_id}")
            identifier = ("zwave_js", f"{home_id}-{node_id}")

            device = dev_reg.async_get_device(identifiers={identifier})
            if device:
                self._node_to_device[node_id] = device.id
                return device.id

        return None

    def _get_client(self) -> Any | None:
        """Get Z-Wave JS client from config entries."""
        for entry in self.hass.config_entries.async_entries("zwave_js"):
            if hasattr(entry, "runtime_data") and entry.runtime_data is not None:
                return entry.runtime_data.client
        return None

    async def async_get_groups(self) -> dict[str, dict[str, Any]]:
        """Get all Z-Wave groups for reconciliation.

        Z-Wave doesn't have persistent groups, so we return our tracked groups.
        """
        result: dict[str, dict[str, Any]] = {}
        for name, nodes in self._groups.items():
            result[name] = {"name": name, "members": nodes}
        return result

    # ─────────────────────────────────────────────────────────────
    # GROUP MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    async def async_create_group(
        self,
        name: str,
        member_native_ids: list[int],
    ) -> str:
        """Create a Z-Wave 'group' (stored locally, multicast is ad-hoc).

        Z-Wave doesn't have persistent multicast groups like Zigbee.
        """
        self._groups[name] = list(member_native_ids)
        _LOGGER.debug("Created Z-Wave group %s with nodes %s", name, member_native_ids)
        return name

    async def async_create_capability_groups(
        self,
        base_name: str,
        members_by_capability: dict[str, list[int]],
    ) -> str:
        """Create capability-based sub-groups for a base group.

        Args:
            base_name: Base group name (e.g., "ha_floor_first_floor_zwave_js")
            members_by_capability: Dict mapping capability to node IDs
                e.g., {"binary": [5, 6], "dimmer": [7, 8], "color": [9]}

        Returns:
            Base group name
        """
        self._group_capabilities[base_name] = set()

        for capability, node_ids in members_by_capability.items():
            if node_ids:
                group_key = f"{base_name}.{capability}"
                self._groups[group_key] = list(node_ids)
                self._group_capabilities[base_name].add(capability)
                _LOGGER.debug(
                    "Created Z-Wave %s group %s with nodes %s",
                    capability,
                    group_key,
                    node_ids,
                )

        return base_name

    async def async_delete_group(self, group_id: str | int) -> None:
        """Delete a Z-Wave group and its capability sub-groups."""
        group_key = str(group_id)
        self._groups.pop(group_key, None)

        # Also delete capability sub-groups
        if group_key in self._group_capabilities:
            for cap in self._group_capabilities[group_key]:
                self._groups.pop(f"{group_key}.{cap}", None)
            del self._group_capabilities[group_key]

        _LOGGER.debug("Deleted Z-Wave group %s", group_id)

    async def async_update_group_members(
        self,
        group_id: str | int,
        add_members: list[int] | None = None,
        remove_members: list[int] | None = None,
    ) -> None:
        """Update Z-Wave group membership."""
        group_key = str(group_id)
        if group_key not in self._groups:
            self._groups[group_key] = []

        if add_members:
            self._groups[group_key].extend(add_members)

        if remove_members:
            self._groups[group_key] = [
                n for n in self._groups[group_key] if n not in remove_members
            ]

    async def async_group_exists(self, group_id: str | int) -> bool:
        """Check if group exists."""
        return str(group_id) in self._groups

    def has_capability_groups(self, base_name: str) -> bool:
        """Check if a base group has capability sub-groups."""
        return base_name in self._group_capabilities

    def get_capability_group_nodes(self, base_name: str, capability: str) -> list[int]:
        """Get node IDs for a capability sub-group."""
        group_key = f"{base_name}.{capability}"
        return self._groups.get(group_key, [])

    # ─────────────────────────────────────────────────────────────
    # SCENE MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    async def async_supports_native_scenes(self) -> bool:
        """Z-Wave supports Scene Actuator Configuration CC."""
        return True

    async def async_store_scene(
        self,
        group_id: str | int,
        scene_id: int,
        device_states: dict[int, dict[str, Any]],
    ) -> None:
        """Program scene into each Z-Wave device using CC API."""
        for node_id, state in device_states.items():
            level = state.get("level", 99)
            duration = state.get("duration", "default")

            device_id = self._get_device_id_from_node_id(node_id)
            if not device_id:
                _LOGGER.warning("Could not find device for node %d", node_id)
                continue

            try:
                await self.hass.services.async_call(
                    "zwave_js",
                    "invoke_cc_api",
                    {
                        "device_id": device_id,
                        "command_class": CC_SCENE_ACTUATOR_CONFIGURATION,
                        "method_name": "set",
                        "parameters": [scene_id, level, duration],
                    },
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Failed to store scene %d on node %d: %s",
                    scene_id,
                    node_id,
                    err,
                )

    async def async_recall_scene(self, group_id: str | int, scene_id: int) -> None:
        """Activate scene on all nodes in group."""
        group_key = str(group_id)
        node_ids = self._groups.get(group_key, [])

        tasks = []
        for node_id in node_ids:
            device_id = self._get_device_id_from_node_id(node_id)
            if not device_id:
                continue

            tasks.append(
                self.hass.services.async_call(
                    "zwave_js",
                    "invoke_cc_api",
                    {
                        "device_id": device_id,
                        "command_class": CC_SCENE_ACTIVATION,
                        "method_name": "set",
                        "parameters": [scene_id, "default"],
                    },
                    blocking=False,
                )
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def async_remove_scene(self, group_id: str | int, scene_id: int) -> None:
        """Remove scene from devices (set level to 0)."""
        group_key = str(group_id)
        node_ids = self._groups.get(group_key, [])

        for node_id in node_ids:
            device_id = self._get_device_id_from_node_id(node_id)
            if not device_id:
                continue

            try:
                await self.hass.services.async_call(
                    "zwave_js",
                    "invoke_cc_api",
                    {
                        "device_id": device_id,
                        "command_class": CC_SCENE_ACTUATOR_CONFIGURATION,
                        "method_name": "set",
                        "parameters": [scene_id, 0, "default"],
                    },
                    blocking=True,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Failed to remove scene %d from node %d: %s",
                    scene_id,
                    node_id,
                    err,
                )

    # ─────────────────────────────────────────────────────────────
    # COMMAND DISPATCH (Capability-Aware)
    # ─────────────────────────────────────────────────────────────

    async def async_send_group_command(
        self,
        group_id: str | int,
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Send command to Z-Wave group via capability-aware multicast.

        If the group has capability sub-groups, sends appropriate commands
        to each capability group. Otherwise, uses simple multicast.
        """
        base_name = str(group_id)

        # Check if this is a capability-based group
        if self.has_capability_groups(base_name):
            await self._send_capability_aware_command(
                base_name, domain, service, service_data
            )
        else:
            # Simple multicast for legacy groups
            node_ids = self._groups.get(base_name, [])
            await self.async_send_multicast(node_ids, domain, service, service_data)

    async def _send_capability_aware_command(
        self,
        base_name: str,
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Send capability-appropriate commands to all sub-groups.

        For a light.turn_on with brightness and color:
        1. Color lights get: Color CC (with color) + Multilevel CC (brightness)
        2. Dimmers get: Multilevel CC (brightness only)
        3. Switches get: Binary CC (on/off only)
        """
        tasks: list[asyncio.Task[None]] = []

        # Get capabilities for this group
        capabilities = self._group_capabilities.get(base_name, set())

        # Determine what kind of command this is
        has_color = any(
            k in service_data
            for k in ("rgb_color", "rgbw_color", "hs_color", "xy_color", "color_temp")
        )
        has_brightness = "brightness" in service_data

        # Send to color devices (if any)
        if ZWAVE_CAP_COLOR in capabilities:
            color_nodes = self.get_capability_group_nodes(base_name, ZWAVE_CAP_COLOR)
            if color_nodes:
                if has_color and service == "turn_on":
                    # Send color command
                    tasks.append(
                        asyncio.create_task(
                            self._send_color_command(color_nodes, service_data)
                        )
                    )
                elif has_brightness and service == "turn_on":
                    # Send brightness via Multilevel CC
                    tasks.append(
                        asyncio.create_task(
                            self._send_multilevel_command(
                                color_nodes, service_data["brightness"]
                            )
                        )
                    )
                else:
                    # Simple on/off
                    tasks.append(
                        asyncio.create_task(
                            self._send_binary_command(color_nodes, service == "turn_on")
                        )
                    )

        # Send to dimmer devices (if any)
        if ZWAVE_CAP_DIMMER in capabilities:
            dimmer_nodes = self.get_capability_group_nodes(base_name, ZWAVE_CAP_DIMMER)
            if dimmer_nodes:
                if has_brightness and service == "turn_on":
                    # Send brightness via Multilevel CC
                    tasks.append(
                        asyncio.create_task(
                            self._send_multilevel_command(
                                dimmer_nodes, service_data["brightness"]
                            )
                        )
                    )
                else:
                    # Simple on/off via Binary CC (dimmers respond to this too)
                    tasks.append(
                        asyncio.create_task(
                            self._send_binary_command(
                                dimmer_nodes, service == "turn_on"
                            )
                        )
                    )

        # Send to binary devices (switches)
        if ZWAVE_CAP_BINARY in capabilities:
            binary_nodes = self.get_capability_group_nodes(base_name, ZWAVE_CAP_BINARY)
            if binary_nodes:
                # Always use Binary CC for switches
                tasks.append(
                    asyncio.create_task(
                        self._send_binary_command(binary_nodes, service == "turn_on")
                    )
                )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_binary_command(self, node_ids: list[int], turn_on: bool) -> None:
        """Send Binary Switch CC command to nodes via multicast."""
        if not node_ids:
            return

        # Convert node_ids to device_ids for the service call
        device_ids = []
        for node_id in node_ids:
            device_id = self._get_device_id_from_node_id(node_id)
            if device_id:
                device_ids.append(device_id)

        if not device_ids:
            _LOGGER.warning("No valid devices found for binary command")
            return

        try:
            await self.hass.services.async_call(
                "zwave_js",
                "multicast_set_value",
                {
                    "device_id": device_ids,
                    "command_class": CC_BINARY_SWITCH,
                    "property": "targetValue",
                    "value": turn_on,
                },
                blocking=True,
            )
            _LOGGER.debug(
                "Sent binary %s to nodes %s", "ON" if turn_on else "OFF", node_ids
            )
        except Exception as err:
            _LOGGER.error("Z-Wave binary multicast failed: %s", err)

    async def _send_multilevel_command(
        self, node_ids: list[int], brightness: int
    ) -> None:
        """Send Multilevel Switch CC command to nodes via multicast."""
        if not node_ids:
            return

        # Convert node_ids to device_ids
        device_ids = []
        for node_id in node_ids:
            device_id = self._get_device_id_from_node_id(node_id)
            if device_id:
                device_ids.append(device_id)

        if not device_ids:
            _LOGGER.warning("No valid devices found for multilevel command")
            return

        # Convert HA brightness (0-255) to Z-Wave level (0-99)
        level = int(brightness * 99 / 255)

        try:
            await self.hass.services.async_call(
                "zwave_js",
                "multicast_set_value",
                {
                    "device_id": device_ids,
                    "command_class": CC_MULTILEVEL_SWITCH,
                    "property": "targetValue",
                    "value": level,
                },
                blocking=True,
            )
            _LOGGER.debug("Sent multilevel %d to nodes %s", level, node_ids)
        except Exception as err:
            _LOGGER.error("Z-Wave multilevel multicast failed: %s", err)

    async def _send_color_command(
        self, node_ids: list[int], service_data: dict[str, Any]
    ) -> None:
        """Send Color Switch CC command to nodes via multicast.

        Z-Wave Color Switch CC uses a combined "targetColor" property with
        color components (red, green, blue, warmWhite, coldWhite, etc.).
        """
        if not node_ids:
            return

        # Convert node_ids to device_ids
        device_ids = []
        for node_id in node_ids:
            device_id = self._get_device_id_from_node_id(node_id)
            if device_id:
                device_ids.append(device_id)

        if not device_ids:
            _LOGGER.warning("No valid devices found for color command")
            return

        tasks: list[asyncio.Task[None]] = []

        # Build color payload from service data
        color_value = self._build_color_value(service_data)

        if color_value:
            # Send color via Color Switch CC multicast
            tasks.append(
                asyncio.create_task(
                    self._send_color_switch_multicast(device_ids, color_value)
                )
            )

        # Also send brightness if specified (some devices need both)
        if "brightness" in service_data:
            tasks.append(
                asyncio.create_task(
                    self._send_multilevel_command(node_ids, service_data["brightness"])
                )
            )
        elif not color_value:
            # No color or brightness - just turn on
            tasks.append(asyncio.create_task(self._send_binary_command(node_ids, True)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _build_color_value(self, service_data: dict[str, Any]) -> dict[str, int] | None:
        """Build Z-Wave color value from Home Assistant service data.

        Returns a dictionary with color component names and values.
        """
        # RGB color
        if "rgb_color" in service_data:
            r, g, b = service_data["rgb_color"]
            return {"red": r, "green": g, "blue": b}

        # RGBW color
        if "rgbw_color" in service_data:
            r, g, b, w = service_data["rgbw_color"]
            return {"red": r, "green": g, "blue": b, "warmWhite": w}

        # RGBWW color (RGB + warm white + cold white)
        if "rgbww_color" in service_data:
            r, g, b, ww, cw = service_data["rgbww_color"]
            return {"red": r, "green": g, "blue": b, "warmWhite": ww, "coldWhite": cw}

        # HS color - convert to RGB
        if "hs_color" in service_data:
            h, s = service_data["hs_color"]
            # Convert HS to RGB (assuming full brightness)
            r, g, b = self._hs_to_rgb(h, s)
            return {"red": r, "green": g, "blue": b}

        # XY color - convert to RGB
        if "xy_color" in service_data:
            x, y = service_data["xy_color"]
            r, g, b = self._xy_to_rgb(x, y)
            return {"red": r, "green": g, "blue": b}

        # Color temperature - use warm/cold white channels
        if "color_temp" in service_data or "color_temp_kelvin" in service_data:
            return self._color_temp_to_white_channels(service_data)

        return None

    def _hs_to_rgb(self, h: float, s: float) -> tuple[int, int, int]:
        """Convert HS color to RGB."""
        import colorsys  # noqa: PLC0415

        # h is 0-360, s is 0-100 in HA
        r, g, b = colorsys.hsv_to_rgb(h / 360, s / 100, 1.0)
        return int(r * 255), int(g * 255), int(b * 255)

    def _xy_to_rgb(self, x: float, y: float) -> tuple[int, int, int]:
        """Convert XY color to RGB (simplified)."""
        # Simplified conversion - for accurate results, use color_util
        # This provides reasonable approximation for multicast
        z = 1.0 - x - y
        Y = 1.0  # Assume full brightness
        X = (Y / y) * x if y > 0 else 0
        Z = (Y / y) * z if y > 0 else 0

        # XYZ to RGB (sRGB)
        r = X * 3.2406 - Y * 1.5372 - Z * 0.4986
        g = -X * 0.9689 + Y * 1.8758 + Z * 0.0415
        b = X * 0.0557 - Y * 0.2040 + Z * 1.0570

        # Clamp and scale
        r = max(0, min(1, r))
        g = max(0, min(1, g))
        b = max(0, min(1, b))

        return int(r * 255), int(g * 255), int(b * 255)

    def _color_temp_to_white_channels(
        self, service_data: dict[str, Any]
    ) -> dict[str, int]:
        """Convert color temperature to warm/cold white channel values."""
        # Get color temp in Kelvin
        if "color_temp_kelvin" in service_data:
            kelvin = service_data["color_temp_kelvin"]
        elif "color_temp" in service_data:
            # Convert mireds to Kelvin
            mireds = service_data["color_temp"]
            kelvin = 1000000 / mireds if mireds > 0 else 4000
        else:
            kelvin = 4000  # Default neutral

        # Map Kelvin to warm/cold ratio
        # 2700K = full warm, 6500K = full cold
        min_k, max_k = 2700, 6500
        kelvin = max(min_k, min(max_k, kelvin))

        # Calculate ratio (0 = warm, 1 = cold)
        ratio = (kelvin - min_k) / (max_k - min_k)

        warm = int((1 - ratio) * 255)
        cold = int(ratio * 255)

        return {"warmWhite": warm, "coldWhite": cold}

    async def _send_color_switch_multicast(
        self, device_ids: list[str], color_value: dict[str, int]
    ) -> None:
        """Send Color Switch CC multicast with combined color value."""
        try:
            await self.hass.services.async_call(
                "zwave_js",
                "multicast_set_value",
                {
                    "device_id": device_ids,
                    "command_class": CC_COLOR_SWITCH,
                    "property": "targetColor",
                    "value": color_value,
                },
                blocking=True,
            )
            _LOGGER.debug("Sent color %s to devices %s", color_value, device_ids)
        except Exception as err:
            _LOGGER.error("Z-Wave color multicast failed: %s", err)

    async def async_send_multicast(
        self,
        native_ids: list[int],
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Send simple multicast command to Z-Wave nodes."""
        if not native_ids:
            return

        # Convert node_ids to device_ids
        device_ids = []
        for node_id in native_ids:
            device_id = self._get_device_id_from_node_id(node_id)
            if device_id:
                device_ids.append(device_id)

        if not device_ids:
            _LOGGER.warning("No valid devices found for multicast")
            return

        cc, prop, value = self._map_service_to_zwave(domain, service, service_data)

        try:
            await self.hass.services.async_call(
                "zwave_js",
                "multicast_set_value",
                {
                    "device_id": device_ids,
                    "command_class": cc,
                    "property": prop,
                    "value": value,
                },
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("Z-Wave multicast failed: %s", err)
            raise

    def _map_service_to_zwave(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> tuple[int, str, Any]:
        """Convert HA service to Z-Wave CC/property/value."""
        if domain == "light":
            if service == "turn_on":
                brightness = data.get("brightness")
                if brightness is not None:
                    return (
                        CC_MULTILEVEL_SWITCH,
                        "targetValue",
                        int(brightness * 99 / 255),
                    )
                return (CC_BINARY_SWITCH, "targetValue", True)
            if service == "turn_off":
                return (CC_BINARY_SWITCH, "targetValue", False)

        elif domain == "switch":
            return (CC_BINARY_SWITCH, "targetValue", service == "turn_on")

        elif domain == "cover":
            if service == "open_cover":
                return (CC_MULTILEVEL_SWITCH, "targetValue", 99)
            if service == "close_cover":
                return (CC_MULTILEVEL_SWITCH, "targetValue", 0)
            if service == "set_cover_position":
                return (CC_MULTILEVEL_SWITCH, "targetValue", data.get("position", 0))

        raise ValueError(f"Unsupported service: {domain}.{service}")

    # ─────────────────────────────────────────────────────────────
    # ENTITY RESOLUTION
    # ─────────────────────────────────────────────────────────────

    def get_native_id(self, entity_id: str) -> int | None:
        """Extract Z-Wave node ID from entity."""
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)

        if not entry or entry.platform != "zwave_js":
            return None

        # unique_id format: "config_entry_id-node_id-endpoint-..."
        if entry.unique_id:
            try:
                parts = entry.unique_id.split("-")
                return int(parts[1])
            except (IndexError, ValueError):
                pass

        return None

    def convert_service_data(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert HA service data to Z-Wave format."""
        result: dict[str, Any] = {}

        if "brightness" in data:
            result["level"] = int(data["brightness"] * 99 / 255)
        elif service == "turn_on":
            result["level"] = 99
        elif service == "turn_off":
            result["level"] = 0

        if "transition" in data:
            result["duration"] = data["transition"]

        return result
