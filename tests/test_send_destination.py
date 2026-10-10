"""Tests for the send_destination and end_trip actions."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from rivian import VehicleCommand
from rivian.exceptions import RivianApiException
from rivian.rivian import GRAPHQL_GATEWAY

from custom_components.rivian.api_ext import (
    async_end_trip,
    async_find_place,
    async_plan_trip,
    async_share_location,
    end_trip_payload,
)
from custom_components.rivian.const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
)
from custom_components.rivian.services import (
    SERVICE_END_TRIP,
    SERVICE_PLAN_TRIP,
    SERVICE_SEND_DESTINATION,
    async_setup_services,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.device_registry as dr

VEHICLE_ID = "01-123"
USER_ID = "02-456"
VIN = "7FCTGAAA0NN000001"
LOCATION = "Costco Wholesale, 1051 W Burbank Blvd, Burbank, CA 91506"
SHARE_OK = {
    "data": {"parseAndShareLocationToVehicle": {"publishResponse": {"result": 0}}}
}
END_TRIP_OK = {"data": {"sendParallaxPayload": {"success": True, "sequenceNumber": 0}}}
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


def _mock_api(*responses: tuple[dict[str, Any], int] | dict[str, Any]) -> MagicMock:
    """Return a Rivian client whose GraphQL helper returns the responses in turn."""
    mocks = []
    for item in responses:
        response_json, status = item if isinstance(item, tuple) else (item, 200)
        response = MagicMock(status=status)
        response.json = AsyncMock(return_value=response_json)
        mocks.append(response)
    api = MagicMock(
        _csrf_token="csrf", _app_session_token="a-sess", _user_session_token="u-sess"
    )
    api._Rivian__graphql_query = AsyncMock(side_effect=mocks)
    return api


def _operations(api: MagicMock) -> list[str]:
    return [
        c.args[2]["operationName"] for c in api._Rivian__graphql_query.call_args_list
    ]


async def test_share_location_request() -> None:
    """The mutation goes to the gateway with the session headers."""
    api = _mock_api(SHARE_OK)

    assert await async_share_location(api, VEHICLE_ID, LOCATION, wake=False) == 0

    headers, url, body = api._Rivian__graphql_query.call_args.args
    assert url == GRAPHQL_GATEWAY
    assert headers["Csrf-Token"] == "csrf"
    assert headers["A-Sess"] == "a-sess"
    assert headers["U-Sess"] == "u-sess"
    assert body["operationName"] == "ParseAndShareLocationToVehicle"
    assert body["variables"] == {
        "str": LOCATION,
        "vehicleId": VEHICLE_ID,
        "requiresWakeup": False,
    }


async def test_share_location_graphql_error_hides_tokens() -> None:
    """GraphQL errors surface their message but never the request headers."""
    api = _mock_api()
    api._Rivian__graphql_query.side_effect = RivianApiException(
        "Error occurred while reading the graphql response from Rivian.",
        400,
        {"errors": [{"message": "Vehicle not found"}]},
        {"U-Sess": "u-sess"},
        {},
    )

    with pytest.raises(HomeAssistantError, match="Vehicle not found") as err:
        await async_share_location(api, VEHICLE_ID, LOCATION)
    assert "u-sess" not in str(err.value)


@pytest.mark.parametrize(
    ("response_json", "status"),
    [({"data": None}, 200), (SHARE_OK, 500)],
)
async def test_share_location_bad_response(
    response_json: dict[str, Any], status: int
) -> None:
    """A missing result or non-200 status raises."""
    with pytest.raises(HomeAssistantError):
        await async_share_location(
            _mock_api((response_json, status)), VEHICLE_ID, LOCATION
        )


def test_end_trip_payload_bytes() -> None:
    """The payload matches the app's protobuf layout (verified live 2026-10-10)."""
    correlation_id = bytes(range(16))
    payload = end_trip_payload("user-placeholder", 1_760_000_000_000, correlation_id)

    expected = bytes.fromhex(
        "5232"  # 10: value (50 bytes)
        "1230"  # 2: bytes (48)
        "0a18"
        "0a16"
        + b"end_trip_1760000000000".hex()  # 1: plan { 1: abrpRouteId }
        + "1200"  # 2: preferences {}
        "1a12"
        "0a10"
        + b"user-placeholder".hex()  # 3: publisher { 1: userId }
        + "6208"
        "0880f09dc706"
        "1000"  # 12: Timestamp { 1: 1760000000, 2: 0 }
        "820110" + correlation_id.hex()  # 16: correlationId
    )
    assert payload == expected


def test_end_trip_payload_without_user() -> None:
    """No publisher field when the user id is unknown."""
    payload = end_trip_payload(None, 1_760_000_000_000, bytes(16))
    # value is 30 bytes: plan (26) + empty preferences (2), no publisher
    assert payload.startswith(bytes.fromhex("521e121c0a180a16"))
    assert b"\x1a" not in payload


async def test_end_trip_request() -> None:
    """End trip sends SendUpdatedTrip with a base64 payload and returns the result."""
    api = _mock_api(END_TRIP_OK)

    result = await async_end_trip(api, VEHICLE_ID, USER_ID)

    assert result == {"success": True, "sequence_number": 0}
    body = api._Rivian__graphql_query.call_args.args[2]
    assert body["operationName"] == "SendUpdatedTrip"
    assert body["variables"]["vehicleId"] == VEHICLE_ID
    assert "c2v_navigation.trip_copilot.updated_trip" in body["query"]
    assert body["variables"]["payload"].isascii()


def _setup_vehicle(
    hass: HomeAssistant,
    power_state: str = "ready",
    control: bool = True,
    *responses: dict[str, Any],
) -> tuple[dr.DeviceEntry, MagicMock, MagicMock]:
    """Register a vehicle device and its loaded entry data."""
    entry = MockConfigEntry(
        domain=DOMAIN, options={CONF_VEHICLE_CONTROL: control} if control else {}
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, VIN), (DOMAIN, VEHICLE_ID)},
    )
    coordinator = MagicMock(vehicle_id=VEHICLE_ID, config_entry=entry)
    coordinator.get = MagicMock(
        side_effect=lambda key: {"powerState": power_state, "batteryLevel": 69.9}.get(
            key
        )
    )
    coordinator.data = {"gnssLocation": {"latitude": 34.181, "longitude": -118.327}}
    coordinator.send_vehicle_command = AsyncMock()
    coordinator._awake = asyncio.Event()
    coordinator._awake.set()
    api = _mock_api(*(responses or (END_TRIP_OK, SHARE_OK)))
    vehicle = {"phone_identity_id": "identity-1"} if control else {}
    hass.data[DOMAIN] = {
        entry.entry_id: {
            ATTR_API: api,
            ATTR_VEHICLE: {VEHICLE_ID: vehicle},
            ATTR_COORDINATOR: {
                ATTR_USER: MagicMock(data={"id": USER_ID}),
                ATTR_VEHICLE: {VEHICLE_ID: coordinator},
            },
        }
    }
    async_setup_services(hass)
    return device, coordinator, api


async def _call(hass: HomeAssistant, service: str, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def test_send_destination_replaces_trip(hass: HomeAssistant) -> None:
    """By default the current trip is ended before the destination is sent."""
    device, coordinator, api = _setup_vehicle(hass)

    result = await _call(
        hass, SERVICE_SEND_DESTINATION, device_id=device.id, location=LOCATION
    )

    assert result == {"result": 0}
    assert _operations(api) == ["SendUpdatedTrip", "ParseAndShareLocationToVehicle"]
    end_trip, share = (c.args[2] for c in api._Rivian__graphql_query.call_args_list)
    assert end_trip["variables"]["vehicleId"] == VEHICLE_ID
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

    assert _operations(api) == ["ParseAndShareLocationToVehicle"]


async def test_send_destination_not_a_vehicle(hass: HomeAssistant) -> None:
    """A Rivian device that isn't a loaded vehicle is rejected."""
    _setup_vehicle(hass)
    wallbox = dr.async_get(hass).async_get_or_create(
        config_entry_id=next(iter(hass.data[DOMAIN])),
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
    device, coordinator, api = _setup_vehicle(hass, "sleep", control)

    result = await _call(
        hass,
        SERVICE_SEND_DESTINATION,
        device_id=device.id,
        location=LOCATION,
        wake=wake,
    )

    assert result == {"result": 0}
    coordinator.send_vehicle_command.assert_not_called()
    share = api._Rivian__graphql_query.call_args_list[-1].args[2]
    assert share["variables"]["requiresWakeup"] is wake


async def test_end_trip_service(hass: HomeAssistant) -> None:
    """end_trip resolves the device and returns Rivian's result."""
    device, _, api = _setup_vehicle(hass, "ready", True, END_TRIP_OK)

    result = await _call(hass, SERVICE_END_TRIP, device_id=device.id)

    assert result == {"success": True, "sequence_number": 0}
    assert _operations(api) == ["SendUpdatedTrip"]


async def test_find_place_text_uses_places() -> None:
    """Free text goes to the places search, biased to the vehicle's position."""
    api = _mock_api(PLACES_OK)

    place = await async_find_place(api, "Costco Goleta", (34.181, -118.327))

    body = api._Rivian__graphql_query.call_args.args[2]
    assert body["operationName"] == "places"
    assert body["variables"]["params"] == {
        "query": "Costco Goleta",
        "userLocation": {"latitude": 34.181, "longitude": -118.327},
        "maxResults": 1,
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

    assert (await async_find_place(api, "https://maps.app.goo.gl/x", None))["name"]
    assert api._Rivian__graphql_query.call_args.args[2]["operationName"] == "PlaceByUrl"
    assert await async_find_place(api, "https://maps.app.goo.gl/y", None) is None


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
