"""Tests for the bundled blueprints."""

import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
import yaml

from custom_components.rivian import install_blueprints
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import Context, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.script import Script
from homeassistant.util import dt as dt_util

BLUEPRINTS = Path(__file__).parents[1] / "custom_components" / "rivian" / "blueprints"


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Home Assistant's !input tag."""


_Loader.add_constructor("!input", lambda loader, node: {"input": node.value})


def test_blueprints_are_valid_automation_blueprints() -> None:
    """Each bundled file parses and declares an automation blueprint with inputs."""
    files = sorted(BLUEPRINTS.glob("*.yaml"))
    assert [f.name for f in files] == ["first_event_of_day.yaml", "time_to_leave.yaml"]
    for file in files:
        data = BLUEPRINT_SCHEMA(yaml.load(file.read_text(), Loader=_Loader))
        assert data["blueprint"]["domain"] == "automation"
        assert {"vehicle", "calendars"} <= data["blueprint"]["input"].keys()
        assert data["triggers"] and data["actions"]


def test_install_blueprints_copies_and_updates(tmp_path: Path) -> None:
    """Blueprints are copied on first run and refreshed when they change."""
    target = tmp_path / "blueprints" / "automation" / "rivian"

    install_blueprints(target)

    names = sorted(p.name for p in target.glob("*.yaml"))
    assert names == ["first_event_of_day.yaml", "time_to_leave.yaml"]
    stale = target / "time_to_leave.yaml"
    stale.write_text("old")

    install_blueprints(target)

    assert stale.read_text() == (BLUEPRINTS / "time_to_leave.yaml").read_text()


def test_install_blueprints_tolerates_unwritable_target(tmp_path: Path) -> None:
    """A read-only config folder logs a warning instead of failing setup."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")

    install_blueprints(blocked / "blueprints")  # mkdir fails: parent is a file


def _load_blueprint(name, inputs):
    """Substitute inputs, including defaults, as HA does before script validation."""
    data = yaml.load((BLUEPRINTS / name).read_text(), Loader=_Loader)
    values = {
        key: value["default"]
        for key, value in data["blueprint"]["input"].items()
        if "default" in value
    }
    values.update(inputs)

    def substitute(value):
        if isinstance(value, dict):
            if set(value) == {"input"}:
                return values[value["input"]]
            return {key: substitute(item) for key, item in value.items()}
        if isinstance(value, list):
            return [substitute(item) for item in value]
        return value

    return substitute(data)


@pytest.fixture
async def navigation(hass, freezer):
    """Run real HA actions against local calendar, planner, and notifier mocks."""
    dt_util.set_default_time_zone(ZoneInfo("America/Los_Angeles"))
    freezer.move_to("2026-10-15T15:00:00+00:00")  # 08:00 local
    entry = MockConfigEntry(domain="rivian")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("rivian", "vehicle")}
    )
    gear = er.async_get(hass).async_get_or_create(
        "sensor",
        "rivian",
        "gear",
        suggested_object_id="renamed_gear",
        device_id=device.id,
        config_entry=entry,
    )
    tracker = er.async_get(hass).async_get_or_create(
        "device_tracker", "rivian", "gps", device_id=device.id, config_entry=entry
    )
    hass.states.async_set(gear.entity_id, "Park")
    hass.states.async_set(
        tracker.entity_id, "not_home", {"latitude": 34, "longitude": -119}
    )
    data = {
        "inputs": {
            "vehicle": device.id,
            "gear_sensor": gear.entity_id,
            "calendars": ["calendar.test"],
            "notify_service": "notify.test",
            "send_mode": "early",
        },
        "event": {
            "start": "2026-10-15T09:00:00-07:00",
            "summary": "Dentist",
            "location": "1 Main St",
        },
        "events": [],
        "sent": [],
        "notifications": [],
        "google_calls": [],
        "paused": asyncio.Event(),
        "pause_at": "wait template",
        "plan": {
            "drive_seconds": 1200,
            "charge_seconds": 600,
            "destination": {"name": "Dentist", "address": "1 Main St"},
        },
        "google": {"routes": [{"duration": 2400}]},
    }

    async def calendar(call):
        return {"calendar.test": {"events": data["events"]}}

    async def plan(call):
        return data["plan"]

    async def google(call):
        data["google_calls"].append(dict(call.data))
        if isinstance(data["google"], Exception):
            raise data["google"]
        return data["google"]

    async def send(call):
        data["sent"].append(dict(call.data))

    async def notify(call):
        data["notifications"].append(dict(call.data))

    for domain, action, handler in [
        ("calendar", "get_events", calendar),
        ("rivian", "plan_trip", plan),
        ("google_travel_time", "get_travel_times", google),
    ]:
        hass.services.async_register(
            domain, action, handler, supports_response=SupportsResponse.ONLY
        )
    hass.services.async_register("rivian", "send_destination", send)
    hass.services.async_register("notify", "test", notify)
    return data


async def _run_blueprint(hass, navigation, name="time_to_leave.yaml", **inputs):
    data = _load_blueprint(name, navigation["inputs"] | inputs)
    sequence = [
        {"variables": data["variables"]},
        *data.get("conditions", []),
        *data["actions"],
    ]

    @callback
    def changed():
        if (
            script.last_action
            and script.last_action.startswith(navigation["pause_at"])
            and script.last_action != "delay 0:00:00"
        ):
            navigation["paused"].set()

    script = Script(
        hass,
        cv.SCRIPT_SCHEMA(sequence),
        "Navigation test",
        "automation",
        change_listener=changed,
    )
    return await script.async_run(
        {"trigger": {"calendar_event": navigation["event"]}}, context=Context()
    )


@pytest.mark.parametrize(
    "google_result",
    [
        {"routes": []},
        {"routes": [{"duration": 2400}]},
        HomeAssistantError("Unavailable"),
    ],
)
async def test_google_results_and_fallback(hass, navigation, google_result):
    """Missing routes and service failures retain the estimate; valid traffic replaces it."""
    navigation["google"] = google_result
    await _run_blueprint(hass, navigation, google_entry="google-entry")
    assert len(navigation["sent"]) == 1
    assert len(navigation["google_calls"]) == 1
    call = navigation["google_calls"][0]
    assert call["origin"].startswith("device_tracker.")
    assert call["destination"] == "1 Main St"
    assert call["departure_time"] == "08:30"
    with_traffic = isinstance(google_result, dict) and bool(google_result["routes"])
    message = navigation["notifications"][0]["message"]
    assert ("50 min with traffic" if with_traffic else "40 min est.") in message
    assert ("7:55 AM" if with_traffic else "8:05 AM") in message


async def test_google_omits_past_departure(hass, navigation):
    navigation["event"]["start"] = "2026-10-15T08:10:00-07:00"
    await _run_blueprint(hass, navigation, google_entry="google-entry")
    assert "departure_time" not in navigation["google_calls"][0]
    assert len(navigation["sent"]) == 1


@pytest.mark.parametrize("name", ["time_to_leave.yaml", "first_event_of_day.yaml"])
@pytest.mark.parametrize("gear", ["Drive", "unknown", "unavailable"])
async def test_blueprints_wait_for_park(hass, navigation, name, gear):
    """Both flows refuse to send without positive Park confirmation."""
    navigation["events"] = [navigation["event"]]
    hass.states.async_set(navigation["inputs"]["gear_sensor"], gear)
    await _run_blueprint(hass, navigation, name, max_park_wait={"seconds": 0})
    assert navigation["sent"] == []


@pytest.mark.parametrize("name", ["time_to_leave.yaml", "first_event_of_day.yaml"])
async def test_blueprints_send_after_park(hass, navigation, name):
    """Renamed sensors work and a live transition to Park releases the wait."""
    navigation["events"] = [navigation["event"]]
    sensor = navigation["inputs"]["gear_sensor"]
    hass.states.async_set(sensor, "Drive")
    task = asyncio.create_task(_run_blueprint(hass, navigation, name))
    await navigation["paused"].wait()
    assert not navigation["sent"]
    hass.states.async_set(sensor, "Park")
    await task
    assert len(navigation["sent"]) == 1


@pytest.mark.parametrize("name", ["time_to_leave.yaml", "first_event_of_day.yaml"])
async def test_wrong_vehicle_gear_sensor_is_rejected(hass, navigation, name):
    navigation["events"] = [navigation["event"]]
    hass.states.async_set("sensor.some_other_gear", "Park")
    await _run_blueprint(hass, navigation, name, gear_sensor="sensor.some_other_gear")
    assert not navigation["sent"]


async def test_morning_orders_instants_and_hides_excluded_charging(hass, navigation):
    """A UTC event can precede one with an earlier-looking local-time string."""
    navigation["events"] = [
        navigation["event"],
        {
            "start": "2026-10-15T15:30:00+00:00",
            "summary": "Earlier",
            "location": "2 Main St",
        },
    ]
    await _run_blueprint(
        hass, navigation, "first_event_of_day.yaml", include_charging=False
    )
    assert len(navigation["sent"]) == 1
    notification = navigation["notifications"][0]
    assert "Earlier" in notification["title"]
    assert "7:45 AM" in notification["message"]
    assert "charging" not in notification["message"]


async def test_earlier_event_blocks_early_send_across_offsets(hass, navigation):
    navigation["events"] = [
        {
            "start": "2026-10-15T15:30:00+00:00",
            "summary": "Earlier",
            "location": "2 Main St",
        }
    ]
    await _run_blueprint(hass, navigation)
    assert not navigation["sent"]


@pytest.mark.parametrize("send_mode", ["early", "just_in_time", "both"])
async def test_sends_respect_quiet_hours(hass, navigation, freezer, send_mode):
    freezer.move_to("2026-10-15T09:30:00+00:00")  # 02:30 local
    navigation["event"]["start"] = "2026-10-15T05:30:00-07:00"
    await _run_blueprint(hass, navigation, send_mode=send_mode)
    assert not navigation["sent"]


async def test_cross_midnight_just_in_time_waits_until_six(hass, navigation, freezer):
    freezer.move_to("2026-10-15T06:00:00+00:00")  # Previous day, 23:00
    navigation["event"]["start"] = "2026-10-15T06:30:00-07:00"
    navigation["pause_at"] = "delay"
    task = asyncio.create_task(
        _run_blueprint(hass, navigation, send_mode="just_in_time")
    )
    await navigation["paused"].wait()
    freezer.move_to("2026-10-15T12:40:00+00:00")
    async_fire_time_changed(hass)
    await asyncio.sleep(0)
    assert not navigation["sent"]
    freezer.move_to("2026-10-15T13:00:00+00:00")
    async_fire_time_changed(hass)
    await task
    assert len(navigation["sent"]) == 1


async def test_both_sends_collapse_when_park_arrives_late(hass, navigation, freezer):
    sensor = navigation["inputs"]["gear_sensor"]
    hass.states.async_set(sensor, "Drive")
    task = asyncio.create_task(_run_blueprint(hass, navigation, send_mode="both"))
    await navigation["paused"].wait()
    freezer.move_to("2026-10-15T15:25:00+00:00")  # Past buffered departure at 08:05
    async_fire_time_changed(hass)
    hass.states.async_set(sensor, "Park")
    await task
    assert len(navigation["sent"]) == 1


async def test_park_after_event_start_does_not_send(hass, navigation, freezer):
    sensor = navigation["inputs"]["gear_sensor"]
    hass.states.async_set(sensor, "Drive")
    task = asyncio.create_task(_run_blueprint(hass, navigation))
    await navigation["paused"].wait()
    freezer.move_to("2026-10-15T16:01:00+00:00")
    async_fire_time_changed(hass)
    hass.states.async_set(sensor, "Park")
    await task
    assert not navigation["sent"]


async def test_both_sends_run_at_distinct_deadlines(hass, navigation, freezer):
    """Consolidating the send steps must preserve both phases when they are distinct."""
    navigation["pause_at"] = "delay"
    task = asyncio.create_task(_run_blueprint(hass, navigation, send_mode="both"))
    await navigation["paused"].wait()
    assert len(navigation["sent"]) == 1
    assert navigation["notifications"][0]["title"] == "Loaded: Dentist"
    freezer.move_to("2026-10-15T15:05:00+00:00")
    async_fire_time_changed(hass)
    await task
    assert len(navigation["sent"]) == 2
    assert navigation["notifications"][1]["title"] == "Time to leave: Dentist"
