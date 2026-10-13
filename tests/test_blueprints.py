"""Tests for the bundled blueprints."""

from pathlib import Path

import yaml

from custom_components.rivian import install_blueprints

BLUEPRINTS = Path(__file__).parents[1] / "custom_components" / "rivian" / "blueprints"


class _Loader(yaml.SafeLoader):
    """SafeLoader that accepts Home Assistant's !input tag."""


_Loader.add_constructor("!input", lambda loader, node: {"input": node.value})


def test_blueprints_are_valid_automation_blueprints() -> None:
    """Each bundled file parses and declares an automation blueprint with inputs."""
    files = sorted(BLUEPRINTS.glob("*.yaml"))
    assert [f.name for f in files] == ["first_event_of_day.yaml", "time_to_leave.yaml"]
    for file in files:
        data = yaml.load(file.read_text(), Loader=_Loader)
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
