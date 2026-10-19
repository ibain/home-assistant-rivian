"""Send lock and unlock over Parallax, the way the Rivian app does.

Kept small and shaped like client methods so it can move into rivian-python-client
later. The operations and the protobuf layouts come from the Rivian Android app
(3.17.0). The vehicle must list the `PVS_BD_CMD` feature.

The command is a `sendParallaxPayload` mutation whose payload is a signed envelope:

    envelope { 10: { 2: operation }, 12: Timestamp, 16: command id (16 bytes),
               17: { 1: { 1: 1 (HMAC-SHA256), 2: signature }, 2: identity id } }
    operation { 1: rvm, 2: 3 (request), 3: command id, 4: command, 5: Timestamp,
                7: vehicle id }

The signature is HMAC-SHA256 over the serialized operation, keyed like the legacy
command's (ECDH of the phone's private key and the vehicle's public key, then HKDF).
Progress comes from a `vehicleCommandStatePx` subscription keyed by the command id.

A command can't be taken back once Rivian has it, so it is only sent the legacy way
instead when nothing can have reached Rivian: it could not be signed or followed, or
Rivian refused the request outright.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import hmac
import logging
import time
from typing import Any
import uuid

from rivian import Rivian, VehicleCommand
from rivian.rivian import BASE_HEADERS, GRAPHQL_GATEWAY
from rivian.utils import get_secret_key

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

BODY_FEATURE = "PVS_BD_CMD"
FEATURE_90S_TIMEOUT = "HMAC_TIMEOUT_90S"

# The app waits longer for a vehicle that is asleep
TIMEOUT = 60.0
TIMEOUT_90S = 90.0
TIMEOUT_ASLEEP = 120.0

OPERATION_REQUEST = 3  # PARALLAX_OPERATION_REQUEST
SIGNATURE_HMAC_SHA256 = 1

# command name -> (rvm, serialized command message). Unlock is `{ 1: all = true }`;
# lock has no fields. Keyed by name, which VehicleCommand members equal.
COMMANDS: dict[str, tuple[str, bytes]] = {
    "UNLOCK_ALL_CLOSURES": ("body.locks.unlock", b"\x08\x01"),
    "LOCK_ALL_CLOSURES_FEEDBACK": ("body.locks.lock", b""),
}

PROGRESS_COMPLETE = 4
PROGRESS = {
    0: "unspecified",
    1: "waking up",
    2: "validating",
    3: "executing",
    PROGRESS_COMPLETE: "complete",
}
STATUS_SUCCESS = 1
# The vehicle or Rivian does not handle the command this way
STATUS_FALL_BACK = (3, 4)
STATUS = {
    0: "undefined",
    STATUS_SUCCESS: "success",
    2: "failed",
    3: "invalid request",
    4: "not supported",
    5: "timeout",
}

# GraphQL errors that don't mean the request was refused as such: the legacy command
# would fail the same way, or the command may have been taken before the error
KEEP_ON_ERROR_CODES = frozenset(
    {"UNAUTHENTICATED", "RATE_LIMIT", "INTERNAL_SERVER_ERROR", "SESSION_MANAGER_ERROR"}
)
MAX_TEXT = 200  # of anything Rivian sends that is shown to the user
MAX_VARINT_BYTES = 10

SEND_QUERY = (
    "mutation SendRemoteCommand($vehicleId: String!, $model: String!, "
    "$parallaxPayloadB64: String!) { sendParallaxPayload("
    "payload: $parallaxPayloadB64, meta: { vehicleId: $vehicleId model: $model "
    "isVehicleModelOp: true requiresWakeup: true } ) { success sequenceNumber } }"
)
STATE_QUERY = (
    "subscription vehicleCommandStatePx($vehicleId: String!, $commandId: String!) "
    "{ vehicleCommandStatePx(vehicleId: $vehicleId, commandId: $commandId) "
    "{ payload } }"
)


class ParallaxUnavailable(Exception):
    """The command wasn't sent over Parallax and can be sent the legacy way instead.

    `permanent` means Parallax won't work for the vehicle, so don't try it again.
    """

    def __init__(self, reason: str, *, permanent: bool = False) -> None:
        """Initialize the exception."""
        super().__init__(reason)
        self.permanent = permanent


@dataclass(frozen=True)
class CommandState:
    """A vehicle command's progress, as reported by Rivian.

    `status` is None when the report has no result at all.
    """

    ref_uuid: bytes = b""
    progress: int = 0
    status: int | None = None
    error: str | None = None

    @property
    def complete(self) -> bool:
        """Return whether the command has finished, successfully or not."""
        return self.progress == PROGRESS_COMPLETE

    @property
    def succeeded(self) -> bool:
        """Return whether the vehicle reported that it did the command."""
        return self.complete and self.status == STATUS_SUCCESS and not self.error

    @property
    def description(self) -> str:
        """Return a short description for logs and errors."""
        text = PROGRESS.get(self.progress, f"progress {self.progress}")
        if self.status is not None:
            text += f", {STATUS.get(self.status, f'status {self.status}')}"
        elif self.complete and not self.error:
            text += ", no result reported"
        if self.error:
            text += f" ({self.error})"
        return text


def is_body_command(command: VehicleCommand) -> bool:
    """Return whether the command is one that can go over Parallax at all."""
    return str(command) in COMMANDS


def supports(command: VehicleCommand, vehicle: dict[str, Any]) -> bool:
    """Return whether the command can be sent over Parallax to the vehicle."""
    return (
        is_body_command(command)
        and BODY_FEATURE in vehicle.get("supported_features", [])
        and bool(vehicle.get("phone_identity_id"))
        and bool(vehicle.get("public_key"))
    )


def timeout_for(vehicle: dict[str, Any], asleep: bool) -> float:
    """Return how long to wait for a command to complete."""
    if asleep:
        return TIMEOUT_ASLEEP
    if FEATURE_90S_TIMEOUT in vehicle.get("supported_features", []):
        return TIMEOUT_90S
    return TIMEOUT


async def async_send_body_command(
    api: Rivian,
    command: VehicleCommand,
    *,
    vehicle_id: str,
    vehicle: dict[str, Any],
    private_key: str,
    timeout: float,
) -> CommandState:
    """Send a body command and wait for the vehicle to finish it.

    Raises ParallaxUnavailable when it should be sent the legacy way instead, and
    HomeAssistantError when it may have been sent but the vehicle did not do it.
    """
    rvm, message = COMMANDS[str(command)]
    action = rvm.rpartition(".")[2]
    command_id = uuid.uuid4()
    try:
        payload = build_payload(
            rvm,
            message,
            vehicle_id=vehicle_id,
            command_id=command_id.bytes,
            identity_id=vehicle["phone_identity_id"],
            private_key=private_key,
            vehicle_key=vehicle["public_key"],
            now_ms=_now_ms(),
        )
    except Exception as err:
        raise ParallaxUnavailable(f"could not sign the command: {err}") from err

    # States, or the text of an error that came with the subscription
    events: asyncio.Queue[CommandState | str] = asyncio.Queue()

    def on_message(message: dict[str, Any]) -> None:
        try:
            payload = message.get("payload") or {}
            if errors := payload.get("errors"):
                events.put_nowait(_error_text(errors) or "unknown error")
                return
            data = ((payload.get("data")) or {}).get("vehicleCommandStatePx") or {}
            encoded = data.get("payload")
            if isinstance(encoded, str) and (state := decode_state(encoded)):
                events.put_nowait(state)
        except Exception:
            _LOGGER.debug("Ignoring an unreadable command message", exc_info=True)

    unsubscribe = await _subscribe(
        api,
        {
            "operationName": "vehicleCommandStatePx",
            "query": STATE_QUERY,
            "variables": {"vehicleId": vehicle_id, "commandId": str(command_id)},
        },
        on_message,
        f"{command_id} state",
    )
    if unsubscribe is None:
        raise ParallaxUnavailable("could not follow the command's progress")
    try:
        await _async_send(api, vehicle_id, rvm, payload, action)
        _LOGGER.debug("%s sent over Parallax as %s", command, command_id)
        try:
            async with asyncio.timeout(timeout):
                while True:
                    event = await events.get()
                    if isinstance(event, str):
                        raise HomeAssistantError(
                            f"Rivian could not report on the {action}: {event} "
                            "(it may still run when the vehicle wakes)"
                        )
                    if event.ref_uuid and event.ref_uuid != command_id.bytes:
                        continue
                    _LOGGER.debug("%s %s: %s", command, command_id, event.description)
                    if event.complete:
                        state = event
                        break
        except TimeoutError:
            raise HomeAssistantError(
                f"The vehicle did not finish the {action} within {timeout:.0f} "
                "seconds (it may still run when the vehicle wakes)"
            ) from None
    finally:
        # Never let this replace the real outcome of the command
        try:
            await unsubscribe()
        except Exception:
            _LOGGER.debug("Could not unsubscribe %s", command_id, exc_info=True)

    if state.status in STATUS_FALL_BACK:
        raise ParallaxUnavailable(state.description, permanent=True)
    if not state.succeeded:
        raise HomeAssistantError(f"The vehicle did not {action}: {state.description}")
    return state


async def _subscribe(
    api: Rivian,
    payload: dict[str, Any],
    callback: Callable[[dict[str, Any]], None],
    description: str,
) -> Callable[[], Awaitable[None]] | None:
    """Start a subscription; None if it could not.

    TEST SHIM: clients before 2.3 have no `_start_subscription`, so connect the
    web socket the way their own subscribe methods do.
    """
    if hasattr(api, "_start_subscription"):
        return await api._start_subscription(payload, callback, description)
    try:
        await api._ws_connect()
        async with asyncio.timeout(api.request_timeout):
            await api._ws_monitor.connection_ack.wait()
        return await api._ws_monitor.start_subscription(payload, callback)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Unable to subscribe to %s: %s", description, err)
        return None


async def _async_send(
    api: Rivian, vehicle_id: str, rvm: str, payload: bytes, action: str
) -> None:
    """POST the command.

    Raises ParallaxUnavailable only if Rivian refused the request itself, so that
    nothing can have been queued. Anything unclear raises HomeAssistantError, since
    the command may have arrived and sending it again could repeat it.
    """
    headers = BASE_HEADERS | {
        "Csrf-Token": api._csrf_token,
        "A-Sess": api._app_session_token,
        "U-Sess": api._user_session_token,
    }
    body = {
        "operationName": "SendRemoteCommand",
        "variables": {
            "vehicleId": vehicle_id,
            "model": rvm,
            "parallaxPayloadB64": base64.b64encode(payload).decode(),
        },
        "query": SEND_QUERY,
    }
    try:
        response = await api._Rivian__graphql_query(headers, GRAPHQL_GATEWAY, body)
        status = response.status
        data = (await response.json()).get("data")
    except Exception as err:  # noqa: BLE001
        # Not str(err): client exceptions can carry the request headers (tokens)
        errors = _graphql_errors(err)
        codes = {
            (e.get("extensions") or {}).get("code")
            for e in errors
            if isinstance(e, dict)
        }
        if errors and not codes & KEEP_ON_ERROR_CODES:
            raise ParallaxUnavailable(
                f"Rivian refused it: {_error_text(errors)}", permanent=True
            ) from None
        detail = _error_text(errors) if errors else type(err).__name__
        raise HomeAssistantError(
            f"Rivian did not confirm the {action} ({detail}); it may or may not "
            "have reached the vehicle"
        ) from None
    if status != 200:
        raise HomeAssistantError(
            f"Rivian returned HTTP {status} for the {action}; it may or may not "
            "have reached the vehicle"
        )
    result = (data or {}).get("sendParallaxPayload") or {}
    if not result.get("success"):
        raise HomeAssistantError(f"Rivian did not accept the {action} for the vehicle")


def _graphql_errors(err: Exception) -> list[Any]:
    """Return the GraphQL errors a client exception carries, if any."""
    return next(
        (
            arg["errors"]
            for arg in err.args
            if isinstance(arg, dict) and isinstance(arg.get("errors"), list)
        ),
        [],
    )


def _error_text(errors: list[Any]) -> str:
    """Return the messages of GraphQL errors, safe to show."""
    messages = (e.get("message") for e in errors if isinstance(e, dict))
    return _clean("; ".join(m for m in messages if isinstance(m, str)))


def _clean(text: str) -> str:
    """Make text from Rivian fit to log and show: printable, and not too long."""
    return "".join(c if c.isprintable() else " " for c in text)[:MAX_TEXT].strip()


def sign(operation: bytes, private_key: str, vehicle_key: str) -> bytes:
    """Return the HMAC-SHA256 of the operation, as the app signs it."""
    secret = get_secret_key(private_key, vehicle_key)
    return hmac.new(secret, operation, hashlib.sha256).digest()


def build_payload(
    rvm: str,
    message: bytes,
    *,
    vehicle_id: str,
    command_id: bytes,
    identity_id: str,
    private_key: str,
    vehicle_key: str,
    now_ms: int,
) -> bytes:
    """Build the signed envelope for a command."""
    operation = _operation(rvm, message, vehicle_id, command_id, now_ms)
    signature = sign(operation, private_key, vehicle_key)
    security = _bytes(
        1, _varint_field(1, SIGNATURE_HMAC_SHA256) + _bytes(2, signature)
    ) + _string(2, identity_id)
    return (
        _bytes(10, _bytes(2, operation))
        + _bytes(12, _timestamp(now_ms))
        + _bytes(16, command_id)
        + _bytes(17, security)
    )


def _operation(
    rvm: str, message: bytes, vehicle_id: str, command_id: bytes, now_ms: int
) -> bytes:
    """Build the operation the envelope carries, and the signature covers."""
    return (
        _string(1, rvm)
        + _varint_field(2, OPERATION_REQUEST)
        + _bytes(3, command_id)
        + (_bytes(4, message) if message else b"")
        + _bytes(5, _timestamp(now_ms))
        + _string(7, vehicle_id)
    )


def decode_state(encoded: str) -> CommandState | None:
    """Decode a `vehicleCommandStatePx` payload, or None if it is malformed.

    state { 1: command id, 2: rvm, 3: progress,
            4: operation error { 1: { 1: code, 2: message }, 2: message },
            5: result { 1: status, 2: { 1: code, 2: message } } }

    Protobuf leaves out a zero, so a result without a status has status 0
    (undefined), which is not the same as no result (None).
    """
    try:
        ref, progress, status, error = b"", 0, None, None
        for number, value in _read(base64.b64decode(encoded, validate=True)):
            if number == 1 and isinstance(value, bytes):
                ref = value
            elif number == 3 and isinstance(value, int):
                progress = value
            elif number == 4 and isinstance(value, bytes):
                error = _operation_error(value)
            elif number == 5 and isinstance(value, bytes):
                status = 0
                for sub, subvalue in _read(value):
                    if sub == 1 and isinstance(subvalue, int):
                        status = subvalue
                    elif sub == 2 and isinstance(subvalue, bytes):
                        error = _code_message(subvalue) or error
        return CommandState(ref, progress, status, error)
    except (ValueError, IndexError):
        _LOGGER.debug("Unreadable command state: %s", _clean(encoded))
        return None


def _operation_error(message: bytes) -> str:
    """Describe an operation error, which has no result of its own."""
    parts = []
    for number, value in _read(message):
        if number == 1 and isinstance(value, bytes):
            parts.append(_code_message(value))
        elif number == 2 and isinstance(value, bytes):
            parts.append(_clean(value.decode(errors="replace")))
    return ": ".join(filter(None, parts)) or "operation error"


def _code_message(error: bytes) -> str | None:
    """Return "code: message" for an error `{ 1: code, 2: message }`, if it has any."""
    code, text = 0, ""
    for number, value in _read(error):
        if number == 1 and isinstance(value, int):
            code = value
        elif number == 2 and isinstance(value, bytes):
            text = _clean(value.decode(errors="replace"))
    return ": ".join(part for part in (str(code) if code else "", text) if part) or None


def _now_ms() -> int:
    return int(time.time() * 1000)


# Minimal protobuf wire encoding and decoding (varint and length-delimited fields).
def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if not value:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _varint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _bytes(number: int, value: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _string(number: int, value: str) -> bytes:
    return _bytes(number, value.encode())


def _timestamp(now_ms: int) -> bytes:
    return _varint_field(1, now_ms // 1000) + _varint_field(
        2, (now_ms % 1000) * 1_000_000
    )


def _read(data: bytes) -> list[tuple[int, int | bytes]]:
    """Return a message's (field number, varint or bytes) pairs."""
    fields: list[tuple[int, int | bytes]] = []
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(data, pos)
            fields.append((number, value))
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise ValueError("truncated field")
            fields.append((number, data[pos : pos + length]))
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return fields


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for index in range(MAX_VARINT_BYTES):
        byte = data[pos + index]
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return result, pos + index + 1
    raise ValueError("varint too long")
