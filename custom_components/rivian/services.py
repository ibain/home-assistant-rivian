"""Rivian actions."""

from __future__ import annotations

import asyncio
import logging

from rivian import Rivian, VehicleCommand
from rivian.exceptions import RivianApiException
import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .api_ext import async_end_trip, async_share_location
from .const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
)
from .coordinator import UserCoordinator, VehicleCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_DESTINATION = "send_destination"
SERVICE_END_TRIP = "end_trip"
ATTR_LOCATION = "location"
ATTR_WAKE = "wake"
ATTR_REPLACE = "replace"
WAKE_TIMEOUT = 30

DEVICE_SCHEMA = {vol.Required(ATTR_DEVICE_ID): cv.string}
SEND_DESTINATION_SCHEMA = vol.Schema(
    DEVICE_SCHEMA
    | {
        vol.Required(ATTR_LOCATION): vol.All(cv.string, str.strip, vol.Length(min=1)),
        vol.Optional(ATTR_WAKE, default=True): cv.boolean,
        vol.Optional(ATTR_REPLACE, default=True): cv.boolean,
    }
)
END_TRIP_SCHEMA = vol.Schema(DEVICE_SCHEMA)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Rivian actions."""

    async def async_send_destination(call: ServiceCall) -> ServiceResponse:
        coordinator, api = _get_vehicle(hass, call.data[ATTR_DEVICE_ID])
        if call.data[ATTR_WAKE]:
            await _async_wake(hass, coordinator)
        if call.data[ATTR_REPLACE]:
            # Otherwise the destination is added as a stop ahead of any open trip.
            await async_end_trip(
                api, coordinator.vehicle_id, _user_id(hass, coordinator)
            )
        result = await async_share_location(
            api, coordinator.vehicle_id, call.data[ATTR_LOCATION], call.data[ATTR_WAKE]
        )
        return {"result": result}

    async def async_end_trip_service(call: ServiceCall) -> ServiceResponse:
        coordinator, api = _get_vehicle(hass, call.data[ATTR_DEVICE_ID])
        return await async_end_trip(
            api, coordinator.vehicle_id, _user_id(hass, coordinator)
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_DESTINATION,
        async_send_destination,
        schema=SEND_DESTINATION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_END_TRIP,
        async_end_trip_service,
        schema=END_TRIP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _get_vehicle(
    hass: HomeAssistant, device_id: str
) -> tuple[VehicleCoordinator, Rivian]:
    """Return the vehicle coordinator and API client for a device."""
    if (device := dr.async_get(hass).async_get(device_id)) and (
        entry_data := hass.data.get(DOMAIN, {}).get(device.config_entry_id)
    ):
        coordinators = entry_data[ATTR_COORDINATOR][ATTR_VEHICLE]
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier in coordinators:
                return coordinators[identifier], entry_data[ATTR_API]
    raise ServiceValidationError(f"Device {device_id} is not a loaded Rivian vehicle")


def _user_id(hass: HomeAssistant, coordinator: VehicleCoordinator) -> str | None:
    """Return the Rivian user id for the vehicle's config entry."""
    entry_data = hass.data[DOMAIN][coordinator.config_entry.entry_id]
    user: UserCoordinator = entry_data[ATTR_COORDINATOR][ATTR_USER]
    return (user.data or {}).get("id")


async def _async_wake(hass: HomeAssistant, coordinator: VehicleCoordinator) -> None:
    """Wake a sleeping vehicle if vehicle control is set up; otherwise skip."""
    if coordinator.get("powerState") != "sleep":
        return
    entry = coordinator.config_entry
    vehicle = hass.data[DOMAIN][entry.entry_id][ATTR_VEHICLE][coordinator.vehicle_id]
    if not (
        entry.options.get(CONF_VEHICLE_CONTROL) and vehicle.get("phone_identity_id")
    ):
        _LOGGER.debug("Vehicle control not set up; sending without waking")
        return
    try:
        await coordinator.send_vehicle_command(VehicleCommand.WAKE_VEHICLE)
        await asyncio.wait_for(coordinator._awake.wait(), WAKE_TIMEOUT)
    except RivianApiException as err:
        _LOGGER.warning(
            "Could not wake vehicle, sending anyway: %s", type(err).__name__
        )
    except TimeoutError:
        _LOGGER.debug("Vehicle did not wake within %ss, sending anyway", WAKE_TIMEOUT)
