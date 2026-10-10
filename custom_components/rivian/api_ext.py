"""Rivian API calls not yet in rivian-python-client.

Kept small and shaped like client methods so they can move upstream later.
The operations and the protobuf layouts come from the Rivian Android app (3.17.0).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any
import uuid

from rivian import Rivian
from rivian.exceptions import RivianApiException
from rivian.rivian import BASE_HEADERS, GRAPHQL_GATEWAY

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

SHARE_LOCATION_QUERY = (
    "mutation ParseAndShareLocationToVehicle($str: String!, $vehicleId: String!, "
    "$requiresWakeup: Boolean) { parseAndShareLocationToVehicle(str: $str, "
    "vehicleId: $vehicleId, requiresWakeup: $requiresWakeup) "
    "{ publishResponse { result } } }"
)
SEND_UPDATED_TRIP_QUERY = (
    "mutation SendUpdatedTrip($vehicleId: String!, $payload: String!) { "
    "sendParallaxPayload(payload: $payload, meta: { vehicleId: $vehicleId "
    'model: "c2v_navigation.trip_copilot.updated_trip" isVehicleModelOp: false '
    "requiresWakeup: true } ) { success sequenceNumber } }"
)


async def async_share_location(
    api: Rivian, vehicle_id: str, location: str, wake: bool = True
) -> int | None:
    """Send a destination to the vehicle's navigation.

    Rivian's server runs a place search on `location`. Place names, addresses and
    Google Maps share links resolve; a person's name does not. `wake` asks Rivian
    to wake a sleeping vehicle first.
    """
    data = await _async_gateway(
        api,
        {
            "operationName": "ParseAndShareLocationToVehicle",
            "variables": {
                "str": location,
                "vehicleId": vehicle_id,
                "requiresWakeup": wake,
            },
            "query": SHARE_LOCATION_QUERY,
        },
        "the destination",
    )
    try:
        result = data["parseAndShareLocationToVehicle"]["publishResponse"]["result"]
    except (KeyError, TypeError) as err:
        raise HomeAssistantError(
            "Unexpected response from Rivian when sending the destination"
        ) from err
    _LOGGER.debug("Destination sent to %s, result %s", vehicle_id, result)
    return result


async def async_end_trip(
    api: Rivian, vehicle_id: str, user_id: str | None
) -> dict[str, Any]:
    """Clear the vehicle's active trip, as the app's End Trip button does.

    Rivian wakes a sleeping vehicle for this itself (requiresWakeup is part of
    the mutation).
    """
    payload = base64.b64encode(
        end_trip_payload(user_id, int(time.time() * 1000), uuid.uuid4().bytes)
    ).decode()
    data = await _async_gateway(
        api,
        {
            "operationName": "SendUpdatedTrip",
            "variables": {"vehicleId": vehicle_id, "payload": payload},
            "query": SEND_UPDATED_TRIP_QUERY,
        },
        "the end trip command",
    )
    try:
        result = data["sendParallaxPayload"]
        success, sequence = result["success"], result["sequenceNumber"]
    except (KeyError, TypeError) as err:
        raise HomeAssistantError(
            "Unexpected response from Rivian when ending the trip"
        ) from err
    _LOGGER.debug("End trip sent to %s: %s", vehicle_id, result)
    return {"success": success, "sequence_number": sequence}


def end_trip_payload(user_id: str | None, now_ms: int, correlation_id: bytes) -> bytes:
    """Build the Parallax payload the app sends to end a trip.

    UpdatedTrip { 1: plan { 1: abrpRouteId "end_trip_<ms>" }, 2: preferences {},
    3: publisher { 1: userId } }, wrapped in the c2v envelope
    { 10: value { 2: bytes }, 12: Timestamp, 16: correlationId }.
    """
    trip = _pb_bytes(1, _pb_string(1, f"end_trip_{now_ms}")) + _pb_bytes(2, b"")
    if user_id:
        trip += _pb_bytes(3, _pb_string(1, user_id))
    timestamp = _pb_varint(1, now_ms // 1000) + _pb_varint(2, now_ms % 1_000_000_000)
    return (
        _pb_bytes(10, _pb_bytes(2, trip))
        + _pb_bytes(12, timestamp)
        + _pb_bytes(16, correlation_id)
    )


async def _async_gateway(api: Rivian, body: dict[str, Any], what: str) -> Any:
    """POST a GraphQL body to the gateway with the session headers; return `data`."""
    headers = BASE_HEADERS | {
        "Csrf-Token": api._csrf_token,
        "A-Sess": api._app_session_token,
        "U-Sess": api._user_session_token,
    }
    try:
        response = await api._Rivian__graphql_query(headers, GRAPHQL_GATEWAY, body)
        response_json = await response.json()
    except RivianApiException as err:
        # str(err) would include the request headers (session tokens), so only
        # surface the GraphQL error messages.
        raise HomeAssistantError(
            f"Rivian rejected {what}: {_error_messages(err)}"
        ) from None
    if response.status != 200:
        raise HomeAssistantError(f"Rivian returned HTTP {response.status} for {what}")
    return response_json.get("data")


def _error_messages(err: RivianApiException) -> str:
    """Return the GraphQL error messages from a client exception."""
    errors = next(
        (
            arg["errors"]
            for arg in err.args
            if isinstance(arg, dict) and "errors" in arg
        ),
        [],
    )
    messages = [e.get("message", "") for e in errors if isinstance(e, dict)]
    if messages := "; ".join(filter(None, messages)):
        return messages
    if err.args and isinstance(err.args[0], str):
        return err.args[0]
    return type(err).__name__


# Minimal protobuf wire encoding (varint and length-delimited fields only).
def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if not value:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _pb_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _pb_bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _pb_string(field: int, value: str) -> bytes:
    return _pb_bytes(field, value.encode())
