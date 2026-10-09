"""Tests for the send_destination action."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from rivian import VehicleCommand
from rivian.exceptions import RivianApiException
from rivian.rivian import GRAPHQL_GATEWAY

from custom_components.rivian.api_ext import async_share_location
from custom_components.rivian.const import (
    ATTR_API,
    ATTR_COORDINATOR,
    ATTR_VEHICLE,
    CONF_VEHICLE_CONTROL,
    DOMAIN,
)
from custom_components.rivian.services import (
    SERVICE_SEND_DESTINATION,
    async_setup_services,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.device_registry as dr

VEHICLE_ID = "01-123"
VIN = "7FCTGAAA0NN000001"
LOCATION = "Costco Wholesale, 1051 W Burbank Blvd, Burbank, CA 91506"


def _mock_api(response_json: dict[str, Any], status: int = 200) -> MagicMock:
    """Return a Rivian client whose GraphQL helper returns response_json."""
    response = MagicMock(status=status)
    response.json = AsyncMock(return_value=response_json)
    api = MagicMock(
        _csrf_token="csrf", _app_session_token="a-sess", _user_session_token="u-sess"
    )
    api._Rivian__graphql_query = AsyncMock(return_value=response)
    return api


def _ok(result: int = 0) -> dict[str, Any]:
    return {
        "data": {
            "parseAndShareLocationToVehicle": {"publishResponse": {"result": result}}
        }
    }


async def test_share_location_request() -> None:
    """The mutation goes to the gateway with the session headers."""
    api = _mock_api(_ok())

    assert await async_share_location(api, VEHICLE_ID, LOCATION) == 0

    headers, url, body = api._Rivian__graphql_query.call_args.args
    assert url == GRAPHQL_GATEWAY
    assert headers["Csrf-Token"] == "csrf"
    assert headers["A-Sess"] == "a-sess"
    assert headers["U-Sess"] == "u-sess"
    assert body["operationName"] == "ParseAndShareLocationToVehicle"
    assert body["variables"] == {"str": LOCATION, "vehicleId": VEHICLE_ID}


async def test_share_location_graphql_error_hides_tokens() -> None:
    """GraphQL errors surface their message but never the request headers."""
    api = _mock_api({})
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
    [({"data": None}, 200), (_ok(), 500)],
)
async def test_share_location_bad_response(
    response_json: dict[str, Any], status: int
) -> None:
    """A missing result or non-200 status raises."""
    with pytest.raises(HomeAssistantError):
        await async_share_location(
            _mock_api(response_json, status), VEHICLE_ID, LOCATION
        )


def _setup_vehicle(
    hass: HomeAssistant, power_state: str = "ready", control: bool = True
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
    coordinator.get = MagicMock(return_value=power_state)
    coordinator.send_vehicle_command = AsyncMock()
    coordinator._awake = asyncio.Event()
    coordinator._awake.set()
    api = _mock_api(_ok())
    vehicle = {"phone_identity_id": "identity-1"} if control else {}
    hass.data[DOMAIN] = {
        entry.entry_id: {
            ATTR_API: api,
            ATTR_VEHICLE: {VEHICLE_ID: vehicle},
            ATTR_COORDINATOR: {ATTR_VEHICLE: {VEHICLE_ID: coordinator}},
        }
    }
    async_setup_services(hass)
    return device, coordinator, api


async def _send(hass: HomeAssistant, device_id: str, **data: Any) -> Any:
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_DESTINATION,
        {"device_id": device_id, "location": LOCATION, **data},
        blocking=True,
        return_response=True,
    )


async def test_send_destination(hass: HomeAssistant) -> None:
    """The device resolves to its vehicle id and the result is returned."""
    device, coordinator, api = _setup_vehicle(hass)

    assert await _send(hass, device.id) == {"result": 0}

    body = api._Rivian__graphql_query.call_args.args[2]
    assert body["variables"]["vehicleId"] == VEHICLE_ID
    coordinator.send_vehicle_command.assert_not_called()


async def test_send_destination_not_a_vehicle(hass: HomeAssistant) -> None:
    """A Rivian device that isn't a loaded vehicle is rejected."""
    _setup_vehicle(hass)
    wallbox = dr.async_get(hass).async_get_or_create(
        config_entry_id=next(iter(hass.data[DOMAIN])),
        identifiers={(DOMAIN, "wallbox-1")},
    )

    with pytest.raises(ServiceValidationError):
        await _send(hass, wallbox.id)


async def test_send_destination_wakes_sleeping_vehicle(hass: HomeAssistant) -> None:
    """A sleeping vehicle is woken first when vehicle control is set up."""
    device, coordinator, _ = _setup_vehicle(hass, power_state="sleep")

    await _send(hass, device.id)

    coordinator.send_vehicle_command.assert_awaited_once_with(
        VehicleCommand.WAKE_VEHICLE
    )


@pytest.mark.parametrize(
    ("control", "wake"), [(False, True), (True, False)], ids=["no_control", "wake_off"]
)
async def test_send_destination_skips_wake(
    hass: HomeAssistant, control: bool, wake: bool
) -> None:
    """No wake without vehicle control, or when wake is off; still sends."""
    device, coordinator, api = _setup_vehicle(
        hass, power_state="sleep", control=control
    )

    assert await _send(hass, device.id, wake=wake) == {"result": 0}

    coordinator.send_vehicle_command.assert_not_called()
    api._Rivian__graphql_query.assert_awaited_once()
