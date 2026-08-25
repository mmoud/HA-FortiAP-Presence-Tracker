"""Safe diagnostics for FortiGate Policy Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import FortiGatePolicyConfigEntry
from .const import (
    CONF_ALLOWED_SSIDS,
    CONF_API_TOKEN,
    CONF_NETWORK_CREATE_TRACKER_ENTITIES,
    CONF_NETWORK_NEW_DEVICE_DETECTION,
    CONF_NETWORK_TRACK_FORTIAP_CLIENTS,
    CONF_TRACKED_CLIENTS,
    CONF_WIFI_AWAY_GRACE_PERIOD,
    CONF_WIFI_POLL_INTERVAL,
    CONF_WIFI_TRACKING_ENABLED,
)
from .policy_config import configured_policies
from .presence_users import configured_presence_users
from .wifi import normalize_mac

TO_REDACT = {CONF_API_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FortiGatePolicyConfigEntry
) -> dict[str, Any]:
    """Return useful aggregate Wi-Fi diagnostics without client identities."""
    runtime = entry.runtime_data
    wifi = runtime.wifi_coordinator
    tracked = entry.options.get(CONF_TRACKED_CLIENTS, {})
    tracked_macs = {
        normalized
        for mac in tracked
        if isinstance(tracked, dict) and (normalized := normalize_mac(mac)) is not None
    }
    ssid_filtered_tracker_count = (
        sum(
            1
            for metadata in tracked.values()
            if isinstance(metadata, dict)
            and isinstance(metadata.get(CONF_ALLOWED_SSIDS), list)
            and bool(metadata[CONF_ALLOWED_SSIDS])
        )
        if isinstance(tracked, dict)
        else 0
    )
    presence_users = configured_presence_users(
        entry.options,
        tracked_macs,
        {policy.policy_id for policy in configured_policies(entry.data)},
    )
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
                "ssid_filtered_tracker_count": ssid_filtered_tracker_count,
                "presence_source": "fortiap_association",
                "endpoint": "/api/v2/monitor/wifi/client",
                "system_status_endpoint": "/api/v2/monitor/system/status",
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
                "fortios_version_source": (
                    wifi.data.fortios_version_source if wifi and wifi.data else None
                ),
                "system_status_endpoint_supported": (
                    wifi.data.system_status_endpoint_supported
                    if wifi and wifi.data
                    else None
                ),
                "network_device_presence": {
                    "fortiap_provider_enabled": entry.options.get(
                        CONF_NETWORK_TRACK_FORTIAP_CLIENTS, True
                    ),
                    "tracker_entities_enabled": entry.options.get(
                        CONF_NETWORK_CREATE_TRACKER_ENTITIES, True
                    ),
                    "new_device_detection_enabled": entry.options.get(
                        CONF_NETWORK_NEW_DEVICE_DETECTION, True
                    ),
                    "persistent_inventory_initialized": (
                        runtime.network_store.initialized
                        if runtime.network_store
                        else False
                    ),
                    "known_client_count": (
                        len(runtime.network_store.records)
                        if runtime.network_store
                        else 0
                    ),
                    "connected_client_count": (
                        sum(
                            state.is_connected is True
                            for state in wifi.data.presence.values()
                        )
                        if wifi and wifi.data
                        else None
                    ),
                    "away_grace_client_count": (
                        sum(
                            state.missing_since is not None
                            and state.is_connected is not False
                            for state in wifi.data.presence.values()
                        )
                        if wifi and wifi.data
                        else None
                    ),
                    "unknown_connected_client_count": (
                        len(set(wifi.data.clients) - tracked_macs)
                        if wifi and wifi.data
                        else None
                    ),
                    "fortiap_count": (
                        len(
                            {
                                client.ap_serial or client.ap_name
                                for client in wifi.data.clients.values()
                                if client.ap_serial or client.ap_name
                            }
                        )
                        if wifi and wifi.data
                        else None
                    ),
                    "new_device_event": "fortigate_new_network_device",
                },
            },
            "presence_groups": {
                "configured_user_count": len(presence_users),
                "assigned_device_count": sum(len(user.macs) for user in presence_users),
            },
        },
        TO_REDACT,
    )
