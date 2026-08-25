"""Dependency-free tests for FortiOS Wi-Fi normalization and presence safety."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).parents[1] / "custom_components" / "fortigate_policy"),
)

from wifi import (
    FortiGateClientIdentity,
    FortiGateWifiClient,
    WifiPresence,
    advance_presence,
    client_matches_ssid_filter,
    enrich_wifi_clients,
    normalize_mac,
    parse_client_identities,
    parse_fortios_version,
    parse_wifi_clients,
)

NOW = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)
MAC = "aa:bb:cc:dd:ee:ff"
GRACE = timedelta(seconds=180)


def client(**overrides: object) -> FortiGateWifiClient:
    """Return one known client with optional association changes."""
    values = {"mac": MAC, "hostname": "Example Phone", "ap_name": "AP-1"}
    values.update(overrides)
    return FortiGateWifiClient(**values)  # type: ignore[arg-type]


class TestFortiGateWifiParser(unittest.TestCase):
    """FortiOS monitor response parsing behavior."""

    def test_normalizes_documented_client_fields(self) -> None:
        clients, skipped, version = parse_wifi_clients(
            {
                "status": "success",
                "version": "v7.0.2",
                "results": [
                    {
                        "sta_mac": "AA-BB-CC-DD-EE-FF",
                        "sta_ip": "192.168.20.10",
                        "hostname": "Example-Phone",
                        "vap_name": "Home-WiFi",
                        "wtp_name": "Upstairs-AP",
                        "wtp_id": "FP431TEST",
                        "wtp_radio": "2",
                        "channel": "149",
                        "sta_snr": "41 dB",
                        "interface_name": "Home-VLAN",
                        "vendor": "Example vendor",
                    }
                ],
            },
            "root",
        )
        parsed = clients[MAC]
        self.assertEqual(0, skipped)
        self.assertEqual("v7.0.2", version)
        self.assertEqual("192.168.20.10", parsed.ip)
        self.assertEqual("Home-WiFi", parsed.ssid)
        self.assertEqual("Upstairs-AP", parsed.ap_name)
        self.assertEqual("FP431TEST", parsed.ap_serial)
        self.assertEqual(2, parsed.radio)
        self.assertEqual(149, parsed.channel)
        self.assertEqual(41, parsed.snr)
        self.assertEqual("Home-VLAN", parsed.interface)
        self.assertEqual("Example vendor", parsed.manufacturer)
        self.assertEqual("wifi", parsed.connection_type)
        self.assertEqual("fortiap", parsed.source)

    def test_multiple_and_missing_optional_fields(self) -> None:
        clients, skipped, _ = parse_wifi_clients(
            {
                "status": "success",
                "results": [
                    {"mac": "aabbccddeeff"},
                    {"mac": "11:22:33:44:55:66", "ip": "192.168.1.9"},
                    {"hostname": "malformed-without-mac"},
                ],
            },
            "root",
        )
        self.assertEqual(2, len(clients))
        self.assertEqual(1, skipped)
        self.assertIsNone(clients[MAC].hostname)
        self.assertEqual("192.168.1.9", clients["11:22:33:44:55:66"].ip)

    def test_empty_success_is_valid_and_unknown_shape_is_not(self) -> None:
        clients, skipped, _ = parse_wifi_clients(
            {"status": "success", "results": []}, "root"
        )
        self.assertEqual({}, clients)
        self.assertEqual(0, skipped)
        with self.assertRaises(ValueError):
            parse_wifi_clients({"status": "success", "results": {}}, "root")

    def test_mac_normalization(self) -> None:
        for value in ("AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabbccddeeff"):
            self.assertEqual(MAC, normalize_mac(value))
        self.assertIsNone(normalize_mac("not-a-mac"))

    def test_system_status_version_parsing_is_defensive(self) -> None:
        self.assertEqual(
            "v7.4.8",
            parse_fortios_version(
                {"status": "success", "results": {"version": "v7.4.8"}}
            ),
        )
        self.assertEqual(
            "v7.6.1", parse_fortios_version({"status": "success", "version": "v7.6.1"})
        )
        with self.assertRaises(ValueError):
            parse_fortios_version({"status": "error", "results": {}})

    def test_ssid_filter_is_exact_and_empty_means_any_managed_ssid(self) -> None:
        associated = client(ssid="Home")
        self.assertIs(associated, client_matches_ssid_filter(associated, frozenset()))
        self.assertIs(
            associated,
            client_matches_ssid_filter(associated, frozenset({"Home", "IoT"})),
        )
        self.assertIsNone(client_matches_ssid_filter(associated, frozenset({"home"})))
        self.assertIsNone(client_matches_ssid_filter(None, frozenset({"Home"})))

    def test_detected_device_and_dhcp_identity_enrichment(self) -> None:
        detected = parse_client_identities(
            {
                "status": "success",
                "results": {
                    "devices": [
                        {
                            "mac_address": "AA-BB-CC-DD-EE-FF",
                            "hostname": "Example iPhone",
                            "ip_address": "192.168.20.10",
                        }
                    ]
                },
            },
            "detected_device",
        )
        dhcp = parse_client_identities(
            {
                "status": "success",
                "results": [{"hwaddr": "aabbccddeeff", "host": "dhcp-phone"}],
            },
            "dhcp",
        )
        clients = {MAC: FortiGateWifiClient(mac=MAC)}
        clients = enrich_wifi_clients(clients, detected)
        clients = enrich_wifi_clients(clients, dhcp)

        self.assertEqual("Example iPhone", clients[MAC].hostname)
        self.assertEqual("192.168.20.10", clients[MAC].ip)
        self.assertEqual("detected_device", detected[MAC].source)

        reservations = parse_client_identities(
            {
                "status": "success",
                "results": [
                    {
                        "reserved-address": [
                            {
                                "mac": "AA:BB:CC:DD:EE:FF",
                                "description": "Friendly reservation",
                            }
                        ]
                    }
                ],
            },
            "dhcp_reservation",
        )
        clients = enrich_wifi_clients(clients, reservations, prefer_identity_name=True)
        self.assertEqual("Friendly reservation", clients[MAC].hostname)

    def test_identity_enrichment_never_creates_unassociated_client(self) -> None:
        clients = {MAC: FortiGateWifiClient(mac=MAC)}
        identities = {
            "11:22:33:44:55:66": FortiGateClientIdentity(
                mac="11:22:33:44:55:66", hostname="Unassociated"
            )
        }

        self.assertEqual(clients, enrich_wifi_clients(clients, identities))


class TestConservativePresence(unittest.TestCase):
    """Arrival, roaming, grace period, and outage invariants."""

    def test_arrival_is_immediate(self) -> None:
        state = advance_presence(None, client(), NOW, GRACE)
        self.assertTrue(state.is_connected)
        self.assertIsNone(state.missing_since)

    def test_first_absence_is_unknown_then_away_after_grace(self) -> None:
        first = advance_presence(None, None, NOW, GRACE)
        self.assertIsNone(first.is_connected)
        during_grace = advance_presence(
            first, None, NOW + timedelta(seconds=179), GRACE
        )
        self.assertIsNone(during_grace.is_connected)
        away = advance_presence(first, None, NOW + GRACE, GRACE)
        self.assertFalse(away.is_connected)

    def test_one_missed_poll_does_not_mark_a_home_device_away(self) -> None:
        home = advance_presence(None, client(), NOW, GRACE)
        missing = advance_presence(home, None, NOW + timedelta(seconds=30), GRACE)
        self.assertTrue(missing.is_connected)
        self.assertEqual(NOW + timedelta(seconds=30), missing.missing_since)

    def test_grace_expiry_marks_not_home(self) -> None:
        home = advance_presence(None, client(), NOW, GRACE)
        missing = advance_presence(home, None, NOW + timedelta(seconds=1), GRACE)
        away = advance_presence(missing, None, NOW + timedelta(seconds=181), GRACE)
        self.assertFalse(away.is_connected)

    def test_return_during_grace_clears_absence(self) -> None:
        home = advance_presence(None, client(), NOW, GRACE)
        missing = advance_presence(home, None, NOW + timedelta(seconds=1), GRACE)
        returned = advance_presence(
            missing, client(ap_name="AP-2"), NOW + timedelta(seconds=60), GRACE
        )
        self.assertTrue(returned.is_connected)
        self.assertIsNone(returned.missing_since)
        self.assertEqual("AP-2", returned.client.ap_name if returned.client else None)

    def test_roaming_stays_home(self) -> None:
        on_ap_one = advance_presence(None, client(ap_name="AP-1"), NOW, GRACE)
        on_ap_two = advance_presence(
            on_ap_one, client(ap_name="AP-2"), NOW + timedelta(seconds=30), GRACE
        )
        self.assertTrue(on_ap_two.is_connected)
        self.assertEqual("AP-2", on_ap_two.client.ap_name if on_ap_two.client else None)

    def test_api_failure_cannot_mark_away_without_a_valid_poll(self) -> None:
        """The coordinator preserves this object unchanged on API failure."""
        home = WifiPresence(True, None, NOW, client())
        # There is deliberately no advance_presence() call for a failed poll.
        after_failed_poll = home
        self.assertTrue(after_failed_poll.is_connected)
        self.assertIsNone(after_failed_poll.missing_since)

    def test_nonmatching_ssid_uses_normal_away_grace_period(self) -> None:
        home_client = client(ssid="Home")
        home = advance_presence(
            None,
            client_matches_ssid_filter(home_client, frozenset({"Home"})),
            NOW,
            GRACE,
        )
        filtered = client_matches_ssid_filter(client(ssid="Guest"), frozenset({"Home"}))
        missing = advance_presence(home, filtered, NOW + timedelta(seconds=30), GRACE)
        away = advance_presence(missing, filtered, NOW + timedelta(seconds=210), GRACE)

        self.assertTrue(missing.is_connected)
        self.assertFalse(away.is_connected)

    def test_multiple_clients_progress_independently(self) -> None:
        other = "11:22:33:44:55:66"
        first = advance_presence(None, client(), NOW, GRACE)
        second = advance_presence(None, client(mac=other), NOW, GRACE)
        first = advance_presence(first, None, NOW + timedelta(seconds=1), GRACE)
        second = advance_presence(
            second, client(mac=other), NOW + timedelta(seconds=181), GRACE
        )
        first = advance_presence(first, None, NOW + timedelta(seconds=181), GRACE)
        self.assertFalse(first.is_connected)
        self.assertTrue(second.is_connected)


if __name__ == "__main__":
    unittest.main()
