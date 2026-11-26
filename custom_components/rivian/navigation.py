"""Navigation actions and the time-to-leave blueprint for calendar automations.

Kept apart from services.py, which follows upstream, and built on its helpers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import shutil

from rivian import VehicleCommand
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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .api_ext import _error_messages, async_find_place, async_plan_trip
from .const import CONF_VEHICLE_CONTROL, DOMAIN
from .coordinator import VehicleCoordinator
from .services import _async_get_controlled_vehicle, _position, _user_id

_LOGGER = logging.getLogger(__name__)

SERVICE_SEND_DESTINATION = "send_destination"
SERVICE_PLAN_TRIP = "plan_trip"
SERVICE_WAIT_FOR_PARK = "wait_for_park"
ATTR_LOCATION = "location"
ATTR_DEPARTURE_TIME = "departure_time"
ATTR_WAKE = "wake"
ATTR_REPLACE = "replace"
ATTR_WAIT_FOR_PARK = "wait_for_park"
ATTR_PARK_TIMEOUT = "park_timeout"
ATTR_NOT_AFTER = "not_after"
ATTR_AFTER_DRIVE = "after_drive"
ATTR_TIMEOUT = "timeout"
WAKE_TIMEOUT = 30
DEFAULT_PARK_TIMEOUT = timedelta(hours=2)

DEVICE_SCHEMA = {vol.Required(ATTR_DEVICE_ID): cv.string}
TEXT_SCHEMA = vol.All(cv.string, str.strip, vol.Length(min=1))
SEND_DESTINATION_SCHEMA = vol.Schema(
    DEVICE_SCHEMA
    | {
        vol.Required(ATTR_LOCATION): TEXT_SCHEMA,
        vol.Optional(ATTR_WAKE, default=True): cv.boolean,
        vol.Optional(ATTR_REPLACE, default=True): cv.boolean,
        vol.Optional(ATTR_WAIT_FOR_PARK, default=True): cv.boolean,
        vol.Optional(
            ATTR_PARK_TIMEOUT, default=DEFAULT_PARK_TIMEOUT
        ): cv.positive_time_period,
        vol.Optional(ATTR_NOT_AFTER): cv.datetime,
    }
)
PLAN_TRIP_SCHEMA = vol.Schema(
    DEVICE_SCHEMA
    | {
        vol.Required(ATTR_LOCATION): TEXT_SCHEMA,
        vol.Optional(ATTR_DEPARTURE_TIME): cv.datetime,
    }
)
WAIT_FOR_PARK_SCHEMA = vol.Schema(
    DEVICE_SCHEMA
    | {
        vol.Optional(ATTR_AFTER_DRIVE, default=False): cv.boolean,
        vol.Optional(
            ATTR_TIMEOUT, default=DEFAULT_PARK_TIMEOUT
        ): cv.positive_time_period,
        vol.Optional(ATTR_NOT_AFTER): cv.datetime,
    }
)


@callback
def async_setup_navigation(hass: HomeAssistant) -> None:
    """Register the navigation actions and install the bundled blueprints."""
    _async_setup_services(hass)

    async def _install_blueprints(hass: HomeAssistant) -> None:
        target = Path(hass.config.path("blueprints", "automation", DOMAIN))
        if await hass.async_add_executor_job(install_blueprints, target):
            # Automations load first, so ones built on an updated blueprint were
            # validated against the old copy; load them again.
            await hass.services.async_call("automation", "reload")

    async_at_started(hass, _install_blueprints)


def install_blueprints(target: Path) -> bool:
    """Copy the bundled automation blueprints into the config's blueprint folder.

    Overwrites earlier copies so updates reach users and removes blueprints no
    longer shipped; a customized copy should live under a different folder name.
    Returns True if anything was written or removed.
    """
    source = Path(__file__).parent / "blueprints"
    changed = False
    try:
        target.mkdir(parents=True, exist_ok=True)
        bundled = {blueprint.name for blueprint in source.glob("*.yaml")}
        for stale in target.glob("*.yaml"):
            if stale.name not in bundled:
                stale.unlink()
                changed = True
        for name in bundled:
            blueprint, dest = source / name, target / name
            if not dest.exists() or dest.read_bytes() != blueprint.read_bytes():
                shutil.copyfile(blueprint, dest)
                changed = True
    except OSError as err:
        _LOGGER.warning("Could not install blueprints into %s: %s", target, err)
    return changed


@callback
def _async_setup_services(hass: HomeAssistant) -> None:
    """Register the navigation actions."""
    navigation_locks: dict[str, asyncio.Lock] = {}

    async def send_destination(call: ServiceCall) -> ServiceResponse:
        coordinator = await _async_get_controlled_vehicle(hass, call)
        not_after = call.data.get(ATTR_NOT_AFTER)
        if _is_past(not_after):
            return {"result": None, "sent": False}
        if call.data[ATTR_WAIT_FOR_PARK]:
            # Never replace the trip being driven; the wait happens outside the
            # lock so other sends aren't held up by it.
            timeout = call.data[ATTR_PARK_TIMEOUT]
            parked = await _async_wait_for_gear(
                coordinator, True, _wait_budget(timeout, not_after)
            )
            if _is_past(not_after):
                return {"result": None, "sent": False}
            if not parked:
                raise HomeAssistantError(
                    f"Vehicle did not shift into Park within {timeout}"
                )
        api = coordinator.api
        async with navigation_locks.setdefault(coordinator.vehicle_id, asyncio.Lock()):
            try:
                if call.data[ATTR_WAKE]:
                    await _async_wake(coordinator)
                # A destination sent during a trip is added as its first stop,
                # so end the trip first. Kept with the send when automations overlap.
                if call.data[ATTR_REPLACE] and not await api.end_trip(
                    coordinator.vehicle_id, _user_id(coordinator)
                ):
                    raise HomeAssistantError("Rivian could not clear the current trip")
                response = await api.send_location_to_vehicle(
                    coordinator.vehicle_id, call.data[ATTR_LOCATION]
                )
                data = (await response.json()).get("data") or {}
            except RivianApiException as err:
                # str(err) would include the request headers (session tokens)
                raise HomeAssistantError(
                    f"Rivian rejected the destination: {_error_messages(err)}"
                ) from None
        share = data.get("parseAndShareLocationToVehicle") or {}
        result = (share.get("publishResponse") or {}).get("result")
        _LOGGER.debug(
            "Destination sent to %s, result %s", coordinator.vehicle_id, result
        )
        return {"result": result, "sent": True}

    async def plan_trip(call: ServiceCall) -> ServiceResponse:
        coordinator = await _async_get_controlled_vehicle(hass, call)
        origin = _position(coordinator)
        if coords := _parse_coordinates(call.data[ATTR_LOCATION]):
            destination = {"name": None, "address": None}
            destination["latitude"], destination["longitude"] = coords
        elif not (
            destination := await async_find_place(
                coordinator.api, call.data[ATTR_LOCATION], origin
            )
        ):
            raise ServiceValidationError(
                f"No place found for {call.data[ATTR_LOCATION]!r}"
            )
        departure = call.data.get(ATTR_DEPARTURE_TIME)
        plan = await async_plan_trip(
            coordinator.api,
            coordinator.vehicle_id,
            origin,
            (destination["latitude"], destination["longitude"]),
            coordinator.get("batteryLevel"),
            int(departure.timestamp()) if departure else None,
        )
        return {"destination": destination, **plan}

    async def wait_for_park(call: ServiceCall) -> ServiceResponse:
        coordinator = await _async_get_controlled_vehicle(hass, call)
        not_after = call.data.get(ATTR_NOT_AFTER)
        deadline = dt_util.utcnow() + _wait_budget(call.data[ATTR_TIMEOUT], not_after)
        parked = True
        if call.data[ATTR_AFTER_DRIVE]:
            parked = await _async_wait_for_gear(
                coordinator, False, deadline - dt_util.utcnow()
            )
        if parked:
            parked = await _async_wait_for_gear(
                coordinator, True, deadline - dt_util.utcnow()
            )
        return {
            "parked": parked and not _is_past(not_after),
            "trip_loaded": bool(coordinator.get("waypoints")),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_DESTINATION,
        send_destination,
        schema=SEND_DESTINATION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAN_TRIP,
        plan_trip,
        schema=PLAN_TRIP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WAIT_FOR_PARK,
        wait_for_park,
        schema=WAIT_FOR_PARK_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def _is_past(moment: datetime | None) -> bool:
    """Return True if `moment` is set and already behind us."""
    return moment is not None and dt_util.as_utc(moment) <= dt_util.utcnow()


def _wait_budget(timeout: timedelta, not_after: datetime | None) -> timedelta:
    """Return how long to wait: `timeout`, cut short at `not_after`."""
    if not_after is None:
        return timeout
    return max(min(timeout, dt_util.as_utc(not_after) - dt_util.utcnow()), timedelta(0))


async def _async_wait_for_gear(
    coordinator: VehicleCoordinator, parked: bool, timeout: timedelta
) -> bool:
    """Wait until the vehicle is in Park (or out of it, with parked=False).

    Returns False if that didn't happen within `timeout`.
    """
    reached = asyncio.Event()

    @callback
    def _check_gear() -> None:
        if (coordinator.get("gearStatus") == "park") == parked:
            reached.set()

    _check_gear()
    if reached.is_set():
        return True
    unsubscribe = coordinator.async_add_listener(_check_gear)
    try:
        await asyncio.wait_for(reached.wait(), timeout.total_seconds())
    except TimeoutError:
        return False
    finally:
        unsubscribe()
    return True


def _parse_coordinates(text: str) -> tuple[float, float] | None:
    """Return (lat, lon) if text is a bare 'lat,lon' pair."""
    parts = text.split(",")
    if len(parts) != 2:
        return None
    try:
        lat, lon = (float(p) for p in parts)
    except ValueError:
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


async def _async_wake(coordinator: VehicleCoordinator) -> None:
    """Wake a sleeping vehicle if vehicle control is set up; otherwise skip.

    Rivian's own wake hint on the send doesn't reliably wake a sleeping vehicle;
    it then gets the destination when it next wakes.
    """
    if coordinator.get("powerState") != "sleep":
        return
    entry = coordinator.config_entry
    vehicle = entry.runtime_data.vehicles[coordinator.vehicle_id]
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
