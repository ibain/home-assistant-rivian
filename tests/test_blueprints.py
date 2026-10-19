"""Tests for the bundled blueprint."""

import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
import yaml

from custom_components.rivian import async_setup, install_blueprints
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
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
BLUEPRINT = "time_to_leave.yaml"


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Home Assistant's !input tag."""


_Loader.add_constructor("!input", lambda loader, node: {"input": node.value})


def test_blueprint_is_a_valid_automation_blueprint() -> None:
    """The bundled file parses and declares an automation blueprint with inputs."""
    assert [f.name for f in sorted(BLUEPRINTS.glob("*.yaml"))] == [BLUEPRINT]
    data = BLUEPRINT_SCHEMA(
        yaml.load((BLUEPRINTS / BLUEPRINT).read_text(), Loader=_Loader)
    )
    assert data["blueprint"]["domain"] == "automation"
    assert {"vehicle", "calendars", "morning_time"} <= data["blueprint"]["input"].keys()
    assert [t["id"] for t in data["triggers"]] == ["event", "morning", "restart"]


def test_install_blueprints_copies_updates_and_prunes(tmp_path: Path) -> None:
    """Blueprints are copied on first run, refreshed when changed, stale ones removed."""
    target = tmp_path / "blueprints" / "automation" / "rivian"

    assert install_blueprints(target) is True

    assert [p.name for p in target.glob("*.yaml")] == [BLUEPRINT]
    assert install_blueprints(target) is False  # nothing to do the second time
    stale = target / BLUEPRINT
    stale.write_text("old")
    retired = target / "first_event_of_day.yaml"
    retired.write_text("no longer shipped")

    assert install_blueprints(target) is True

    assert stale.read_text() == (BLUEPRINTS / BLUEPRINT).read_text()
    assert not retired.exists()


def test_install_blueprints_tolerates_unwritable_target(tmp_path: Path) -> None:
    """A read-only config folder logs a warning instead of failing setup."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")

    install_blueprints(blocked / "blueprints")  # mkdir fails: parent is a file


def _load_blueprint(inputs):
    """Substitute inputs, including defaults, as HA does before script validation."""
    data = yaml.load((BLUEPRINTS / BLUEPRINT).read_text(), Loader=_Loader)
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
    """Run the blueprint's actions against local calendar, planner, and send mocks."""
    dt_util.set_default_time_zone(ZoneInfo("America/Los_Angeles"))
    freezer.move_to("2026-10-15T15:00:00+00:00")  # 08:00 local
    entry = MockConfigEntry(domain="rivian")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("rivian", "vehicle")}
    )
    tracker = er.async_get(hass).async_get_or_create(
        "device_tracker", "rivian", "gps", device_id=device.id, config_entry=entry
    )
    hass.states.async_set(
        tracker.entity_id, "not_home", {"latitude": 34, "longitude": -119}
    )
    data = {
        "inputs": {
            "vehicle": device.id,
            "calendars": ["calendar.test"],
            "notify_service": "notify.test",
            "send_mode": "early",
        },
        "event": {
            "start": "2026-10-15T09:00:00-07:00",
            "end": "2026-10-15T10:00:00-07:00",
            "summary": "Dentist",
            "location": "1 Main St",
        },
        "tracker": tracker.entity_id,
        "ended": [],
        "events": [],
        "sent": [],
        "send_result": {"result": 0, "sent": True},
        "release": None,  # an Event makes the send action block until it is set
        "send_started": asyncio.Event(),
        "notifications": [],
        "google_calls": [],
        "paused": asyncio.Event(),
        "pause_at": "delay",
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
        data["send_started"].set()
        if data["release"] is not None:
            await data["release"].wait()
        return data["send_result"]

    async def notify(call):
        data["notifications"].append(dict(call.data))

    async def end_trip(call):
        data["ended"].append(dict(call.data))
        return {"success": True, "sequence_number": 0}

    for domain, action, handler, response in [
        ("calendar", "get_events", calendar, SupportsResponse.ONLY),
        ("rivian", "plan_trip", plan, SupportsResponse.ONLY),
        ("google_travel_time", "get_travel_times", google, SupportsResponse.ONLY),
        ("rivian", "send_destination", send, SupportsResponse.OPTIONAL),
        ("rivian", "end_trip", end_trip, SupportsResponse.OPTIONAL),
    ]:
        hass.services.async_register(
            domain, action, handler, supports_response=response
        )
    hass.services.async_register("notify", "test", notify)
    return data


async def _run_blueprint(hass, navigation, trigger_id="event", **inputs):
    data = _load_blueprint(navigation["inputs"] | inputs)
    sequence = [{"variables": data["variables"]}, *data["actions"]]

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
    trigger = {"id": trigger_id}
    if trigger_id == "event":
        trigger["calendar_event"] = navigation["event"]
    return await script.async_run({"trigger": trigger}, context=Context())


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


async def test_event_send_waits_for_park_inside_the_action(hass, navigation):
    """The park wait and the event-start deadline are delegated to send_destination."""
    await _run_blueprint(hass, navigation)
    (call,) = navigation["sent"]
    assert call["wait_for_park"] is True
    assert call["park_timeout"] == {"hours": 2}
    assert call["not_after"] == navigation["event"]["start"]
    assert call["replace"] is True


async def test_morning_send_waits_for_park_inside_the_action(hass, navigation):
    navigation["events"] = [navigation["event"]]
    await _run_blueprint(hass, navigation, "morning")
    (call,) = navigation["sent"]
    assert call["wait_for_park"] is True
    assert call["park_timeout"] == {"hours": 2}
    assert call["not_after"] == navigation["event"]["start"]
    assert navigation["notifications"][0]["title"] == "Loaded: Dentist"


async def test_skipped_send_is_not_notified(hass, navigation):
    """send_destination reporting sent: false (deadline passed) means no notification."""
    navigation["send_result"] = {"result": None, "sent": False}
    await _run_blueprint(hass, navigation)
    assert len(navigation["sent"]) == 1
    assert navigation["notifications"] == []


async def test_event_without_location_is_skipped(hass, navigation):
    navigation["event"]["location"] = ""
    await _run_blueprint(hass, navigation)
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
    await _run_blueprint(hass, navigation, "morning", include_charging=False)
    assert len(navigation["sent"]) == 1
    notification = navigation["notifications"][0]
    assert "Earlier" in notification["title"]
    assert "7:45 AM" in notification["message"]
    assert "charging" not in notification["message"]


async def test_morning_send_can_be_turned_off(hass, navigation):
    navigation["events"] = [navigation["event"]]
    await _run_blueprint(hass, navigation, "morning", morning_send=False)
    assert not navigation["sent"]


async def test_morning_without_located_events_sends_nothing(hass, navigation):
    navigation["events"] = [{"start": "2026-10-15T09:00:00-07:00", "summary": "Call"}]
    await _run_blueprint(hass, navigation, "morning")
    assert not navigation["sent"]


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


async def test_both_sends_collapse_when_the_first_is_held(hass, navigation, freezer):
    """If the early send only completes (parked) after the just-in-time point, one send."""
    navigation["release"] = asyncio.Event()
    task = asyncio.create_task(_run_blueprint(hass, navigation, send_mode="both"))
    await asyncio.wait_for(navigation["send_started"].wait(), 1)
    assert len(navigation["sent"]) == 1
    freezer.move_to("2026-10-15T15:25:00+00:00")  # Past buffered departure at 08:05
    async_fire_time_changed(hass)
    navigation["release"].set()
    await task
    assert len(navigation["sent"]) == 1
    assert navigation["notifications"][0]["title"] == "Loaded: Dentist"


async def test_both_sends_run_at_distinct_deadlines(hass, navigation, freezer):
    """Consolidating the send steps must preserve both phases when they are distinct."""
    task = asyncio.create_task(_run_blueprint(hass, navigation, send_mode="both"))
    await navigation["paused"].wait()
    assert len(navigation["sent"]) == 1
    assert navigation["notifications"][0]["title"] == "Loaded: Dentist"
    freezer.move_to("2026-10-15T15:05:00+00:00")
    async_fire_time_changed(hass)
    await task
    assert len(navigation["sent"]) == 2
    assert navigation["notifications"][1]["title"] == "Time to leave: Dentist"


async def test_setup_reloads_automations_after_a_blueprint_change(hass, tmp_path):
    """A changed blueprint triggers one automation reload once HA has started."""
    hass.config.config_dir = str(tmp_path)
    reloads = []
    hass.services.async_register("automation", "reload", lambda call: reloads.append(1))

    assert await async_setup(hass, {})
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert reloads == [1]

    # unchanged blueprint on the next start: no reload
    assert await async_setup(hass, {})
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert reloads == [1]


async def _run_until_clear_time(hass, navigation, at, trigger_id="event", **inputs):
    """Run the blueprint, let the clear step start waiting, then jump to `at`."""
    task = asyncio.create_task(_run_blueprint(hass, navigation, trigger_id, **inputs))
    await asyncio.wait_for(navigation["paused"].wait(), 1)
    assert len(navigation["sent"]) == 1 and not navigation["ended"]
    return task, at


async def _jump_and_finish(hass, task, freezer, at):
    freezer.move_to(at)
    async_fire_time_changed(hass)
    await asyncio.wait_for(task, 1)


async def test_clear_at_event_end_when_the_vehicle_stayed(hass, navigation, freezer):
    task, at = await _run_until_clear_time(
        hass, navigation, "2026-10-15T17:00:00+00:00", clear_mode="event_end"
    )
    await _jump_and_finish(hass, task, freezer, at)
    assert len(navigation["ended"]) == 1
    assert navigation["notifications"][-1]["title"] == "Cleared: Dentist"


async def test_no_clear_when_the_vehicle_moved(hass, navigation, freezer):
    task, at = await _run_until_clear_time(
        hass, navigation, "2026-10-15T17:00:00+00:00", clear_mode="event_end"
    )
    hass.states.async_set(
        navigation["tracker"], "not_home", {"latitude": 34.2, "longitude": -119.3}
    )
    await _jump_and_finish(hass, task, freezer, at)
    assert navigation["ended"] == []


async def test_no_clear_when_another_located_event_follows(hass, navigation, freezer):
    navigation["events"] = [
        {
            "start": "2026-10-15T11:00:00-07:00",
            "end": "2026-10-15T12:00:00-07:00",
            "summary": "Lunch",
            "location": "2 Main St",
        }
    ]
    task, at = await _run_until_clear_time(
        hass, navigation, "2026-10-15T17:00:00+00:00", clear_mode="event_end"
    )
    await _jump_and_finish(hass, task, freezer, at)
    assert navigation["ended"] == []


async def test_clear_relative_to_start_can_be_before_it(hass, navigation, freezer):
    """A negative offset clears before the event starts (08:30 for a 09:00 event)."""
    task, at = await _run_until_clear_time(
        hass,
        navigation,
        "2026-10-15T15:30:00+00:00",
        clear_mode="from_start",
        clear_minutes=-30,
    )
    freezer.move_to("2026-10-15T15:25:00+00:00")
    async_fire_time_changed(hass)
    await asyncio.sleep(0)
    assert navigation["ended"] == []
    await _jump_and_finish(hass, task, freezer, at)
    assert len(navigation["ended"]) == 1


async def test_morning_send_also_clears(hass, navigation, freezer):
    navigation["events"] = [navigation["event"]]
    task, at = await _run_until_clear_time(
        hass, navigation, "2026-10-15T17:00:00+00:00", "morning", clear_mode="event_end"
    )
    await _jump_and_finish(hass, task, freezer, at)
    assert len(navigation["ended"]) == 1


async def test_clear_off_by_default(hass, navigation):
    await _run_blueprint(hass, navigation)
    assert navigation["ended"] == []


async def test_restart_resumes_the_next_upcoming_event(hass, navigation):
    """After a restart, an event with a location inside the look-ahead is picked up."""
    navigation["events"] = [
        {
            "start": "2026-10-15T08:30:00-07:00",
            "end": "2026-10-15T09:00:00-07:00",
            "summary": "Call",
        },
        navigation["event"],
    ]
    await _run_blueprint(hass, navigation, "restart")
    (call,) = navigation["sent"]
    assert call["not_after"] == navigation["event"]["start"]
    assert navigation["notifications"][0]["title"] == "Loaded: Dentist"


async def test_restart_with_nothing_due_sends_nothing(hass, navigation):
    """The calendar returns only events in the look-ahead window; none means no send."""
    navigation["events"] = []
    await _run_blueprint(hass, navigation, "restart")
    assert not navigation["sent"]
