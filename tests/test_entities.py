"""Entity behavior tests using Home Assistant's entity base classes."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.fortigate_policy import (
    _cleanup_stale_wifi_registry_entries,
    async_migrate_entry,
    async_setup_entry,
    tracked_ssid_filters_from_options,
)
from custom_components.fortigate_policy.api import Policy
from custom_components.fortigate_policy.binary_sensor import (
    FortiGatePresenceUserBinarySensor,
    FortiGateWifiPresenceBinarySensor,
)
from custom_components.fortigate_policy.button import FortiGateRefreshButton
from custom_components.fortigate_policy.config_flow import (
    FortiGatePolicyOptionsFlow,
    _async_validate_input,
    _entry_data,
    _normalize,
    _options_hub_summary,
    _people_overview,
    _policy_options_schema,
    _preserved_client_names,
    _selected_wifi_macs,
)
from custom_components.fortigate_policy.const import (
    CONF_ALLOWED_SSIDS,
    CONF_API_TOKEN,
    CONF_FRIENDLY_NAME,
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_NETWORK_CREATE_TRACKER_ENTITIES,
    CONF_NETWORK_NEW_DEVICE_DETECTION,
    CONF_NETWORK_TRACK_FORTIAP_CLIENTS,
    CONF_POLICIES,
    CONF_POLICY_IDS,
    CONF_PRESENCE_USER_MACS,
    CONF_PRESENCE_USER_NAME,
    CONF_PRESENCE_USERS,
    CONF_TRACKED_CLIENTS,
    CONF_USER_AWAY_GRACE_PERIOD,
    CONF_VDOM,
    CONF_VERIFY_SSL,
)
from custom_components.fortigate_policy.presence_users import PresenceUser
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

    def test_user_sensor_aggregates_multiple_grace_aware_devices(self) -> None:
        second_mac = "11:22:33:44:55:66"
        coordinator = FakeWifiCoordinator(None)
        coordinator.data = SimpleNamespace(
            presence={
                MAC: WifiPresence(False, None, NOW, None),
                second_mac: WifiPresence(True, None, NOW, None),
            }
        )
        user = PresenceUser(
            "stable-user-id",
            "Example user",
            frozenset({MAC, second_mac}),
        )
        entity = FortiGatePresenceUserBinarySensor(
            SimpleNamespace(entry_id="entry-1"),  # type: ignore[arg-type]
            coordinator,  # type: ignore[arg-type]
            user,
        )

        self.assertTrue(entity.is_on)
        coordinator.data.presence[second_mac] = WifiPresence(False, None, NOW, None)
        self.assertFalse(entity.is_on)
        coordinator.data.presence[second_mac] = WifiPresence(None, None, None, None)
        self.assertIsNone(entity.is_on)
        self.assertEqual(
            "entry-1_presence_user_stable-user-id_presence", entity.unique_id
        )


class TestWifiTrackerOptions(unittest.TestCase):
    def test_discovered_and_manual_macs_are_normalized_and_deduplicated(self) -> None:
        self.assertEqual(
            ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
            _selected_wifi_macs(
                ["AA-BB-CC-DD-EE-FF"],
                "aabbccddeeff, 11:22:33:44:55:66; invalid",
            ),
        )

    def test_removed_tracker_entities_and_device_are_deleted_from_registries(
        self,
    ) -> None:
        current_compact = MAC.replace(":", "")
        removed_compact = "112233445566"
        entity_registry = SimpleNamespace(
            removed=[],
            async_remove=lambda entity_id: entity_registry.removed.append(entity_id),
        )
        device_registry = SimpleNamespace(
            removed=[],
            async_remove_device=lambda device_id: device_registry.removed.append(
                device_id
            ),
        )
        entities = [
            SimpleNamespace(
                entity_id="device_tracker.current",
                platform="fortigate_policy",
                unique_id=f"entry-1_wifi_{current_compact}",
            ),
            SimpleNamespace(
                entity_id="device_tracker.removed",
                platform="fortigate_policy",
                unique_id=f"entry-1_wifi_{removed_compact}",
            ),
            SimpleNamespace(
                entity_id="binary_sensor.removed_presence",
                platform="fortigate_policy",
                unique_id=f"entry-1_wifi_{removed_compact}_presence",
            ),
            SimpleNamespace(
                entity_id="switch.policy",
                platform="fortigate_policy",
                unique_id="entry-1",
            ),
        ]
        devices = [
            SimpleNamespace(
                id="current-device",
                identifiers={("fortigate_policy", f"entry-1_wifi_{current_compact}")},
            ),
            SimpleNamespace(
                id="removed-device",
                identifiers={("fortigate_policy", f"entry-1_wifi_{removed_compact}")},
            ),
            SimpleNamespace(
                id="fortigate-device",
                identifiers={("fortigate_policy", "entry-1")},
            ),
        ]

        with (
            patch(
                "custom_components.fortigate_policy.er.async_get",
                return_value=entity_registry,
            ),
            patch(
                "custom_components.fortigate_policy.er.async_entries_for_config_entry",
                return_value=entities,
            ),
            patch(
                "custom_components.fortigate_policy.dr.async_get",
                return_value=device_registry,
            ),
            patch(
                "custom_components.fortigate_policy.dr.async_entries_for_config_entry",
                return_value=devices,
            ),
        ):
            _cleanup_stale_wifi_registry_entries(
                SimpleNamespace(),
                "entry-1",
                {MAC},  # type: ignore[arg-type]
            )

        self.assertEqual(
            ["device_tracker.removed", "binary_sensor.removed_presence"],
            entity_registry.removed,
        )
        self.assertEqual(["removed-device"], device_registry.removed)

    def test_existing_names_are_preserved_only_for_selected_trackers(self) -> None:
        self.assertEqual(
            {
                "aa:bb:cc:dd:ee:ff": {
                    CONF_FRIENDLY_NAME: "Example phone",
                    CONF_ALLOWED_SSIDS: ["Home", "IoT"],
                }
            },
            _preserved_client_names(
                ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
                {
                    "AA-BB-CC-DD-EE-FF": {
                        CONF_FRIENDLY_NAME: " Example phone ",
                        CONF_ALLOWED_SSIDS: ["IoT", "Home", "Home"],
                    },
                    "22:33:44:55:66:77": {CONF_FRIENDLY_NAME: "Removed"},
                },
            ),
        )

    def test_ssid_filters_are_normalized_by_mac_and_ignore_bad_values(self) -> None:
        self.assertEqual(
            {MAC: frozenset({"Home", "IoT"})},
            tracked_ssid_filters_from_options(
                {
                    CONF_TRACKED_CLIENTS: {
                        "AA-BB-CC-DD-EE-FF": {CONF_ALLOWED_SSIDS: ["Home", "IoT", 123]},
                        "invalid": {CONF_ALLOWED_SSIDS: ["Guest"]},
                    }
                }
            ),
        )


class TestPolicyOptions(unittest.TestCase):
    @staticmethod
    def _options_flow(entry: SimpleNamespace) -> FortiGatePolicyOptionsFlow:
        flow = FortiGatePolicyOptionsFlow()
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_known_entry=lambda _entry_id: entry
            )
        )
        flow.handler = "entry-1"
        return flow

    def test_configuration_hub_summarizes_setup_without_sensitive_data(self) -> None:
        summary = _options_hub_summary(
            {
                CONF_POLICIES: [
                    {"policy_id": "61", "policy_name": "Family access"},
                    {"policy_id": "72", "policy_name": "Guest access"},
                ],
                CONF_API_TOKEN: "must-not-appear",
            },
            {
                CONF_TRACKED_CLIENTS: {MAC: {}, "11:22:33:44:55:66": {}},
                CONF_PRESENCE_USERS: {"person-1": {}},
            },
        )

        self.assertEqual(
            {
                "tracked": "2",
                "users": "1",
                "policies": "2",
            },
            summary,
        )
        self.assertNotIn("must-not-appear", str(summary))

    def test_configuration_hub_uses_task_centered_menu(self) -> None:
        flow = self._options_flow(SimpleNamespace(data={CONF_POLICIES: []}, options={}))

        result = asyncio.run(flow.async_step_init())

        self.assertEqual(
            [
                "people_devices",
                "firewall_policies",
                "advanced_settings",
            ],
            result["menu_options"],
        )

    def test_people_overview_lists_people_and_friendly_device_names(self) -> None:
        second_mac = "11:22:33:44:55:66"
        overview = _people_overview(
            {
                CONF_TRACKED_CLIENTS: {
                    MAC: {CONF_FRIENDLY_NAME: "Phone"},
                    second_mac: {CONF_FRIENDLY_NAME: "Watch"},
                },
                CONF_PRESENCE_USERS: {
                    "person-1": {
                        CONF_PRESENCE_USER_NAME: "Example person",
                        CONF_PRESENCE_USER_MACS: [MAC, second_mac],
                        CONF_USER_AWAY_GRACE_PERIOD: 240,
                    }
                },
            }
        )

        self.assertEqual("1", overview["count"])
        self.assertEqual(
            "• Example person: Phone, Watch · Away grace 240s",
            overview["people"],
        )

    def test_people_and_devices_page_includes_read_only_overview(self) -> None:
        flow = self._options_flow(
            SimpleNamespace(
                data={CONF_POLICIES: []},
                options={
                    CONF_TRACKED_CLIENTS: {MAC: {CONF_FRIENDLY_NAME: "Phone"}},
                    CONF_PRESENCE_USERS: {
                        "person-1": {
                            CONF_PRESENCE_USER_NAME: "Example person",
                            CONF_PRESENCE_USER_MACS: [MAC],
                        }
                    },
                },
            )
        )

        result = asyncio.run(flow.async_step_people_devices())

        self.assertEqual(
            [
                "wifi_clients",
                "wifi_tracker_filters",
                "presence_users",
                "remove_wifi_trackers",
            ],
            result["menu_options"],
        )
        self.assertEqual(
            "• Example person: Phone · Away grace 180s",
            result["description_placeholders"]["people"],
        )

    def test_wifi_tracker_filter_is_saved_without_changing_unique_identity(
        self,
    ) -> None:
        entry = SimpleNamespace(
            data={CONF_POLICIES: []},
            options={
                CONF_TRACKED_CLIENTS: {MAC: {CONF_FRIENDLY_NAME: "Phone"}},
                "recent_wifi_clients": {MAC: {"ssid": "Home"}},
            },
        )
        flow = self._options_flow(entry)

        chooser = asyncio.run(flow.async_step_wifi_tracker_filters())
        self.assertEqual("wifi_tracker_filters", chooser["step_id"])
        form = asyncio.run(
            flow.async_step_wifi_tracker_filters({flow._FILTER_TRACKER: MAC})
        )
        self.assertEqual("wifi_tracker_filter", form["step_id"])
        result = asyncio.run(
            flow.async_step_wifi_tracker_filter({CONF_ALLOWED_SSIDS: ["Home"]})
        )

        self.assertEqual(
            ["Home"],
            result["data"][CONF_TRACKED_CLIENTS][MAC][CONF_ALLOWED_SSIDS],
        )

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
            entry_id="entry-1",
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

        with (
            patch(
                "custom_components.fortigate_policy.async_get_clientsession",
                return_value=FakeSession([]),
            ),
            patch(
                "custom_components.fortigate_policy._cleanup_stale_wifi_registry_entries"
            ),
        ):
            result = asyncio.run(async_setup_entry(hass, entry))  # type: ignore[arg-type]

        self.assertTrue(result)
        self.assertEqual({}, entry.runtime_data.policy_coordinators)
        self.assertIsNone(entry.runtime_data.wifi_coordinator)
        self.assertTrue(config_entries.forwarded)

    def test_version_six_migration_preserves_old_away_default(self) -> None:
        updates = []
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda _entry, **values: updates.append(values)
            )
        )
        entry = SimpleNamespace(version=6, data={CONF_POLICIES: []}, options={})

        self.assertTrue(asyncio.run(async_migrate_entry(hass, entry)))
        migrated = updates[-1]
        self.assertEqual(8, migrated["version"])
        self.assertEqual(180, migrated["options"]["wifi_away_grace_period"])
        self.assertTrue(migrated["options"][CONF_NETWORK_TRACK_FORTIAP_CLIENTS])
        self.assertTrue(migrated["options"][CONF_NETWORK_CREATE_TRACKER_ENTITIES])
        self.assertTrue(migrated["options"][CONF_NETWORK_NEW_DEVICE_DETECTION])

    def test_version_eight_migration_removes_internal_policy_automation(self) -> None:
        updates = []
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda _entry, **values: updates.append(values)
            )
        )
        entry = SimpleNamespace(
            version=7,
            data={CONF_POLICIES: [{"policy_id": "61", "policy_name": "Access"}]},
            options={
                CONF_TRACKED_CLIENTS: {MAC: {CONF_FRIENDLY_NAME: "Phone"}},
                CONF_PRESENCE_USERS: {
                    "person-1": {
                        CONF_PRESENCE_USER_NAME: "Example person",
                        CONF_PRESENCE_USER_MACS: [MAC],
                    }
                },
                "policy_rules_v2": {"old-rule": {}},
                "policy_automation_enabled": True,
                "policy_automation_dry_run": True,
                "default_override_minutes": 60,
            },
        )

        self.assertTrue(asyncio.run(async_migrate_entry(hass, entry)))
        migrated = updates[-1]
        self.assertEqual(8, migrated["version"])
        self.assertEqual(
            [MAC],
            migrated["options"][CONF_PRESENCE_USERS]["person-1"][
                CONF_PRESENCE_USER_MACS
            ],
        )
        self.assertNotIn("policy_rules_v2", migrated["options"])
        self.assertNotIn("policy_automation_enabled", migrated["options"])
        self.assertNotIn("policy_automation_dry_run", migrated["options"])
        self.assertNotIn("default_override_minutes", migrated["options"])


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
