"""Tests for the send_destination, plan_trip and wait_for_park actions."""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from rivian import Rivian, VehicleCommand
from rivian.exceptions import RivianApiException

from custom_components.rivian.api_ext import async_find_place, async_plan_trip
from custom_components.rivian.const import CONF_VEHICLE_CONTROL, DOMAIN
from custom_components.rivian.navigation import (
    SERVICE_PLAN_TRIP,
    SERVICE_SEND_DESTINATION,
    SERVICE_WAIT_FOR_PARK,
    _async_setup_services,
)
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import Group
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.device_registry as dr
from homeassistant.util import dt as dt_util

VEHICLE_ID = "01-123"
USER_ID = "02-456"
VIN = "7FCTGAAA0NN000001"
LOCATION = "Costco Wholesale, 1051 W Burbank Blvd, Burbank, CA 91506"
SHARE = "parseAndShareLocationToVehicle"
SHARE_OK = {"data": {SHARE: {"publishResponse": {"result": 0}}}}
END_TRIP_OK = {"data": {"sendParallaxPayload": {"success": True}}}
PLACE = {
    "id": "ChIJ3",
    "name": "Costco Wholesale",
    "location": {
        "formattedAddress": "7095 Market Pl Dr #474c, Goleta, CA 93117, USA",
        "coordinate": {"latitude": 34.4280052, "longitude": -119.8747787},
        "navCoordinate": {"latitude": 34.4283714, "longitude": -119.8741895},
    },
}
PLACES_OK = {"data": {"places": {"results": [PLACE]}}}
PLACE_BY_URL_OK = {"data": {"placeByUrl": PLACE}}
PLAN_OK = {
    "data": {
        "planTrip2": {
            "status": "OK",
            "plans": [
                {
                    "waypoints": [
                        {
                            "waypointType": "OTHER",
                            "latitude": 34.18,
                            "longitude": -118.33,
                        },
                        {
                            "waypointType": "DC_CHARGE_STATION",
                            "latitude": 35.4,
                            "longitude": -119.5,
                            "charger": {
                                "name": "Buttonwillow, CA",
                                "chargeDurationSeconds": 900,
                            },
                        },
                        {
                            "waypointType": "OTHER",
                            "latitude": 37.77,
                            "longitude": -122.42,
                        },
                    ],
                    "summary": {
                        "destinationReachable": True,
                        "totalChargeDurationSeconds": 900,
                        "totalDriveDurationSeconds": 20000,
                        "totalDriveDistanceMeters": 600000,
                        "totalTripDurationSeconds": 20900,
                        "arrivalSOCPercent": 12.5,
                    },
                }
            ],
        }
    }
}


def _mock_api(*responses: tuple[dict[str, Any], int] | dict[str, Any]) -> Rivian:
    """Return a real client whose GraphQL helper returns the responses in turn."""
    mocks = []
    for item in responses:
        response_json, status = item if isinstance(item, tuple) else (item, 200)
        response = MagicMock(status=status)
        response.json = AsyncMock(return_value=response_json)
        mocks.append(response)
    api = Rivian(
        csrf_token="csrf", app_session_token="a-sess", user_session_token="u-sess"
    )
    api._Rivian__graphql_query = AsyncMock(side_effect=mocks)
    return api


def _operations(api: Rivian) -> list[str]:
    return [
        c.args[2]["operationName"] for c in api._Rivian__graphql_query.call_args_list
    ]


def _setup_vehicle(
    hass: HomeAssistant,
    power_state: str = "ready",
    control: bool = True,
    *responses: dict[str, Any],
) -> tuple[dr.DeviceEntry, MagicMock, Rivian]:
    """Register a vehicle device and its loaded entry data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_VEHICLE_CONTROL: control} if control else {},
        state=ConfigEntryState.LOADED,
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, VIN), (DOMAIN, VEHICLE_ID)},
    )
    api = _mock_api(*(responses or (END_TRIP_OK, SHARE_OK)))
    coordinator = MagicMock(vehicle_id=VEHICLE_ID, config_entry=entry, api=api)
    coordinator.state = {
        "powerState": power_state,
        "batteryLevel": 69.9,
        "gearStatus": "park",
    }
    coordinator.get = MagicMock(side_effect=coordinator.state.get)
    coordinator.listeners = []

    def add_listener(update: Any) -> Any:
        coordinator.listeners.append(update)
        return lambda: coordinator.listeners.remove(update)

    coordinator.async_add_listener = MagicMock(side_effect=add_listener)
    coordinator.data = {"gnssLocation": {"latitude": 34.181, "longitude": -118.327}}
    coordinator.send_vehicle_command = AsyncMock()
    coordinator._awake = asyncio.Event()
    coordinator._awake.set()
    coordinator.async_shutdown = AsyncMock()
    vehicle = {"vin": VIN} | ({"phone_identity_id": "identity-1"} if control else {})
    entry.runtime_data = MagicMock(
        vehicles={VEHICLE_ID: vehicle},
        vehicle_coordinators={VEHICLE_ID: coordinator},
        user_coordinator=MagicMock(data={"id": USER_ID}),
        api=api,
    )
    _async_setup_services(hass)
    return device, coordinator, api


async def _call(hass: HomeAssistant, service: str, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


def _set_gear(coordinator: MagicMock, gear: str) -> None:
    """Change the mocked vehicle's gear and notify coordinator listeners."""
    coordinator.state["gearStatus"] = gear
    for update in list(coordinator.listeners):
        update()


async def test_send_destination_replaces_trip(hass: HomeAssistant) -> None:
    """By default the current trip is ended before the destination is sent."""
    device, coordinator, api = _setup_vehicle(hass)

    result = await _call(
        hass, SERVICE_SEND_DESTINATION, device_id=device.id, location=LOCATION
    )

    assert result == {"result": 0, "sent": True}
    assert _operations(api) == ["SendUpdatedTrip", SHARE]
    end_trip, share = (c.args[2] for c in api._Rivian__graphql_query.call_args_list)
    assert end_trip["variables"]["meta"]["vehicleId"] == VEHICLE_ID
    assert share["variables"] == {
        "str": LOCATION,
        "vehicleId": VEHICLE_ID,
        "requiresWakeup": True,
    }
    coordinator.send_vehicle_command.assert_not_called()


async def test_send_destination_without_replace(hass: HomeAssistant) -> None:
    """replace: false only sends the destination."""
    device, _, api = _setup_vehicle(hass, "ready", True, SHARE_OK)

    await _call(
        hass,
        SERVICE_SEND_DESTINATION,
        device_id=device.id,
        location=LOCATION,
        replace=False,
    )

    assert _operations(api) == [SHARE]


async def test_send_destination_hides_tokens(hass: HomeAssistant) -> None:
    """A GraphQL error surfaces its message, never the session headers."""
    device, _, api = _setup_vehicle(hass, "ready", True, SHARE_OK)
    api._Rivian__graphql_query.side_effect = RivianApiException(
        "Error", {"errors": [{"message": "bad vehicle"}]}, {"U-Sess": "u-sess"}
    )

    with pytest.raises(HomeAssistantError) as err:
        await _call(
            hass,
            SERVICE_SEND_DESTINATION,
            device_id=device.id,
            location=LOCATION,
            replace=False,
        )
    assert "bad vehicle" in str(err.value)
    assert "u-sess" not in str(err.value)
    assert err.value.__cause__ is None


async def test_send_destination_not_a_vehicle(hass: HomeAssistant) -> None:
    """A Rivian device that isn't a loaded vehicle is rejected."""
    _, coordinator, _ = _setup_vehicle(hass)
    wallbox = dr.async_get(hass).async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "wallbox-1")},
    )

    with pytest.raises(ServiceValidationError):
        await _call(
            hass, SERVICE_SEND_DESTINATION, device_id=wallbox.id, location=LOCATION
        )


async def test_send_destination_wakes_sleeping_vehicle(hass: HomeAssistant) -> None:
    """A sleeping vehicle is woken first when vehicle control is set up."""
    device, coordinator, _ = _setup_vehicle(hass, power_state="sleep")

    await _call(hass, SERVICE_SEND_DESTINATION, device_id=device.id, location=LOCATION)

    coordinator.send_vehicle_command.assert_awaited_once_with(
        VehicleCommand.WAKE_VEHICLE
    )


@pytest.mark.parametrize(
    ("control", "wake"), [(False, True), (True, False)], ids=["no_control", "wake_off"]
)
async def test_send_destination_skips_wake(
    hass: HomeAssistant, control: bool, wake: bool
) -> None:
    """No wake command without vehicle control, or when wake is off; still sends."""
    device, coordinator, _ = _setup_vehicle(hass, "sleep", control)

    result = await _call(
        hass,
        SERVICE_SEND_DESTINATION,
        device_id=device.id,
        location=LOCATION,
        wake=wake,
    )

    assert result == {"result": 0, "sent": True}
    coordinator.send_vehicle_command.assert_not_called()


async def test_find_place_text_uses_places() -> None:
    """Free text goes to the client's place search, from the vehicle's position."""
    api = _mock_api(PLACES_OK)

    place = await async_find_place(api, "Costco Goleta", (34.181, -118.327))

    body = api._Rivian__graphql_query.call_args.args[2]
    assert body["operationName"] == "places"
    assert body["variables"]["params"]["query"] == "Costco Goleta"
    assert body["variables"]["params"]["userLocation"] == {
        "latitude": 34.181,
        "longitude": -118.327,
    }
    assert place == {
        "name": "Costco Wholesale",
        "address": "7095 Market Pl Dr #474c, Goleta, CA 93117, USA",
        "latitude": 34.4283714,
        "longitude": -119.8741895,
    }


async def test_find_place_link_uses_place_by_url() -> None:
    """A map share link goes to PlaceByUrl; no match returns None."""
    api = _mock_api(PLACE_BY_URL_OK, {"data": {"placeByUrl": None}})

    assert (await async_find_place(api, "https://maps.app.goo.gl/x", (0, 0)))["name"]
    assert api._Rivian__graphql_query.call_args.args[2]["operationName"] == "PlaceByUrl"
    assert await async_find_place(api, "https://maps.app.goo.gl/y", (0, 0)) is None


async def test_plan_trip_summary() -> None:
    """The plan's summary and charging stops come back in plain units."""
    api = _mock_api(PLAN_OK)

    plan = await async_plan_trip(
        api, VEHICLE_ID, (34.18, -118.33), (37.77, -122.42), 69.9, 1_760_000_000
    )

    body = api._Rivian__graphql_query.call_args.args[2]
    assert body["operationName"] == "planTripWithMultiStopV2"
    assert body["variables"] == {
        "waypoints": [
            {"latitude": 34.18, "longitude": -118.33},
            {"latitude": 37.77, "longitude": -122.42},
        ],
        "vehicle": VEHICLE_ID,
        "startingSoc": 69.9,
        "departureTime": 1_760_000_000,
    }
    assert plan == {
        "trip_seconds": 20900,
        "drive_seconds": 20000,
        "charge_seconds": 900,
        "distance_meters": 600000,
        "arrival_soc": 12.5,
        "destination_reachable": True,
        "charging_stops": [
            {
                "name": "Buttonwillow, CA",
                "charge_seconds": 900,
                "latitude": 35.4,
                "longitude": -119.5,
            }
        ],
    }


async def test_plan_trip_failed_status() -> None:
    """A non-OK plan status raises."""
    api = _mock_api({"data": {"planTrip2": {"status": "NO_ROUTE", "plans": []}}})
    with pytest.raises(HomeAssistantError, match="NO_ROUTE"):
        await async_plan_trip(api, VEHICLE_ID, (0, 0), (1, 1), None, None)


async def test_plan_trip_service(hass: HomeAssistant) -> None:
    """plan_trip geocodes the text, plans from the vehicle, and returns both."""
    device, _, api = _setup_vehicle(hass, "ready", True, PLACES_OK, PLAN_OK)

    result = await _call(
        hass, SERVICE_PLAN_TRIP, device_id=device.id, location="Costco Goleta"
    )

    assert _operations(api) == ["places", "planTripWithMultiStopV2"]
    plan_vars = api._Rivian__graphql_query.call_args_list[1].args[2]["variables"]
    assert plan_vars["waypoints"][0] == {"latitude": 34.181, "longitude": -118.327}
    assert plan_vars["waypoints"][1] == {
        "latitude": 34.4283714,
        "longitude": -119.8741895,
    }
    assert plan_vars["startingSoc"] == 69.9
    assert result["destination"]["name"] == "Costco Wholesale"
    assert result["trip_seconds"] == 20900


async def test_plan_trip_service_coordinates(hass: HomeAssistant) -> None:
    """A bare lat,lon skips the place search."""
    device, _, api = _setup_vehicle(hass, "ready", True, PLAN_OK)

    result = await _call(
        hass, SERVICE_PLAN_TRIP, device_id=device.id, location="37.77, -122.42"
    )

    assert _operations(api) == ["planTripWithMultiStopV2"]
    assert result["destination"] == {
        "name": None,
        "address": None,
        "latitude": 37.77,
        "longitude": -122.42,
    }


async def test_plan_trip_service_no_place(hass: HomeAssistant) -> None:
    """An unresolvable destination is a validation error."""
    device, _, _ = _setup_vehicle(
        hass, "ready", True, {"data": {"places": {"results": []}}}
    )
    with pytest.raises(ServiceValidationError, match="No place found"):
        await _call(hass, SERVICE_PLAN_TRIP, device_id=device.id, location="zzz")


@pytest.mark.parametrize(
    ("service", "data"),
    [
        (SERVICE_SEND_DESTINATION, {"location": LOCATION}),
        (SERVICE_PLAN_TRIP, {"location": "37.77, -122.42"}),
        (SERVICE_WAIT_FOR_PARK, {}),
    ],
)
async def test_navigation_rejects_read_only_user(hass, service, data):
    """A read-only account can't use the actions, as with upstream's actions."""

    await hass.auth.async_create_user("Owner")
    user = await hass.auth.async_create_user("Reader", group_ids=[GROUP_ID_READ_ONLY])
    device, coordinator, api = _setup_vehicle(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        VIN + "-gear_status",
        device_id=device.id,
        config_entry=coordinator.config_entry,
    )
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": device.id, **data},
            blocking=True,
            return_response=True,
            context=Context(user_id=user.id),
        )
    api._Rivian__graphql_query.assert_not_awaited()
    coordinator.send_vehicle_command.assert_not_awaited()


async def test_navigation_permissions_are_for_selected_vehicle(hass):
    """Permission to another device does not authorize this vehicle."""

    await hass.auth.async_create_user("Owner")
    user = await hass.auth.async_create_user("Restricted")
    device, coordinator, api = _setup_vehicle(hass, "ready", True, SHARE_OK)
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        VIN + "-gear_status",
        device_id=device.id,
        config_entry=coordinator.config_entry,
    )
    user.perm_lookup = PermissionLookup(er.async_get(hass), dr.async_get(hass))
    user.groups = [
        Group(
            name="Other vehicle", policy={"entities": {"device_ids": {"other": True}}}
        )
    ]
    data = {"device_id": device.id, "location": LOCATION, "replace": False}
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_DESTINATION,
            data,
            blocking=True,
            return_response=True,
            context=Context(user_id=user.id),
        )
    api._Rivian__graphql_query.assert_not_awaited()
    user.groups = [
        Group(
            name="Selected vehicle",
            policy={"entities": {"device_ids": {device.id: True}}},
        )
    ]
    assert user.permissions.check_entity(entity.entity_id, "control")
    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_DESTINATION,
        data,
        blocking=True,
        return_response=True,
        context=Context(user_id=user.id),
    )
    assert result["sent"] is True


async def test_replace_stops_when_end_trip_is_rejected(hass):
    """A rejected clear must not silently append the destination to the old trip."""
    failed = {"data": {"sendParallaxPayload": {"success": False}}}
    device, _, api = _setup_vehicle(hass, "ready", True, failed)
    with pytest.raises(HomeAssistantError, match="clear the current trip"):
        await _call(
            hass, SERVICE_SEND_DESTINATION, device_id=device.id, location=LOCATION
        )
    assert _operations(api) == ["SendUpdatedTrip"]


async def test_replacing_sends_are_serialized(hass):
    """Another replacing send can't split an end/share pair."""
    device, _, api = _setup_vehicle(hass)
    clearing = asyncio.Event()
    release = asyncio.Event()

    async def response(headers, url, body):
        if not clearing.is_set():
            clearing.set()
            await release.wait()
        data = END_TRIP_OK if body["operationName"] == "SendUpdatedTrip" else SHARE_OK
        return MagicMock(status=200, json=AsyncMock(return_value=data))

    api._Rivian__graphql_query.side_effect = response
    first = asyncio.create_task(
        _call(hass, SERVICE_SEND_DESTINATION, device_id=device.id, location=LOCATION)
    )
    await asyncio.wait_for(clearing.wait(), 1)
    second = asyncio.create_task(
        _call(hass, SERVICE_SEND_DESTINATION, device_id=device.id, location="1 Main St")
    )
    await hass.async_block_till_done()
    assert _operations(api) == ["SendUpdatedTrip"]
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert _operations(api) == ["SendUpdatedTrip", SHARE, "SendUpdatedTrip", SHARE]


async def test_send_destination_waits_for_park(hass: HomeAssistant) -> None:
    """While driving, the send waits until the vehicle shifts into Park."""
    device, coordinator, api = _setup_vehicle(hass)
    _set_gear(coordinator, "drive")

    task = asyncio.create_task(
        _call(hass, SERVICE_SEND_DESTINATION, device_id=device.id, location=LOCATION)
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    api._Rivian__graphql_query.assert_not_awaited()
    assert coordinator.listeners

    _set_gear(coordinator, "park")
    assert await asyncio.wait_for(task, 1) == {"result": 0, "sent": True}
    assert coordinator.listeners == []


async def test_send_destination_park_timeout(hass: HomeAssistant) -> None:
    """Not parking within park_timeout is an error, and nothing is sent."""
    device, coordinator, api = _setup_vehicle(hass)
    _set_gear(coordinator, "drive")

    with pytest.raises(HomeAssistantError, match="did not shift into Park"):
        await _call(
            hass,
            SERVICE_SEND_DESTINATION,
            device_id=device.id,
            location=LOCATION,
            park_timeout={"seconds": 0},
        )
    api._Rivian__graphql_query.assert_not_awaited()


async def test_send_destination_without_park_wait(hass: HomeAssistant) -> None:
    """wait_for_park: false sends while driving."""
    device, coordinator, api = _setup_vehicle(hass)
    _set_gear(coordinator, "drive")

    result = await _call(
        hass,
        SERVICE_SEND_DESTINATION,
        device_id=device.id,
        location=LOCATION,
        wait_for_park=False,
    )

    assert result == {"result": 0, "sent": True}
    assert _operations(api) == ["SendUpdatedTrip", SHARE]


async def test_send_destination_not_after(hass: HomeAssistant) -> None:
    """A deadline in the past skips the send without an error."""
    device, _, api = _setup_vehicle(hass)

    result = await _call(
        hass,
        SERVICE_SEND_DESTINATION,
        device_id=device.id,
        location=LOCATION,
        not_after="2020-01-01T00:00:00+00:00",
    )

    assert result == {"result": None, "sent": False}
    api._Rivian__graphql_query.assert_not_awaited()


async def test_send_destination_not_after_checked_after_park(
    hass: HomeAssistant,
) -> None:
    """A deadline that passes while waiting for Park skips the send."""
    device, coordinator, api = _setup_vehicle(hass)
    _set_gear(coordinator, "drive")
    soon = (dt_util.utcnow() + timedelta(milliseconds=200)).isoformat()

    task = asyncio.create_task(
        _call(
            hass,
            SERVICE_SEND_DESTINATION,
            device_id=device.id,
            location=LOCATION,
            not_after=soon,
        )
    )
    await asyncio.sleep(0.3)
    _set_gear(coordinator, "park")

    assert await asyncio.wait_for(task, 1) == {"result": None, "sent": False}
    api._Rivian__graphql_query.assert_not_awaited()


async def test_wait_for_park_when_already_parked(hass: HomeAssistant) -> None:
    device, _, _ = _setup_vehicle(hass)
    result = await _call(hass, SERVICE_WAIT_FOR_PARK, device_id=device.id)
    assert result == {"parked": True, "trip_loaded": False}


async def test_wait_for_park_reports_a_loaded_trip(hass: HomeAssistant) -> None:
    device, coordinator, _ = _setup_vehicle(hass)
    coordinator.state["waypoints"] = [{"name": "Costco"}]
    result = await _call(hass, SERVICE_WAIT_FOR_PARK, device_id=device.id)
    assert result == {"parked": True, "trip_loaded": True}


async def test_wait_for_park_after_a_drive(hass: HomeAssistant) -> None:
    """after_drive waits for the vehicle to leave Park and park again."""
    device, coordinator, _ = _setup_vehicle(hass)
    task = asyncio.create_task(
        _call(hass, SERVICE_WAIT_FOR_PARK, device_id=device.id, after_drive=True)
    )
    await asyncio.sleep(0)
    assert not task.done()
    _set_gear(coordinator, "drive")
    await asyncio.sleep(0)
    assert not task.done()
    _set_gear(coordinator, "park")
    assert await task == {"parked": True, "trip_loaded": False}


async def test_wait_for_park_timeout_is_not_an_error(hass: HomeAssistant) -> None:
    device, coordinator, _ = _setup_vehicle(hass)
    _set_gear(coordinator, "drive")
    result = await _call(
        hass, SERVICE_WAIT_FOR_PARK, device_id=device.id, timeout={"seconds": 0}
    )
    assert result == {"parked": False, "trip_loaded": False}


async def test_wait_for_park_stops_at_not_after(hass: HomeAssistant) -> None:
    """A two-hour timeout is cut short at not_after."""
    device, coordinator, _ = _setup_vehicle(hass)
    _set_gear(coordinator, "drive")
    result = await asyncio.wait_for(
        _call(
            hass,
            SERVICE_WAIT_FOR_PARK,
            device_id=device.id,
            not_after=dt_util.utcnow() + timedelta(milliseconds=200),
        ),
        5,
    )
    assert result == {"parked": False, "trip_loaded": False}


async def test_send_destination_park_wait_ends_at_not_after(
    hass: HomeAssistant,
) -> None:
    """Waiting for Park stops at the deadline and the send is skipped, not failed."""
    device, coordinator, api = _setup_vehicle(hass)
    _set_gear(coordinator, "drive")
    result = await asyncio.wait_for(
        _call(
            hass,
            SERVICE_SEND_DESTINATION,
            device_id=device.id,
            location=LOCATION,
            not_after=dt_util.utcnow() + timedelta(milliseconds=200),
        ),
        5,
    )
    assert result == {"result": None, "sent": False}
    assert _operations(api) == []
