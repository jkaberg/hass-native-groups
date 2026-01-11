"""Base class for protocol handlers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class ProtocolHandler(ABC):
    """Abstract base class for protocol-specific handlers."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize handler."""
        self.hass = hass

    @property
    @abstractmethod
    def protocol_id(self) -> str:
        """Return protocol identifier."""

    @abstractmethod
    async def async_is_available(self) -> bool:
        """Check if this protocol's integration is loaded and available."""

    async def async_cleanup(self) -> None:
        """Clean up handler resources on shutdown.

        Override in subclasses if cleanup is needed.
        """

    # ─────────────────────────────────────────────────────────────
    # GROUP MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def async_create_group(
        self,
        name: str,
        member_native_ids: list[Any],
    ) -> str | int:
        """Create a native protocol group.

        Returns the native group identifier.
        """

    @abstractmethod
    async def async_delete_group(self, group_id: str | int) -> None:
        """Delete a native protocol group."""

    @abstractmethod
    async def async_update_group_members(
        self,
        group_id: str | int,
        add_members: list[Any] | None = None,
        remove_members: list[Any] | None = None,
    ) -> None:
        """Update membership of an existing native group."""

    @abstractmethod
    async def async_group_exists(self, group_id: str | int) -> bool:
        """Check if a native group exists."""

    async def async_get_groups(self) -> dict[str | int, dict[str, Any]]:
        """Get all groups from the protocol.

        Returns dict mapping group_id to group info (name, members, etc.).
        Used for reconciliation. Override in subclasses.
        """
        return {}

    # ─────────────────────────────────────────────────────────────
    # SCENE MANAGEMENT
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def async_supports_native_scenes(self) -> bool:
        """Check if this protocol supports per-device scene storage."""

    @abstractmethod
    async def async_store_scene(
        self,
        group_id: str | int,
        scene_id: int,
        device_states: dict[Any, dict[str, Any]],  # native_id → state dict
    ) -> None:
        """Store a scene with per-device states.

        Devices store their target state locally for fast recall.
        """

    @abstractmethod
    async def async_recall_scene(
        self,
        group_id: str | int,
        scene_id: int,
    ) -> None:
        """Recall a stored scene (single multicast command)."""

    @abstractmethod
    async def async_remove_scene(
        self,
        group_id: str | int,
        scene_id: int,
    ) -> None:
        """Remove a stored scene from devices."""

    # ─────────────────────────────────────────────────────────────
    # COMMAND DISPATCH
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def async_send_group_command(
        self,
        group_id: str | int,
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Send a command to all devices in a native group."""

    @abstractmethod
    async def async_send_multicast(
        self,
        native_ids: list[Any],
        domain: str,
        service: str,
        service_data: dict[str, Any],
    ) -> None:
        """Send ad-hoc multicast to specific devices (no pre-created group)."""

    # ─────────────────────────────────────────────────────────────
    # ENTITY RESOLUTION
    # ─────────────────────────────────────────────────────────────

    @abstractmethod
    def get_native_id(self, entity_id: str) -> Any | None:
        """Extract native protocol ID from an HA entity.

        Returns None if entity doesn't belong to this protocol.
        """

    @abstractmethod
    def convert_service_data(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert HA service data to protocol-specific format."""
