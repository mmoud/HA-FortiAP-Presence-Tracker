"""Safe diagnostics for FortiGate Policy Presence."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import FortiGatePolicyConfigEntry
from .const import (
    CONF_ALLOWED_SSIDS,
    CONF_API_TOKEN,
    CONF_DEFAULT_OVERRIDE_MINUTES,
    CONF_POLICY_AUTOMATION_DRY_RUN,
    CONF_POLICY_AUTOMATION_ENABLED,
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
    rule_manager = runtime.rule_manager
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
            },
            "presence_policy_rules": {
                "configured_user_count": len(presence_users),
                "assigned_device_count": sum(len(user.macs) for user in presence_users),
                "configured_rule_count": (
                    len(rule_manager.rules) + len(rule_manager.policy_rules)
                    if rule_manager
                    else 0
                ),
                "automation_enabled": entry.options.get(
                    CONF_POLICY_AUTOMATION_ENABLED, True
                ),
                "dry_run": entry.options.get(CONF_POLICY_AUTOMATION_DRY_RUN, False),
                "default_override_minutes": entry.options.get(
                    CONF_DEFAULT_OVERRIDE_MINUTES, 60
                ),
                "active_override_count": (
                    len(rule_manager.overrides) if rule_manager else 0
                ),
                "last_reconcile": (
                    rule_manager.last_reconcile.isoformat()
                    if rule_manager and rule_manager.last_reconcile
                    else None
                ),
                "blocked_policy_count": (
                    len(rule_manager.last_result.blocked_unknown)
                    if rule_manager and rule_manager.last_result
                    else 0
                ),
                "conflict_policy_count": (
                    len(rule_manager.last_result.conflicts)
                    if rule_manager and rule_manager.last_result
                    else 0
                ),
                "last_error": rule_manager.last_error if rule_manager else None,
            },
        },
        TO_REDACT,
    )
