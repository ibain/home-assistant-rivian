"""Send lock and unlock over Parallax, the way the Rivian app does.

The client signs and sends the command (`send_parallax_command`), and reports on it
through a `vehicleCommandStatePx` subscription (`subscribe_for_parallax_command_state`).
The vehicle must list the `PVS_BD_CMD` feature.

A command can't be taken back once Rivian has it, so it is only sent the legacy way
instead when nothing can have reached Rivian: it could not be signed or followed, or
Rivian refused the request outright.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any
import uuid

from rivian import Rivian, VehicleCommand
from rivian.parallax import (
    LOCK_RVM,
    UNLOCK_RVM,
    decode_command_state_message,
    encode_lock,
    encode_unlock,
)
from rivian.utils import generate_command_hmac

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

BODY_FEATURE = "PVS_BD_CMD"
FEATURE_90S_TIMEOUT = "HMAC_TIMEOUT_90S"

# The app waits longer for a vehicle that is asleep
TIMEOUT = 60.0
TIMEOUT_90S = 90.0
TIMEOUT_ASLEEP = 120.0

# command name -> (rvm, command message). Keyed by name, which VehicleCommand
# members equal.
COMMANDS: dict[str, tuple[str, Callable[[], bytes]]] = {
    "UNLOCK_ALL_CLOSURES": (UNLOCK_RVM, encode_unlock),
    "LOCK_ALL_CLOSURES_FEEDBACK": (LOCK_RVM, encode_lock),
}

PROGRESS_COMPLETE = "complete"
STATUS_SUCCESS = "success"
# The vehicle or Rivian does not handle the command this way
STATUS_FALL_BACK = ("invalid_request", "not_supported")

# GraphQL errors that don't mean the request was refused as such: the legacy command
# would fail the same way, or the command may have been taken before the error
KEEP_ON_ERROR_CODES = frozenset(
    {"UNAUTHENTICATED", "RATE_LIMIT", "INTERNAL_SERVER_ERROR", "SESSION_MANAGER_ERROR"}
)
MAX_TEXT = 200  # of anything Rivian sends that is shown to the user


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

    Values the client doesn't know come as numbers. `status` is None when the
    report has no result at all.
    """

    command_id: str | None = None
    progress: str | int = "unspecified"
    status: str | int | None = None
    error: str | None = None

    @classmethod
    def from_decoded(cls, state: dict[str, Any]) -> CommandState:
        """Return the state the client decoded, with its error text made safe."""
        error = None
        if isinstance(found := state.get("error"), dict):
            parts = (str(found.get("code") or ""), str(found.get("message") or ""))
            error = _clean(": ".join(part for part in parts if part)) or "error"
        return cls(
            state.get("commandId"),
            state.get("progress", "unspecified"),
            state.get("status"),
            error,
        )

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
        text = _word(self.progress, "progress")
        if self.status is not None:
            text += f", {_word(self.status, 'status')}"
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
    rvm, encode = COMMANDS[str(command)]
    action = rvm.rpartition(".")[2]
    try:
        # The client signs as it sends. A bad key is the one failure that can't
        # have reached Rivian, so find it before anything is sent.
        generate_command_hmac(b"", vehicle["public_key"], private_key)
    except Exception as err:
        raise ParallaxUnavailable(f"could not sign the command: {err}") from err
    command_id = str(uuid.uuid4())

    # States, or the text of an error that came with the subscription
    events: asyncio.Queue[CommandState | str] = asyncio.Queue()

    def on_message(message: dict[str, Any]) -> None:
        try:
            if errors := (message.get("payload") or {}).get("errors"):
                events.put_nowait(_error_text(errors) or "unknown error")
            elif decoded := decode_command_state_message(message):
                events.put_nowait(CommandState.from_decoded(decoded))
        except Exception:
            _LOGGER.debug("Ignoring an unreadable command message", exc_info=True)

    # Before sending, since Rivian may not replay the states from before it
    unsubscribe = await api.subscribe_for_parallax_command_state(
        vehicle_id, command_id, on_message
    )
    if unsubscribe is None:
        raise ParallaxUnavailable("could not follow the command's progress")
    try:
        await _async_send(
            api,
            vehicle_id,
            rvm,
            encode(),
            action,
            command_id=command_id,
            vehicle=vehicle,
            private_key=private_key,
        )
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
                    if event.command_id and event.command_id != command_id:
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


async def _async_send(
    api: Rivian,
    vehicle_id: str,
    rvm: str,
    payload: bytes,
    action: str,
    *,
    command_id: str,
    vehicle: dict[str, Any],
    private_key: str,
) -> None:
    """Send the command.

    Raises ParallaxUnavailable only if Rivian refused the request itself, so that
    nothing can have been queued. Anything unclear raises HomeAssistantError, since
    the command may have arrived and sending it again could repeat it.
    """
    try:
        sent = await api.send_parallax_command(
            vehicle_id,
            rvm,
            payload,
            identity_id=vehicle["phone_identity_id"],
            vehicle_key=vehicle["public_key"],
            private_key=private_key,
            command_id=command_id,
        )
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
    if sent is None:
        raise HomeAssistantError(
            f"Rivian did not confirm the {action}; it may or may not have reached "
            "the vehicle"
        )


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


def _word(value: str | int, what: str) -> str:
    """Return a progress or status for people to read."""
    return value.replace("_", " ") if isinstance(value, str) else f"{what} {value}"
