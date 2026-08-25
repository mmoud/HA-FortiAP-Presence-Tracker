"""Tests for the importable presence policy blueprint."""

from pathlib import Path

from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint.models import Blueprint
from homeassistant.util.yaml.loader import load_yaml

BLUEPRINT = (
    Path(__file__).parents[1]
    / "blueprints"
    / "automation"
    / "fortiap_presence_tracker"
    / "policy_by_presence.yaml"
)


def test_presence_policy_blueprint_loads() -> None:
    """Home Assistant's YAML loader accepts the complete blueprint."""
    loaded = load_yaml(BLUEPRINT)
    blueprint = Blueprint(
        loaded,
        path=str(BLUEPRINT),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )

    assert blueprint.validate() is None
    assert loaded["blueprint"]["domain"] == "automation"
    assert loaded["mode"] == "queued"
    assert len(loaded["triggers"]) == 4
    assert {trigger["to"] for trigger in loaded["triggers"]} == {
        "home",
        "not_home",
        "on",
        "off",
    }


def test_presence_policy_blueprint_has_failure_safe_actions() -> None:
    """The blueprint only acts on explicit presence states and verified switches."""
    source = BLUEPRINT.read_text(encoding="utf-8")

    assert "to: unavailable" not in source
    assert "to: unknown" not in source
    assert "integration: fortigate_policy" in source
    assert "action: switch.turn_on" in source
    assert "action: switch.turn_off" in source
    assert "target: !input policy_switches" in source
