"""Data structures for native group mappings."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import Any


@dataclass
class ProtocolInfo:
    """Information about an entity's protocol."""

    protocol: str  # "zwave_js", "zigbee2mqtt", "zha", "unknown"
    native_id: Any  # Node ID (int), IEEE address (str), etc.
    entity_id: str

    # Protocol-specific fields
    node_id: int | None = None  # Z-Wave
    ieee_address: str | None = None  # Zigbee
    endpoint: int = 0
    friendly_name: str | None = None

    # Device capabilities (for Z-Wave capability-based grouping)
    # "color" > "dimmer" > "binary" (in order of capability)
    capability: str | None = None  # "binary", "dimmer", "color"


@dataclass
class CommandProfile:
    """A unique command configuration.

    Entities with the same CommandProfile can be batched together.
    """

    domain: str  # "light", "switch", "cover"
    service: str  # "turn_on", "turn_off"
    service_data: dict[str, Any]

    _signature: str = field(init=False, default="")

    def __post_init__(self) -> None:
        """Generate deterministic signature."""
        data_str = json.dumps(self.service_data, sort_keys=True)
        self._signature = hashlib.md5(
            f"{self.domain}:{self.service}:{data_str}".encode()
        ).hexdigest()[:12]

    @property
    def signature(self) -> str:
        """Return command signature for grouping."""
        return self._signature


@dataclass
class NativeGroupRef:
    """Reference to a native protocol group."""

    protocol: str
    group_id: str | int  # Native group identifier
    group_name: str  # Human-readable name
    member_entity_ids: list[str]  # HA entity IDs
    member_native_ids: list[Any]  # Protocol-specific IDs


@dataclass
class NativeSceneRef:
    """Reference to a native protocol scene."""

    protocol: str
    group_name: str  # Group the scene belongs to
    scene_id: int  # Native scene ID (1-255)
    member_entity_ids: list[str]


@dataclass
class CommandBatch:
    """A batch of entities that receive the same command.

    Used when native scenes aren't available.
    """

    command_profile: CommandProfile
    native_groups: dict[str, NativeGroupRef] = field(default_factory=dict)
    ungrouped_entities: list[str] = field(default_factory=list)


@dataclass
class GroupMapping:
    """Complete mapping for an HA group/scene/label to native resources."""

    ha_entity_id: str  # "group.living_room", "scene.movie_night"
    ha_entity_type: str  # "group", "scene", "label", "area"

    # For simple groups: one native group per protocol
    native_groups: dict[str, NativeGroupRef] = field(default_factory=dict)

    # For scenes: native scene references (per-device state storage)
    native_scenes: dict[str, NativeSceneRef] = field(default_factory=dict)

    # For scenes without native scene support: command batches
    command_batches: list[CommandBatch] = field(default_factory=list)

    # Entities that couldn't be mapped to any protocol
    ungrouped_entities: list[str] = field(default_factory=list)

    # Metadata
    last_synced: float = field(default_factory=time.time)
    sync_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for storage."""
        return {
            "ha_entity_id": self.ha_entity_id,
            "ha_entity_type": self.ha_entity_type,
            "native_groups": {
                k: {
                    "protocol": v.protocol,
                    "group_id": v.group_id,
                    "group_name": v.group_name,
                    "member_entity_ids": v.member_entity_ids,
                    "member_native_ids": v.member_native_ids,
                }
                for k, v in self.native_groups.items()
            },
            "native_scenes": {
                k: {
                    "protocol": v.protocol,
                    "group_name": v.group_name,
                    "scene_id": v.scene_id,
                    "member_entity_ids": v.member_entity_ids,
                }
                for k, v in self.native_scenes.items()
            },
            "ungrouped_entities": self.ungrouped_entities,
            "last_synced": self.last_synced,
            "sync_error": self.sync_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GroupMapping:
        """Deserialize from dictionary."""
        mapping = cls(
            ha_entity_id=data["ha_entity_id"],
            ha_entity_type=data["ha_entity_type"],
            ungrouped_entities=data.get("ungrouped_entities", []),
            last_synced=data.get("last_synced", 0),
            sync_error=data.get("sync_error"),
        )

        for k, v in data.get("native_groups", {}).items():
            mapping.native_groups[k] = NativeGroupRef(**v)

        for k, v in data.get("native_scenes", {}).items():
            mapping.native_scenes[k] = NativeSceneRef(**v)

        return mapping

