"""Persistent network-client history for FortiGate presence tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, MAX_STORED_NETWORK_CLIENTS
from .wifi import NetworkClient, normalize_mac

STORAGE_VERSION = 1
SAVE_DELAY = 30


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class NetworkDeviceRecord:
    """Restart-safe facts for a MAC on one FortiGate config entry."""

    mac: str
    first_seen: datetime
    last_seen: datetime
    connected_since: datetime | None
    connected: bool
    announced: bool = True
    metadata: dict[str, str | int] = field(default_factory=dict)
    friendly_name: str | None = None
    owner: str | None = None

    @classmethod
    def from_dict(cls, mac: str, data: object) -> NetworkDeviceRecord | None:
        """Load one record defensively; corrupt records do not break setup."""
        if not isinstance(data, dict):
            return None
        first_seen = _datetime(data.get("first_seen"))
        last_seen = _datetime(data.get("last_seen"))
        if first_seen is None or last_seen is None:
            return None
        raw_metadata = data.get("metadata", {})
        metadata = (
            {
                str(key): value
                for key, value in raw_metadata.items()
                if isinstance(value, (str, int))
            }
            if isinstance(raw_metadata, dict)
            else {}
        )
        return cls(
            mac=mac,
            first_seen=first_seen,
            last_seen=last_seen,
            connected_since=_datetime(data.get("connected_since")),
            connected=data.get("connected") is True,
            announced=data.get("announced") is not False,
            metadata=metadata,
            friendly_name=(
                data["friendly_name"]
                if isinstance(data.get("friendly_name"), str)
                else None
            ),
            owner=data["owner"] if isinstance(data.get("owner"), str) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialize to Home Assistant's JSON storage format."""
        return {
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "connected_since": (
                self.connected_since.isoformat() if self.connected_since else None
            ),
            "connected": self.connected,
            "announced": self.announced,
            "metadata": self.metadata,
            "friendly_name": self.friendly_name,
            "owner": self.owner,
        }


class NetworkDeviceStore:
    """Bounded persistent inventory scoped to one FortiGate config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{DOMAIN}.network_devices.{entry_id}"
        )
        self.records: dict[str, NetworkDeviceRecord] = {}
        self.initialized = False

    async def async_load(self) -> None:
        """Load history. The first successful poll becomes a silent baseline."""
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            return
        self.initialized = raw.get("initialized") is True
        devices = raw.get("devices", {})
        if not isinstance(devices, dict):
            return
        for raw_mac, value in devices.items():
            mac = normalize_mac(raw_mac)
            if mac and (record := NetworkDeviceRecord.from_dict(mac, value)):
                self.records[mac] = record

    def process_clients(
        self,
        clients: dict[str, NetworkClient],
        now: datetime,
        *,
        tracked_names: dict[str, str],
        owners: dict[str, str],
        retention_days: int,
    ) -> list[NetworkDeviceRecord]:
        """Merge one valid bulk poll and return genuinely new announced devices."""
        new_records: list[NetworkDeviceRecord] = []
        baseline = not self.initialized
        for mac, client in clients.items():
            record = self.records.get(mac)
            if record is None:
                record = NetworkDeviceRecord(
                    mac=mac,
                    first_seen=now,
                    last_seen=now,
                    connected_since=now,
                    connected=True,
                    announced=baseline,
                )
                self.records[mac] = record
                if not baseline:
                    new_records.append(record)
            else:
                if not record.connected:
                    record.connected_since = now
                record.last_seen = now
                record.connected = True
            record.metadata = client.as_storage_metadata()
            record.friendly_name = tracked_names.get(mac, record.friendly_name)
            record.owner = owners.get(mac, record.owner)

        self.initialized = True
        self._prune(now, tracked_names.keys(), retention_days)
        self.async_schedule_save()
        return new_records

    def mark_away(self, mac: str) -> None:
        """Persist a confirmed departure; API failures never call this method."""
        if (record := self.records.get(mac)) is not None and record.connected:
            record.connected = False
            record.connected_since = None
            self.async_schedule_save()

    def mark_announced(self, mac: str) -> None:
        if (record := self.records.get(mac)) is not None:
            record.announced = True
            self.async_schedule_save()

    def async_schedule_save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY)

    async def async_save(self) -> None:
        """Immediately flush the inventory during config-entry unload."""
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {
            "initialized": self.initialized,
            "devices": {mac: record.as_dict() for mac, record in self.records.items()},
        }

    def _prune(self, now: datetime, tracked_macs: object, retention_days: int) -> None:
        tracked = set(tracked_macs)
        cutoff = now - timedelta(days=retention_days)
        removable = sorted(
            (
                record
                for record in self.records.values()
                if record.mac not in tracked and record.last_seen < cutoff
            ),
            key=lambda record: record.last_seen,
        )
        for record in removable:
            self.records.pop(record.mac, None)
        if len(self.records) <= MAX_STORED_NETWORK_CLIENTS:
            return
        for record in sorted(self.records.values(), key=lambda item: item.last_seen):
            if len(self.records) <= MAX_STORED_NETWORK_CLIENTS:
                break
            if record.mac not in tracked:
                self.records.pop(record.mac, None)
