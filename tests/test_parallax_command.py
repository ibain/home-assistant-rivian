"""Tests for sending lock and unlock over Parallax."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from custom_components.rivian import const, coordinator, lock, parallax_command as pc
from homeassistant.exceptions import HomeAssistantError

UNLOCK = "UNLOCK_ALL_CLOSURES"
LOCK = "LOCK_ALL_CLOSURES_FEEDBACK"
COMMAND_UUID = uuid.UUID(bytes=bytes(range(16)))
COMMAND_ID = str(COMMAND_UUID)
VEHICLE = {
    "supported_features": [pc.BODY_FEATURE, pc.FEATURE_90S_TIMEOUT],
    "phone_identity_id": "identity-1",
    "public_key": "vehicle-key",
}


def state(
    progress: str | int,
    status: str | int | None = None,
    *,
    command_id: str | None = COMMAND_ID,
    error: tuple[int, str] | None = None,
) -> dict[str, Any]:
    """A command state as the client decodes it (`decode_command_state_message`)."""
    return {
        "commandId": command_id,
        "rvm": "body.locks.unlock",
        "progress": progress,
        "status": status,
        "error": {"code": error[0], "message": error[1]} if error else None,
    }


def message(payload: Any) -> dict[str, Any]:
    """A websocket message carrying a state."""
    return {"payload": {"data": {"vehicleCommandStatePx": {"payload": payload}}}}


def fake_decode(data: Any) -> dict[str, Any] | None:
    """Unwrap a message like the client does, for states this suite builds."""
    for key in ("payload", "data", "vehicleCommandStatePx", "payload"):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data if isinstance(data, dict) else None


class TestSupports:
    def test_lock_and_unlock_with_the_feature(self) -> None:
        assert pc.supports(UNLOCK, VEHICLE)
        assert pc.supports(LOCK, VEHICLE)

    def test_other_commands_use_the_usual_way(self) -> None:
        assert pc.is_body_command(UNLOCK)
        assert not pc.is_body_command("WAKE_VEHICLE")
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
        ([pc.BODY_FEATURE], False, 60),
        ([pc.BODY_FEATURE, pc.FEATURE_90S_TIMEOUT], False, 90),
        ([pc.BODY_FEATURE], True, 120),
        ([pc.BODY_FEATURE, pc.FEATURE_90S_TIMEOUT], True, 120),
    ],
)
def test_timeout(features: list[str], asleep: bool, timeout: int) -> None:
    assert pc.timeout_for({"supported_features": features}, asleep) == timeout


class TestCommandState:
    """The states the client decodes, as the coordinator reads them."""

    def test_progress(self) -> None:
        got = pc.CommandState.from_decoded(state("waking_up"))
        assert got == pc.CommandState(COMMAND_ID, "waking_up", None, None)
        assert not got.complete
        assert got.description == "waking up"

    def test_success(self) -> None:
        got = pc.CommandState.from_decoded(state("complete", "success"))
        assert got.succeeded
        assert got.description == "complete, success"

    def test_failure_with_a_vehicle_error(self) -> None:
        got = pc.CommandState.from_decoded(
            state("complete", "failed", error=(22, "actuation disabled"))
        )
        assert not got.succeeded
        assert got.error == "22: actuation disabled"
        assert got.description == "complete, failed (22: actuation disabled)"

    def test_an_error_without_a_status_is_not_success(self) -> None:
        """An operation error, which has a code but no status."""
        got = pc.CommandState.from_decoded(
            state("complete", error=(9, "wakeup failed in time"))
        )
        assert got.complete
        assert not got.succeeded
        assert got.error == "9: wakeup failed in time"
        assert got.description == "complete (9: wakeup failed in time)"

    def test_undefined_is_not_success(self) -> None:
        got = pc.CommandState.from_decoded(state("complete", "undefined"))
        assert not got.succeeded
        assert got.description == "complete, undefined"

    def test_no_result_is_not_success(self) -> None:
        got = pc.CommandState.from_decoded(state("complete"))
        assert got.complete
        assert not got.succeeded
        assert got.description == "complete, no result reported"

    def test_unknown_values_are_numbers(self) -> None:
        got = pc.CommandState.from_decoded(state(9, 7))
        assert not got.complete
        assert got.description == "progress 9, status 7"

    def test_text_from_rivian_is_made_safe(self) -> None:
        got = pc.CommandState.from_decoded(
            state("complete", "failed", error=(1, "bad\nthing\x00" + "x" * 500))
        )
        assert "\n" not in got.error
        assert "\x00" not in got.error
        assert len(got.error) <= pc.MAX_TEXT


class Harness:
    """A client whose subscription the test pushes messages through."""

    def __init__(self, *, subscribes: bool = True) -> None:
        self.callback: Any = None
        self.unsubscribe = AsyncMock()
        self.deliver: list[Any] = []
        self._subscribes = subscribes
        self.api = SimpleNamespace(
            subscribe_for_parallax_command_state=AsyncMock(side_effect=self._subscribe),
            send_parallax_command=AsyncMock(side_effect=self._accept),
        )
        self.subscribe = self.api.subscribe_for_parallax_command_state
        self.send = self.api.send_parallax_command

    async def _subscribe(self, vehicle_id: str, command_id: str, callback: Any) -> Any:
        self.callback = callback
        return self.unsubscribe if self._subscribes else None

    async def _accept(self, *_: Any, command_id: str, **__: Any) -> str:
        for item in self.deliver:
            self.push(item)
        return command_id

    def push(self, item: Any) -> None:
        """Deliver a state, or a whole websocket message."""
        self.callback(message(item) if "progress" in item else item)

    async def run(
        self, *deliver: Any, command: str = UNLOCK, timeout: float = 1
    ) -> pc.CommandState:
        """Send the command; the vehicle reports `deliver` once Rivian accepts it."""
        self.deliver = list(deliver)
        with (
            patch.object(pc, "uuid", SimpleNamespace(uuid4=lambda: COMMAND_UUID)),
            patch.object(pc, "generate_command_hmac", return_value=b"signature"),
            patch.object(pc, "decode_command_state_message", fake_decode),
        ):
            return await pc.async_send_body_command(
                self.api,
                command,
                vehicle_id="vehicle-1",
                vehicle=VEHICLE,
                private_key="private",
                timeout=timeout,
            )


async def test_unlock_follows_the_command_to_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=pc.__name__)
    harness = Harness()
    done = await harness.run(
        state("waking_up"),
        state("validating"),
        state("executing"),
        state("complete", "success"),
    )
    assert done.succeeded
    progress = [r.getMessage().rpartition(": ")[2] for r in caplog.records]
    assert progress[1:] == ["waking up", "validating", "executing", "complete, success"]
    harness.unsubscribe.assert_awaited_once()

    # It follows the command it sends, subscribing before sending it
    vehicle_id, command_id, _ = harness.subscribe.await_args.args
    assert (vehicle_id, command_id) == ("vehicle-1", COMMAND_ID)
    assert harness.send.await_args.args == (
        "vehicle-1",
        "body.locks.unlock",
        b"\x08\x01",
    )
    assert harness.send.await_args.kwargs == {
        "identity_id": "identity-1",
        "vehicle_key": "vehicle-key",
        "private_key": "private",
        "command_id": COMMAND_ID,
    }


async def test_lock_sends_the_lock_command() -> None:
    harness = Harness()
    await harness.run(state("complete", "success"), command=LOCK)
    assert harness.send.await_args.args[1:] == ("body.locks.lock", b"")


async def test_other_commands_progress_is_ignored() -> None:
    harness = Harness()
    other = str(uuid.UUID(bytes=bytes(reversed(COMMAND_UUID.bytes))))
    done = await harness.run(
        state("complete", "failed", command_id=other), state("complete", "success")
    )
    assert done.status == "success"


async def test_a_state_without_a_command_id_is_ours() -> None:
    """Only a state for another command is skipped, not one without an id."""
    harness = Harness()
    done = await harness.run(state("complete", "success", command_id=None))
    assert done.succeeded


async def test_a_failure_to_do_it_is_an_error() -> None:
    harness = Harness()
    with pytest.raises(
        HomeAssistantError, match="did not unlock: complete, failed"
    ) as err:
        await harness.run(
            state("executing"),
            state("complete", "failed", error=(22, "actuation disabled")),
        )
    assert not isinstance(err.value, pc.ParallaxUnavailable)
    assert "22: actuation disabled" in str(err.value)
    harness.unsubscribe.assert_awaited_once()


async def test_an_operation_error_is_an_error() -> None:
    harness = Harness()
    with pytest.raises(HomeAssistantError, match="9: denied") as err:
        await harness.run(state("complete", error=(9, "denied")))
    assert not isinstance(err.value, pc.ParallaxUnavailable)


async def test_a_vehicle_timeout_is_an_error() -> None:
    harness = Harness()
    with pytest.raises(
        HomeAssistantError, match="did not unlock: complete, timeout"
    ) as err:
        await harness.run(state("complete", "timeout"))
    assert not isinstance(err.value, pc.ParallaxUnavailable)


@pytest.mark.parametrize(
    ("result", "text"),
    [
        (state("complete", "undefined"), "complete, undefined"),
        (state("complete"), "complete, no result reported"),
    ],
)
async def test_complete_without_success_is_an_error(
    result: dict[str, Any], text: str
) -> None:
    """Only a reported success counts, not a missing or undefined result."""
    harness = Harness()
    with pytest.raises(HomeAssistantError, match=f"did not unlock: {text}") as err:
        await harness.run(result)
    assert not isinstance(err.value, pc.ParallaxUnavailable)


@pytest.mark.parametrize("status", ["invalid_request", "not_supported"])
async def test_not_supported_falls_back_for_good(status: str) -> None:
    harness = Harness()
    with pytest.raises(pc.ParallaxUnavailable) as err:
        await harness.run(state("complete", status))
    assert err.value.permanent
    harness.unsubscribe.assert_awaited_once()


async def test_no_answer_in_time_is_an_error_not_a_fall_back() -> None:
    """The vehicle may still act on it when it wakes, so don't send it again."""
    harness = Harness()
    with pytest.raises(
        HomeAssistantError, match="did not finish the unlock within"
    ) as err:
        await harness.run(state("waking_up"), timeout=0.05)
    assert not isinstance(err.value, pc.ParallaxUnavailable)
    harness.unsubscribe.assert_awaited_once()


async def test_cannot_follow_progress_falls_back_without_sending() -> None:
    harness = Harness(subscribes=False)
    with pytest.raises(pc.ParallaxUnavailable) as err:
        await harness.run(state("complete", "success"))
    assert not err.value.permanent
    harness.send.assert_not_awaited()


async def test_a_signing_failure_falls_back_without_sending() -> None:
    harness = Harness()
    with (
        patch.object(pc, "generate_command_hmac", side_effect=ValueError("bad key")),
        pytest.raises(pc.ParallaxUnavailable, match="could not sign") as err,
    ):
        await pc.async_send_body_command(
            harness.api,
            UNLOCK,
            vehicle_id="vehicle-1",
            vehicle=VEHICLE,
            private_key="private",
            timeout=1,
        )
    assert not err.value.permanent
    harness.subscribe.assert_not_awaited()
    harness.send.assert_not_awaited()


async def test_signing_is_checked_with_the_vehicles_key() -> None:
    harness = Harness()
    harness.deliver = [state("complete", "success")]
    with (
        patch.object(pc, "uuid", SimpleNamespace(uuid4=lambda: COMMAND_UUID)),
        patch.object(pc, "generate_command_hmac", return_value=b"") as sign,
        patch.object(pc, "decode_command_state_message", fake_decode),
    ):
        await pc.async_send_body_command(
            harness.api,
            UNLOCK,
            vehicle_id="vehicle-1",
            vehicle=VEHICLE,
            private_key="private",
            timeout=1,
        )
    sign.assert_called_once_with(b"", "vehicle-key", "private")


async def test_a_failing_unsubscribe_does_not_hide_a_success() -> None:
    harness = Harness()
    harness.unsubscribe.side_effect = OSError("socket closed")
    done = await harness.run(state("complete", "success"))
    assert done.succeeded
    harness.unsubscribe.assert_awaited_once()


async def test_a_failing_unsubscribe_does_not_hide_a_failure() -> None:
    harness = Harness()
    harness.unsubscribe.side_effect = OSError("socket closed")
    with pytest.raises(HomeAssistantError, match="did not unlock: complete, failed"):
        await harness.run(state("complete", "failed"))


async def test_cancelling_still_unsubscribes() -> None:
    harness = Harness()
    task = asyncio.create_task(harness.run(timeout=30))
    for _ in range(100):
        await asyncio.sleep(0)
        if harness.send.await_count:
            break
    assert harness.send.await_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    harness.unsubscribe.assert_awaited_once()


async def test_a_subscription_error_fails_fast() -> None:
    """Don't wait out the timeout when Rivian says it can't report on the command."""
    harness = Harness()
    error = {"payload": {"errors": [{"message": "no access\nto this\x07vehicle"}]}}
    with pytest.raises(
        HomeAssistantError, match="could not report on the unlock: no access"
    ) as err:
        await harness.run(error, timeout=30)
    assert "\n" not in str(err.value)
    assert "\x07" not in str(err.value)
    harness.unsubscribe.assert_awaited_once()


async def test_unreadable_messages_are_ignored() -> None:
    harness = Harness()
    junk = [
        {},
        {"payload": None},
        {"payload": {"data": None}},
        {"payload": {"data": {"vehicleCommandStatePx": {"payload": 5}}}},
        message("!!!"),
        {"payload": "text"},
    ]
    done = await harness.run(*junk, state("complete", "success"))
    assert done.succeeded


class TestSend:
    """What the reply to the send means for sending it again the legacy way."""

    @staticmethod
    def rejection(code: str, message: str = "nope") -> Exception:
        """A client exception for a GraphQL error, as the client builds it."""
        errors = [{"message": message, "extensions": {"code": code}}]
        return Exception(400, {"data": None, "errors": errors})

    @staticmethod
    async def run(send: Any) -> Harness:
        harness = Harness()
        harness.send.side_effect = send
        await harness.run(state("complete", "success"))
        return harness

    async def test_a_refused_request_falls_back_for_good(self) -> None:
        refused = self.rejection("GRAPHQL_VALIDATION_FAILED", "bad\nfield")
        with pytest.raises(
            pc.ParallaxUnavailable, match="Rivian refused it: bad"
        ) as err:
            await self.run(refused)
        assert err.value.permanent
        assert "\n" not in str(err.value)

    @pytest.mark.parametrize(
        "code",
        [
            "UNAUTHENTICATED",
            "RATE_LIMIT",
            "INTERNAL_SERVER_ERROR",
            "SESSION_MANAGER_ERROR",
        ],
    )
    async def test_other_errors_are_not_a_reason_to_send_it_again(
        self, code: str
    ) -> None:
        with pytest.raises(HomeAssistantError, match="may or may not") as err:
            await self.run(self.rejection(code))
        assert not isinstance(err.value, pc.ParallaxUnavailable)

    @pytest.mark.parametrize(
        "error",
        [
            Exception("Timeout occurred while connecting to Rivian API."),
            ValueError("not json"),
        ],
    )
    async def test_an_unclear_reply_may_have_reached_the_vehicle(
        self, error: Exception
    ) -> None:
        """Rivian may have queued it, so sending the legacy command could repeat it."""
        with pytest.raises(HomeAssistantError, match="may or may not") as err:
            await self.run(error)
        assert not isinstance(err.value, pc.ParallaxUnavailable)

    async def test_not_accepted_may_have_reached_the_vehicle(self) -> None:
        with pytest.raises(HomeAssistantError, match="did not confirm") as err:
            await self.run(lambda *_, **__: None)
        assert not isinstance(err.value, pc.ParallaxUnavailable)

    async def test_the_session_is_not_leaked(self) -> None:
        with pytest.raises(HomeAssistantError) as err:
            await self.run(RuntimeError("request headers: U-Sess secret-token"))
        assert "secret-token" not in str(err.value)
        assert err.value.__cause__ is None


def vehicle_coordinator(
    data: dict[str, Any] | None = None,
    info: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    subscribed: bool = True,
) -> coordinator.VehicleCoordinator:
    """A vehicle coordinator whose vehicle can take Parallax commands."""
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
    coor.config_entry.options = options or {
        "private_key": "private",
        "public_key": "pub",
    }
    coor.vehicle_id = "vehicle"
    coor.data = data or {"powerState": {"value": "ready"}}
    coor.api = SimpleNamespace(send_vehicle_command=AsyncMock(return_value="id"))
    coor._unsub_handler = AsyncMock() if subscribed else None
    coor._unsub_parallax = None
    coor._body_command_lock = asyncio.Lock()
    return coor


class TestCoordinator:
    """How the vehicle coordinator chooses between Parallax and the usual way."""

    async def test_unlock_goes_over_parallax(self) -> None:
        coor = vehicle_coordinator()
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        send.assert_awaited_once()
        assert send.await_args.args[1] == UNLOCK
        assert send.await_args.kwargs["private_key"] == "private"
        assert send.await_args.kwargs["timeout"] == 90
        coor.api.send_vehicle_command.assert_not_awaited()

    async def test_timeout_without_the_90_second_feature(self) -> None:
        coor = vehicle_coordinator(info={"supported_features": [pc.BODY_FEATURE]})
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        assert send.await_args.kwargs["timeout"] == 60

    async def test_a_sleeping_vehicle_is_not_woken_first(self) -> None:
        coor = vehicle_coordinator({"powerState": {"value": "sleep"}})
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        assert send.await_args.kwargs["timeout"] == 120
        coor.api.send_vehicle_command.assert_not_awaited()

    async def test_a_failed_attempt_falls_back_but_is_tried_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        coor = vehicle_coordinator()
        unavailable = AsyncMock(side_effect=pc.ParallaxUnavailable("no socket"))
        with patch.object(pc, "async_send_body_command", unavailable):
            await coor.send_vehicle_command(UNLOCK)
            await coor.send_vehicle_command(UNLOCK)
        assert unavailable.await_count == 2
        assert coor.api.send_vehicle_command.await_count == 2
        kwargs = coor.api.send_vehicle_command.await_args.kwargs
        assert kwargs["command"] == UNLOCK
        assert kwargs["phone_id"] == "phone"
        assert kwargs["identity_id"] == "identity-1"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    async def test_a_refusal_is_remembered_and_warned_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        coor = vehicle_coordinator()
        refused = AsyncMock(side_effect=pc.ParallaxUnavailable("no", permanent=True))
        with patch.object(pc, "async_send_body_command", refused):
            await coor.send_vehicle_command(UNLOCK)
            await coor.send_vehicle_command(LOCK)
        refused.assert_awaited_once()
        assert coor.api.send_vehicle_command.await_count == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    async def test_a_failure_is_not_sent_again(self) -> None:
        coor = vehicle_coordinator()
        failed = AsyncMock(side_effect=HomeAssistantError("did not unlock"))
        with (
            patch.object(pc, "async_send_body_command", failed),
            pytest.raises(HomeAssistantError, match="did not unlock"),
        ):
            await coor.send_vehicle_command(UNLOCK)
        coor.api.send_vehicle_command.assert_not_awaited()

    async def test_vehicles_without_the_feature_are_unchanged(self) -> None:
        coor = vehicle_coordinator(info={"supported_features": []})
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        send.assert_not_awaited()
        coor.api.send_vehicle_command.assert_awaited_once()

    async def test_without_a_web_socket_it_is_the_usual_way(self) -> None:
        """Following the command must not be what opens or closes the connection."""
        coor = vehicle_coordinator(subscribed=False)
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        send.assert_not_awaited()
        coor.api.send_vehicle_command.assert_awaited_once()

    async def test_without_a_private_key_it_is_the_usual_way(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        coor = vehicle_coordinator(options={"public_key": "pub"})
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command(UNLOCK)
        send.assert_not_awaited()
        coor.api.send_vehicle_command.assert_awaited_once()
        assert not [r for r in caplog.records if r.levelno >= logging.INFO]

    async def test_other_commands_and_params_are_unchanged(self) -> None:
        coor = vehicle_coordinator()
        with patch.object(pc, "async_send_body_command", AsyncMock()) as send:
            await coor.send_vehicle_command("WAKE_VEHICLE")
            await coor.send_vehicle_command(UNLOCK, {"level": 1})
        send.assert_not_awaited()
        assert coor.api.send_vehicle_command.await_count == 2

    async def test_lock_and_unlock_go_one_at_a_time(self) -> None:
        coor = vehicle_coordinator()
        events: list[tuple[str, str]] = []
        release = asyncio.Event()

        async def send(api: Any, command: str, **kwargs: Any) -> None:
            events.append(("start", command))
            if command == UNLOCK:
                await release.wait()
            events.append(("end", command))

        with patch.object(pc, "async_send_body_command", send):
            first = asyncio.create_task(coor.send_vehicle_command(UNLOCK))
            await asyncio.sleep(0)
            second = asyncio.create_task(coor.send_vehicle_command(LOCK))
            await asyncio.sleep(0.01)
            assert events == [("start", UNLOCK)]
            release.set()
            await asyncio.gather(first, second)
        assert events == [
            ("start", UNLOCK),
            ("end", UNLOCK),
            ("start", LOCK),
            ("end", LOCK),
        ]


class TestLockEntity:
    """The lock shows locking or unlocking while the vehicle does it."""

    @staticmethod
    def entity(command: Any) -> Any:
        entity = object.__new__(lock.RivianLockEntity)
        entity.entity_description = SimpleNamespace(lock=command, unlock=command)
        entity.coordinator = object()
        entity.async_write_ha_state = MagicMock()
        return entity

    async def test_unlocking_while_it_runs(self) -> None:
        seen: list[tuple[bool, bool]] = []

        async def command(_: Any) -> None:
            seen.append((entity._attr_is_locking, entity._attr_is_unlocking))

        entity = self.entity(command)
        entity._attr_is_locking = False
        await entity.async_unlock()
        assert seen == [(False, True)]
        assert (entity._attr_is_locking, entity._attr_is_unlocking) == (False, False)
        assert entity.async_write_ha_state.call_count == 2

    async def test_locking_while_it_runs(self) -> None:
        seen: list[tuple[bool, bool]] = []

        async def command(_: Any) -> None:
            seen.append((entity._attr_is_locking, entity._attr_is_unlocking))

        entity = self.entity(command)
        entity._attr_is_unlocking = False
        await entity.async_lock()
        assert seen == [(True, False)]

    async def test_it_stops_when_the_command_fails(self) -> None:
        entity = self.entity(AsyncMock(side_effect=HomeAssistantError("no")))
        with pytest.raises(HomeAssistantError):
            await entity.async_unlock()
        assert (entity._attr_is_locking, entity._attr_is_unlocking) == (False, False)
        assert entity.async_write_ha_state.call_count == 2
