"""Safe diagnostics for FortiGate Policy Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import FortiGatePolicyConfigEntry
from .const import (
    CONF_API_TOKEN,
    CONF_TRACKED_CLIENTS,
    CONF_WIFI_AWAY_GRACE_PERIOD,
    CONF_WIFI_POLL_INTERVAL,
    CONF_WIFI_TRACKING_ENABLED,
)
from .policy_config import configured_policies

TO_REDACT = {CONF_API_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FortiGatePolicyConfigEntry
) -> dict[str, Any]:
    """Return useful aggregate Wi-Fi diagnostics without client identities."""
    runtime = entry.runtime_data
    wifi = runtime.wifi_coordinator
    tracked = entry.options.get(CONF_TRACKED_CLIENTS, {})
    return async_redact_data(
        {
            "config_entry": dict(entry.data),
            "policy_count": len(configured_policies(entry.data)),
            "policies": {
                policy_id: {
                    "available": coordinator.last_update_success,
                    "status": (coordinator.data.status if coordinator.data else None),
                    "last_successful_check": (
                        coordinator.last_successful_check.isoformat()
                        if coordinator.last_successful_check
                        else None
                    ),
                }
                for policy_id, coordinator in runtime.policy_coordinators.items()
            },
            "wifi_tracking": {
                "enabled": entry.options.get(CONF_WIFI_TRACKING_ENABLED, False),
                "poll_interval": entry.options.get(CONF_WIFI_POLL_INTERVAL),
                "away_grace_period": entry.options.get(CONF_WIFI_AWAY_GRACE_PERIOD),
                "tracked_client_count": (
                    len(tracked) if isinstance(tracked, dict) else 0
                ),
                "endpoint": "/api/v2/monitor/wifi/client",
                "last_successful_update": (
                    wifi.last_successful_update.isoformat()
                    if wifi and wifi.last_successful_update
                    else None
                ),
                "discovered_client_count": (
                    len(wifi.data.clients) if wifi and wifi.data else None
                ),
                "malformed_client_records_skipped": (
                    wifi.data.skipped_clients if wifi and wifi.data else None
                ),
                "fortios_version": (
                    wifi.data.fortios_version if wifi and wifi.data else None
                ),
            },
        },
        TO_REDACT,
    )
