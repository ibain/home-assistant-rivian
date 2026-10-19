"""Support for Rivian lock entities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN, LOCK_STATE_ENTITIES
from .coordinator import VehicleCoordinator
from .data_classes import RivianLockEntityDescription
from .entity import RivianVehicleControlEntity

_LOGGER = logging.getLogger(__name__)


LOCKS: Final[tuple[RivianLockEntityDescription, ...]] = (
    RivianLockEntityDescription(
        key="closures",
        name="Closures",
        is_locked=lambda coordinator: (
            not any(coordinator.get(key) == "unlocked" for key in LOCK_STATE_ENTITIES)
        ),
        lock=lambda coordinator: coordinator.send_vehicle_command(
            command=VehicleCommand.LOCK_ALL_CLOSURES_FEEDBACK
        ),
        unlock=lambda coordinator: coordinator.send_vehicle_command(
            command=VehicleCommand.UNLOCK_ALL_CLOSURES
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the lock entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities = [
        RivianLockEntity(coordinators[vehicle_id], entry, description, vehicle)
        for vehicle_id, vehicle in vehicles.items()
        if vehicle.get("phone_identity_id")
        for description in LOCKS
    ]
    async_add_entities(entities)


class RivianLockEntity(RivianVehicleControlEntity, LockEntity):
    """Representation of a Rivian lock entity."""

    entity_description: RivianLockEntityDescription

    @property
    def is_locked(self) -> bool:
        """Return true if the lock is locked."""
        return self.entity_description.is_locked(self.coordinator)

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        self._attr_is_locking = True
        await self._async_command(self.entity_description.lock)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        self._attr_is_unlocking = True
        await self._async_command(self.entity_description.unlock)

    async def _async_command(
        self, command: Callable[[VehicleCoordinator], Awaitable[None]]
    ) -> None:
        """Run a command, showing the lock as locking or unlocking until it is done.

        The vehicle can take a while to wake and do it.
        """
        self.async_write_ha_state()
        try:
            await command(self.coordinator)
        finally:
            self._attr_is_locking = self._attr_is_unlocking = False
            self.async_write_ha_state()
