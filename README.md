# Native Group Orchestration

The Native Group Orchestration integration automatically provisions and manages native protocol groups for Home Assistant groupings. This eliminates the "popcorn effect" where devices turn on sequentially, reduces network congestion, and provides faster response times by leveraging protocol-native multicast and group commands.

## Installation

### HACS (recommended)

1. Open HACS in your Home Assistant instance
2. Click the three dots in the top right corner and select **Custom repositories**
3. Add this repository URL and select **Integration** as the category
4. Click **Add**, then search for "Native Group Orchestration"
5. Click **Download**
6. Restart Home Assistant
7. Go to **Settings** → **Devices & Services** → **Add Integration** and search for "Native Group Orchestration"

### Manual

1. Download the contents of this repository
2. Copy the files to `config/custom_components/native_groups/`
3. Restart Home Assistant
4. Go to **Settings** → **Devices & Services** → **Add Integration** and search for "Native Group Orchestration"

## Usage

### Initial setup

When adding the integration, you'll be asked to configure:

- **Enabled protocols**: Select which protocols to manage (Z-Wave JS, Zigbee2MQTT, ZHA). Only protocols you have installed will be shown.
- **Enable for groups/scenes/areas/floors/labels**: Choose which Home Assistant grouping mechanisms should create native groups.

### How to use it

Once configured, **the integration works automatically**. You don't need to change how you use Home Assistant:

1. **Create groups as usual** — Use the Helpers UI, `group.set`, or YAML to create groups
2. **Create scenes as usual** — Use the scene editor or YAML
3. **Organize with areas/floors/labels** — Assign devices and entities normally

The integration monitors these changes and automatically creates the corresponding native protocol groups in the background.

### Triggering native group commands

To send commands through native groups, use the `native_groups.dispatch` service:

```yaml
service: native_groups.dispatch
data:
  domain: light
  service: turn_on
  target:
    area_id: living_room
  data:
    brightness: 255
```

This sends a single multicast command to all lights in the living room instead of individual commands to each device.

### Verifying it works

1. **Check diagnostics**: Go to **Settings** → **Devices & Services** → **Native Group Orchestration** → **three dots** → **Download diagnostics**
2. **Use the status service**: Call `native_groups.get_status` with an entity_id to see its native group mappings
3. **Watch for the difference**: When controlling groups, all devices should respond simultaneously instead of one-by-one

### Troubleshooting sync issues

If groups get out of sync:

- Call `native_groups.sync_all` to force a full re-sync
- Call `native_groups.sync_entity` with a specific entity_id to sync just that grouping

## Overview

When you create a group, scene, area, floor, or label in Home Assistant containing smart home devices, this integration automatically creates corresponding native groups in the underlying protocols (Z-Wave JS, Zigbee2MQTT, ZHA). Service calls targeting these groupings are then routed through efficient native commands instead of individual unicast messages.

## Supported protocols

| Protocol | Group support | Scene support | Status |
|----------|--------------|---------------|--------|
| Z-Wave JS | Multicast commands | Scene Actuator Configuration CC | Full support |
| Zigbee2MQTT | Native Zigbee groups | Native Zigbee scenes | Full support |
| ZHA | Native Zigbee groups | Native Zigbee Scenes cluster | Full support |

## Supported grouping mechanisms

The integration monitors and provisions native groups for:

- **Groups**: Home Assistant group entities (`group.*`)
- **Scenes**: Home Assistant scene entities (`scene.*`)
- **Areas**: Entities and devices assigned to areas
- **Floors**: All entities in areas belonging to a floor
- **Labels**: Entities and devices with specific labels

## How it works

### Architecture

The integration consists of several components:

1. **Orchestrator**: The central coordinator that manages lifecycle events, provisioning, and service dispatch
2. **Entity Classifier**: Identifies the protocol and capabilities of each entity
3. **Protocol Handlers**: Protocol-specific implementations for group management and command dispatch
4. **Mapping Store**: Persistent storage of group mappings across restarts

### Provisioning flow

When a grouping is created or modified:

1. The orchestrator detects the change via event listeners
2. Member entities are classified by protocol (Z-Wave JS, Zigbee2MQTT, ZHA, or unknown)
3. For each protocol with members, a native group is created:
   - Z-Wave JS: Entities are grouped by capability for multicast
   - Zigbee2MQTT: A native Zigbee group is created via MQTT
   - ZHA: A native Zigbee group is created via ZHA services
4. The mapping is persisted to storage

### Service dispatch

When a service call targets a managed grouping:

1. The orchestrator intercepts the call
2. For each protocol with a native group, the appropriate handler sends the command:
   - Z-Wave JS: Multicast command to all nodes
   - Zigbee2MQTT: Group command via MQTT
   - ZHA: Group command via ZHA services
3. Entities that could not be grouped receive individual service calls (unicast fallback)

### Mixed protocol handling

Groups containing devices from multiple protocols are handled transparently:

- Each protocol receives its own native group
- Commands are dispatched in parallel to all protocol groups
- Unknown protocol devices receive individual service calls

## Z-Wave JS capability-based grouping

Z-Wave multicast is limited to a single Command Class per transmission. Different device types require different Command Classes:

| Capability | Command Class | Devices |
|------------|---------------|---------|
| Binary | Binary Switch CC (0x25) | On/off switches |
| Dimmer | Multilevel Switch CC (0x26) | Dimmable lights, covers with position |
| Color | Color Switch CC (0x33) | Color-capable lights |

The integration automatically detects device capabilities and creates sub-groups for each capability. When a command is dispatched:

1. Color devices receive the appropriate color or brightness command
2. Dimmer devices receive brightness commands via Multilevel Switch CC
3. Binary devices receive on/off commands via Binary Switch CC

All commands are sent in parallel for simultaneous response.

### Groupable domains

Only certain entity domains support Z-Wave multicast grouping:

| Domain | Groupable | Notes |
|--------|-----------|-------|
| `light` | Yes | Capability detected from color modes |
| `switch` | Yes | Always binary capability |
| `cover` | Yes | Dimmer if position supported, otherwise binary |
| `climate` | No | Uses Thermostat CCs, unicast fallback |
| `lock` | No | Uses Door Lock CC, unicast fallback |
| `fan` | No | Uses different CCs, unicast fallback |

Non-groupable entities are tracked separately and receive standard Home Assistant service calls.

## Zigbee group handling

### Zigbee2MQTT

The integration creates native Zigbee groups through the Zigbee2MQTT MQTT API:

- Groups are created with friendly names derived from the Home Assistant grouping
- Device IEEE addresses are added as group members
- Group commands use the `set` topic for the group
- Scenes are stored and recalled using Zigbee scene commands

### ZHA

The integration uses ZHA services for group management:

- Groups are created via `zha.create_group`
- Members are managed via `zha.add_group_member` and `zha.remove_group_member`
- Commands are sent to the group entity

## Scenes

For scene entities, the integration provisions native protocol scenes where supported:

1. When a scene is created, the target states are stored in device memory
2. When the scene is activated, native scene recall commands are used
3. Scene IDs are allocated from a reserved range (100-255)

This provides instant scene recall without requiring individual commands to each device.

## Services

The integration provides the following services:

### `native_groups.sync_all`

Synchronizes all groupings with their native protocol groups. Use this after significant changes or to recover from sync issues.

### `native_groups.sync_entity`

Synchronizes a specific entity or grouping.

| Parameter | Description |
|-----------|-------------|
| `entity_id` | The entity ID to synchronize |

### `native_groups.get_status`

Returns the current status of native group mappings for diagnostics.

## Event-driven synchronization

The integration listens to the following events for automatic synchronization:

- `state_changed`: Detects group and scene entity changes
- `area_registry_updated`: Detects area changes
- `floor_registry_updated`: Detects floor changes
- `label_registry_updated`: Detects label changes
- `entity_registry_updated`: Detects entity assignment changes
- `device_registry_updated`: Detects device assignment changes

Changes are debounced to prevent excessive sync operations during rapid modifications.

## Storage

Group mappings are persisted to `.storage/native_groups.mappings` and restored on Home Assistant startup. This ensures native groups remain synchronized across restarts.

## Cleanup

When a grouping is deleted:

1. The orchestrator detects the removal
2. Native groups are deleted from each protocol
3. Scene configurations are removed from devices
4. The mapping is removed from storage

## Limitations

- Z-Wave multicast requires all target nodes to support the same Command Class for a given command type
- Native groups are managed by Home Assistant; manual changes to native groups may be overwritten

## Why Matter/Thread is not supported

Matter devices (including those using Thread as the network transport) are not currently supported for native group orchestration. While Matter includes a Groups cluster (0x0004) that supports multicast commands, the python-matter-server library used by Home Assistant does not yet expose APIs for creating groups or sending group-addressed commands. 

When python-matter-server adds support for the Groups cluster, Matter support can be added to this integration. Matter devices will continue to work normally through standard Home Assistant service calls.

## Debugging

Enable debug logging to troubleshoot issues:

```yaml
logger:
  default: info
  logs:
    homeassistant.components.native_groups: debug
```

Log messages include:

- Group provisioning and cleanup operations
- Capability detection and distribution
- Command dispatch routing
- Sync operations and errors

