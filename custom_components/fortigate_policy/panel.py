"""Full-page FortiAP Presence Tracker management API."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.frontend import async_panel_exists
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FortiGateError, FortiGatePolicyApi
from .const import (
    CONF_ALLOWED_SSIDS,
    CONF_API_TOKEN,
    CONF_DEFAULT_OVERRIDE_MINUTES,
    CONF_FRIENDLY_NAME,
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_POLICIES,
    CONF_POLICY_AUTOMATION_DRY_RUN,
    CONF_POLICY_AUTOMATION_ENABLED,
    CONF_POLICY_RULE_ACTION,
    CONF_POLICY_RULE_MATCH,
    CONF_POLICY_RULE_NAME,
    CONF_POLICY_RULE_POLICIES,
    CONF_POLICY_RULE_PRESENCE,
    CONF_POLICY_RULE_PRIORITY,
    CONF_POLICY_RULE_SCHEDULE,
    CONF_POLICY_RULE_USERS,
    CONF_POLICY_RULES_V2,
    CONF_POLL_INTERVAL,
    CONF_PRESENCE_USER_MACS,
    CONF_PRESENCE_USER_NAME,
    CONF_PRESENCE_USERS,
    CONF_RECENT_CLIENT_RETENTION_DAYS,
    CONF_RECENT_WIFI_CLIENTS,
    CONF_TRACKED_CLIENTS,
    CONF_USER_AWAY_GRACE_PERIOD,
    CONF_VDOM,
    CONF_VERIFY_SSL,
    CONF_WIFI_AWAY_GRACE_PERIOD,
    CONF_WIFI_CLIENT_COUNT_SENSOR,
    CONF_WIFI_POLL_INTERVAL,
    CONF_WIFI_TRACKING_ENABLED,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_POLICY_AUTOMATION_DRY_RUN,
    DEFAULT_POLICY_AUTOMATION_ENABLED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
    DEFAULT_USER_AWAY_GRACE_PERIOD,
    DEFAULT_WIFI_AWAY_GRACE_PERIOD,
    DEFAULT_WIFI_CLIENT_COUNT_SENSOR,
    DEFAULT_WIFI_POLL_INTERVAL,
    DEFAULT_WIFI_TRACKING_ENABLED,
    DOMAIN,
    MAX_OVERRIDE_MINUTES,
    MAX_POLL_INTERVAL,
    MAX_RECENT_CLIENT_RETENTION_DAYS,
    MAX_USER_AWAY_GRACE_PERIOD,
    MAX_WIFI_AWAY_GRACE_PERIOD,
    MAX_WIFI_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_USER_AWAY_GRACE_PERIOD,
    MIN_WIFI_AWAY_GRACE_PERIOD,
    MIN_WIFI_POLL_INTERVAL,
)
from .policy_config import (
    PolicyDefinition,
    configured_policies,
    fortigate_entry_title,
    serialize_policies,
)
from .policy_rules import configured_policy_rules, serialize_policy_rules
from .presence_users import configured_presence_users, serialize_presence_users
from .wifi import FortiGateWifiClient, normalize_mac, utcnow

PANEL_PATH = "fortiap-presence"
STATIC_URL = "/fortiap_presence_static"
FRONTEND_FILE = "fortiap-panel.js"
PANEL_VERSION = "3.2.4"


def _entry(hass: HomeAssistant, entry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ValueError("FortiAP Presence Tracker entry was not found")
    return entry


def _bounded_int(value: object, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{label} must be a number") from err
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be true or false")
    return value


def _client_payload(client: FortiGateWifiClient) -> dict[str, Any]:
    """Return only normalized, useful wireless client fields."""
    return {
        key: value
        for key, value in asdict(client).items()
        if value is not None and key not in {"rssi", "snr"}
    }


def _tracker_state(entry, mac: str) -> dict[str, Any]:
    coordinator = entry.runtime_data.wifi_coordinator
    if coordinator is None:
        return {"state": "unavailable", "available": False}
    presence = coordinator.presence_for(mac)
    state = (
        "home"
        if presence and presence.is_connected is True
        else "not_home"
        if presence and presence.is_connected is False
        else "unavailable"
    )
    result: dict[str, Any] = {
        "state": state,
        "available": coordinator.last_update_success,
    }
    if presence:
        if presence.last_seen:
            result["last_seen"] = presence.last_seen.isoformat()
        if presence.missing_since:
            result["missing_since"] = presence.missing_since.isoformat()
        if presence.client:
            result["client"] = _client_payload(presence.client)
    return result


def _panel_data(entry) -> dict[str, Any]:
    """Build one safe, complete management-page snapshot."""
    options = entry.options
    tracked_raw = options.get(CONF_TRACKED_CLIENTS, {})
    tracked_raw = tracked_raw if isinstance(tracked_raw, Mapping) else {}
    trackers = []
    for raw_mac, raw_metadata in tracked_raw.items():
        mac = normalize_mac(raw_mac)
        if mac is None:
            continue
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        trackers.append(
            {
                "mac": mac,
                "name": str(metadata.get(CONF_FRIENDLY_NAME, mac)),
                "allowed_ssids": [
                    ssid
                    for ssid in metadata.get(CONF_ALLOWED_SSIDS, [])
                    if isinstance(ssid, str) and ssid
                ]
                if isinstance(metadata.get(CONF_ALLOWED_SSIDS, []), list)
                else [],
                **_tracker_state(entry, mac),
            }
        )

    policy_ids = {policy.policy_id for policy in configured_policies(entry.data)}
    tracked_macs = {tracker["mac"] for tracker in trackers}
    users = configured_presence_users(options, tracked_macs, policy_ids)
    rules = configured_policy_rules(
        options, {user.user_id for user in users}, policy_ids
    )
    runtime = entry.runtime_data
    recent = options.get(CONF_RECENT_WIFI_CLIENTS, {})
    recent = recent if isinstance(recent, Mapping) else {}
    known_ssids = {
        str(metadata["ssid"])
        for metadata in recent.values()
        if isinstance(metadata, Mapping) and metadata.get("ssid")
    }
    if runtime.wifi_coordinator and runtime.wifi_coordinator.data:
        known_ssids.update(
            client.ssid
            for client in runtime.wifi_coordinator.data.clients.values()
            if client.ssid
        )

    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "version": PANEL_VERSION,
        "connection": {
            "host": entry.data[CONF_HOST],
            "port": entry.data[CONF_PORT],
            "vdom": entry.data[CONF_VDOM],
            "verify_ssl": entry.data[CONF_VERIFY_SSL],
        },
        "settings": {
            CONF_POLL_INTERVAL: options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            CONF_WIFI_TRACKING_ENABLED: options.get(
                CONF_WIFI_TRACKING_ENABLED, DEFAULT_WIFI_TRACKING_ENABLED
            ),
            CONF_WIFI_POLL_INTERVAL: options.get(
                CONF_WIFI_POLL_INTERVAL, DEFAULT_WIFI_POLL_INTERVAL
            ),
            CONF_WIFI_AWAY_GRACE_PERIOD: options.get(
                CONF_WIFI_AWAY_GRACE_PERIOD, DEFAULT_WIFI_AWAY_GRACE_PERIOD
            ),
            CONF_WIFI_CLIENT_COUNT_SENSOR: options.get(
                CONF_WIFI_CLIENT_COUNT_SENSOR, DEFAULT_WIFI_CLIENT_COUNT_SENSOR
            ),
            CONF_POLICY_AUTOMATION_ENABLED: options.get(
                CONF_POLICY_AUTOMATION_ENABLED, DEFAULT_POLICY_AUTOMATION_ENABLED
            ),
            CONF_POLICY_AUTOMATION_DRY_RUN: options.get(
                CONF_POLICY_AUTOMATION_DRY_RUN, DEFAULT_POLICY_AUTOMATION_DRY_RUN
            ),
            CONF_DEFAULT_OVERRIDE_MINUTES: options.get(
                CONF_DEFAULT_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES
            ),
            CONF_RECENT_CLIENT_RETENTION_DAYS: options.get(
                CONF_RECENT_CLIENT_RETENTION_DAYS,
                DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
            ),
        },
        "trackers": sorted(trackers, key=lambda item: item["name"].casefold()),
        "users": [
            {
                "id": user.user_id,
                "name": user.name,
                "macs": sorted(user.macs),
                "away_grace_period": user.away_grace_period,
            }
            for user in users
        ],
        "policies": [
            {
                "id": policy.policy_id,
                "name": policy.expected_name,
                "state": (
                    runtime.policy_coordinators[policy.policy_id].data.status
                    if runtime.policy_coordinators.get(policy.policy_id)
                    and runtime.policy_coordinators[policy.policy_id].data
                    else "unavailable"
                ),
            }
            for policy in configured_policies(entry.data)
        ],
        "rules": [
            {
                "id": rule.rule_id,
                "name": rule.name,
                "users": sorted(rule.user_ids),
                "match": rule.match,
                "presence": rule.presence,
                "action": rule.action,
                "policies": sorted(rule.policy_ids),
                "priority": rule.priority,
                "schedule": rule.schedule_entity_id or "",
            }
            for rule in rules
        ],
        "known_ssids": sorted(known_ssids, key=str.casefold),
        "recent_clients": [
            {"mac": mac, **dict(metadata)}
            for raw_mac, metadata in recent.items()
            if (mac := normalize_mac(raw_mac)) is not None
            and isinstance(metadata, Mapping)
            and mac not in tracked_macs
        ],
        "health": {
            "wifi_available": (
                runtime.wifi_coordinator.last_update_success
                if runtime.wifi_coordinator
                else None
            ),
            "last_wifi_update": (
                runtime.wifi_coordinator.last_successful_update.isoformat()
                if runtime.wifi_coordinator
                and runtime.wifi_coordinator.last_successful_update
                else None
            ),
            "fortios_version": (
                runtime.wifi_coordinator.data.fortios_version
                if runtime.wifi_coordinator and runtime.wifi_coordinator.data
                else None
            ),
            "automation_error": (
                runtime.rule_manager.last_error if runtime.rule_manager else None
            ),
        },
    }


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/panel/get", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def websocket_get_panel(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a safe management snapshot."""
    try:
        entry = _entry(hass, msg["entry_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"], _panel_data(entry))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/panel/entries"})
@websocket_api.async_response
async def websocket_get_entries(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List entry IDs and titles without credentials."""
    connection.send_result(
        msg["id"],
        [
            {"entry_id": entry.entry_id, "title": entry.title}
            for entry in hass.config_entries.async_entries(DOMAIN)
        ],
    )


def _raw_configuration(
    msg_config: Mapping[str, Any],
) -> tuple[list[object], list[object], list[object], dict[str, Any], list[object]]:
    trackers = msg_config.get("trackers", [])
    users = msg_config.get("users", [])
    rules = msg_config.get("rules", [])
    settings = msg_config.get("settings", {})
    policies = msg_config.get("policy_ids", [])
    if not all(isinstance(value, list) for value in (trackers, users, rules, policies)):
        raise TypeError("Trackers, people, rules, and policies must be lists")
    if not isinstance(settings, Mapping):
        raise TypeError("Settings must be an object")
    return trackers, users, rules, dict(settings), policies


async def _validate_policies(
    hass: HomeAssistant, entry, raw_policy_ids: list[object]
) -> tuple[PolicyDefinition, ...]:
    policy_ids: list[str] = []
    for raw in raw_policy_ids:
        policy_id = str(raw).strip()
        if not policy_id.isdigit():
            raise ValueError("Firewall policy IDs must contain only numbers")
        if policy_id not in policy_ids:
            policy_ids.append(policy_id)
    session = async_get_clientsession(hass)
    current_by_id = {
        policy.policy_id: policy for policy in configured_policies(entry.data)
    }
    policies: list[PolicyDefinition] = []
    for policy_id in policy_ids:
        existing = current_by_id.get(policy_id)
        api = FortiGatePolicyApi(
            session,
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_PORT],
            vdom=entry.data[CONF_VDOM],
            policy_id=policy_id,
            expected_policy_name=existing.expected_name if existing else "",
            token=entry.data[CONF_API_TOKEN],
            verify_ssl=entry.data[CONF_VERIFY_SSL],
        )
        policy = await api.async_get_policy()
        policies.append(PolicyDefinition(policy.policy_id, policy.name))
    return tuple(policies)


def _normalize_configuration(
    entry,
    trackers_raw: list[object],
    users_raw: list[object],
    rules_raw: list[object],
    settings_raw: Mapping[str, Any],
    policies: tuple[PolicyDefinition, ...],
) -> dict[str, Any]:
    tracked: dict[str, dict[str, Any]] = {}
    for raw in trackers_raw:
        if not isinstance(raw, Mapping):
            raise TypeError("Each tracker must be an object")
        mac = normalize_mac(raw.get("mac"))
        name = raw.get("name")
        if mac is None or not isinstance(name, str) or not name.strip():
            raise ValueError("Every tracker needs a valid MAC address and name")
        if mac in tracked:
            raise ValueError(f"Duplicate tracker MAC: {mac}")
        allowed = raw.get("allowed_ssids", [])
        if not isinstance(allowed, list):
            raise TypeError("Allowed Wi-Fi networks must be a list")
        metadata: dict[str, Any] = {CONF_FRIENDLY_NAME: name.strip()}
        ssids = sorted(
            {ssid for ssid in allowed if isinstance(ssid, str) and ssid},
            key=str.casefold,
        )
        if ssids:
            metadata[CONF_ALLOWED_SSIDS] = ssids
        tracked[mac] = metadata

    policy_ids = {policy.policy_id for policy in policies}
    users_map: dict[str, object] = {}
    assigned: set[str] = set()
    for raw in users_raw:
        if not isinstance(raw, Mapping):
            raise TypeError("Each person must be an object")
        user_id = str(raw.get("id") or uuid4())
        macs = raw.get("macs", [])
        if not isinstance(macs, list):
            raise TypeError("Person devices must be a list")
        normalized_macs = [mac for value in macs if (mac := normalize_mac(value))]
        duplicate_assignment = assigned.intersection(normalized_macs)
        if duplicate_assignment:
            raise ValueError("A tracked device can belong to only one person")
        assigned.update(normalized_macs)
        users_map[user_id] = {
            CONF_PRESENCE_USER_NAME: raw.get("name"),
            CONF_PRESENCE_USER_MACS: normalized_macs,
            CONF_USER_AWAY_GRACE_PERIOD: _bounded_int(
                raw.get("away_grace_period", DEFAULT_USER_AWAY_GRACE_PERIOD),
                MIN_USER_AWAY_GRACE_PERIOD,
                MAX_USER_AWAY_GRACE_PERIOD,
                "Person away grace period",
            ),
        }
    users = serialize_presence_users(users_map, set(tracked), policy_ids)
    if len(users) != len(users_raw):
        raise ValueError("Every person needs a name and at least one tracked device")

    rules_map: dict[str, object] = {}
    for raw in rules_raw:
        if not isinstance(raw, Mapping):
            raise TypeError("Each policy rule must be an object")
        rule_id = str(raw.get("id") or uuid4())
        rules_map[rule_id] = {
            CONF_POLICY_RULE_NAME: raw.get("name"),
            CONF_POLICY_RULE_USERS: raw.get("users", []),
            CONF_POLICY_RULE_MATCH: raw.get("match"),
            CONF_POLICY_RULE_PRESENCE: raw.get("presence"),
            CONF_POLICY_RULE_ACTION: raw.get("action"),
            CONF_POLICY_RULE_POLICIES: raw.get("policies", []),
            CONF_POLICY_RULE_PRIORITY: raw.get("priority", 50),
            CONF_POLICY_RULE_SCHEDULE: raw.get("schedule", ""),
        }
    rules = serialize_policy_rules(rules_map, set(users), policy_ids)
    if len(rules) != len(rules_raw):
        raise ValueError("Every rule needs valid people, policies, and behavior")

    options = dict(entry.options)
    options.update(
        {
            CONF_TRACKED_CLIENTS: tracked,
            CONF_PRESENCE_USERS: users,
            CONF_POLICY_RULES_V2: rules,
            CONF_POLL_INTERVAL: _bounded_int(
                settings_raw.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                MIN_POLL_INTERVAL,
                MAX_POLL_INTERVAL,
                "Policy polling interval",
            ),
            CONF_WIFI_POLL_INTERVAL: _bounded_int(
                settings_raw.get(CONF_WIFI_POLL_INTERVAL, DEFAULT_WIFI_POLL_INTERVAL),
                MIN_WIFI_POLL_INTERVAL,
                MAX_WIFI_POLL_INTERVAL,
                "Wi-Fi polling interval",
            ),
            CONF_WIFI_AWAY_GRACE_PERIOD: _bounded_int(
                settings_raw.get(
                    CONF_WIFI_AWAY_GRACE_PERIOD, DEFAULT_WIFI_AWAY_GRACE_PERIOD
                ),
                MIN_WIFI_AWAY_GRACE_PERIOD,
                MAX_WIFI_AWAY_GRACE_PERIOD,
                "Away grace period",
            ),
            CONF_DEFAULT_OVERRIDE_MINUTES: _bounded_int(
                settings_raw.get(
                    CONF_DEFAULT_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES
                ),
                0,
                MAX_OVERRIDE_MINUTES,
                "Override duration",
            ),
            CONF_RECENT_CLIENT_RETENTION_DAYS: _bounded_int(
                settings_raw.get(
                    CONF_RECENT_CLIENT_RETENTION_DAYS,
                    DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
                ),
                1,
                MAX_RECENT_CLIENT_RETENTION_DAYS,
                "Recent-client retention",
            ),
            CONF_WIFI_TRACKING_ENABLED: _boolean(
                settings_raw.get(
                    CONF_WIFI_TRACKING_ENABLED, DEFAULT_WIFI_TRACKING_ENABLED
                ),
                "Wi-Fi tracking",
            ),
            CONF_WIFI_CLIENT_COUNT_SENSOR: _boolean(
                settings_raw.get(
                    CONF_WIFI_CLIENT_COUNT_SENSOR, DEFAULT_WIFI_CLIENT_COUNT_SENSOR
                ),
                "Wi-Fi client count sensor",
            ),
            CONF_POLICY_AUTOMATION_ENABLED: _boolean(
                settings_raw.get(
                    CONF_POLICY_AUTOMATION_ENABLED,
                    DEFAULT_POLICY_AUTOMATION_ENABLED,
                ),
                "Policy automation",
            ),
            CONF_POLICY_AUTOMATION_DRY_RUN: _boolean(
                settings_raw.get(
                    CONF_POLICY_AUTOMATION_DRY_RUN,
                    DEFAULT_POLICY_AUTOMATION_DRY_RUN,
                ),
                "Policy dry run",
            ),
        }
    )
    return options


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/save",
        vol.Required("entry_id"): str,
        vol.Required("config"): dict,
    }
)
@websocket_api.async_response
async def websocket_save_panel(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Validate and atomically save the full management page."""
    try:
        entry = _entry(hass, msg["entry_id"])
        trackers, users, rules, settings, policy_ids = _raw_configuration(msg["config"])
        policies = await _validate_policies(hass, entry, policy_ids)
        options = _normalize_configuration(
            entry, trackers, users, rules, settings, policies
        )
    except FortiGateError:
        connection.send_error(
            msg["id"],
            "fortigate_validation_failed",
            "FortiGate rejected a policy ID or could not verify it",
        )
        return
    except (TypeError, ValueError) as err:
        connection.send_error(msg["id"], "invalid_config", str(err))
        return

    data = dict(entry.data)
    data[CONF_POLICIES] = serialize_policies(policies)
    legacy_primary = entry.data.get(CONF_LEGACY_PRIMARY_POLICY_ID)
    if legacy_primary and legacy_primary in {policy.policy_id for policy in policies}:
        data[CONF_LEGACY_PRIMARY_POLICY_ID] = legacy_primary
    else:
        data.pop(CONF_LEGACY_PRIMARY_POLICY_ID, None)
    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        title=fortigate_entry_title(data),
    )
    await hass.config_entries.async_reload(entry.entry_id)
    connection.send_result(msg["id"], {"saved": True})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/panel/discover", vol.Required("entry_id"): str}
)
@websocket_api.async_response
async def websocket_discover_clients(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Discover associated and named wireless clients on explicit request."""
    try:
        entry = _entry(hass, msg["entry_id"])
        api = FortiGatePolicyApi(
            async_get_clientsession(hass),
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_PORT],
            vdom=entry.data[CONF_VDOM],
            policy_id="",
            expected_policy_name="",
            token=entry.data[CONF_API_TOKEN],
            verify_ssl=entry.data[CONF_VERIFY_SSL],
        )
        clients, _skipped, _version = await api.async_get_wifi_client_catalog()
    except FortiGateError:
        connection.send_error(
            msg["id"], "discovery_failed", "FortiGate client discovery failed"
        )
        return
    connection.send_result(
        msg["id"],
        {
            "clients": [_client_payload(client) for client in clients.values()],
            "observed_at": utcnow().isoformat(),
        },
    )


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register the full-page panel and its authenticated WebSocket API."""
    websocket_api.async_register_command(hass, websocket_get_entries)
    websocket_api.async_register_command(hass, websocket_get_panel)
    websocket_api.async_register_command(hass, websocket_save_panel)
    websocket_api.async_register_command(hass, websocket_discover_clients)
    if async_panel_exists(hass, PANEL_PATH):
        return
    frontend_dir = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(frontend_dir), cache_headers=True)]
    )
    await panel_custom.async_register_panel(
        hass=hass,
        frontend_url_path=PANEL_PATH,
        webcomponent_name="fortiap-presence-panel",
        sidebar_title="FortiAP Presence",
        sidebar_icon="mdi:wifi-marker",
        module_url=f"{STATIC_URL}/{FRONTEND_FILE}?v={PANEL_VERSION}",
        embed_iframe=True,
        require_admin=True,
        config_panel_domain=DOMAIN,
        handle_safe_area=True,
    )
