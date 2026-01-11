"""Zigbee2MQTT protocol handler."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import DEFAULT_SCENE_STORE_DELAY, PROTOCOL_ZIGBEE2MQTT, Z2M_BASE_TOPIC
from .base import ProtocolHandler

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class Zigbee2MQTTHandler(ProtocolHandler):
    """Handler for Zigbee2MQTT integration."""

    def __init__(
        self, hass: HomeAssistant, base_topic: str = Z2M_BASE_TOPIC
    ) -> None:
        """Initialize Zigbee2MQTT handler."""
        super().__init__(hass)
        self._base_topic = base_topic
        self._groups: dict[str, list[str]] = {}  # group_name → IEEE addresses

    @property
    def protocol_id(self) -> str:
        """Return protocol identifier."""
        return PROTOCOL_ZIGBEE2MQTT

    async def async_is_available(self) -> bool:
        """Check if MQTT integration is loaded (Z2M uses MQTT)."""
        return "mqtt" in self.hass.config.components

    async def async_cleanup(self) -> None:
        """Clean up handler resources."""
        self._groups.clear()

    async def async_get_groups(self) -> dict[str, dict[str, Any]]:
        """Get all Z2M groups for reconciliation.

        Returns locally tracked groups. Could be enhanced to query
        Z2M via bridge/config topic.
        """
        result: dict[str, dict[str, Any]] = {}
        for name, members in self._groups.items():
            result[name] = {"name": name, "members": members}
        return result

    async def _async_publish(self, topic: str, payload: str) -> None:
        """Publish MQTT message."""
        # Import here to avoid circular imports
        from homeassistant.components import mqtt  # noqa: PLC0415

        await mqtt.async_publish(self.hass, topic, payload)

    # ─────────────────────────────────────────────────────────────
    # GROUP MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    async def async_create_group(
        self,
        name: str,
        member_native_ids: list[str],
    ) -> str:
        """Create a Zigbee2MQTT group."""
        # Create the group
        await self._async_publish(
            f"{self._base_topic}/bridge/request/group/add",
            json.dumps({"friendly_name": name}),
        )

        # Wait briefly for group creation
        await asyncio.sleep(0.2)

        # Add members
        for ieee in member_native_ids:
            await self._async_publish(
                f"{self._base_topic}/bridge/request/group/members/add",
                json.dumps({"group": name, "device": ieee}),
            )

        self._groups[name] = list(member_native_ids)
        _LOGGER.debug("Created Z2M group %s with devices %s", name, member_native_ids)
        return name

    async def async_delete_group(self, group_id: str | int) -> None:
        """Delete a Zigbee2MQTT group."""
        group_name = str(group_id)
        await self._async_publish(
            f"{self._base_topic}/bridge/request/group/remove",
            json.dumps({"friendly_name": group_name}),
        )
        self._groups.pop(group_name, None)
        _LOGGER.debug("Deleted Z2M group %s", group_id)

    async def async_update_group_members(
        self,
        group_id: str | int,
        add_members: list[str] | None = None,
        remove_members: list[str] | None = None,
    ) -> None:
        """Update Zigbee2MQTT group membership."""
        group_name = str(group_id)

        if add_members:
            for ieee in add_members:
                await self._async_publish(
                    f"{self._base_topic}/bridge/request/group/members/add",
                    json.dumps({"group": group_name, "device": ieee}),
                )

        if remove_members:
            for ieee in remove_members:
                await self._async_publish(
                    f"{self._base_topic}/bridge/request/group/members/remove",
                    json.dumps({"group": group_name, "device": ieee}),
                )

        # Update local cache
        if group_name not in self._groups:
            self._groups[group_name] = []
        if add_members:
            self._groups[group_name].extend(add_members)
        if remove_members:
            self._groups[group_name] = [
                m for m in self._groups[group_name] if m not in remove_members
            ]

    async def async_group_exists(self, group_id: str | int) -> bool:
        """Check if group exists (local cache)."""
        return str(group_id) in self._groups

    # ─────────────────────────────────────────────────────────────
    # SCENE MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    async def async_supports_native_scenes(self) -> bool:
        """Zigbee supports Scenes cluster (0x0005)."""
        return True

    async def async_store_scene(
        self,
        group_id: str | int,
        scene_id: int,
        device_states: dict[str, dict[str, Any]],
    ) -> None:
        """Store a Zigbee scene.

        NOTE: Zigbee scene_store captures CURRENT device state.
        We must first set each device to target state, then store.
        """
        group_name = str(group_id)

        # Step 1: Set each device to its target state
        tasks = []
        for device_ieee, state in device_states.items():
            tasks.append(
                self._async_publish(
                    f"{self._base_topic}/{device_ieee}/set",
                    json.dumps(state),
                )
            )
        await asyncio.gather(*tasks)

        # Step 2: Wait for devices to reach target state
        await asyncio.sleep(DEFAULT_SCENE_STORE_DELAY)

        # Step 3: Store current state as scene
        await self._async_publish(
            f"{self._base_topic}/{group_name}/set",
            json.dumps({"scene_store": scene_id}),
        )

        _LOGGER.debug("Stored Z2M scene %d for group %s", scene_id, group_id)

    async def async_recall_scene(self, group_id: str | int, scene_id: int) -> None:
        """Recall a Zigbee scene.

        This is ONE multicast command - all devices respond with their stored states.
        """
        group_name = str(group_id)
        await self._async_publish(
            f"{self._base_topic}/{group_name}/set",
            json.dumps({"scene_recall": scene_id}),
        )
        _LOGGER.debug("Recalled Z2M scene %d for group %s", scene_id, group_id)

    async def async_remove_scene(self, group_id: str | int, scene_id: int) -> None:
        """Remove a Zigbee scene."""
        group_name = str(group_id)
        await self._async_publish(
            f"{self._base_topic}/{group_name}/set",
            json.dumps({"scene_remove": scene_id}),
        )
        _LOGGER.debug("Removed Z2M scene %d from group %s", scene_id, group_id)

    # ─────────────────────────────────────────────────────────────
    # COMMAND DISPATCH
    # ─────────────────────────────────────────────────────────────

    async def async_send_group_command(
        self,
        group_id: str | int,
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Send command to Zigbee2MQTT group."""
        group_name = str(group_id)
        payload = self.convert_service_data(domain, service, service_data)

        await self._async_publish(
            f"{self._base_topic}/{group_name}/set",
            json.dumps(payload),
        )

    async def async_send_multicast(
        self,
        native_ids: list[str],
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Send to multiple devices.

        Z2M doesn't support ad-hoc multicast, so send individually.
        """
        payload = self.convert_service_data(domain, service, service_data)
        payload_str = json.dumps(payload)

        tasks = []
        for device_id in native_ids:
            tasks.append(
                self._async_publish(
                    f"{self._base_topic}/{device_id}/set",
                    payload_str,
                )
            )
        await asyncio.gather(*tasks)

    # ─────────────────────────────────────────────────────────────
    # ENTITY RESOLUTION
    # ─────────────────────────────────────────────────────────────

    def get_native_id(self, entity_id: str) -> str | None:
        """Extract IEEE address from Zigbee2MQTT entity."""
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        entry = ent_reg.async_get(entity_id)
        if not entry or entry.platform != "mqtt":
            return None

        # Check if this is a Z2M device
        device = dev_reg.async_get(entry.device_id) if entry.device_id else None
        if not device:
            return None

        # Z2M devices have identifiers like ("mqtt", "zigbee2mqtt_0x00158d...")
        for domain, identifier in device.identifiers:
            if domain == "mqtt" and "zigbee2mqtt" in identifier:
                # Extract IEEE address
                match = re.search(r"(0x[0-9a-fA-F]+)", identifier)
                if match:
                    return match.group(1)

        return None

    def convert_service_data(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert HA service data to Z2M payload format."""
        if domain == "light":
            if service == "turn_on":
                payload: dict[str, Any] = {"state": "ON"}
                if "brightness" in data:
                    payload["brightness"] = data["brightness"]
                if "color_temp" in data:
                    payload["color_temp"] = data["color_temp"]
                if "rgb_color" in data:
                    r, g, b = data["rgb_color"]
                    payload["color"] = {"r": r, "g": g, "b": b}
                if "xy_color" in data:
                    x, y = data["xy_color"]
                    payload["color"] = {"x": x, "y": y}
                if "hs_color" in data:
                    h, s = data["hs_color"]
                    payload["color"] = {"hue": h, "saturation": s}
                if "transition" in data:
                    payload["transition"] = data["transition"]
                return payload

            if service == "turn_off":
                payload = {"state": "OFF"}
                if "transition" in data:
                    payload["transition"] = data["transition"]
                return payload

        elif domain == "switch":
            return {"state": "ON" if service == "turn_on" else "OFF"}

        elif domain == "cover":
            if service == "open_cover":
                return {"state": "OPEN"}
            if service == "close_cover":
                return {"state": "CLOSE"}
            if service == "set_cover_position":
                return {"position": data.get("position", 0)}

        return {"state": "ON" if service == "turn_on" else "OFF"}

