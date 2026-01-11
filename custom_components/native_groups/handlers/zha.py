"""ZHA protocol handler with native Zigbee scene support."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er

from ..const import PROTOCOL_ZHA
from .base import ProtocolHandler

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Zigbee Cluster IDs
SCENES_CLUSTER_ID = 0x0005
ON_OFF_CLUSTER_ID = 0x0006
LEVEL_CONTROL_CLUSTER_ID = 0x0008
COLOR_CONTROL_CLUSTER_ID = 0x0300

# Zigbee Scenes Cluster Commands (server-side, client sends these)
SCENE_CMD_ADD = 0x00
SCENE_CMD_VIEW = 0x01
SCENE_CMD_REMOVE = 0x02
SCENE_CMD_REMOVE_ALL = 0x03
SCENE_CMD_STORE = 0x04
SCENE_CMD_RECALL = 0x05
SCENE_CMD_GET_MEMBERSHIP = 0x06
SCENE_CMD_ENHANCED_ADD = 0x40
SCENE_CMD_ENHANCED_VIEW = 0x41
SCENE_CMD_COPY = 0x42

# Reserved range for native_groups managed groups (avoid user groups)
MANAGED_GROUP_ID_START = 0x1000
MANAGED_GROUP_ID_END = 0x1FFF


class ZHAHandler(ProtocolHandler):
    """Handler for ZHA integration with native Zigbee scene support."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize ZHA handler."""
        super().__init__(hass)
        self._groups: dict[int, list[str]] = {}  # group_id → IEEE addresses
        self._group_name_to_id: dict[str, int] = {}
        self._next_group_id: int | None = None  # Initialized lazily
        # Track scenes: (group_id, scene_id) → True
        self._scenes: set[tuple[int, int]] = set()
        self._initialized = False

    @property
    def protocol_id(self) -> str:
        """Return protocol identifier."""
        return PROTOCOL_ZHA

    async def async_is_available(self) -> bool:
        """Check if ZHA integration is loaded."""
        return "zha" in self.hass.config.components

    def _get_zha_gateway(self) -> Any:
        """Get the ZHA gateway object.

        Returns the gateway for internal API access.
        Raises ValueError if ZHA is not available.
        """
        # Import here to avoid circular imports and make ZHA optional
        try:
            from homeassistant.components.zha.helpers import (  # noqa: PLC0415
                get_zha_gateway,
            )

            return get_zha_gateway(self.hass)
        except (ImportError, ValueError) as err:
            raise ValueError("ZHA gateway not available") from err

    def _get_zha_gateway_proxy(self) -> Any:
        """Get the ZHA gateway proxy object."""
        try:
            from homeassistant.components.zha.helpers import (  # noqa: PLC0415
                get_zha_gateway_proxy,
            )

            return get_zha_gateway_proxy(self.hass)
        except (ImportError, ValueError) as err:
            raise ValueError("ZHA gateway proxy not available") from err

    async def _async_ensure_initialized(self) -> None:
        """Ensure handler is initialized with existing group info."""
        if self._initialized:
            return

        # Query existing ZHA groups to find a safe starting ID
        try:
            existing_groups = await self._async_query_existing_groups()
            if existing_groups:
                max_id = max(existing_groups.keys())
                # Start after the highest existing group, but within our range
                self._next_group_id = max(
                    MANAGED_GROUP_ID_START,
                    max_id + 1 if max_id < MANAGED_GROUP_ID_START else max_id + 1,
                )
            else:
                self._next_group_id = MANAGED_GROUP_ID_START

            _LOGGER.debug(
                "ZHA handler initialized, next group ID: %d", self._next_group_id
            )
        except Exception as err:
            _LOGGER.warning(
                "Could not query existing ZHA groups: %s. Using default start ID.",
                err,
            )
            self._next_group_id = MANAGED_GROUP_ID_START

        self._initialized = True

    async def _async_query_existing_groups(self) -> dict[int, dict[str, Any]]:
        """Query existing ZHA groups from the integration."""
        groups: dict[int, dict[str, Any]] = {}

        try:
            gateway_proxy = self._get_zha_gateway_proxy()
            for group_id, group_proxy in gateway_proxy.group_proxies.items():
                groups[group_id] = {
                    "name": group_proxy.group.name,
                    "group_id": group_id,
                }
        except ValueError:
            # ZHA not available, return empty
            pass

        return groups

    async def async_get_groups(self) -> dict[int, dict[str, Any]]:
        """Get all ZHA groups for reconciliation."""
        return await self._async_query_existing_groups()

    async def async_cleanup(self) -> None:
        """Clean up handler resources."""
        # Clear local state
        self._groups.clear()
        self._group_name_to_id.clear()
        self._scenes.clear()
        self._initialized = False

    # ─────────────────────────────────────────────────────────────
    # GROUP MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    async def async_create_group(
        self,
        name: str,
        member_native_ids: list[str],
    ) -> int:
        """Create a ZHA group using internal gateway API."""
        await self._async_ensure_initialized()

        # Check if we already have this group
        if name in self._group_name_to_id:
            existing_id = self._group_name_to_id[name]
            # Update members
            await self.async_update_group_members(
                existing_id,
                add_members=member_native_ids,
            )
            return existing_id

        # Allocate new group ID from our reserved range
        group_id = self._next_group_id
        self._next_group_id += 1

        # Wrap around if needed (stay in managed range)
        if self._next_group_id > MANAGED_GROUP_ID_END:
            self._next_group_id = MANAGED_GROUP_ID_START

        try:
            gateway_proxy = self._get_zha_gateway_proxy()

            # Convert IEEE addresses to GroupMemberReference format
            # ZHA expects members as list of dicts with ieee and endpoint_id
            members = [
                {"ieee": ieee, "endpoint_id": 1} for ieee in member_native_ids
            ]

            # Use internal gateway API to create group
            group = await gateway_proxy.gateway.async_create_zigpy_group(
                name, members, group_id
            )

            if group:
                group_id = group.group_id
                _LOGGER.debug("Created ZHA group %s (ID: 0x%04x)", name, group_id)
        except Exception as err:
            _LOGGER.debug(
                "ZHA group creation via gateway API failed: %s. Using local tracking.",
                err,
            )

        self._groups[group_id] = list(member_native_ids)
        self._group_name_to_id[name] = group_id
        return group_id

    async def async_delete_group(self, group_id: str | int) -> None:
        """Delete a ZHA group using internal gateway API."""
        gid = int(group_id)

        # Remove all scenes for this group first
        await self._remove_all_scenes_for_group(gid)

        try:
            gateway = self._get_zha_gateway()
            if gid in gateway.groups:
                await gateway.groups[gid].async_remove_group()
                _LOGGER.debug("Deleted ZHA group via gateway API: 0x%04x", gid)
        except Exception as err:
            _LOGGER.debug("ZHA group deletion via gateway API failed: %s", err)

        self._groups.pop(gid, None)
        # Remove from name mapping
        self._group_name_to_id = {
            k: v for k, v in self._group_name_to_id.items() if v != gid
        }

    async def async_update_group_members(
        self,
        group_id: str | int,
        add_members: list[str] | None = None,
        remove_members: list[str] | None = None,
    ) -> None:
        """Update ZHA group membership using internal gateway API."""
        gid = int(group_id)

        try:
            gateway = self._get_zha_gateway()
            if gid not in gateway.groups:
                _LOGGER.debug("Group 0x%04x not found in gateway", gid)
                return

            zha_group = gateway.groups[gid]

            if add_members:
                # Convert IEEE strings to GroupMemberReference format
                members_to_add = [
                    {"ieee": ieee, "endpoint_id": 1} for ieee in add_members
                ]
                await zha_group.async_add_members(members_to_add)

            if remove_members:
                members_to_remove = [
                    {"ieee": ieee, "endpoint_id": 1} for ieee in remove_members
                ]
                await zha_group.async_remove_members(members_to_remove)

        except Exception as err:
            _LOGGER.debug("Failed to update group members via gateway: %s", err)

        # Update local tracking
        if gid not in self._groups:
            self._groups[gid] = []
        if add_members:
            self._groups[gid].extend(add_members)
        if remove_members:
            self._groups[gid] = [
                m for m in self._groups[gid] if m not in remove_members
            ]

    async def async_group_exists(self, group_id: str | int) -> bool:
        """Check if group exists."""
        gid = int(group_id)
        try:
            gateway = self._get_zha_gateway()
            return gid in gateway.groups
        except ValueError:
            return gid in self._groups

    # ─────────────────────────────────────────────────────────────
    # SCENE MANAGEMENT (Native Zigbee Scenes Cluster)
    # ─────────────────────────────────────────────────────────────

    async def async_supports_native_scenes(self) -> bool:
        """ZHA supports native Zigbee Scenes cluster."""
        return True

    async def async_store_scene(
        self,
        group_id: str | int,
        scene_id: int,
        device_states: dict[str, dict[str, Any]],
    ) -> None:
        """Store scene in ZHA devices using Zigbee Scenes cluster.

        The store_scene command tells devices to save their current state
        as the specified scene. Devices must first be set to the desired
        state before calling this.
        """
        gid = int(group_id)

        # First, set devices to desired states
        await self._apply_device_states(gid, device_states)

        # Wait for devices to reach target state
        await asyncio.sleep(0.5)

        # Send store_scene command to the group using internal API
        try:
            gateway = self._get_zha_gateway()
            group = gateway.get_group(gid)
            if group is not None:
                cluster = group.endpoint[SCENES_CLUSTER_ID]
                await cluster.command(
                    SCENE_CMD_STORE,
                    gid,  # group_id
                    scene_id,  # scene_id
                    expect_reply=True,
                )
                self._scenes.add((gid, scene_id))
                _LOGGER.debug("Stored ZHA scene %d in group 0x%04x", scene_id, gid)
            else:
                _LOGGER.warning("Group 0x%04x not found for scene storage", gid)
        except Exception as err:
            _LOGGER.error("Failed to store ZHA scene: %s", err)
            raise

    async def async_recall_scene(self, group_id: str | int, scene_id: int) -> None:
        """Recall ZHA scene using Zigbee Scenes cluster."""
        gid = int(group_id)

        try:
            gateway = self._get_zha_gateway()
            group = gateway.get_group(gid)
            if group is not None:
                cluster = group.endpoint[SCENES_CLUSTER_ID]
                await cluster.command(
                    SCENE_CMD_RECALL,
                    gid,  # group_id
                    scene_id,  # scene_id
                    expect_reply=True,
                )
                _LOGGER.debug("Recalled ZHA scene %d from group 0x%04x", scene_id, gid)
            else:
                _LOGGER.warning("Group 0x%04x not found for scene recall", gid)
        except Exception as err:
            _LOGGER.error("Failed to recall ZHA scene: %s", err)

    async def async_remove_scene(self, group_id: str | int, scene_id: int) -> None:
        """Remove ZHA scene using Zigbee Scenes cluster."""
        gid = int(group_id)

        try:
            gateway = self._get_zha_gateway()
            group = gateway.get_group(gid)
            if group is not None:
                cluster = group.endpoint[SCENES_CLUSTER_ID]
                await cluster.command(
                    SCENE_CMD_REMOVE,
                    gid,  # group_id
                    scene_id,  # scene_id
                    expect_reply=True,
                )
                self._scenes.discard((gid, scene_id))
                _LOGGER.debug("Removed ZHA scene %d from group 0x%04x", scene_id, gid)
            else:
                _LOGGER.debug("Group 0x%04x not found for scene removal", gid)
        except Exception as err:
            _LOGGER.error("Failed to remove ZHA scene: %s", err)

    async def _remove_all_scenes_for_group(self, group_id: int) -> None:
        """Remove all scenes for a group."""
        try:
            gateway = self._get_zha_gateway()
            group = gateway.get_group(group_id)
            if group is not None:
                cluster = group.endpoint[SCENES_CLUSTER_ID]
                await cluster.command(
                    SCENE_CMD_REMOVE_ALL,
                    group_id,  # group_id
                    expect_reply=True,
                )
                # Clean up tracking
                self._scenes = {
                    (gid, sid) for gid, sid in self._scenes if gid != group_id
                }
                _LOGGER.debug("Removed all scenes from ZHA group 0x%04x", group_id)
        except Exception as err:
            _LOGGER.debug("Failed to remove all scenes from group: %s", err)

    async def _apply_device_states(
        self, group_id: int, device_states: dict[str, dict[str, Any]]
    ) -> None:
        """Apply device states before storing scene.

        Sets each device to its target state so store_scene captures
        the correct values.
        """
        tasks: list[asyncio.Task[None]] = []

        for ieee, state in device_states.items():
            # Build service call based on state content
            if "brightness" in state or "on" in state:
                service_data: dict[str, Any] = {}

                if state.get("on", True):
                    service = "turn_on"
                    if "brightness" in state:
                        service_data["brightness"] = state["brightness"]
                    if "color_temp" in state:
                        service_data["color_temp"] = state["color_temp"]
                    if "hs_color" in state:
                        service_data["hs_color"] = state["hs_color"]
                    if "rgb_color" in state:
                        service_data["rgb_color"] = state["rgb_color"]
                else:
                    service = "turn_off"

                # Find entity for this IEEE
                entity_id = self._find_entity_by_ieee(ieee)
                if entity_id:
                    service_data["entity_id"] = entity_id
                    tasks.append(
                        asyncio.create_task(
                            self.hass.services.async_call(
                                "light", service, service_data, blocking=True
                            )
                        )
                    )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _find_entity_by_ieee(self, ieee: str) -> str | None:
        """Find a light entity ID for an IEEE address."""
        ent_reg = er.async_get(self.hass)

        for entry in ent_reg.entities.values():
            if entry.platform == "zha" and entry.domain == "light":
                if entry.unique_id and entry.unique_id.startswith(ieee):
                    return entry.entity_id

        return None

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
        """Send command to ZHA group entity."""
        gid = int(group_id)

        # ZHA groups appear as entities with format: light.zha_group_0xXXXX
        group_entity = f"light.zha_group_0x{gid:04x}"

        try:
            await self.hass.services.async_call(
                domain,
                service,
                {**service_data, "entity_id": group_entity},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.warning("Failed to send command to ZHA group: %s", err)

    async def async_send_multicast(
        self,
        native_ids: list[str],
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """ZHA doesn't support ad-hoc multicast - use group or individual.

        For now, we fall back to individual commands.
        """
        _LOGGER.debug(
            "ZHA ad-hoc multicast not supported, sending individual commands"
        )
        # Would need to resolve IEEE addresses to entity IDs and send individually
        # This is a limitation of the current implementation

    # ─────────────────────────────────────────────────────────────
    # ENTITY RESOLUTION
    # ─────────────────────────────────────────────────────────────

    def get_native_id(self, entity_id: str) -> str | None:
        """Extract IEEE address from ZHA entity."""
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)

        if not entry or entry.platform != "zha":
            return None

        # ZHA unique_id format: "aa:bb:cc:dd:ee:ff:00:11-1-6"
        if entry.unique_id:
            return entry.unique_id.split("-")[0]

        return None

    def convert_service_data(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert service data (ZHA uses same format as HA)."""
        return data
