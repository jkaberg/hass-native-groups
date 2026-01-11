"""Native Group Orchestrator - core coordination logic."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_ENTITY_ID,
    ATTR_FLOOR_ID,
    ATTR_LABEL_ID,
    EVENT_STATE_CHANGED,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .classifier import EntityClassifier
from .const import (
    CONF_ENABLE_AREAS,
    CONF_ENABLE_FLOORS,
    CONF_ENABLE_GROUPS,
    CONF_ENABLE_LABELS,
    CONF_ENABLE_SCENES,
    CONF_ENABLED_PROTOCOLS,
    DEFAULT_SYNC_DEBOUNCE,
    DOMAIN,
    EVENT_MEMBERSHIP_CHANGED,
    EVENT_SCENE_CHANGED,
    GROUPING_TYPE_AREA,
    GROUPING_TYPE_FLOOR,
    GROUPING_TYPE_GROUP,
    GROUPING_TYPE_LABEL,
    GROUPING_TYPE_SCENE,
    PROTOCOL_ZIGBEE2MQTT,
    PROTOCOL_ZHA,
    PROTOCOL_ZWAVE_JS,
    RECONCILE_INTERVAL,
    SCENE_ID_MAX,
    SCENE_ID_START,
    STORAGE_KEY,
    STORAGE_VERSION,
    ZWAVE_CAP_BINARY,
    ZWAVE_CAP_COLOR,
    ZWAVE_CAP_DIMMER,
)
from .handlers.base import ProtocolHandler
from .handlers.registry import HandlerRegistry
from .mapping import GroupMapping, NativeGroupRef, NativeSceneRef

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)


class NativeGroupOrchestrator:
    """Orchestrates synchronization between HA groups and native protocol groups."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize orchestrator."""
        self.hass = hass
        self._config_entry = config_entry
        self._classifier = EntityClassifier(hass)
        self._handler_registry = HandlerRegistry(hass)
        self._handlers: dict[str, ProtocolHandler] = {}
        self._mappings: dict[str, GroupMapping] = {}
        self._managed_resources: dict[str, set[str]] = defaultdict(set)
        self._scene_id_counter = SCENE_ID_START
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._sync_debouncer: Debouncer[None] | None = None
        self._started = False
        self._unsub_listeners: list[CALLBACK_TYPE] = []
        self._pending_tasks: set[asyncio.Task[Any]] = set()

    @property
    def _options(self) -> dict[str, Any]:
        """Get current options."""
        return self._config_entry.options

    @property
    def _enabled_protocols(self) -> list[str]:
        """Get list of enabled protocols."""
        return self._options.get(
            CONF_ENABLED_PROTOCOLS,
            [PROTOCOL_ZWAVE_JS, PROTOCOL_ZIGBEE2MQTT, PROTOCOL_ZHA],
        )

    # ─────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ─────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Start the orchestrator after HA is fully loaded."""
        if self._started:
            return

        _LOGGER.info("Starting Native Group Orchestrator")

        # Initialize protocol handlers via registry
        await self._async_setup_handlers()

        # Load persisted state
        await self._async_load_state()

        # Set up debouncer for sync operations
        self._sync_debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=DEFAULT_SYNC_DEBOUNCE,
            immediate=False,
            function=self._async_process_sync_queue,
        )

        # Set up event listeners
        self._setup_listeners()

        # Set up periodic reconciliation
        self._unsub_listeners.append(
            async_track_time_interval(
                self.hass,
                self._async_reconcile,
                timedelta(seconds=RECONCILE_INTERVAL),
            )
        )

        # Initial sync of all groupings
        await self.async_sync_all()

        self._started = True
        _LOGGER.info("Native Group Orchestrator started")

    async def async_stop(self) -> None:
        """Stop the orchestrator."""
        _LOGGER.info("Stopping Native Group Orchestrator")

        # Cancel pending tasks
        for task in self._pending_tasks:
            task.cancel()
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        self._pending_tasks.clear()

        # Remove listeners
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

        # Shutdown debouncer
        if self._sync_debouncer:
            self._sync_debouncer.async_shutdown()

        # Save state
        await self._async_save_state()

        # Cleanup handlers
        for handler in self._handlers.values():
            await handler.async_cleanup()

        self._started = False

    async def _async_setup_handlers(self) -> None:
        """Initialize protocol handlers based on available integrations."""
        enabled = set(self._enabled_protocols)

        for protocol, handler in self._handler_registry.get_available_handlers():
            if protocol in enabled:
                if await handler.async_is_available():
                    self._handlers[protocol] = handler
                    _LOGGER.debug("%s handler initialized", protocol)

    def _setup_listeners(self) -> None:
        """Set up event listeners."""
        options = self._options

        # Listen for state changes (groups, scenes)
        if options.get(CONF_ENABLE_GROUPS, True) or options.get(CONF_ENABLE_SCENES, True):
            self._unsub_listeners.append(
                self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed)
            )

        # Listen for custom events
        self._unsub_listeners.append(
            self.hass.bus.async_listen(
                EVENT_MEMBERSHIP_CHANGED, self._on_membership_changed
            )
        )
        self._unsub_listeners.append(
            self.hass.bus.async_listen(EVENT_SCENE_CHANGED, self._on_scene_changed)
        )

        # Listen for registry changes (areas, labels, floors)
        if options.get(CONF_ENABLE_AREAS, True):
            self._unsub_listeners.append(
                self.hass.bus.async_listen(
                    ar.EVENT_AREA_REGISTRY_UPDATED, self._on_area_registry_updated
                )
            )

        if options.get(CONF_ENABLE_LABELS, True):
            self._unsub_listeners.append(
                self.hass.bus.async_listen(
                    lr.EVENT_LABEL_REGISTRY_UPDATED, self._on_label_registry_updated
                )
            )

        if options.get(CONF_ENABLE_FLOORS, True):
            self._unsub_listeners.append(
                self.hass.bus.async_listen(
                    fr.EVENT_FLOOR_REGISTRY_UPDATED, self._on_floor_registry_updated
                )
            )

        # Listen for entity registry changes (area/label assignments)
        if options.get(CONF_ENABLE_AREAS, True) or options.get(CONF_ENABLE_LABELS, True):
            self._unsub_listeners.append(
                self.hass.bus.async_listen(
                    er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_entity_registry_updated
                )
            )
            self._unsub_listeners.append(
                self.hass.bus.async_listen(
                    dr.EVENT_DEVICE_REGISTRY_UPDATED, self._on_device_registry_updated
                )
            )

    def _create_background_task(self, coro: Any, name: str) -> None:
        """Create a tracked background task with proper error handling."""
        task = self.hass.async_create_background_task(coro, name)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    # ─────────────────────────────────────────────────────────────
    # RECONCILIATION
    # ─────────────────────────────────────────────────────────────

    async def _async_reconcile(self, now: Any = None) -> None:
        """Periodically reconcile state with actual device state."""
        _LOGGER.debug("Running periodic reconciliation")

        for protocol, handler in self._handlers.items():
            try:
                # Get actual groups from protocol
                actual_groups = await handler.async_get_groups()

                # Find orphaned groups (in protocol but not in mappings)
                managed_group_ids = set()
                for mapping in self._mappings.values():
                    if protocol in mapping.native_groups:
                        group_ref = mapping.native_groups[protocol]
                        managed_group_ids.add(str(group_ref.group_id))

                for group_id, group_info in actual_groups.items():
                    group_name = group_info.get("name", "")
                    # Check if this is one of our managed groups
                    if group_name.startswith("ha_") and str(group_id) not in managed_group_ids:
                        _LOGGER.info(
                            "Cleaning up orphaned %s group: %s",
                            protocol,
                            group_name,
                        )
                        await handler.async_delete_group(group_id)

            except Exception as err:
                _LOGGER.debug(
                    "Reconciliation failed for %s: %s",
                    protocol,
                    err,
                )

    # ─────────────────────────────────────────────────────────────
    # EVENT HANDLERS
    # ─────────────────────────────────────────────────────────────

    @callback
    def _on_state_changed(self, event: Event) -> None:
        """Handle state_changed events."""
        entity_id = event.data.get("entity_id", "")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        options = self._options

        if entity_id.startswith("group.") and options.get(CONF_ENABLE_GROUPS, True):
            if new_state is None:
                self._create_background_task(
                    self._on_group_deleted(entity_id),
                    f"native_groups_delete_{entity_id}",
                )
            elif old_state is None:
                self._create_background_task(
                    self._on_group_created(entity_id),
                    f"native_groups_create_{entity_id}",
                )
            else:
                # Check for membership change
                old_members = set(old_state.attributes.get("entity_id", []))
                new_members = set(new_state.attributes.get("entity_id", []))
                if old_members != new_members:
                    self._create_background_task(
                        self._on_group_updated(entity_id),
                        f"native_groups_update_{entity_id}",
                    )

        elif entity_id.startswith("scene.") and options.get(CONF_ENABLE_SCENES, True):
            if new_state is None:
                self._create_background_task(
                    self._on_scene_deleted(entity_id),
                    f"native_groups_scene_delete_{entity_id}",
                )
            elif old_state is None:
                self._create_background_task(
                    self._on_scene_created(entity_id),
                    f"native_groups_scene_create_{entity_id}",
                )

    @callback
    def _on_membership_changed(self, event: Event) -> None:
        """Handle custom membership changed event."""
        entity_id = event.data.get("entity_id")
        if entity_id:
            self._create_background_task(
                self._on_group_updated(entity_id),
                f"native_groups_membership_{entity_id}",
            )

    @callback
    def _on_scene_changed(self, event: Event) -> None:
        """Handle custom scene changed event."""
        entity_id = event.data.get("entity_id")
        action = event.data.get("action")

        if action == "created":
            self._create_background_task(
                self._on_scene_created(entity_id),
                f"native_groups_scene_create_{entity_id}",
            )
        elif action == "updated":
            self._create_background_task(
                self._on_scene_updated(entity_id),
                f"native_groups_scene_update_{entity_id}",
            )
        elif action == "deleted":
            self._create_background_task(
                self._on_scene_deleted(entity_id),
                f"native_groups_scene_delete_{entity_id}",
            )

    @callback
    def _on_area_registry_updated(self, event: Event) -> None:
        """Handle area registry changes."""
        action = event.data.get("action")
        area_id = event.data.get("area_id")

        if action == "create" and area_id:
            self._create_background_task(
                self._provision_area(area_id),
                f"native_groups_area_create_{area_id}",
            )
        elif action == "remove" and area_id:
            mapping_key = f"area.{area_id}"
            self._create_background_task(
                self._cleanup_resources(mapping_key),
                f"native_groups_area_remove_{area_id}",
            )
            self._mappings.pop(mapping_key, None)
        elif action == "update" and area_id:
            mapping_key = f"area.{area_id}"
            self._create_background_task(
                self._reprovision_mapping(mapping_key, lambda: self._provision_area(area_id)),
                f"native_groups_area_update_{area_id}",
            )

    @callback
    def _on_floor_registry_updated(self, event: Event) -> None:
        """Handle floor registry changes."""
        action = event.data.get("action")
        floor_id = event.data.get("floor_id")

        if action == "create" and floor_id:
            self._create_background_task(
                self._provision_floor(floor_id),
                f"native_groups_floor_create_{floor_id}",
            )
        elif action == "remove" and floor_id:
            mapping_key = f"floor.{floor_id}"
            self._create_background_task(
                self._cleanup_resources(mapping_key),
                f"native_groups_floor_remove_{floor_id}",
            )
            self._mappings.pop(mapping_key, None)
        elif action == "update" and floor_id:
            mapping_key = f"floor.{floor_id}"
            self._create_background_task(
                self._reprovision_mapping(mapping_key, lambda: self._provision_floor(floor_id)),
                f"native_groups_floor_update_{floor_id}",
            )

    @callback
    def _on_label_registry_updated(self, event: Event) -> None:
        """Handle label registry changes."""
        action = event.data.get("action")
        label_id = event.data.get("label_id")

        if action == "create" and label_id:
            self._create_background_task(
                self._provision_label(label_id),
                f"native_groups_label_create_{label_id}",
            )
        elif action == "remove" and label_id:
            mapping_key = f"label.{label_id}"
            self._create_background_task(
                self._cleanup_resources(mapping_key),
                f"native_groups_label_remove_{label_id}",
            )
            self._mappings.pop(mapping_key, None)
        elif action == "update" and label_id:
            mapping_key = f"label.{label_id}"
            self._create_background_task(
                self._reprovision_mapping(mapping_key, lambda: self._provision_label(label_id)),
                f"native_groups_label_update_{label_id}",
            )

    @callback
    def _on_entity_registry_updated(self, event: Event) -> None:
        """Handle entity registry changes (area/label assignments)."""
        action = event.data.get("action")
        if action == "update":
            changes = event.data.get("changes", {})
            # If area_id or labels changed, we need to re-sync affected areas/labels
            if "area_id" in changes or "labels" in changes:
                # Schedule debounced re-sync
                if self._sync_debouncer:
                    self._sync_debouncer.async_schedule_call()

    @callback
    def _on_device_registry_updated(self, event: Event) -> None:
        """Handle device registry changes (area/label assignments)."""
        action = event.data.get("action")
        if action == "update":
            changes = event.data.get("changes", {})
            # If area_id or labels changed, we need to re-sync
            if "area_id" in changes or "labels" in changes:
                if self._sync_debouncer:
                    self._sync_debouncer.async_schedule_call()

    async def _reprovision_mapping(self, mapping_key: str, provision_func: Any) -> None:
        """Clean up and reprovision a mapping."""
        await self._cleanup_resources(mapping_key)
        await provision_func()

    # ─────────────────────────────────────────────────────────────
    # GROUP LIFECYCLE
    # ─────────────────────────────────────────────────────────────

    async def _on_group_created(self, group_id: str) -> None:
        """Handle new HA group creation."""
        _LOGGER.debug("Group created: %s", group_id)
        await self._provision_group(group_id)

    async def _on_group_updated(self, group_id: str) -> None:
        """Handle HA group membership change."""
        _LOGGER.debug("Group updated: %s", group_id)
        await self._cleanup_resources(group_id)
        await self._provision_group(group_id)

    async def _on_group_deleted(self, group_id: str) -> None:
        """Handle HA group deletion."""
        _LOGGER.debug("Group deleted: %s", group_id)
        await self._cleanup_resources(group_id)
        self._mappings.pop(group_id, None)

    async def _provision_group(self, group_id: str) -> None:
        """Provision native groups for an HA group."""
        state = self.hass.states.get(group_id)
        if not state:
            return

        members = state.attributes.get("entity_id", [])
        if not members:
            return

        await self._provision_entity_list(
            group_id, GROUPING_TYPE_GROUP, list(members)
        )

    # ─────────────────────────────────────────────────────────────
    # SCENE LIFECYCLE
    # ─────────────────────────────────────────────────────────────

    async def _on_scene_created(self, scene_id: str) -> None:
        """Handle new HA scene creation."""
        _LOGGER.debug("Scene created: %s", scene_id)
        await self._provision_scene(scene_id)

    async def _on_scene_updated(self, scene_id: str) -> None:
        """Handle HA scene modification."""
        _LOGGER.debug("Scene updated: %s", scene_id)
        await self._cleanup_resources(scene_id)
        await self._provision_scene(scene_id)

    async def _on_scene_deleted(self, scene_id: str) -> None:
        """Handle HA scene deletion."""
        _LOGGER.debug("Scene deleted: %s", scene_id)
        await self._cleanup_resources(scene_id)
        self._mappings.pop(scene_id, None)

    async def _provision_scene(self, scene_id: str) -> None:
        """Provision native scenes for an HA scene."""
        scene_config = await self._get_scene_config(scene_id)
        if not scene_config:
            return

        entities_config = scene_config.get("entities", {})
        if not entities_config:
            return

        # Classify entities by protocol
        by_protocol: dict[str, list[tuple[str, Any, dict[str, Any]]]] = defaultdict(
            list
        )

        for entity_id, target_state in entities_config.items():
            info = self._classifier.classify_entity(entity_id)
            by_protocol[info.protocol].append((entity_id, info, target_state))

        mapping = GroupMapping(
            ha_entity_id=scene_id,
            ha_entity_type=GROUPING_TYPE_SCENE,
        )

        native_scene_id = self._allocate_scene_id()

        for protocol, entities in by_protocol.items():
            handler = self._handlers.get(protocol)
            if not handler or not entities:
                mapping.ungrouped_entities.extend([e[0] for e in entities])
                continue

            # Check if handler supports native scenes
            if await handler.async_supports_native_scenes() and len(entities) > 1:
                await self._provision_native_scene(
                    handler, protocol, scene_id, native_scene_id, entities, mapping
                )
            else:
                # Fall back to ungrouped
                mapping.ungrouped_entities.extend([e[0] for e in entities])

        self._mappings[scene_id] = mapping
        await self._async_save_state()

    async def _provision_native_scene(
        self,
        handler: ProtocolHandler,
        protocol: str,
        scene_id: str,
        native_scene_id: int,
        entities: list[tuple[str, Any, dict[str, Any]]],
        mapping: GroupMapping,
    ) -> None:
        """Provision a native scene with improved reliability."""
        group_name = self._generate_group_name(scene_id, protocol)
        native_ids = [e[1].native_id for e in entities]

        try:
            # Create group first
            await handler.async_create_group(group_name, native_ids)

            # Build per-device state map
            device_states: dict[Any, dict[str, Any]] = {}
            for entity_id, info, target_state in entities:
                state_dict = target_state if isinstance(target_state, dict) else {}
                device_states[info.native_id] = handler.convert_service_data(
                    entity_id.split(".")[0],
                    "turn_on" if state_dict.get("state", "on") == "on" else "turn_off",
                    state_dict,
                )

            # Store native scene with retry
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    await handler.async_store_scene(
                        group_name, native_scene_id, device_states
                    )
                    break
                except Exception as err:
                    if attempt == max_retries - 1:
                        raise
                    _LOGGER.debug(
                        "Scene store attempt %d failed, retrying: %s",
                        attempt + 1,
                        err,
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))

            mapping.native_scenes[protocol] = NativeSceneRef(
                protocol=protocol,
                group_name=group_name,
                scene_id=native_scene_id,
                member_entity_ids=[e[0] for e in entities],
            )

            self._managed_resources[scene_id].add(f"{protocol}:group:{group_name}")
            self._managed_resources[scene_id].add(
                f"{protocol}:scene:{group_name}:{native_scene_id}"
            )

        except Exception as err:
            _LOGGER.error("Failed to create native scene for %s: %s", scene_id, err)
            mapping.sync_error = str(err)

    # ─────────────────────────────────────────────────────────────
    # AREA LIFECYCLE
    # ─────────────────────────────────────────────────────────────

    async def _provision_area(self, area_id: str) -> None:
        """Provision native groups for entities in an area."""
        _LOGGER.debug("Provisioning area: %s", area_id)

        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        # Get all entities in this area
        entity_ids: set[str] = set()

        # Direct entity assignments
        for entry in ent_reg.entities.get_entries_for_area_id(area_id):
            if entry.entity_category is None and entry.hidden_by is None:
                entity_ids.add(entry.entity_id)

        # Entities via device assignments
        for device in dev_reg.devices.get_devices_for_area_id(area_id):
            for entry in ent_reg.entities.get_entries_for_device_id(device.id):
                if (
                    entry.entity_category is None
                    and entry.hidden_by is None
                    and not entry.area_id  # No explicit area override
                ):
                    entity_ids.add(entry.entity_id)

        if entity_ids:
            mapping_key = f"area.{area_id}"
            await self._provision_entity_list(
                mapping_key, GROUPING_TYPE_AREA, list(entity_ids)
            )

    # ─────────────────────────────────────────────────────────────
    # FLOOR LIFECYCLE
    # ─────────────────────────────────────────────────────────────

    async def _provision_floor(self, floor_id: str) -> None:
        """Provision native groups for entities on a floor."""
        _LOGGER.debug("Provisioning floor: %s", floor_id)

        area_reg = ar.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        entity_ids: set[str] = set()

        # Get all areas on this floor
        for area in area_reg.areas.get_areas_for_floor(floor_id):
            # Direct entity assignments
            for entry in ent_reg.entities.get_entries_for_area_id(area.id):
                if entry.entity_category is None and entry.hidden_by is None:
                    entity_ids.add(entry.entity_id)

            # Entities via device assignments
            for device in dev_reg.devices.get_devices_for_area_id(area.id):
                for entry in ent_reg.entities.get_entries_for_device_id(device.id):
                    if (
                        entry.entity_category is None
                        and entry.hidden_by is None
                        and not entry.area_id
                    ):
                        entity_ids.add(entry.entity_id)

        if entity_ids:
            mapping_key = f"floor.{floor_id}"
            await self._provision_entity_list(
                mapping_key, GROUPING_TYPE_FLOOR, list(entity_ids)
            )

    # ─────────────────────────────────────────────────────────────
    # LABEL LIFECYCLE
    # ─────────────────────────────────────────────────────────────

    async def _provision_label(self, label_id: str) -> None:
        """Provision native groups for entities with a label."""
        _LOGGER.debug("Provisioning label: %s", label_id)

        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        entity_ids: set[str] = set()

        # Direct entity label assignments
        for entry in ent_reg.entities.get_entries_for_label(label_id):
            if entry.hidden_by is None:
                entity_ids.add(entry.entity_id)

        # Entities via device label assignments
        for device in dev_reg.devices.get_devices_for_label(label_id):
            for entry in ent_reg.entities.get_entries_for_device_id(device.id):
                if entry.hidden_by is None:
                    entity_ids.add(entry.entity_id)

        if entity_ids:
            mapping_key = f"label.{label_id}"
            await self._provision_entity_list(
                mapping_key, GROUPING_TYPE_LABEL, list(entity_ids)
            )

    # ─────────────────────────────────────────────────────────────
    # COMMON PROVISIONING
    # ─────────────────────────────────────────────────────────────

    async def _provision_entity_list(
        self,
        mapping_key: str,
        grouping_type: str,
        entity_ids: list[str],
    ) -> None:
        """Provision native groups for a list of entities."""
        if not entity_ids:
            return

        # Classify members by protocol
        by_protocol = self._classifier.classify_entities(entity_ids)

        mapping = GroupMapping(
            ha_entity_id=mapping_key,
            ha_entity_type=grouping_type,
        )

        # Create native group for each protocol
        for protocol, entities in by_protocol.items():
            handler = self._handlers.get(protocol)
            if not handler or not entities:
                mapping.ungrouped_entities.extend([e.entity_id for e in entities])
                continue

            if len(entities) >= 1:
                group_name = self._generate_group_name(mapping_key, protocol)
                ungrouped_from_protocol: list[str] = []

                try:
                    # For Z-Wave, use capability-based grouping
                    if protocol == PROTOCOL_ZWAVE_JS and len(entities) > 1:
                        native_group_id, ungrouped_from_protocol = (
                            await self._create_zwave_capability_groups(
                                handler, group_name, entities
                            )
                        )
                    elif len(entities) > 1:
                        native_ids = [e.native_id for e in entities]
                        native_group_id = await handler.async_create_group(
                            group_name, native_ids
                        )
                    else:
                        native_group_id = None  # Single entity, no group needed

                    # Track entities that couldn't be grouped (e.g., climate on Z-Wave)
                    if ungrouped_from_protocol:
                        mapping.ungrouped_entities.extend(ungrouped_from_protocol)

                    # Only track grouped entities in native_groups
                    grouped_entity_ids = [
                        e.entity_id
                        for e in entities
                        if e.entity_id not in ungrouped_from_protocol
                    ]

                    mapping.native_groups[protocol] = NativeGroupRef(
                        protocol=protocol,
                        group_id=native_group_id,
                        group_name=group_name,
                        member_entity_ids=grouped_entity_ids,
                        member_native_ids=[
                            e.native_id
                            for e in entities
                            if e.entity_id not in ungrouped_from_protocol
                        ],
                    )

                    if native_group_id:
                        self._managed_resources[mapping_key].add(
                            f"{protocol}:group:{native_group_id}"
                        )

                except Exception as err:
                    _LOGGER.error(
                        "Failed to create native group for %s: %s", mapping_key, err
                    )
                    mapping.sync_error = str(err)

        self._mappings[mapping_key] = mapping
        await self._async_save_state()

    async def _create_zwave_capability_groups(
        self,
        handler: ProtocolHandler,
        group_name: str,
        entities: list[Any],
    ) -> tuple[str, list[str]]:
        """Create capability-based sub-groups for Z-Wave."""
        from .handlers.zwave_js import ZWaveJSHandler

        if not isinstance(handler, ZWaveJSHandler):
            # Fall back to standard group if not ZWaveJSHandler
            group_id = await handler.async_create_group(
                group_name, [e.native_id for e in entities]
            )
            return group_id, []

        # Group entities by capability
        members_by_capability: dict[str, list[int]] = {
            ZWAVE_CAP_BINARY: [],
            ZWAVE_CAP_DIMMER: [],
            ZWAVE_CAP_COLOR: [],
        }
        ungrouped: list[str] = []

        for entity in entities:
            if entity.capability is None:
                # Entity domain not groupable (climate, lock, fan, etc.)
                ungrouped.append(entity.entity_id)
            elif entity.capability in members_by_capability and entity.native_id:
                members_by_capability[entity.capability].append(entity.native_id)

        # Log capability distribution
        caps_with_members = {
            k: len(v) for k, v in members_by_capability.items() if v
        }
        if caps_with_members:
            _LOGGER.debug(
                "Z-Wave group %s capability distribution: %s",
                group_name,
                caps_with_members,
            )
        if ungrouped:
            _LOGGER.debug(
                "Z-Wave group %s ungroupable entities (unicast fallback): %s",
                group_name,
                ungrouped,
            )

        # Create capability-based groups
        group_id = await handler.async_create_capability_groups(
            group_name, members_by_capability
        )
        return group_id, ungrouped

    # ─────────────────────────────────────────────────────────────
    # SERVICE DISPATCH
    # ─────────────────────────────────────────────────────────────

    async def async_dispatch(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> bool:
        """Dispatch service call via native groups.

        Returns True if handled via native groups, False otherwise.
        """
        # Check for direct entity_id targeting
        entity_ids = data.get(ATTR_ENTITY_ID, [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        # Check for area/floor/label targeting
        area_ids = data.get(ATTR_AREA_ID, [])
        if isinstance(area_ids, str):
            area_ids = [area_ids]

        floor_ids = data.get(ATTR_FLOOR_ID, [])
        if isinstance(floor_ids, str):
            floor_ids = [floor_ids]

        label_ids = data.get(ATTR_LABEL_ID, [])
        if isinstance(label_ids, str):
            label_ids = [label_ids]

        handled = False
        tasks: list[asyncio.Task[None]] = []

        # Handle direct entity targeting (groups/scenes)
        for entity_id in entity_ids:
            if entity_id in self._mappings:
                mapping = self._mappings[entity_id]
                if mapping.ha_entity_type == GROUPING_TYPE_SCENE:
                    tasks.append(
                        asyncio.create_task(
                            self._dispatch_scene(mapping, domain, service, data)
                        )
                    )
                else:
                    tasks.append(
                        asyncio.create_task(
                            self._dispatch_group(mapping, domain, service, data)
                        )
                    )
                handled = True

        # Handle area targeting
        for area_id in area_ids:
            mapping_key = f"area.{area_id}"
            if mapping_key in self._mappings:
                tasks.append(
                    asyncio.create_task(
                        self._dispatch_group(
                            self._mappings[mapping_key], domain, service, data
                        )
                    )
                )
                handled = True

        # Handle floor targeting
        for floor_id in floor_ids:
            mapping_key = f"floor.{floor_id}"
            if mapping_key in self._mappings:
                tasks.append(
                    asyncio.create_task(
                        self._dispatch_group(
                            self._mappings[mapping_key], domain, service, data
                        )
                    )
                )
                handled = True

        # Handle label targeting
        for label_id in label_ids:
            mapping_key = f"label.{label_id}"
            if mapping_key in self._mappings:
                tasks.append(
                    asyncio.create_task(
                        self._dispatch_group(
                            self._mappings[mapping_key], domain, service, data
                        )
                    )
                )
                handled = True

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        return handled

    async def _dispatch_scene(
        self,
        mapping: GroupMapping,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> None:
        """Dispatch scene activation."""
        tasks: list[asyncio.Task[None]] = []

        # Use native scene recall where available
        for protocol, scene_ref in mapping.native_scenes.items():
            handler = self._handlers.get(protocol)
            if handler:
                tasks.append(
                    asyncio.create_task(
                        handler.async_recall_scene(
                            scene_ref.group_name, scene_ref.scene_id
                        )
                    )
                )

        # Handle ungrouped entities individually
        for entity_id in mapping.ungrouped_entities:
            tasks.append(
                asyncio.create_task(
                    self.hass.services.async_call(
                        domain, service, {**data, "entity_id": entity_id}
                    )
                )
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dispatch_group(
        self,
        mapping: GroupMapping,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> None:
        """Dispatch group command."""
        tasks: list[asyncio.Task[None]] = []

        for protocol, group_ref in mapping.native_groups.items():
            handler = self._handlers.get(protocol)
            if not handler:
                continue

            if group_ref.group_id:
                # Use native group command
                tasks.append(
                    asyncio.create_task(
                        handler.async_send_group_command(
                            group_ref.group_id, domain, service, data
                        )
                    )
                )
            else:
                # Single entity - send directly
                tasks.append(
                    asyncio.create_task(
                        handler.async_send_multicast(
                            group_ref.member_native_ids, domain, service, data
                        )
                    )
                )

        # Handle ungrouped entities
        for entity_id in mapping.ungrouped_entities:
            tasks.append(
                asyncio.create_task(
                    self.hass.services.async_call(
                        domain, service, {**data, "entity_id": entity_id}
                    )
                )
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ─────────────────────────────────────────────────────────────
    # SYNC & CLEANUP
    # ─────────────────────────────────────────────────────────────

    async def async_sync_all(self) -> None:
        """Sync all groups, scenes, areas, floors, and labels."""
        _LOGGER.info("Syncing all groupings")
        options = self._options

        # Sync groups
        if options.get(CONF_ENABLE_GROUPS, True):
            for state in self.hass.states.async_all("group"):
                await self._provision_group(state.entity_id)

        # Sync scenes
        if options.get(CONF_ENABLE_SCENES, True):
            for state in self.hass.states.async_all("scene"):
                await self._provision_scene(state.entity_id)

        # Sync areas
        if options.get(CONF_ENABLE_AREAS, True):
            area_reg = ar.async_get(self.hass)
            for area in area_reg.areas.values():
                await self._provision_area(area.id)

        # Sync floors
        if options.get(CONF_ENABLE_FLOORS, True):
            floor_reg = fr.async_get(self.hass)
            for floor in floor_reg.floors.values():
                await self._provision_floor(floor.floor_id)

        # Sync labels
        if options.get(CONF_ENABLE_LABELS, True):
            label_reg = lr.async_get(self.hass)
            for label in label_reg.labels.values():
                await self._provision_label(label.label_id)

        await self._async_save_state()

    async def async_sync_entity(self, entity_id: str) -> None:
        """Sync a specific entity or grouping."""
        await self._cleanup_resources(entity_id)

        if entity_id.startswith("group."):
            await self._provision_group(entity_id)
        elif entity_id.startswith("scene."):
            await self._provision_scene(entity_id)
        elif entity_id.startswith("area."):
            area_id = entity_id[5:]  # Remove "area." prefix
            await self._provision_area(area_id)
        elif entity_id.startswith("floor."):
            floor_id = entity_id[6:]  # Remove "floor." prefix
            await self._provision_floor(floor_id)
        elif entity_id.startswith("label."):
            label_id = entity_id[6:]  # Remove "label." prefix
            await self._provision_label(label_id)

    async def _cleanup_resources(self, ha_entity_id: str) -> None:
        """Clean up native resources for an HA entity."""
        resources = self._managed_resources.pop(ha_entity_id, set())

        for resource_ref in resources:
            parts = resource_ref.split(":")
            protocol = parts[0]
            resource_type = parts[1]

            handler = self._handlers.get(protocol)
            if not handler:
                continue

            try:
                if resource_type == "group":
                    await handler.async_delete_group(parts[2])
                elif resource_type == "scene":
                    await handler.async_remove_scene(parts[2], int(parts[3]))
            except Exception as err:
                _LOGGER.warning("Failed to cleanup %s: %s", resource_ref, err)

    async def _async_process_sync_queue(self) -> None:
        """Process queued sync operations (debounced)."""
        # Re-sync all areas, floors, and labels since entity assignments changed
        _LOGGER.debug("Processing debounced sync for registry changes")
        await self.async_sync_all()

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _generate_group_name(self, ha_entity_id: str, protocol: str) -> str:
        """Generate unique name for native groups."""
        # Handle both entity IDs (group.xxx) and mapping keys (area.xxx)
        base = ha_entity_id.replace(".", "_")
        return f"ha_{base}_{protocol}"

    def _allocate_scene_id(self) -> int:
        """Allocate a unique scene ID."""
        scene_id = self._scene_id_counter
        self._scene_id_counter += 1
        if self._scene_id_counter > SCENE_ID_MAX:
            self._scene_id_counter = SCENE_ID_START
        return scene_id

    async def _get_scene_config(self, scene_id: str) -> dict[str, Any] | None:
        """Get scene configuration."""
        state = self.hass.states.get(scene_id)
        if state:
            return {"entities": state.attributes.get("entity_id", {})}
        return None

    def get_mapping(self, entity_id: str) -> GroupMapping | None:
        """Get mapping for an entity."""
        return self._mappings.get(entity_id)

    def get_all_mappings(self) -> dict[str, GroupMapping]:
        """Get all mappings."""
        return dict(self._mappings)

    @property
    def is_started(self) -> bool:
        """Return whether the orchestrator is started."""
        return self._started

    @property
    def enabled_protocols(self) -> list[str]:
        """Return list of enabled protocols."""
        return self._enabled_protocols

    @property
    def scene_id_counter(self) -> int:
        """Return current scene ID counter."""
        return self._scene_id_counter

    @property
    def pending_task_count(self) -> int:
        """Return number of pending tasks."""
        return len(self._pending_tasks)

    @property
    def handlers(self) -> dict[str, ProtocolHandler]:
        """Return protocol handlers."""
        return dict(self._handlers)

    @property
    def managed_resources(self) -> dict[str, set[str]]:
        """Return managed resources."""
        return {k: set(v) for k, v in self._managed_resources.items()}

    # ─────────────────────────────────────────────────────────────
    # PERSISTENCE
    # ─────────────────────────────────────────────────────────────

    async def _async_load_state(self) -> None:
        """Load persisted state."""
        data = await self._store.async_load()
        if not data:
            return

        self._scene_id_counter = data.get("scene_id_counter", SCENE_ID_START)

        for mapping_data in data.get("mappings", []):
            try:
                mapping = GroupMapping.from_dict(mapping_data)
                self._mappings[mapping.ha_entity_id] = mapping
            except Exception as err:
                _LOGGER.warning("Failed to load mapping: %s", err)

        self._managed_resources = defaultdict(
            set, {k: set(v) for k, v in data.get("managed_resources", {}).items()}
        )

    async def _async_save_state(self) -> None:
        """Save state to storage."""
        # Update last_synced timestamp
        for mapping in self._mappings.values():
            mapping.last_synced = time.time()

        await self._store.async_save(
            {
                "scene_id_counter": self._scene_id_counter,
                "mappings": [m.to_dict() for m in self._mappings.values()],
                "managed_resources": {
                    k: list(v) for k, v in self._managed_resources.items()
                },
            }
        )
