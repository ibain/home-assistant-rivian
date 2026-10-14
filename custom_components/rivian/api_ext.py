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
PLACES_QUERY = (
    "query places($params: PlacesQueryParams!) { places(params: $params) { "
    "results { id name location { formattedAddress coordinate { latitude longitude } "
    "navCoordinate { latitude longitude } } } } }"
)
PLACE_BY_URL_QUERY = (
    "query PlaceByUrl($url: String!, $userLocation: GeoCoordinatesInput) { "
    "placeByUrl(url: $url, userLocation: $userLocation) { id name location { "
    "formattedAddress coordinate { latitude longitude } "
    "navCoordinate { latitude longitude } } } }"
)
PLAN_TRIP_QUERY = (
    "query planTripWithMultiStopV2($waypoints: [RequestWaypointInput!]!, "
    "$vehicle: String!, $startingSoc: Float, $departureTime: UnixTimestamp) { "
    "planTrip2(waypoints: $waypoints, vehicle: $vehicle, startingSoc: $startingSoc, "
    "departureTime: $departureTime) { plans { waypoints { waypointType latitude "
    "longitude charger { name chargeDurationSeconds } } summary { "
    "destinationReachable totalChargeDurationSeconds totalDriveDurationSeconds "
    "totalDriveDistanceMeters totalTripDurationSeconds arrivalSOCPercent } } "
    "status } }"
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
    Google Maps share links resolve; a person's name does not. `wake` sets the
    server's wake hint, which does not reliably wake a sleeping vehicle.
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

    A sleeping vehicle may receive the command only when it next wakes, even
    though requiresWakeup is part of the mutation.
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


async def async_find_place(
    api: Rivian, query: str, near: tuple[float, float] | None
) -> dict[str, Any] | None:
    """Resolve a place name, address or map share link to a place.

    Returns {name, address, latitude, longitude} using the road-side
    navCoordinate, or None when nothing matches. `near` biases the search.
    """
    user_location = {"latitude": near[0], "longitude": near[1]} if near else None
    if query.startswith(("http://", "https://")):
        data = await _async_gateway(
            api,
            {
                "operationName": "PlaceByUrl",
                "variables": {"url": query, "userLocation": user_location},
                "query": PLACE_BY_URL_QUERY,
            },
            "the place link",
        )
        place = (data or {}).get("placeByUrl")
    else:
        data = await _async_gateway(
            api,
            {
                "operationName": "places",
                "variables": {
                    "params": {
                        "query": query,
                        "userLocation": user_location,
                        "maxResults": 1,
                    }
                },
                "query": PLACES_QUERY,
            },
            "the place search",
        )
        results = ((data or {}).get("places") or {}).get("results") or []
        place = results[0] if results else None
    if not place:
        return None
    location = place["location"]
    coordinate = location.get("navCoordinate") or location["coordinate"]
    return {
        "name": place.get("name"),
        "address": location.get("formattedAddress"),
        "latitude": coordinate["latitude"],
        "longitude": coordinate["longitude"],
    }


async def async_plan_trip(
    api: Rivian,
    vehicle_id: str,
    origin: tuple[float, float],
    destination: tuple[float, float],
    soc: float | None,
    departure_time: int | None,
) -> dict[str, Any]:
    """Plan a trip with Rivian's planner, charging stops included.

    Returns the plan summary in seconds and meters plus the charging stops.
    """
    variables: dict[str, Any] = {
        "waypoints": [
            {"latitude": origin[0], "longitude": origin[1]},
            {"latitude": destination[0], "longitude": destination[1]},
        ],
        "vehicle": vehicle_id,
    }
    if soc is not None:
        variables["startingSoc"] = soc
    if departure_time is not None:
        variables["departureTime"] = departure_time
    data = await _async_gateway(
        api,
        {
            "operationName": "planTripWithMultiStopV2",
            "variables": variables,
            "query": PLAN_TRIP_QUERY,
        },
        "the trip plan",
    )
    plan = (data or {}).get("planTrip2") or {}
    plans = plan.get("plans") or []
    if plan.get("status") != "OK" or not plans:
        raise HomeAssistantError(
            f"Rivian could not plan the trip (status {plan.get('status')})"
        )
    summary = plans[0]["summary"]
    stops = [
        {
            "name": waypoint["charger"]["name"],
            "charge_seconds": waypoint["charger"]["chargeDurationSeconds"],
            "latitude": waypoint["latitude"],
            "longitude": waypoint["longitude"],
        }
        for waypoint in plans[0].get("waypoints") or []
        if waypoint.get("charger")
    ]
    return {
        "trip_seconds": summary["totalTripDurationSeconds"],
        "drive_seconds": summary["totalDriveDurationSeconds"],
        "charge_seconds": summary["totalChargeDurationSeconds"],
        "distance_meters": summary["totalDriveDistanceMeters"],
        "arrival_soc": summary["arrivalSOCPercent"],
        "destination_reachable": summary["destinationReachable"],
        "charging_stops": stops,
    }


def end_trip_payload(user_id: str | None, now_ms: int, correlation_id: bytes) -> bytes:
    """Build the Parallax payload the app sends to end a trip.

    UpdatedTrip { 1: plan { 1: abrpRouteId "end_trip_<ms>" }, 2: preferences {},
    3: publisher { 1: userId } }, wrapped in the c2v envelope
    { 10: value { 2: bytes }, 12: Timestamp, 16: correlationId }.
    """
    trip = _pb_bytes(1, _pb_string(1, f"end_trip_{now_ms}")) + _pb_bytes(2, b"")
    if user_id:
        trip += _pb_bytes(3, _pb_string(1, user_id))
    timestamp = _pb_varint(1, now_ms // 1000) + _pb_varint(
        2, (now_ms % 1000) * 1_000_000
    )
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
