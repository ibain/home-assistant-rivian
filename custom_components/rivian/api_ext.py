"""Rivian API calls not yet in rivian-python-client.

Kept small and shaped like client methods so they can move upstream later.
"""

from __future__ import annotations

import logging

from rivian import Rivian
from rivian.exceptions import RivianApiException
from rivian.rivian import BASE_HEADERS, GRAPHQL_GATEWAY

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

SHARE_LOCATION_QUERY = (
    "mutation ParseAndShareLocationToVehicle($str: String!, $vehicleId: String!) "
    "{ parseAndShareLocationToVehicle(str: $str, vehicleId: $vehicleId) "
    "{ __typename publishResponse { __typename result } } }"
)


async def async_share_location(
    api: Rivian, vehicle_id: str, location: str
) -> int | None:
    """Send a destination to the vehicle's navigation.

    Rivian's server runs a place search on `location`. Place names, addresses and
    Google Maps share links resolve; a person's name does not.
    """
    headers = BASE_HEADERS | {
        "Csrf-Token": api._csrf_token,
        "A-Sess": api._app_session_token,
        "U-Sess": api._user_session_token,
    }
    body = {
        "operationName": "ParseAndShareLocationToVehicle",
        "variables": {"str": location, "vehicleId": vehicle_id},
        "query": SHARE_LOCATION_QUERY,
    }
    try:
        response = await api._Rivian__graphql_query(headers, GRAPHQL_GATEWAY, body)
        response_json = await response.json()
    except RivianApiException as err:
        # str(err) would include the request headers (session tokens), so only
        # surface the GraphQL error messages.
        raise HomeAssistantError(
            f"Rivian rejected the destination: {_error_messages(err)}"
        ) from None

    if response.status != 200:
        raise HomeAssistantError(
            f"Rivian returned HTTP {response.status} for the destination"
        )
    try:
        result = response_json["data"]["parseAndShareLocationToVehicle"][
            "publishResponse"
        ]["result"]
    except (KeyError, TypeError) as err:
        raise HomeAssistantError(
            "Unexpected response from Rivian when sending the destination"
        ) from err
    _LOGGER.debug("Destination sent to %s, result %s", vehicle_id, result)
    return result


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
