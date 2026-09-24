"""Rivian helpers."""

from __future__ import annotations

from typing import Any

from rivian import Rivian

from homeassistant.components.diagnostics.util import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_USER_SESSION_TOKEN

TO_REDACT = {
    CONF_EMAIL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    "hrid",
    "id",
    "identityId",
    "inviteId",
    "mappedIdentityId",
    "orderId",
    "serialNumber",
    "userId",
    "vas",
    "vehicleId",
    "vin",
    "wallboxId",
}


def get_rivian_api_from_entry(hass: HomeAssistant, entry: ConfigEntry) -> Rivian:
    """Get Rivian API from a config entry."""
    return Rivian(
        request_timeout=30,
        session=async_get_clientsession(hass),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        user_session_token=entry.data.get(CONF_USER_SESSION_TOKEN),
    )


def redact(data: Any) -> dict:
    """Redact sensitive data."""
    return async_redact_data(data, TO_REDACT)


def user_has_2fa(user_data: dict[str, Any]) -> bool:
    """Return True if Rivian user data indicates 2FA is enabled.

    API may use registrationChannels, registrationChannels2FA, or other keys.
    """
    if not user_data:
        return False
    if user_data.get("registrationChannels") or user_data.get("registrationChannels2FA"):
        return True
    key_lower = " ".join(str(k).lower() for k in user_data.keys())
    if "2fa" in key_lower or "twofactor" in key_lower or "registrationchannel" in key_lower:
        for k, v in user_data.items():
            if v and ("2fa" in k.lower() or "channel" in k.lower() or "twofactor" in k.lower()):
                return True
    return False
