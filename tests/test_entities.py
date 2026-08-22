"""Entity behavior tests using Home Assistant's entity base classes."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.fortigate_policy import async_setup_entry
from custom_components.fortigate_policy.api import Policy
from custom_components.fortigate_policy.binary_sensor import (
    FortiGateWifiPresenceBinarySensor,
)
from custom_components.fortigate_policy.button import FortiGateRefreshButton
from custom_components.fortigate_policy.config_flow import (
    _async_validate_input,
    _entry_data,
    _normalize,
    _policy_options_schema,
    _preserved_client_names,
    _selected_wifi_macs,
)
from custom_components.fortigate_policy.const import (
    CONF_API_TOKEN,
    CONF_FRIENDLY_NAME,
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_POLICIES,
    CONF_POLICY_IDS,
    CONF_VDOM,
    CONF_VERIFY_SSL,
)
from custom_components.fortigate_policy.switch import FortiGatePolicySwitch
from custom_components.fortigate_policy.wifi import WifiPresence
from tests.test_api import FakeResponse, FakeSession

MAC = "aa:bb:cc:dd:ee:ff"
NOW = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)


class FakeWifiCoordinator:
    def __init__(self, presence: WifiPresence | None) -> None:
        self.last_update_success = True
        self._presence = presence

    def presence_for(self, _mac: str) -> WifiPresence | None:
        return self._presence


class FakePolicyCoordinator:
    def __init__(self, policy: Policy) -> None:
        self.data = policy
        self.last_update_success = True
        self.last_successful_check = NOW


class FakeRefreshCoordinator:
    def __init__(self) -> None:
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class FakeConfigEntries:
    def __init__(self) -> None:
        self.forwarded = False

    async def async_forward_entry_setups(self, _entry, _platforms) -> None:
        self.forwarded = True


class TestPresenceBinarySensor(unittest.TestCase):
    def test_presence_maps_home_away_unknown_and_availability(self) -> None:
        entry = SimpleNamespace(entry_id="entry-1")
        coordinator = FakeWifiCoordinator(WifiPresence(True, None, NOW, None))
        entity = FortiGateWifiPresenceBinarySensor(
            entry,  # type: ignore[arg-type]
            coordinator,  # type: ignore[arg-type]
            MAC,
            "Example phone",
        )

        self.assertTrue(entity.is_on)
        self.assertTrue(entity.available)
        self.assertEqual("entry-1_wifi_aabbccddeeff_presence", entity.unique_id)

        coordinator._presence = WifiPresence(False, None, NOW, None)
        self.assertFalse(entity.is_on)
        coordinator._presence = None
        self.assertIsNone(entity.is_on)
        coordinator.last_update_success = False
        self.assertFalse(entity.available)


class TestWifiTrackerOptions(unittest.TestCase):
    def test_discovered_and_manual_macs_are_normalized_and_deduplicated(self) -> None:
        self.assertEqual(
            ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
            _selected_wifi_macs(
                ["AA-BB-CC-DD-EE-FF"],
                "aabbccddeeff, 11:22:33:44:55:66; invalid",
            ),
        )

    def test_existing_names_are_preserved_only_for_selected_trackers(self) -> None:
        self.assertEqual(
            {"aa:bb:cc:dd:ee:ff": {CONF_FRIENDLY_NAME: "Example phone"}},
            _preserved_client_names(
                ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
                {
                    "AA-BB-CC-DD-EE-FF": {CONF_FRIENDLY_NAME: " Example phone "},
                    "22:33:44:55:66:77": {CONF_FRIENDLY_NAME: "Removed"},
                },
            ),
        )


class TestPolicyOptions(unittest.TestCase):
    def test_policy_form_defaults_to_all_configured_ids(self) -> None:
        schema = _policy_options_schema(
            {
                CONF_POLICIES: [
                    {"policy_id": "61", "policy_name": "Family access"},
                    {"policy_id": "72", "policy_name": "Guest access"},
                ]
            }
        )

        self.assertEqual("61, 72", schema({})[CONF_POLICY_IDS])

    def test_tracker_only_entry_sets_up_without_policy_coordinator(self) -> None:
        config_entries = FakeConfigEntries()
        hass = SimpleNamespace(config_entries=config_entries)
        entry = SimpleNamespace(
            data={
                CONF_HOST: "fortigate.example.test",
                CONF_PORT: 443,
                CONF_VDOM: "root",
                CONF_POLICIES: [],
                CONF_API_TOKEN: "test-token",
                CONF_VERIFY_SSL: True,
            },
            options={},
        )

        with patch(
            "custom_components.fortigate_policy.async_get_clientsession",
            return_value=FakeSession([]),
        ):
            result = asyncio.run(async_setup_entry(hass, entry))  # type: ignore[arg-type]

        self.assertTrue(result)
        self.assertEqual({}, entry.runtime_data.policy_coordinators)
        self.assertIsNone(entry.runtime_data.wifi_coordinator)
        self.assertTrue(config_entries.forwarded)


class TestPolicySwitchEntities(unittest.TestCase):
    def test_each_policy_has_independent_state_and_stable_unique_id(self) -> None:
        entry = SimpleNamespace(
            entry_id="entry-1",
            title="FortiGate",
            data={
                CONF_HOST: "fortigate.example.test",
                CONF_VDOM: "root",
                CONF_LEGACY_PRIMARY_POLICY_ID: "61",
            },
        )
        primary = FortiGatePolicySwitch(
            entry,  # type: ignore[arg-type]
            FakePolicyCoordinator(  # type: ignore[arg-type]
                Policy("61", "Family access", "enable")
            ),
            "61",
        )
        secondary = FortiGatePolicySwitch(
            entry,  # type: ignore[arg-type]
            FakePolicyCoordinator(  # type: ignore[arg-type]
                Policy("72", "Guest access", "disable")
            ),
            "72",
        )

        self.assertTrue(primary.is_on)
        self.assertFalse(secondary.is_on)
        self.assertEqual("entry-1", primary.unique_id)
        self.assertEqual("entry-1_policy_72", secondary.unique_id)
        self.assertEqual("61", primary.extra_state_attributes["policy_id"])
        self.assertEqual("72", secondary.extra_state_attributes["policy_id"])


class TestRefreshButton(unittest.TestCase):
    def test_refreshes_every_coordinator_without_writes(self) -> None:
        policy_a = FakeRefreshCoordinator()
        policy_b = FakeRefreshCoordinator()
        wifi = FakeRefreshCoordinator()
        entry = SimpleNamespace(
            entry_id="entry-1",
            runtime_data=SimpleNamespace(
                policy_coordinators={"61": policy_a, "72": policy_b},
                wifi_coordinator=wifi,
            ),
        )
        button = FortiGateRefreshButton(entry)  # type: ignore[arg-type]

        asyncio.run(button.async_press())

        self.assertEqual(1, policy_a.refreshes)
        self.assertEqual(1, policy_b.refreshes)
        self.assertEqual(1, wifi.refreshes)
        self.assertEqual("entry-1_refresh_data", button.unique_id)


class TestMultiPolicyValidation(unittest.TestCase):
    def test_tracker_only_setup_validates_wifi_monitor(self) -> None:
        session = FakeSession([FakeResponse(200, {"status": "success", "results": []})])
        normalized = _normalize(
            {
                CONF_HOST: "fortigate.example.test",
                CONF_PORT: 443,
                CONF_VDOM: "root",
                CONF_POLICY_IDS: "",
                CONF_API_TOKEN: "test-token",
                CONF_VERIFY_SSL: True,
            }
        )
        with patch(
            "custom_components.fortigate_policy.config_flow.async_get_clientsession",
            return_value=session,
        ):
            policies, title = asyncio.run(
                _async_validate_input(SimpleNamespace(), normalized)  # type: ignore[arg-type]
            )

        self.assertEqual((), policies)
        self.assertEqual("FortiGate fortigate.example.test", title)
        self.assertEqual("/api/v2/monitor/wifi/client", session.requests[0][1].path)

    def test_every_requested_policy_is_read_before_saving(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": {
                            "policyid": 61,
                            "name": "Family access",
                            "status": "enable",
                        },
                    },
                ),
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": {
                            "policyid": 72,
                            "name": "Guest access",
                            "status": "disable",
                        },
                    },
                ),
            ]
        )
        normalized = _normalize(
            {
                CONF_HOST: "fortigate.example.test",
                CONF_PORT: 443,
                CONF_VDOM: "root",
                CONF_POLICY_IDS: "61, 72",
                CONF_API_TOKEN: "test-token",
                CONF_VERIFY_SSL: True,
            }
        )
        with patch(
            "custom_components.fortigate_policy.config_flow.async_get_clientsession",
            return_value=session,
        ):
            policies, title = asyncio.run(
                _async_validate_input(SimpleNamespace(), normalized)  # type: ignore[arg-type]
            )

        self.assertEqual("FortiGate fortigate.example.test", title)
        self.assertEqual(("61", "72"), tuple(p.policy_id for p in policies))
        self.assertEqual(
            ("Family access", "Guest access"),
            tuple(p.expected_name for p in policies),
        )
        self.assertEqual(
            [
                "/api/v2/cmdb/firewall/policy/61",
                "/api/v2/cmdb/firewall/policy/72",
            ],
            [request[1].path for request in session.requests],
        )
        saved = _entry_data(normalized, policies)
        self.assertNotIn(CONF_POLICY_IDS, saved)
        self.assertEqual(2, len(saved[CONF_POLICIES]))


if __name__ == "__main__":
    unittest.main()
