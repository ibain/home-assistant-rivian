"""Check the Parallax lock and unlock against the real rivian-python-client."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rivian.parallax import (
    LOCK_RVM,
    UNLOCK_RVM,
    decode_command_state_message,
    encode_lock,
    encode_unlock,
)
from rivian.parallax.proto import command_pb2 as pb
from rivian.utils import generate_key_pair

from custom_components.rivian import parallax_command as pc

COMMAND_ID = bytes(range(16))


def decoded(
    progress: int,
    status: int | None = None,
    *,
    error: tuple[int, str] | None = None,
    operation_error: tuple[int, str] | None = None,
) -> pc.CommandState:
    """A state as Rivian encodes it, decoded by the client and read by us."""
    msg = pb.CommandState(command_id=COMMAND_ID, rvm=UNLOCK_RVM, progress=progress)
    if status is not None:
        msg.data.SetInParent()
        msg.data.response_status = status
        if error:
            msg.data.error.code, msg.data.error.message = error
    if operation_error:
        msg.operation_error.error, msg.operation_error.message = operation_error
    payload = base64.b64encode(msg.SerializeToString()).decode()
    message = {"payload": {"data": {"vehicleCommandStatePx": {"payload": payload}}}}
    return pc.CommandState.from_decoded(decode_command_state_message(message))


def test_commands_are_the_clients() -> None:
    assert pc.COMMANDS == {
        "UNLOCK_ALL_CLOSURES": (UNLOCK_RVM, encode_unlock),
        "LOCK_ALL_CLOSURES_FEEDBACK": (LOCK_RVM, encode_lock),
    }
    # The payloads tested live, so a change in the client is noticed
    assert (UNLOCK_RVM, encode_unlock()) == ("body.locks.unlock", b"\x08\x01")
    assert (LOCK_RVM, encode_lock()) == ("body.locks.lock", b"")


@pytest.mark.parametrize(
    ("state", "complete", "succeeded", "description"),
    [
        (
            decoded(pb.VEHICLE_MODEL_OP_PROGRESS_VEHICLE_WAKING_UP),
            False,
            False,
            "waking up",
        ),
        (
            decoded(
                pb.VEHICLE_MODEL_OP_PROGRESS_VEHICLE_COMPLETE,
                pb.RESPONSE_STATUS_SUCCESS,
            ),
            True,
            True,
            "complete, success",
        ),
        (
            decoded(
                pb.VEHICLE_MODEL_OP_PROGRESS_VEHICLE_COMPLETE,
                pb.RESPONSE_STATUS_FAIL,
                error=(22, "actuation disabled"),
            ),
            True,
            False,
            "complete, failed (22: actuation disabled)",
        ),
        (
            decoded(
                pb.VEHICLE_MODEL_OP_PROGRESS_VEHICLE_COMPLETE,
                operation_error=(9, "failed to wake up in time"),
            ),
            True,
            False,
            "complete (9: failed to wake up in time)",
        ),
        (
            decoded(
                pb.VEHICLE_MODEL_OP_PROGRESS_VEHICLE_COMPLETE,
                pb.RESPONSE_STATUS_UNDEFINED,
            ),
            True,
            False,
            "complete, undefined",
        ),
        (
            decoded(pb.VEHICLE_MODEL_OP_PROGRESS_VEHICLE_COMPLETE),
            True,
            False,
            "complete, no result reported",
        ),
    ],
)
def test_states_from_the_client(
    state: pc.CommandState, complete: bool, succeeded: bool, description: str
) -> None:
    assert state.command_id == "00010203-0405-0607-0809-0a0b0c0d0e0f"
    assert (state.complete, state.succeeded) == (complete, succeeded)
    assert state.description == description


@pytest.mark.parametrize(
    "status",
    [pb.RESPONSE_STATUS_INVALID_REQUEST, pb.RESPONSE_STATUS_NOT_SUPPORTED],
)
def test_refusals_from_the_client_fall_back(status: int) -> None:
    state = decoded(pb.VEHICLE_MODEL_OP_PROGRESS_VEHICLE_COMPLETE, status)
    assert state.status in pc.STATUS_FALL_BACK


def api() -> Any:
    """A client that can't follow the command, so nothing is ever sent."""
    return MagicMock(
        subscribe_for_parallax_command_state=AsyncMock(return_value=None),
        send_parallax_command=AsyncMock(),
    )


async def send(client: Any, vehicle_key: str, private_key: str) -> None:
    await pc.async_send_body_command(
        client,
        "UNLOCK_ALL_CLOSURES",
        vehicle_id="vehicle-1",
        vehicle={"phone_identity_id": "identity-1", "public_key": vehicle_key},
        private_key=private_key,
        timeout=1,
    )


async def test_real_keys_pass_the_signing_check() -> None:
    vehicle_key, _ = generate_key_pair()
    _, private_key = generate_key_pair()
    client = api()
    with pytest.raises(pc.ParallaxUnavailable, match="could not follow"):
        await send(client, vehicle_key, private_key)
    client.subscribe_for_parallax_command_state.assert_awaited_once()


async def test_a_bad_key_falls_back_before_anything_is_sent() -> None:
    _, private_key = generate_key_pair()
    client = api()
    with pytest.raises(pc.ParallaxUnavailable, match="could not sign"):
        await send(client, "not a key", private_key)
    client.subscribe_for_parallax_command_state.assert_not_awaited()
    client.send_parallax_command.assert_not_awaited()
