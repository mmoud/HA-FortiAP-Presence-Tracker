"""Persistent Network Device Presence+ inventory tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.fortigate_policy.api import FortiGateConnectionError
from custom_components.fortigate_policy.coordinator import FortiGateWifiCoordinator
from custom_components.fortigate_policy.network_store import (
    MAX_STORED_NETWORK_CLIENTS,
    NetworkDeviceRecord,
    NetworkDeviceStore,
)
from custom_components.fortigate_policy.wifi import NetworkClient

NOW = datetime(2026, 8, 24, 18, 43, tzinfo=UTC)
MAC = "aa:bb:cc:dd:ee:ff"


class FakeStore:
    """Capture delayed saves without requiring a full Home Assistant instance."""

    def __init__(self) -> None:
        self.saved = None

    def async_delay_save(self, data_func, _delay: int) -> None:
        self.saved = data_func()


class FakeApi:
    def __init__(self, clients=None, error: Exception | None = None) -> None:
        self.clients = clients or {}
        self.error = error

    async def async_get_wifi_clients(self):
        if self.error:
            raise self.error
        return self.clients, 0, "v7.6.2"


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    def async_fire(self, event_type, data) -> None:
        self.events.append((event_type, data))


def inventory(*, initialized: bool = False) -> NetworkDeviceStore:
    store = NetworkDeviceStore.__new__(NetworkDeviceStore)
    store._store = FakeStore()  # type: ignore[attr-defined]
    store.records = {}
    store.initialized = initialized
    return store


class TestNetworkDeviceStore(unittest.TestCase):
    def test_first_poll_is_silent_baseline_and_persists_metadata(self) -> None:
        store = inventory()
        new = store.process_clients(
            {
                MAC: NetworkClient(
                    mac=MAC,
                    ip="192.168.10.45",
                    hostname="Phone",
                    ap_name="Upstairs",
                    manufacturer="Apple, Inc.",
                )
            },
            NOW,
            tracked_names={MAC: "Family phone"},
            owners={MAC: "Family member"},
            retention_days=30,
        )

        self.assertEqual([], new)
        record = store.records[MAC]
        self.assertEqual(NOW, record.first_seen)
        self.assertEqual(NOW, record.last_seen)
        self.assertEqual(NOW, record.connected_since)
        self.assertEqual("Family phone", record.friendly_name)
        self.assertEqual("Family member", record.owner)
        self.assertEqual("Upstairs", record.metadata["ap_name"])
        self.assertEqual("Apple, Inc.", record.metadata["manufacturer"])

    def test_new_client_after_baseline_is_reported_once(self) -> None:
        store = inventory(initialized=True)
        first = store.process_clients(
            {MAC: NetworkClient(mac=MAC)},
            NOW,
            tracked_names={},
            owners={},
            retention_days=30,
        )
        store.mark_announced(MAC)
        second = store.process_clients(
            {MAC: NetworkClient(mac=MAC)},
            NOW + timedelta(seconds=30),
            tracked_names={},
            owners={},
            retention_days=30,
        )

        self.assertEqual([MAC], [record.mac for record in first])
        self.assertEqual([], second)
        self.assertTrue(store.records[MAC].announced)

    def test_first_seen_survives_updates_ap_roaming_and_ip_change(self) -> None:
        store = inventory(initialized=True)
        store.process_clients(
            {MAC: NetworkClient(mac=MAC, ip="192.168.1.10", ap_name="AP-1")},
            NOW,
            tracked_names={},
            owners={},
            retention_days=30,
        )
        store.process_clients(
            {MAC: NetworkClient(mac=MAC, ip="192.168.1.99", ap_name="AP-2")},
            NOW + timedelta(minutes=2),
            tracked_names={},
            owners={},
            retention_days=30,
        )

        record = store.records[MAC]
        self.assertEqual(NOW, record.first_seen)
        self.assertEqual("192.168.1.99", record.metadata["ip"])
        self.assertEqual("AP-2", record.metadata["ap_name"])

    def test_friendly_name_is_not_replaced_by_hostname(self) -> None:
        store = inventory(initialized=True)
        store.process_clients(
            {MAC: NetworkClient(mac=MAC, hostname="DESKTOP-FG389AJ")},
            NOW,
            tracked_names={MAC: "Basement Gaming PC"},
            owners={},
            retention_days=30,
        )
        store.process_clients(
            {MAC: NetworkClient(mac=MAC, hostname="NEW-DHCP-NAME")},
            NOW + timedelta(minutes=1),
            tracked_names={MAC: "Basement Gaming PC"},
            owners={},
            retention_days=30,
        )

        self.assertEqual("Basement Gaming PC", store.records[MAC].friendly_name)
        self.assertEqual("NEW-DHCP-NAME", store.records[MAC].metadata["hostname"])

    def test_record_round_trip_preserves_restart_state(self) -> None:
        original = NetworkDeviceRecord(
            mac=MAC,
            first_seen=NOW - timedelta(days=4),
            last_seen=NOW,
            connected_since=NOW - timedelta(hours=2),
            connected=True,
            announced=True,
            metadata={"ssid": "Home"},
            friendly_name="Phone",
            owner="Owner",
        )
        restored = NetworkDeviceRecord.from_dict(MAC, original.as_dict())

        self.assertEqual(original, restored)

    def test_hundreds_of_clients_are_bounded_without_dropping_tracked(self) -> None:
        store = inventory(initialized=True)
        tracked = "02:00:00:00:00:00"
        clients = {
            f"02:00:00:{index // 65536:02x}:{index // 256 % 256:02x}:{index % 256:02x}": NetworkClient(
                mac=f"02:00:00:{index // 65536:02x}:{index // 256 % 256:02x}:{index % 256:02x}"
            )
            for index in range(MAX_STORED_NETWORK_CLIENTS + 100)
        }
        store.process_clients(
            clients,
            NOW,
            tracked_names={tracked: "Tracked"},
            owners={},
            retention_days=30,
        )

        self.assertLessEqual(len(store.records), MAX_STORED_NETWORK_CLIENTS)
        self.assertIn(tracked, store.records)

    def test_confirmed_away_clears_connection_but_keeps_history(self) -> None:
        store = inventory(initialized=True)
        store.process_clients(
            {MAC: NetworkClient(mac=MAC)},
            NOW,
            tracked_names={},
            owners={},
            retention_days=30,
        )
        store.mark_away(MAC)

        self.assertFalse(store.records[MAC].connected)
        self.assertIsNone(store.records[MAC].connected_since)
        self.assertEqual(NOW, store.records[MAC].last_seen)


class TestNetworkCoordinator(unittest.IsolatedAsyncioTestCase):
    def coordinator(self, api: FakeApi, store: NetworkDeviceStore):
        coordinator = SimpleNamespace(
            api=api,
            _system_status_checked=False,
            system_status_endpoint_supported=None,
            fortios_version=None,
            fortios_version_source=None,
            data=None,
            _restored_presence={},
            network_store=store,
            _tracked_names={},
            _owners={},
            _retention_days=30,
            _detect_new_devices=True,
            _tracked_macs=set(),
            _ssid_filters={},
            _grace_period=timedelta(seconds=300),
            last_successful_update=None,
            hass=SimpleNamespace(bus=FakeBus()),
            config_entry=SimpleNamespace(entry_id="entry-1"),
        )
        return coordinator

    async def test_new_device_event_fires_once_after_baseline(self) -> None:
        store = inventory(initialized=True)
        coordinator = self.coordinator(
            FakeApi({MAC: NetworkClient(mac=MAC, ssid="Home", ap_name="AP-1")}),
            store,
        )
        with patch(
            "custom_components.fortigate_policy.coordinator.utcnow", return_value=NOW
        ):
            first = await FortiGateWifiCoordinator._async_update_data(coordinator)
        coordinator.data = first
        with patch(
            "custom_components.fortigate_policy.coordinator.utcnow",
            return_value=NOW + timedelta(seconds=30),
        ):
            await FortiGateWifiCoordinator._async_update_data(coordinator)

        self.assertEqual(1, len(coordinator.hass.bus.events))
        event_type, event = coordinator.hass.bus.events[0]
        self.assertEqual("fortigate_new_network_device", event_type)
        self.assertEqual(MAC, event["mac"])
        self.assertEqual("entry-1", event["fortigate_entry_id"])

    async def test_api_failure_does_not_mutate_presence_or_inventory(self) -> None:
        store = inventory(initialized=True)
        store.process_clients(
            {MAC: NetworkClient(mac=MAC)},
            NOW,
            tracked_names={},
            owners={},
            retention_days=30,
        )
        before = store.records[MAC].as_dict()
        coordinator = self.coordinator(
            FakeApi(error=FortiGateConnectionError("offline")), store
        )

        with self.assertRaises(UpdateFailed):
            await FortiGateWifiCoordinator._async_update_data(coordinator)

        self.assertEqual(before, store.records[MAC].as_dict())
        self.assertEqual([], coordinator.hass.bus.events)


if __name__ == "__main__":
    unittest.main()
