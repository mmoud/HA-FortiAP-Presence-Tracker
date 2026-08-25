"""Tests for the full-page FortiAP management API model."""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.fortigate_policy.api import FortiGateIdentityError, Policy
from custom_components.fortigate_policy.const import (
    CONF_ALLOWED_SSIDS,
    CONF_API_TOKEN,
    CONF_FRIENDLY_NAME,
    CONF_POLICIES,
    CONF_PRESENCE_USER_MACS,
    CONF_PRESENCE_USER_NAME,
    CONF_PRESENCE_USERS,
    CONF_TRACKED_CLIENTS,
    CONF_VDOM,
    CONF_VERIFY_SSL,
    CONF_WIFI_CLIENT_COUNT_SENSOR,
    CONF_WIFI_TRACKING_ENABLED,
)
from custom_components.fortigate_policy.panel import (
    PANEL_VERSION,
    _client_payload,
    _normalize_configuration,
    _panel_data,
    _validate_policies,
    async_register_panel,
    websocket_set_policy_status,
)
from custom_components.fortigate_policy.policy_config import PolicyDefinition
from custom_components.fortigate_policy.wifi import (
    FortiGateWifiClient,
    WifiPresence,
)
from tests.test_api import FakeResponse, FakeSession

MAC = "aa:bb:cc:dd:ee:ff"
OTHER_MAC = "11:22:33:44:55:66"
NOW = datetime(2026, 8, 22, 13, 0, tzinfo=UTC)
FRONTEND = (
    Path(__file__).parents[1]
    / "custom_components"
    / "fortigate_policy"
    / "frontend"
    / f"fortiap-panel-{PANEL_VERSION}.js"
)


class FakeWifiCoordinator:
    """Small coordinator surface used by panel snapshots."""

    last_update_success = True
    last_successful_update = NOW

    def __init__(self) -> None:
        self.client = FortiGateWifiClient(
            MAC,
            hostname="Example phone",
            ssid="Home",
            ap_name="Upstairs AP",
            rssi=-48,
            snr=41,
        )
        self.data = SimpleNamespace(
            clients={MAC: self.client}, fortios_version="v7.4.8"
        )

    def presence_for(self, mac: str) -> WifiPresence | None:
        if mac != MAC:
            return None
        return WifiPresence(True, None, NOW, self.client)


def entry() -> SimpleNamespace:
    """Return a realistic loaded config entry without credentials in output."""
    wifi = FakeWifiCoordinator()
    return SimpleNamespace(
        domain="fortigate_policy",
        entry_id="entry-1",
        title="FortiGate example.test",
        data={
            CONF_HOST: "fortigate.example.test",
            CONF_PORT: 443,
            CONF_VDOM: "root",
            CONF_VERIFY_SSL: True,
            CONF_API_TOKEN: "must-not-appear",
            CONF_POLICIES: [{"policy_id": "61", "policy_name": "Family access"}],
        },
        options={
            CONF_TRACKED_CLIENTS: {
                MAC: {
                    CONF_FRIENDLY_NAME: "Example phone",
                    CONF_ALLOWED_SSIDS: ["Home"],
                }
            },
            CONF_PRESENCE_USERS: {
                "person-1": {
                    CONF_PRESENCE_USER_NAME: "Example person",
                    CONF_PRESENCE_USER_MACS: [MAC],
                }
            },
        },
        runtime_data=SimpleNamespace(
            wifi_coordinator=wifi,
            policy_coordinators={
                "61": SimpleNamespace(data=Policy("61", "Family access", "enable"))
            },
        ),
    )


class TestPanelSnapshot(unittest.TestCase):
    """Panel snapshots expose useful state but never connection secrets."""

    def test_snapshot_combines_configuration_and_actual_presence(self) -> None:
        result = _panel_data(entry())

        self.assertEqual("home", result["trackers"][0]["state"])
        self.assertEqual("Home", result["trackers"][0]["client"]["ssid"])
        self.assertEqual(["Home"], result["trackers"][0]["allowed_ssids"])
        self.assertEqual("enable", result["policies"][0]["state"])
        self.assertEqual("v7.4.8", result["health"]["fortios_version"])
        self.assertNotIn("must-not-appear", str(result))
        self.assertNotIn(CONF_API_TOKEN, str(result))

    def test_high_churn_signal_values_are_not_sent_to_frontend(self) -> None:
        result = _client_payload(FakeWifiCoordinator().client)

        self.assertNotIn("rssi", result)
        self.assertNotIn("snr", result)


class TestPanelFrontend(unittest.TestCase):
    """Keep essential full-page management controls prominent."""

    def test_header_has_home_assistant_exit(self) -> None:
        source = FRONTEND.read_text(encoding="utf-8")

        self.assertIn('href="/home"', source)
        self.assertIn("Back to Home Assistant overview", source)

    def test_frontend_cache_version_matches_manifest(self) -> None:
        manifest = json.loads(
            (FRONTEND.parents[1] / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["version"], PANEL_VERSION)
        self.assertEqual(FRONTEND.name, f"fortiap-panel-{PANEL_VERSION}.js")

    def test_dashboard_uses_bundled_icons_and_semantic_metric_tones(
        self,
    ) -> None:
        source = FRONTEND.read_text(encoding="utf-8")

        self.assertIn('ICON_BASE = "/fortiap_presence_static/icons-color"', source)
        self.assertIn('"view-dashboard-outline"', source)
        self.assertIn(".svg?v=${iconVersion}", source)
        self.assertIn("metric-card ${escapeHtml(tone)}", source)
        self.assertIn("aria-current", source)
        for icon in (
            "access-point-network",
            "account-group",
            "home-assistant",
            "shield-check",
            "view-dashboard-outline",
        ):
            self.assertTrue((FRONTEND.parent / "icons-color" / f"{icon}.svg").is_file())

    def test_people_creation_precedes_large_discovery_catalog(self) -> None:
        source = FRONTEND.read_text(encoding="utf-8")

        people = source.index("<h2>People</h2>")
        trackers = source.index("<h2>Tracked wireless devices</h2>")
        discovered = source.index("<h2>Discovered clients</h2>")
        self.assertLess(people, trackers)
        self.assertLess(trackers, discovered)
        self.assertIn('id="new-person-name"', source)
        self.assertIn('data-action="add-person"', source)
        self.assertIn('class="table-scroll"', source)

    def test_phone_layout_keeps_navigation_and_forms_usable(self) -> None:
        source = FRONTEND.read_text(encoding="utf-8")

        self.assertIn("@media(max-width:560px)", source)
        self.assertIn(".tabs{grid-template-columns:repeat(2,minmax(0,1fr))", source)
        self.assertIn(".metric-card{grid-column:span 6", source)
        self.assertIn("table.mobile-table thead{display:none}", source)
        self.assertIn("content:attr(data-label)", source)
        self.assertIn(".save-note .muted{display:none}", source)
        self.assertIn('class="row add-row"', source)

    def test_people_devices_actions_and_policy_controls_are_explicit(self) -> None:
        source = FRONTEND.read_text(encoding="utf-8")

        self.assertIn('["people","People","account-multiple-outline"]', source)
        self.assertIn('["devices","Devices","cellphone-link"]', source)
        self.assertNotIn("People & devices", source)
        self.assertIn('class="editable"', source)
        self.assertIn('class="btn track"', source)
        self.assertIn('class="btn remove"', source)
        self.assertIn('class="btn home"', source)
        self.assertIn('class="btn refresh"', source)
        self.assertIn('class="btn save"', source)
        self.assertIn('data-action="set-policy"', source)
        self.assertIn("panel/policy/set", source)

    def test_automation_tab_links_to_native_home_assistant_workflows(self) -> None:
        source = FRONTEND.read_text(encoding="utf-8")

        self.assertIn('["automations","Automations","source-branch"]', source)
        self.assertIn("my.home-assistant.io/redirect/blueprint_import", source)
        self.assertIn("policy_by_presence.yaml", source)
        self.assertIn('href="/config/automation/dashboard"', source)
        self.assertIn('href="/config/blueprint/dashboard"', source)
        self.assertIn("Unavailable and unknown states never run", source)


class TestPanelValidation(unittest.TestCase):
    """Full-page saves preserve the same server-side safety boundaries."""

    def test_normalizes_trackers_people_and_settings(self) -> None:
        result = _normalize_configuration(
            entry(),
            [
                {
                    "mac": "AA-BB-CC-DD-EE-FF",
                    "name": " Example phone ",
                    "allowed_ssids": ["Home", "Home"],
                }
            ],
            [
                {
                    "id": "person-1",
                    "name": "Example person",
                    "macs": [MAC],
                    "away_grace_period": 240,
                }
            ],
            {
                CONF_WIFI_TRACKING_ENABLED: True,
                CONF_WIFI_CLIENT_COUNT_SENSOR: False,
            },
            (PolicyDefinition("61", "Family access"),),
        )

        self.assertEqual(
            "Example phone", result[CONF_TRACKED_CLIENTS][MAC][CONF_FRIENDLY_NAME]
        )
        self.assertEqual(
            ["Home"], result[CONF_TRACKED_CLIENTS][MAC][CONF_ALLOWED_SSIDS]
        )
        self.assertEqual(
            240, result[CONF_PRESENCE_USERS]["person-1"]["user_away_grace_period"]
        )

    def test_rejects_one_device_assigned_to_multiple_people(self) -> None:
        with self.assertRaisesRegex(ValueError, "only one person"):
            _normalize_configuration(
                entry(),
                [{"mac": MAC, "name": "Phone", "allowed_ssids": []}],
                [
                    {"id": "one", "name": "One", "macs": [MAC]},
                    {"id": "two", "name": "Two", "macs": [MAC]},
                ],
                {
                    CONF_WIFI_TRACKING_ENABLED: True,
                    CONF_WIFI_CLIENT_COUNT_SENSOR: False,
                },
                (PolicyDefinition("61", "Family access"),),
            )

    def test_existing_policy_name_guard_is_rechecked_before_save(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": {
                            "policyid": 61,
                            "name": "Unexpected replacement",
                            "status": "enable",
                        },
                    },
                )
            ]
        )
        with (
            patch(
                "custom_components.fortigate_policy.panel.async_get_clientsession",
                return_value=session,
            ),
            self.assertRaises(FortiGateIdentityError),
        ):
            asyncio.run(_validate_policies(SimpleNamespace(), entry(), ["61"]))


class TestPanelRegistration(unittest.TestCase):
    def test_policy_command_returns_only_verified_coordinator_state(self) -> None:
        policy_coordinator = SimpleNamespace(
            data=Policy("61", "Family access", "enable"),
            async_request_refresh=AsyncMock(),
        )

        async def set_status(status: str) -> None:
            policy_coordinator.data = Policy("61", "Family access", status)

        policy_coordinator.async_set_policy_status = AsyncMock(side_effect=set_status)
        panel_entry = entry()
        panel_entry.runtime_data.policy_coordinators["61"] = policy_coordinator
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_entry=lambda entry_id: (
                    panel_entry if entry_id == "entry-1" else None
                )
            )
        )
        connection = SimpleNamespace(send_result=Mock(), send_error=Mock())

        asyncio.run(
            websocket_set_policy_status.__wrapped__.__wrapped__(
                hass,
                connection,
                {
                    "id": 7,
                    "entry_id": "entry-1",
                    "policy_id": "61",
                    "status": "disable",
                },
            )
        )

        policy_coordinator.async_set_policy_status.assert_awaited_once_with("disable")
        connection.send_error.assert_not_called()
        connection.send_result.assert_called_once_with(
            7, {"id": "61", "name": "Family access", "state": "disable"}
        )

    def test_policy_command_failure_never_returns_requested_state(self) -> None:
        policy_coordinator = SimpleNamespace(
            data=Policy("61", "Family access", "enable"),
            async_set_policy_status=AsyncMock(side_effect=FortiGateIdentityError()),
            async_request_refresh=AsyncMock(),
        )
        panel_entry = entry()
        panel_entry.runtime_data.policy_coordinators["61"] = policy_coordinator
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_entry=lambda _entry_id: panel_entry
            )
        )
        connection = SimpleNamespace(send_result=Mock(), send_error=Mock())

        asyncio.run(
            websocket_set_policy_status.__wrapped__.__wrapped__(
                hass,
                connection,
                {
                    "id": 8,
                    "entry_id": "entry-1",
                    "policy_id": "61",
                    "status": "disable",
                },
            )
        )

        policy_coordinator.async_request_refresh.assert_awaited_once()
        connection.send_result.assert_not_called()
        connection.send_error.assert_called_once()

    def test_registers_as_the_integration_config_panel(self) -> None:
        hass = SimpleNamespace(
            http=SimpleNamespace(async_register_static_paths=AsyncMock())
        )
        register_panel = AsyncMock()
        with (
            patch(
                "custom_components.fortigate_policy.panel.websocket_api.async_register_command"
            ) as register_command,
            patch(
                "custom_components.fortigate_policy.panel.async_panel_exists",
                return_value=False,
            ),
            patch(
                "custom_components.fortigate_policy.panel.panel_custom.async_register_panel",
                register_panel,
            ),
        ):
            asyncio.run(async_register_panel(hass))  # type: ignore[arg-type]

        self.assertEqual(5, register_command.call_count)
        hass.http.async_register_static_paths.assert_awaited_once()
        register_panel.assert_awaited_once()
        kwargs = register_panel.await_args.kwargs
        self.assertEqual("fortigate_policy", kwargs["config_panel_domain"])
        self.assertEqual("fortiap-presence", kwargs["frontend_url_path"])
        self.assertTrue(kwargs["require_admin"])


if __name__ == "__main__":
    unittest.main()
