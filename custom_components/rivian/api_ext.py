"""Rivian API calls not yet in rivian-python-client.

Kept small and shaped like client methods so they can move upstream later.
The operations come from the Rivian Android app (3.17.0).
"""

from __future__ import annotations

from typing import Any

from rivian import Rivian
from rivian.exceptions import RivianApiException
from rivian.rivian import BASE_HEADERS, GRAPHQL_GATEWAY

from homeassistant.exceptions import HomeAssistantError

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


async def async_find_place(
    api: Rivian, query: str, near: tuple[float, float]
) -> dict[str, Any] | None:
    """Resolve a place name, address or map share link to a place.

    Returns {name, address, latitude, longitude} using the road-side
    navCoordinate, or None when nothing matches. `near` biases the search.
    """
    if query.startswith(("http://", "https://")):
        data = await _async_gateway(
            api,
            {
                "operationName": "PlaceByUrl",
                "variables": {
                    "url": query,
                    "userLocation": {"latitude": near[0], "longitude": near[1]},
                },
                "query": PLACE_BY_URL_QUERY,
            },
            "the place link",
        )
        place = (data or {}).get("placeByUrl")
    else:
        try:
            results = await api.search_places(query, *near)
        except RivianApiException as err:
            # str(err) would include the request headers (session tokens)
            raise HomeAssistantError(
                f"Rivian rejected the place search: {_error_messages(err)}"
            ) from None
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
