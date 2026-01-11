"""Native Group Orchestration for Home Assistant.

This integration provides automatic synchronization between Home Assistant
groups/scenes and native protocol groups (Z-Wave multicast, Zigbee groups/scenes).

Benefits:
- Eliminates "popcorn effect" when controlling groups of lights
- Reduces network congestion by using native multicast
- Lower latency for group and scene activations
- Single source of truth for group definitions
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .orchestrator import NativeGroupOrchestrator

if TYPE_CHECKING:
    from .mapping import GroupMapping

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_SYNC_ALL: Final = "sync_all"
SERVICE_SYNC_ENTITY: Final = "sync_entity"
SERVICE_GET_STATUS: Final = "get_status"
SERVICE_DISPATCH: Final = "dispatch"

# Service schemas
SERVICE_SYNC_ENTITY_SCHEMA: Final = vol.Schema({vol.Required("entity_id"): cv.string})
SERVICE_GET_STATUS_SCHEMA: Final = vol.Schema({vol.Required("entity_id"): cv.string})
SERVICE_DISPATCH_SCHEMA: Final = vol.Schema(
    {
        vol.Required("domain"): cv.string,
        vol.Required("service"): cv.string,
        vol.Optional("target"): vol.Schema(
            {
                vol.Optional("entity_id"): vol.Any(cv.entity_ids, cv.entity_id),
                vol.Optional("area_id"): vol.Any(
                    vol.All(cv.ensure_list, [cv.string]), cv.string
                ),
                vol.Optional("floor_id"): vol.Any(
                    vol.All(cv.ensure_list, [cv.string]), cv.string
                ),
                vol.Optional("label_id"): vol.Any(
                    vol.All(cv.ensure_list, [cv.string]), cv.string
                ),
            }
        ),
        vol.Optional("data"): dict,
    }
)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

type NativeGroupsConfigEntry = ConfigEntry[NativeGroupOrchestrator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Native Group Orchestration from YAML (services only)."""
    # Services are registered here so they're available even without config entry
    # The actual orchestrator is set up via config entry
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: NativeGroupsConfigEntry
) -> bool:
    """Set up Native Group Orchestration from a config entry."""
    orchestrator = NativeGroupOrchestrator(hass, entry)

    # Store in runtime_data for proper lifecycle management
    entry.runtime_data = orchestrator

    # Start the orchestrator
    await orchestrator.async_start()

    # Register services
    await _async_setup_services(hass)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: NativeGroupsConfigEntry
) -> bool:
    """Unload a config entry."""
    orchestrator = entry.runtime_data

    # Stop orchestrator and clean up
    await orchestrator.async_stop()

    # Unregister services if no entries left
    if not hass.config_entries.async_entries(DOMAIN):
        for service_name in (
            SERVICE_SYNC_ALL,
            SERVICE_SYNC_ENTITY,
            SERVICE_GET_STATUS,
            SERVICE_DISPATCH,
        ):
            hass.services.async_remove(DOMAIN, service_name)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: NativeGroupsConfigEntry
) -> None:
    """Handle options update."""
    # Reload the integration to apply new options
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Register native_groups services."""

    def _get_orchestrator() -> NativeGroupOrchestrator:
        """Get the orchestrator from config entries."""
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_loaded",
            )
        entry: NativeGroupsConfigEntry = entries[0]
        return entry.runtime_data

    async def handle_sync_all(call: ServiceCall) -> None:
        """Force re-sync of all native groups."""
        orchestrator = _get_orchestrator()
        await orchestrator.async_sync_all()

    async def handle_sync_entity(call: ServiceCall) -> None:
        """Sync a specific HA group/scene."""
        orchestrator = _get_orchestrator()
        entity_id = call.data["entity_id"]
        await orchestrator.async_sync_entity(entity_id)

    async def handle_get_status(call: ServiceCall) -> ServiceResponse:
        """Get sync status for an entity."""
        orchestrator = _get_orchestrator()
        entity_id = call.data["entity_id"]
        mapping = orchestrator.get_mapping(entity_id)

        if not mapping:
            return {
                "entity_id": entity_id,
                "managed": False,
                "native_groups": {},
                "native_scenes": {},
                "ungrouped_entities": [],
            }

        return _mapping_to_response(entity_id, mapping)

    async def handle_dispatch(call: ServiceCall) -> None:
        """Dispatch a service call through native groups.

        This is the main service interception point. Users can call this
        instead of the original service to use native group commands.
        """
        orchestrator = _get_orchestrator()
        domain = call.data["domain"]
        service = call.data["service"]
        target = call.data.get("target", {})
        data = call.data.get("data", {})

        # Build the service data with target
        service_data: dict[str, Any] = {**data}
        if "entity_id" in target:
            service_data["entity_id"] = target["entity_id"]
        if "area_id" in target:
            service_data["area_id"] = target["area_id"]
        if "floor_id" in target:
            service_data["floor_id"] = target["floor_id"]
        if "label_id" in target:
            service_data["label_id"] = target["label_id"]

        # Try to dispatch via native groups
        handled = await orchestrator.async_dispatch(domain, service, service_data)

        if not handled:
            # Fall back to standard service call
            await hass.services.async_call(domain, service, service_data)

    # Only register if not already registered
    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_ALL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_ALL,
            handle_sync_all,
            schema=vol.Schema({}),
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_ENTITY,
            handle_sync_entity,
            schema=SERVICE_SYNC_ENTITY_SCHEMA,
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_STATUS,
            handle_get_status,
            schema=SERVICE_GET_STATUS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_DISPATCH,
            handle_dispatch,
            schema=SERVICE_DISPATCH_SCHEMA,
        )


@callback
def _mapping_to_response(entity_id: str, mapping: GroupMapping) -> dict[str, Any]:
    """Convert a GroupMapping to a service response."""
    return {
        "entity_id": entity_id,
        "managed": True,
        "type": mapping.ha_entity_type,
        "native_groups": {
            protocol: {
                "group_id": ref.group_id,
                "group_name": ref.group_name,
                "members": ref.member_entity_ids,
            }
            for protocol, ref in mapping.native_groups.items()
        },
        "native_scenes": {
            protocol: {
                "scene_id": ref.scene_id,
                "group_name": ref.group_name,
                "members": ref.member_entity_ids,
            }
            for protocol, ref in mapping.native_scenes.items()
        },
        "ungrouped_entities": mapping.ungrouped_entities,
        "last_synced": mapping.last_synced,
        "sync_error": mapping.sync_error,
    }
