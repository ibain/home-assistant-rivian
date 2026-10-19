"""Tests for sending lock and unlock over Parallax."""

from __future__ import annotations

import base64
import hashlib
import hmac
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
import pytest

from custom_components.rivian import const, coordinator, parallax_command as pc
from homeassistant.exceptions import HomeAssistantError

UNLOCK = "UNLOCK_ALL_CLOSURES"
LOCK = "LOCK_ALL_CLOSURES_FEEDBACK"
COMMAND_ID = bytes(range(16))
NOW_MS = 1_700_000_000_123
SECRET = bytes(range(32, 64))
# unlock for vehicle-1 with the constants above, signed with SECRET
WIRE_UNLOCK = (
    "524512430a11626f64792e6c6f636b732e756e6c6f636b10031a10000102030405060708090a0b0c"
    "0d0e0f220208012a0b0880e2cfaa0610c0a9d33a3a0976656869636c652d31620b0880e2cfaa0610"
    "c0a9d33a820110000102030405060708090a0b0c0d0e0f8a01320a240801122041b86b3b3f6c5b96"
    "d09abf3f77e87af7bbdbc7ef40bed7861b112b154e5c2dd0120a6964656e746974792d31"
)
VEHICLE = {
    "supported_features": [pc.FEATURE, pc.FEATURE_90S_TIMEOUT],
    "phone_identity_id": "identity-1",
    "public_key": "vehicle-key",
}


def _schema() -> Any:
    """The app's message layouts, built with the protobuf library to check ours."""
    file = descriptor_pb2.FileDescriptorProto(
        name="parallax_command_test.proto", package="t", syntax="proto3"
    )
    types = descriptor_pb2.FieldDescriptorProto
    scalar = {
        "string": types.TYPE_STRING,
        "bytes": types.TYPE_BYTES,
        "int32": types.TYPE_INT32,
        "int64": types.TYPE_INT64,
        "bool": types.TYPE_BOOL,
    }

    def message(name: str, **fields: tuple[int, str]) -> None:
        proto = file.message_type.add(name=name)
        for field, (number, kind) in fields.items():
            entry = proto.field.add(
                name=field, number=number, label=types.LABEL_OPTIONAL
            )
            if kind in scalar:
                entry.type = scalar[kind]
            else:
                entry.type = types.TYPE_MESSAGE
                entry.type_name = f".t.{kind}"

    message("Timestamp", seconds=(1, "int64"), nanos=(2, "int32"))
    message(
        "Operation",
        model=(1, "string"),
        operation=(2, "int32"),
        uuid=(3, "bytes"),
        bytes_val=(4, "bytes"),
        created_at=(5, "Timestamp"),
        vehicle_id=(7, "string"),
    )
    message("Composite", bytes_val=(2, "bytes"))
    message("Signature", type=(1, "int32"), bytes=(2, "bytes"))
    message("Security", signature=(1, "Signature"), mapped_identity=(2, "string"))
    message(
        "Envelope",
        composite=(10, "Composite"),
        last_update=(12, "Timestamp"),
        correlation_id=(16, "bytes"),
        security_info=(17, "Security"),
    )
    message("Unlock", all=(1, "bool"))
    message("Error", code=(1, "int32"), message=(2, "string"))
    message("OperationError", error=(1, "Error"), message=(2, "string"))
    message("Result", status=(1, "int32"), error=(2, "Error"))
    message(
        "State",
        ref_uuid=(1, "bytes"),
        ref_model=(2, "string"),
        progress=(3, "int32"),
        operation_error=(4, "OperationError"),
        data=(5, "bytes"),
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return lambda name: message_factory.GetMessageClass(
        pool.FindMessageTypeByName(f"t.{name}")
    )


SCHEMA = _schema()


def payload(command: str = UNLOCK) -> bytes:
    rvm, message = pc.COMMANDS[command]
    with patch.object(pc, "get_secret_key", return_value=SECRET):
        return pc.build_payload(
            rvm,
            message,
            vehicle_id="vehicle-1",
            command_id=COMMAND_ID,
            identity_id="identity-1",
            private_key="private",
            vehicle_key="vehicle-key",
            now_ms=NOW_MS,
        )


def state(
    progress: int,
    status: int | None = None,
    *,
    command_id: bytes = COMMAND_ID,
    error: tuple[int, str] | None = None,
    operation_error: tuple[int, str, str] | None = None,
) -> str:
    """A `vehicleCommandStatePx` payload, encoded with the protobuf library."""
    msg = SCHEMA("State")(ref_uuid=command_id, ref_model="body.locks.unlock")
    msg.progress = progress
    if status is not None:
        result = SCHEMA("Result")(status=status)
        if error:
            result.error.code, result.error.message = error
        msg.data = result.SerializeToString()
    if operation_error:
        msg.operation_error.error.code = operation_error[0]
        msg.operation_error.error.message = operation_error[1]
        msg.operation_error.message = operation_error[2]
    return base64.b64encode(msg.SerializeToString()).decode()


class TestPayload:
    def test_unlock_envelope(self) -> None:
        envelope = SCHEMA("Envelope").FromString(payload())
        assert envelope.correlation_id == COMMAND_ID
        assert (envelope.last_update.seconds, envelope.last_update.nanos) == (
            1_700_000_000,
            123_000_000,
        )
        assert envelope.security_info.mapped_identity == "identity-1"
        assert envelope.security_info.signature.type == 1  # HMAC-SHA256

        operation = SCHEMA("Operation").FromString(envelope.composite.bytes_val)
        assert operation.model == "body.locks.unlock"
        assert operation.operation == 3
        assert operation.uuid == COMMAND_ID
        assert operation.vehicle_id == "vehicle-1"
        assert (operation.created_at.seconds, operation.created_at.nanos) == (
            1_700_000_000,
            123_000_000,
        )
        assert SCHEMA("Unlock").FromString(operation.bytes_val).all is True

    def test_signature_covers_the_operation_as_sent(self) -> None:
        envelope = SCHEMA("Envelope").FromString(payload())
        expected = hmac.new(SECRET, envelope.composite.bytes_val, hashlib.sha256)
        assert envelope.security_info.signature.bytes == expected.digest()

    def test_signing_uses_the_clients_shared_secret(self) -> None:
        with patch.object(pc, "get_secret_key", return_value=SECRET) as secret:
            pc.sign(b"operation", "private", "vehicle-key")
        secret.assert_called_once_with("private", "vehicle-key")

    def test_lock_has_no_command_bytes(self) -> None:
        envelope = SCHEMA("Envelope").FromString(payload(LOCK))
        operation = SCHEMA("Operation").FromString(envelope.composite.bytes_val)
        assert operation.model == "body.locks.lock"
        assert operation.bytes_val == b""

    def test_wire_bytes(self) -> None:
        """The exact bytes, so a change to the encoding is deliberate."""
        assert payload().hex() == WIRE_UNLOCK


class TestSupports:
    def test_lock_and_unlock_with_the_feature(self) -> None:
        assert pc.supports(UNLOCK, VEHICLE)
        assert pc.supports(LOCK, VEHICLE)

    def test_other_commands_use_the_usual_way(self) -> None:
        assert not pc.supports("WAKE_VEHICLE", VEHICLE)
        assert not pc.supports("OPEN_FRUNK", VEHICLE)

    def test_needs_the_body_feature(self) -> None:
        assert not pc.supports(UNLOCK, VEHICLE | {"supported_features": []})
        assert not pc.supports(
            UNLOCK, VEHICLE | {"supported_features": ["PVS_COMF_CMD"]}
        )

    def test_needs_the_enrolled_phone(self) -> None:
        assert not pc.supports(UNLOCK, VEHICLE | {"phone_identity_id": None})
        assert not pc.supports(UNLOCK, VEHICLE | {"public_key": None})


@pytest.mark.parametrize(
    ("features", "asleep", "timeout"),
    [
        ([pc.FEATURE], False, 60),
        ([pc.FEATURE, pc.FEATURE_90S_TIMEOUT], False, 90),
        ([pc.FEATURE], True, 120),
        ([pc.FEATURE, pc.FEATURE_90S_TIMEOUT], True, 120),
    ],
)
def test_timeout(features: list[str], asleep: bool, timeout: int) -> None:
    assert pc.timeout_for({"supported_features": features}, asleep) == timeout


class TestDecodeState:
    def test_progress(self) -> None:
        got = pc.decode_state(state(1))
        assert got == pc.CommandState(COMMAND_ID, 1, None, None)
        assert not got.complete
        assert got.describe == "waking up"

    def test_success(self) -> None:
        got = pc.decode_state(state(4, 1))
        assert got.complete
        assert (got.status, got.error) == (1, None)
        assert got.describe == "complete, success"

    def test_failure_with_a_vehicle_error(self) -> None:
        got = pc.decode_state(state(4, 2, error=(22, "actuation disabled")))
        assert (got.status, got.error) == (2, "22: actuation disabled")
        assert got.describe == "complete, failed (22: actuation disabled)"

    def test_operation_error(self) -> None:
        got = pc.decode_state(state(4, operation_error=(9, "denied", "see logs")))
        assert got.complete
        assert got.error == "9: denied: see logs"

    @pytest.mark.parametrize(
        "bad", ["!!!", base64.b64encode(b"\x1a\x09abc").decode(), "CA=="]
    )
    def test_malformed(self, bad: str) -> None:
        assert pc.decode_state(bad) is None


class Harness:
    """A client whose subscription the test pushes states through."""

    def __init__(self, *, subscribes: bool = True) -> None:
        self.callback: Any = None
        self.unsubscribe = AsyncMock()
        self.subscribed: list[dict[str, Any]] = []
        self.deliver: list[str] = []
        self.api = SimpleNamespace(
            _start_subscription=AsyncMock(side_effect=self._subscribe)
        )
        self._subscribes = subscribes
        self.send = AsyncMock(side_effect=self._accept)

    async def _subscribe(self, query: dict[str, Any], callback: Any, _: str) -> Any:
        self.subscribed.append(query)
        self.callback = callback
        return self.unsubscribe if self._subscribes else None

    async def _accept(self, *_: Any) -> None:
        for encoded in self.deliver:
            self.push(encoded)

    def push(self, encoded: str) -> None:
        self.callback(
            {"payload": {"data": {"vehicleCommandStatePx": {"payload": encoded}}}}
        )

    async def run(
        self, *deliver: str, command: str = UNLOCK, timeout: float = 1, **kwargs: Any
    ) -> pc.CommandState:
        """Send the command; the vehicle reports `deliver` once Rivian accepts it."""
        self.deliver = list(deliver)
        fixed = SimpleNamespace(uuid4=lambda: uuid.UUID(bytes=COMMAND_ID))
        with (
            patch.object(pc, "uuid", fixed),
            patch.object(pc, "get_secret_key", return_value=SECRET),
            patch.object(pc, "_now_ms", return_value=NOW_MS),
            patch.object(pc, "_async_send", self.send),
        ):
            return await pc.async_send_body_command(
                self.api,
                command,
                vehicle_id="vehicle-1",
                vehicle=VEHICLE,
                private_key="private",
                timeout=timeout,
                **kwargs,
            )


async def test_unlock_follows_the_command_to_success() -> None:
    harness = Harness()
    seen: list[str] = []
    done = await harness.run(
        state(1),
        state(2),
        state(3),
        state(4, 1),
        on_progress=lambda s: seen.append(s.describe),
    )
    assert done.complete
    assert seen == ["waking up", "validating", "executing", "complete, success"]
    harness.unsubscribe.assert_awaited_once()

    # It follows the command it sent, subscribing before sending it
    assert harness.subscribed[0]["variables"] == {
        "vehicleId": "vehicle-1",
        "commandId": str(uuid.UUID(bytes=COMMAND_ID)),
    }
    _, vehicle_id, rvm, sent = harness.send.await_args.args
    assert (vehicle_id, rvm) == ("vehicle-1", "body.locks.unlock")
    assert sent == payload()


async def test_lock_sends_the_lock_model() -> None:
    harness = Harness()
    await harness.run(state(4, 1), command=LOCK)
    assert harness.send.await_args.args[2] == "body.locks.lock"


async def test_other_commands_progress_is_ignored() -> None:
    harness = Harness()
    other = bytes(reversed(COMMAND_ID))
    done = await harness.run(state(4, 2, command_id=other), state(4, 1))
    assert done.status == 1


async def test_a_failure_to_do_it_is_an_error() -> None:
    harness = Harness()
    with pytest.raises(
        HomeAssistantError, match="did not unlock: complete, failed"
    ) as err:
        await harness.run(state(3), state(4, 2, error=(22, "actuation disabled")))
    assert not isinstance(err.value, pc.ParallaxUnavailable)
    assert "22: actuation disabled" in str(err.value)
    harness.unsubscribe.assert_awaited_once()


async def test_an_operation_error_is_an_error() -> None:
    harness = Harness()
    with pytest.raises(HomeAssistantError, match="9: denied") as err:
        await harness.run(state(4, operation_error=(9, "denied", "")))
    assert not isinstance(err.value, pc.ParallaxUnavailable)


async def test_a_vehicle_timeout_is_an_error() -> None:
    harness = Harness()
    with pytest.raises(
        HomeAssistantError, match="did not unlock: complete, timeout"
    ) as err:
        await harness.run(state(4, 5))
    assert not isinstance(err.value, pc.ParallaxUnavailable)


@pytest.mark.parametrize("status", [3, 4])
async def test_not_supported_falls_back(status: int) -> None:
    harness = Harness()
    with pytest.raises(pc.ParallaxUnavailable):
        await harness.run(state(4, status))
    harness.unsubscribe.assert_awaited_once()


async def test_no_answer_in_time_is_an_error_not_a_fall_back() -> None:
    """The vehicle may still act on it when it wakes, so don't send it again."""
    harness = Harness()
    with pytest.raises(
        HomeAssistantError, match="did not finish the unlock within"
    ) as err:
        await harness.run(state(1), timeout=0.05)
    assert not isinstance(err.value, pc.ParallaxUnavailable)
    harness.unsubscribe.assert_awaited_once()


async def test_cannot_follow_progress_falls_back_without_sending() -> None:
    harness = Harness(subscribes=False)
    with pytest.raises(pc.ParallaxUnavailable):
        await harness.run(state(4, 1))
    harness.send.assert_not_awaited()


async def test_rivian_rejecting_it_falls_back() -> None:
    harness = Harness()
    harness.send.side_effect = pc.ParallaxUnavailable("Rivian rejected it")
    with pytest.raises(pc.ParallaxUnavailable):
        await harness.run()
    harness.unsubscribe.assert_awaited_once()


async def test_a_signing_failure_falls_back_without_subscribing() -> None:
    harness = Harness()
    with (
        patch.object(pc, "get_secret_key", side_effect=ValueError("bad key")),
        pytest.raises(pc.ParallaxUnavailable, match="could not sign"),
    ):
        await pc.async_send_body_command(
            harness.api,
            UNLOCK,
            vehicle_id="vehicle-1",
            vehicle=VEHICLE,
            private_key="private",
            timeout=1,
        )
    harness.api._start_subscription.assert_not_awaited()


class TestSubscribeShim:
    """Clients before 2.3 have no `_start_subscription`."""

    @staticmethod
    def old_client(*, connects: bool = True) -> SimpleNamespace:
        monitor = SimpleNamespace(
            connection_ack=SimpleNamespace(wait=AsyncMock()),
            start_subscription=AsyncMock(return_value="unsubscribe"),
        )
        return SimpleNamespace(
            request_timeout=5,
            _ws_connect=AsyncMock(
                side_effect=None if connects else OSError("no network")
            ),
            _ws_monitor=monitor,
        )

    async def test_subscribes_through_the_web_socket_monitor(self) -> None:
        api = self.old_client()
        got = await pc._subscribe(api, {"q": 1}, print, "the command")
        assert got == "unsubscribe"
        api._ws_connect.assert_awaited_once()
        api._ws_monitor.connection_ack.wait.assert_awaited_once()
        api._ws_monitor.start_subscription.assert_awaited_once_with({"q": 1}, print)

    async def test_a_failure_returns_none(self) -> None:
        assert (
            await pc._subscribe(self.old_client(connects=False), {}, print, "x") is None
        )

    async def test_newer_clients_use_their_own(self) -> None:
        api = SimpleNamespace(_start_subscription=AsyncMock(return_value="unsub"))
        assert await pc._subscribe(api, {"q": 1}, print, "x") == "unsub"
        api._start_subscription.assert_awaited_once_with({"q": 1}, print, "x")


class TestSend:
    """The mutation itself."""

    @staticmethod
    def api(result: Any = None, error: Exception | None = None) -> SimpleNamespace:
        response = MagicMock()
        response.json = AsyncMock(return_value={"data": result})
        return SimpleNamespace(
            _csrf_token="csrf",
            _app_session_token="app",
            _user_session_token="user",
            _Rivian__graphql_query=AsyncMock(return_value=response, side_effect=error),
        )

    async def test_posts_the_app_mutation(self) -> None:
        api = self.api({"sendParallaxPayload": {"success": True, "sequenceNumber": 1}})
        await pc._async_send(api, "vehicle-1", "body.locks.unlock", b"bytes")
        headers, url, body = api._Rivian__graphql_query.await_args.args
        assert headers["A-Sess"] == "app"
        assert headers["U-Sess"] == "user"
        assert headers["Csrf-Token"] == "csrf"
        assert url.endswith("/graphql")
        assert body["operationName"] == "SendRemoteCommand"
        assert body["variables"] == {
            "vehicleId": "vehicle-1",
            "model": "body.locks.unlock",
            "parallaxPayloadB64": base64.b64encode(b"bytes").decode(),
        }
        assert "isVehicleModelOp: true" in body["query"]
        assert "requiresWakeup: true" in body["query"]

    async def test_not_accepted_is_an_error(self) -> None:
        api = self.api({"sendParallaxPayload": {"success": False}})
        with pytest.raises(HomeAssistantError, match="did not accept") as err:
            await pc._async_send(api, "v", "body.locks.unlock", b"")
        assert not isinstance(err.value, pc.ParallaxUnavailable)

    async def test_a_rejection_falls_back_without_leaking_the_session(self) -> None:
        api = self.api(error=RuntimeError("request headers: U-Sess secret-token"))
        with pytest.raises(pc.ParallaxUnavailable) as err:
            await pc._async_send(api, "v", "body.locks.unlock", b"")
        assert "secret-token" not in str(err.value)
        assert err.value.__cause__ is None


class TestCoordinator:
    """How the vehicle coordinator chooses between Parallax and the usual way."""

    @staticmethod
    def vehicle(
        data: dict[str, Any] | None = None, info: dict[str, Any] | None = None
    ) -> coordinator.VehicleCoordinator:
        coor = object.__new__(coordinator.VehicleCoordinator)
        coor.hass = MagicMock()
        coor.hass.data = {
            const.DOMAIN: {
                "entry": {
                    const.ATTR_VEHICLE: {"vehicle": VEHICLE | (info or {})},
                    const.ATTR_COORDINATOR: {
                        const.ATTR_USER: MagicMock(
                            get_enrolled_phone_data=MagicMock(
                                return_value=("phone", {"vehicle": "identity-1"})
                            )
                        )
                    },
                }
            }
        }
        coor.config_entry = MagicMock(entry_id="entry")
        coor.config_entry.options = {"private_key": "private", "public_key": "pub"}
        coor.vehicle_id = "vehicle"
        coor.data = data or {"powerState": {"value": "ready"}}
        coor.api = SimpleNamespace(send_vehicle_command=AsyncMock(return_value="id"))
        return coor

    async def test_unlock_goes_over_parallax(self) -> None:
        coor = self.vehicle()
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        send.assert_awaited_once()
        assert send.await_args.args[1] == UNLOCK
        assert send.await_args.kwargs["private_key"] == "private"
        assert send.await_args.kwargs["timeout"] == 90
        coor.api.send_vehicle_command.assert_not_awaited()

    async def test_a_sleeping_vehicle_is_not_woken_first(self) -> None:
        coor = self.vehicle({"powerState": {"value": "sleep"}})
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        assert send.await_args.kwargs["timeout"] == 120
        coor.api.send_vehicle_command.assert_not_awaited()

    async def test_falls_back_to_the_usual_way(self) -> None:
        coor = self.vehicle()
        unavailable = AsyncMock(side_effect=pc.ParallaxUnavailable("nope"))
        with patch.object(pc, "async_send_body_command", unavailable):
            await coor.send_vehicle_command(UNLOCK)
        kwargs = coor.api.send_vehicle_command.await_args.kwargs
        assert kwargs["command"] == UNLOCK
        assert kwargs["phone_id"] == "phone"
        assert kwargs["identity_id"] == "identity-1"

    async def test_a_failure_is_not_sent_again(self) -> None:
        coor = self.vehicle()
        failed = AsyncMock(side_effect=HomeAssistantError("did not unlock"))
        with (
            patch.object(pc, "async_send_body_command", failed),
            pytest.raises(HomeAssistantError, match="did not unlock"),
        ):
            await coor.send_vehicle_command(UNLOCK)
        coor.api.send_vehicle_command.assert_not_awaited()

    async def test_vehicles_without_the_feature_are_unchanged(self) -> None:
        coor = self.vehicle(info={"supported_features": []})
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        send.assert_not_awaited()
        coor.api.send_vehicle_command.assert_awaited_once()

    async def test_other_commands_and_params_are_unchanged(self) -> None:
        coor = self.vehicle()
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command("WAKE_VEHICLE")
            await coor.send_vehicle_command(UNLOCK, {"level": 1})
        send.assert_not_awaited()
        assert coor.api.send_vehicle_command.await_count == 2
