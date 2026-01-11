"""Diagnostics support for Native Group Orchestration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import NativeGroupsConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: NativeGroupsConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    orchestrator = entry.runtime_data

    # Get handler status
    handlers_status: dict[str, dict[str, Any]] = {}
    for protocol, handler in orchestrator.handlers.items():
        try:
            groups = await handler.async_get_groups()
            handlers_status[protocol] = {
                "available": True,
                "group_count": len(groups),
                "groups": list(groups.keys()),
            }
        except Exception as err:
            handlers_status[protocol] = {
                "available": False,
                "error": str(err),
            }

    # Get mapping summary
    mappings_summary: dict[str, dict[str, Any]] = {}
    for entity_id, mapping in orchestrator.get_all_mappings().items():
        mappings_summary[entity_id] = {
            "type": mapping.ha_entity_type,
            "native_groups": list(mapping.native_groups.keys()),
            "native_scenes": list(mapping.native_scenes.keys()),
            "ungrouped_count": len(mapping.ungrouped_entities),
            "last_synced": mapping.last_synced,
            "sync_error": mapping.sync_error,
        }

    # Count by type
    type_counts: dict[str, int] = {}
    for mapping in orchestrator.get_all_mappings().values():
        type_counts[mapping.ha_entity_type] = (
            type_counts.get(mapping.ha_entity_type, 0) + 1
        )

    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "options": dict(entry.options),
        },
        "orchestrator": {
            "started": orchestrator.is_started,
            "enabled_protocols": orchestrator.enabled_protocols,
            "scene_id_counter": orchestrator.scene_id_counter,
            "pending_tasks": orchestrator.pending_task_count,
        },
        "handlers": handlers_status,
        "mappings": {
            "total_count": len(orchestrator.get_all_mappings()),
            "by_type": type_counts,
            "details": mappings_summary,
        },
        "managed_resources": {
            entity_id: list(resources)
            for entity_id, resources in orchestrator.managed_resources.items()
        },
    }
