"""Constants for Native Group Orchestration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "native_groups"

# Supported protocols
PROTOCOL_ZWAVE_JS: Final = "zwave_js"
PROTOCOL_ZIGBEE2MQTT: Final = "zigbee2mqtt"
PROTOCOL_ZHA: Final = "zha"
PROTOCOL_UNKNOWN: Final = "unknown"

# Config entry options
CONF_ENABLED_PROTOCOLS: Final = "enabled_protocols"
CONF_ENABLE_GROUPS: Final = "enable_groups"
CONF_ENABLE_SCENES: Final = "enable_scenes"
CONF_ENABLE_AREAS: Final = "enable_areas"
CONF_ENABLE_FLOORS: Final = "enable_floors"
CONF_ENABLE_LABELS: Final = "enable_labels"

# Reconciliation
RECONCILE_INTERVAL: Final = 300  # 5 minutes

# HA grouping types
GROUPING_TYPE_GROUP: Final = "group"
GROUPING_TYPE_SCENE: Final = "scene"
GROUPING_TYPE_AREA: Final = "area"
GROUPING_TYPE_FLOOR: Final = "floor"
GROUPING_TYPE_LABEL: Final = "label"

# Event types
EVENT_MEMBERSHIP_CHANGED: Final = "native_groups_membership_changed"
EVENT_SCENE_CHANGED: Final = "native_groups_scene_changed"
EVENT_SYNC_COMPLETE: Final = "native_groups_sync_complete"
EVENT_SYNC_ERROR: Final = "native_groups_sync_error"

# Storage
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.mappings"

# Defaults
DEFAULT_SCENE_STORE_DELAY: Final = 0.5  # seconds to wait before storing scene
DEFAULT_SYNC_DEBOUNCE: Final = 1.0  # seconds to debounce rapid changes

# Zigbee2MQTT defaults
Z2M_BASE_TOPIC: Final = "zigbee2mqtt"

# Scene ID allocation
SCENE_ID_START: Final = 100  # Reserve 1-99 for user-defined scenes
SCENE_ID_MAX: Final = 255

# Z-Wave Command Classes
CC_BINARY_SWITCH: Final = 37
CC_MULTILEVEL_SWITCH: Final = 38
CC_SCENE_ACTIVATION: Final = 43
CC_SCENE_ACTUATOR_CONFIGURATION: Final = 44
CC_COLOR_SWITCH: Final = 51

# Z-Wave device capabilities (for grouping)
ZWAVE_CAP_BINARY: Final = "binary"  # On/off only (Binary Switch CC)
ZWAVE_CAP_DIMMER: Final = "dimmer"  # Supports brightness (Multilevel Switch CC)
ZWAVE_CAP_COLOR: Final = "color"  # Supports color (Color Switch CC)

# Domains that support Z-Wave multicast grouping
# Other domains (climate, lock, fan) use different CCs and fall through to unicast
ZWAVE_GROUPABLE_DOMAINS: Final = frozenset({"light", "switch", "cover"})

