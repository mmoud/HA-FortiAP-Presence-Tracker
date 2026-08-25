"""Shared Home Assistant Device Registry metadata for network clients."""

from __future__ import annotations

from typing import Any

from .const import DOMAIN


def network_client_identifier(entry_id: str, mac: str) -> str:
    """Return an entry-scoped identity safe for multiple FortiGates."""
    return f"{entry_id}_wifi_{mac.replace(':', '')}"


def network_client_device_info(entry: Any, mac: str, name: str) -> dict[str, Any]:
    """Build consistent metadata for every entity belonging to one client."""
    info: dict[str, Any] = {
        "identifiers": {(DOMAIN, network_client_identifier(entry.entry_id, mac))},
        "name": name,
        "via_device": (DOMAIN, entry.entry_id),
    }
    runtime = getattr(entry, "runtime_data", None)
    store = getattr(runtime, "network_store", None)
    record = store.records.get(mac) if store else None
    manufacturer = record.metadata.get("manufacturer") if record else None
    if isinstance(manufacturer, str) and manufacturer:
        info["manufacturer"] = manufacturer
    return info
