"""FortiOS Wi-Fi client normalization and presence state transitions.

This module deliberately has no Home Assistant dependency.  It makes FortiOS
monitor API variation testable and keeps raw JSON away from entities.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

MAC_HEX_LENGTH = 12
MAC_PATTERN = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True, slots=True)
class FortiGateWifiClient:
    """One currently associated client, normalized across FortiOS variants."""

    mac: str
    ip: str | None = None
    hostname: str | None = None
    ssid: str | None = None
    ap_name: str | None = None
    ap_serial: str | None = None
    radio: int | None = None
    band: str | None = None
    channel: int | None = None
    rssi: int | None = None
    snr: int | None = None
    association_time: str | None = None
    vlan: str | None = None
    username: str | None = None
    vdom: str | None = None

    def as_recent_metadata(self, seen_at: datetime) -> dict[str, str]:
        """Return the bounded, JSON-safe discovery cache representation."""
        result = {"last_seen": seen_at.isoformat()}
        for key, value in (
            ("hostname", self.hostname),
            ("ip", self.ip),
            ("ssid", self.ssid),
            ("ap_name", self.ap_name),
        ):
            if value:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class FortiGateClientIdentity:
    """A MAC-keyed name/IP learned from another FortiGate monitor source."""

    mac: str
    hostname: str | None = None
    ip: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class WifiPresence:
    """Presence state for one explicitly selected client."""

    is_connected: bool | None
    missing_since: datetime | None
    last_seen: datetime | None
    client: FortiGateWifiClient | None


def normalize_mac(value: object) -> str | None:
    """Normalize colon, hyphen, and compact MAC forms to lowercase colon form."""
    if not isinstance(value, str):
        return None
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != MAC_HEX_LENGTH or not MAC_PATTERN.fullmatch(compact.lower()):
        return None
    compact = compact.lower()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def _string(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return None


def _integer(record: Mapping[str, Any], *keys: str) -> int | None:
    value = _string(record, *keys)
    if value is None:
        return None
    match = re.search(r"-?\d+", value)
    return int(match.group()) if match else None


def _client_records(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """Find the list of associated-station records in known monitor wrappers."""
    results = payload.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, Mapping)]
    if isinstance(results, Mapping):
        for key in ("clients", "client", "data", "items", "results"):
            candidates = results.get(key)
            if isinstance(candidates, list):
                return [item for item in candidates if isinstance(item, Mapping)]
    raise ValueError("FortiGate Wi-Fi response lacks a client list")


def parse_wifi_clients(
    payload: Mapping[str, Any], configured_vdom: str
) -> tuple[dict[str, FortiGateWifiClient], int, str | None]:
    """Parse a successful Wi-Fi monitor response without trusting optional fields.

    Returns clients indexed by normalized MAC, the number of malformed records
    skipped, and the FortiOS version if it was supplied by the appliance.
    """
    status = payload.get("status")
    if status is not None and status != "success":
        raise ValueError("FortiGate Wi-Fi monitor request was not successful")

    clients: dict[str, FortiGateWifiClient] = {}
    skipped = 0
    for record in _client_records(payload):
        mac = normalize_mac(
            _string(record, "mac", "sta_mac", "station_mac", "client_mac", "sta_addr")
        )
        if mac is None:
            skipped += 1
            continue
        client = FortiGateWifiClient(
            mac=mac,
            ip=_string(record, "ip", "sta_ip", "ipaddr", "ipv4"),
            hostname=_string(
                record,
                "hostname",
                "host_name",
                "sta_name",
                "device_name",
                "name",
            ),
            ssid=_string(record, "ssid", "vap_name", "wlan", "wifi_ssid"),
            ap_name=_string(record, "wtp_name", "ap_name", "fortiap", "wtp"),
            ap_serial=_string(record, "wtp_id", "wtp_serial", "ap_serial"),
            radio=_integer(record, "wtp_radio", "radio"),
            band=_string(record, "band", "wtp_band", "radio_band"),
            channel=_integer(record, "channel", "wtp_channel", "radio_channel"),
            rssi=_integer(record, "rssi", "sta_rssi", "signal", "signal_strength"),
            snr=_integer(record, "snr", "sta_snr"),
            association_time=_string(record, "sta_assoc_time", "association_time"),
            vlan=_string(record, "vlan", "vlan_id"),
            username=_string(record, "username", "user", "auth_user"),
            vdom=_string(record, "vdom") or configured_vdom,
        )
        # A duplicate MAC can occur during a short roam. Keep one associated
        # record; either one proves presence and no absence transition occurs.
        current = clients.get(mac)
        if current is None or _completeness(client) >= _completeness(current):
            clients[mac] = client

    version = _string(payload, "version")
    return clients, skipped, version


def parse_fortios_version(payload: Mapping[str, Any]) -> str | None:
    """Extract a FortiOS version from the system-status monitor response."""
    status = payload.get("status")
    if status is not None and status != "success":
        raise ValueError("FortiGate system status request was not successful")
    direct = _string(payload, "version", "firmware_version", "os_version")
    if direct is not None:
        return direct
    results = payload.get("results")
    if isinstance(results, Mapping):
        return _string(results, "version", "firmware_version", "os_version")
    return None


def client_matches_ssid_filter(
    client: FortiGateWifiClient | None, allowed_ssids: frozenset[str]
) -> FortiGateWifiClient | None:
    """Return an association only when it matches the optional SSID allowlist."""
    if client is None or not allowed_ssids:
        return client
    return client if client.ssid in allowed_ssids else None


def parse_client_identities(
    payload: Mapping[str, Any], source: str
) -> dict[str, FortiGateClientIdentity]:
    """Extract MAC-keyed identity hints from varying FortiOS monitor schemas.

    Detected-device and DHCP monitor responses have changed wrappers and field
    names across FortiOS releases.  Walk only a small bounded JSON tree, accept
    records containing a valid MAC, and ignore every unrelated record.
    """
    status = payload.get("status")
    if status is not None and status != "success":
        raise ValueError("FortiGate identity monitor request was not successful")

    identities: dict[str, FortiGateClientIdentity] = {}
    for record in _nested_mappings(payload.get("results"), depth=0):
        mac = normalize_mac(
            _string(
                record,
                "mac",
                "mac_address",
                "mac_addr",
                "hardware_address",
                "hwaddr",
                "device_mac",
                "client_mac",
                "sta_mac",
            )
        )
        if mac is None:
            continue
        identity = FortiGateClientIdentity(
            mac=mac,
            hostname=_string(
                record,
                "hostname",
                "host_name",
                "dhcp_hostname",
                "client_hostname",
                "device_name",
                "alias",
                "description",
                "host",
                "name",
            ),
            ip=_string(record, "ip", "ip_address", "ipaddr", "ipv4"),
            source=source,
        )
        current = identities.get(mac)
        if current is None or _identity_completeness(identity) > _identity_completeness(
            current
        ):
            identities[mac] = identity
    return identities


def enrich_wifi_clients(
    clients: dict[str, FortiGateWifiClient],
    identities: Mapping[str, FortiGateClientIdentity],
    *,
    prefer_identity_name: bool = False,
) -> dict[str, FortiGateWifiClient]:
    """Fill missing association names/IP addresses using the same normalized MAC."""
    return {
        mac: replace(
            client,
            hostname=(
                identities[mac].hostname
                if prefer_identity_name
                and mac in identities
                and identities[mac].hostname
                else client.hostname
                or (identities.get(mac).hostname if identities.get(mac) else None)
            ),
            ip=client.ip or (identities.get(mac).ip if identities.get(mac) else None),
        )
        for mac, client in clients.items()
    }


def _nested_mappings(value: object, depth: int) -> list[Mapping[str, Any]]:
    """Return mappings from a bounded monitor response tree."""
    if depth > 6:
        return []
    if isinstance(value, Mapping):
        records: list[Mapping[str, Any]] = [value]
        for nested in value.values():
            records.extend(_nested_mappings(nested, depth + 1))
        return records
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        records = []
        for nested in value:
            records.extend(_nested_mappings(nested, depth + 1))
        return records
    return []


def _identity_completeness(identity: FortiGateClientIdentity) -> int:
    return int(identity.hostname is not None) + int(identity.ip is not None)


def _completeness(client: FortiGateWifiClient) -> int:
    return sum(
        getattr(client, field_name) is not None
        for field_name in client.__dataclass_fields__
    )


def advance_presence(
    previous: WifiPresence | None,
    client: FortiGateWifiClient | None,
    now: datetime,
    grace_period: timedelta,
) -> WifiPresence:
    """Advance one MAC's conservative presence state after a valid API poll."""
    if client is not None:
        return WifiPresence(True, None, now, client)

    if previous is None:
        # The first successful absence after startup is deliberately unknown.
        # A grace period avoids generating a departure solely due to restart.
        return WifiPresence(None, now, None, None)

    missing_since = previous.missing_since or now
    if now - missing_since >= grace_period:
        return WifiPresence(False, missing_since, previous.last_seen, previous.client)
    return WifiPresence(
        previous.is_connected, missing_since, previous.last_seen, previous.client
    )


def utcnow() -> datetime:
    """Indirection keeps presence transition tests deterministic."""
    return datetime.now(UTC)
