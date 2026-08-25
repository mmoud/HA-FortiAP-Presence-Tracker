"""Set up the FortiGate Policy Presence config entry."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FortiGatePolicyApi
from .const import (
    CONF_ALLOWED_SSIDS,
    CONF_API_TOKEN,
    CONF_NETWORK_CREATE_TRACKER_ENTITIES,
    CONF_NETWORK_NEW_DEVICE_DETECTION,
    CONF_NETWORK_TRACK_FORTIAP_CLIENTS,
    CONF_POLL_INTERVAL,
    CONF_PRESENCE_POLICY_RULES,
    CONF_PRESENCE_USERS,
    CONF_RECENT_CLIENT_RETENTION_DAYS,
    CONF_TRACKED_CLIENTS,
    CONF_VDOM,
    CONF_VERIFY_SSL,
    CONF_WIFI_AWAY_GRACE_PERIOD,
    CONF_WIFI_POLL_INTERVAL,
    CONF_WIFI_TRACKING_ENABLED,
    DEFAULT_NETWORK_CREATE_TRACKER_ENTITIES,
    DEFAULT_NETWORK_NEW_DEVICE_DETECTION,
    DEFAULT_NETWORK_TRACK_FORTIAP_CLIENTS,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
    DEFAULT_WIFI_AWAY_GRACE_PERIOD,
    DEFAULT_WIFI_POLL_INTERVAL,
    DEFAULT_WIFI_TRACKING_ENABLED,
    DOMAIN,
)
from .coordinator import FortiGatePolicyCoordinator, FortiGateWifiCoordinator
from .network_store import NetworkDeviceStore
from .policy_config import configured_policies, fortigate_entry_title, migrate_v1_data
from .presence_users import (
    configured_presence_users,
    migrate_tracker_rules_to_users,
    serialize_presence_users,
)
from .wifi import normalize_mac

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.DEVICE_TRACKER,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class FortiGateRuntimeData:
    """Runtime state shared by the policy and optional Wi-Fi platforms."""

    policy_coordinators: dict[str, FortiGatePolicyCoordinator]
    wifi_coordinator: FortiGateWifiCoordinator | None
    network_store: NetworkDeviceStore | None


type FortiGatePolicyConfigEntry = ConfigEntry[FortiGateRuntimeData]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the management panel."""

    from .panel import async_register_panel

    await async_register_panel(hass)

    return True


def tracked_macs_from_options(options: Mapping[str, Any]) -> set[str]:
    """Read valid normalized MACs from persisted tracker selections."""
    raw_clients = options.get(CONF_TRACKED_CLIENTS, {})
    if not isinstance(raw_clients, Mapping):
        return set()
    return {
        normalized
        for mac in raw_clients
        if (normalized := normalize_mac(mac)) is not None
    }


def tracked_ssid_filters_from_options(
    options: Mapping[str, Any],
) -> dict[str, frozenset[str]]:
    """Read optional per-tracker SSID allowlists from persisted selections."""
    raw_clients = options.get(CONF_TRACKED_CLIENTS, {})
    if not isinstance(raw_clients, Mapping):
        return {}
    filters: dict[str, frozenset[str]] = {}
    for raw_mac, metadata in raw_clients.items():
        mac = normalize_mac(raw_mac)
        if mac is None or not isinstance(metadata, Mapping):
            continue
        raw_ssids = metadata.get(CONF_ALLOWED_SSIDS, [])
        if not isinstance(raw_ssids, list):
            continue
        allowed = frozenset(
            ssid for ssid in raw_ssids if isinstance(ssid, str) and ssid
        )
        if allowed:
            filters[mac] = allowed
    return filters


def tracked_names_from_options(options: Mapping[str, Any]) -> dict[str, str]:
    """Return persistent user-assigned names indexed by normalized MAC."""
    raw_clients = options.get(CONF_TRACKED_CLIENTS, {})
    if not isinstance(raw_clients, Mapping):
        return {}
    names: dict[str, str] = {}
    for raw_mac, metadata in raw_clients.items():
        mac = normalize_mac(raw_mac)
        if mac is None or not isinstance(metadata, Mapping):
            continue
        name = metadata.get("friendly_name")
        if isinstance(name, str) and name.strip():
            names[mac] = name.strip()
    return names


def _cleanup_stale_wifi_registry_entries(
    hass: HomeAssistant,
    entry_id: str,
    tracked_macs: set[str],
    presence_user_ids: set[str] | None = None,
    retain_unknown_sensor: bool = True,
) -> None:
    """Remove tracker entities and devices no longer selected in Options."""
    prefix = f"{entry_id}_wifi_"
    retained_entity_prefixes = {
        f"{prefix}{mac.replace(':', '')}" for mac in tracked_macs
    }
    presence_user_ids = presence_user_ids or set()
    user_prefix = f"{entry_id}_presence_user_"
    retained_user_unique_ids = {
        unique_id
        for user_id in presence_user_ids
        for unique_id in (
            f"{user_prefix}{user_id}",
            f"{user_prefix}{user_id}_presence",
        )
    }
    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry_id):
        if (
            (
                entity.platform == DOMAIN
                and entity.unique_id.startswith(prefix)
                and not any(
                    entity.unique_id == retained
                    or entity.unique_id.startswith(f"{retained}_")
                    for retained in retained_entity_prefixes
                )
            )
            or (
                entity.platform == DOMAIN
                and entity.unique_id.startswith(user_prefix)
                and entity.unique_id not in retained_user_unique_ids
            )
            or (
                entity.platform == DOMAIN
                and entity.unique_id == f"{entry_id}_unknown_network_devices"
                and not retain_unknown_sensor
            )
            or (
                entity.platform == DOMAIN
                and entity.unique_id.startswith(f"{entry_id}_policy_")
                and entity.unique_id.endswith(("_decision", "_override"))
            )
        ):
            entity_registry.async_remove(entity.entity_id)

    retained_device_identifiers = {
        f"{prefix}{mac.replace(':', '')}" for mac in tracked_macs
    }
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry_id):
        wifi_identifiers = {
            identifier
            for domain, identifier in device.identifiers
            if domain == DOMAIN and identifier.startswith(prefix)
        }
        if wifi_identifiers and wifi_identifiers.isdisjoint(
            retained_device_identifiers
        ):
            device_registry.async_remove_device(device.id)
        elif wifi_identifiers and getattr(device, "config_entries", {entry_id}) == {
            entry_id
        }:
            # Older releases advertised a global MAC connection, which can
            # merge one client across separate FortiGate config entries.
            connections = getattr(device, "connections", set())
            if any(kind == dr.CONNECTION_NETWORK_MAC for kind, _ in connections):
                device_registry.async_update_device(device.id, new_connections=set())


async def async_setup_entry(
    hass: HomeAssistant, entry: FortiGatePolicyConfigEntry
) -> bool:
    """Set up the FortiGate policy switch from its UI-created config entry."""
    session = async_get_clientsession(hass)
    policy_definitions = configured_policies(entry.data)
    policy_apis = {
        policy.policy_id: FortiGatePolicyApi(
            session,
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_PORT],
            vdom=entry.data[CONF_VDOM],
            policy_id=policy.policy_id,
            expected_policy_name=policy.expected_name,
            token=entry.data[CONF_API_TOKEN],
            verify_ssl=entry.data[CONF_VERIFY_SSL],
        )
        for policy in policy_definitions
    }
    wifi_api = FortiGatePolicyApi(
        session,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        vdom=entry.data[CONF_VDOM],
        policy_id="",
        expected_policy_name="",
        token=entry.data[CONF_API_TOKEN],
        verify_ssl=entry.data[CONF_VERIFY_SSL],
    )
    policy_coordinators = {
        policy_id: FortiGatePolicyCoordinator(
            hass,
            entry,
            api,
            entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )
        for policy_id, api in policy_apis.items()
    }
    refresh_results = await asyncio.gather(
        *(coordinator.async_refresh() for coordinator in policy_coordinators.values()),
        return_exceptions=True,
    )
    for result in refresh_results:
        if isinstance(result, Exception):
            raise result

    wifi_coordinator: FortiGateWifiCoordinator | None = None
    tracked_macs = tracked_macs_from_options(entry.options)
    presence_users = configured_presence_users(
        entry.options, tracked_macs, set(policy_coordinators)
    )
    _cleanup_stale_wifi_registry_entries(
        hass,
        entry.entry_id,
        (
            tracked_macs
            if entry.options.get(
                CONF_NETWORK_CREATE_TRACKER_ENTITIES,
                DEFAULT_NETWORK_CREATE_TRACKER_ENTITIES,
            )
            else set()
        ),
        {user.user_id for user in presence_users},
        entry.options.get(
            CONF_NETWORK_NEW_DEVICE_DETECTION,
            DEFAULT_NETWORK_NEW_DEVICE_DETECTION,
        ),
    )
    network_store: NetworkDeviceStore | None = None
    tracking_enabled = entry.options.get(
        CONF_WIFI_TRACKING_ENABLED, DEFAULT_WIFI_TRACKING_ENABLED
    )
    track_fortiap = entry.options.get(
        CONF_NETWORK_TRACK_FORTIAP_CLIENTS, DEFAULT_NETWORK_TRACK_FORTIAP_CLIENTS
    )
    if tracking_enabled and track_fortiap:
        network_store = NetworkDeviceStore(hass, entry.entry_id)
        await network_store.async_load()
        tracked_names = tracked_names_from_options(entry.options)
        owners = {mac: user.name for user in presence_users for mac in user.macs}
        wifi_coordinator = FortiGateWifiCoordinator(
            hass,
            entry,
            wifi_api,
            tracked_macs,
            entry.options.get(CONF_WIFI_POLL_INTERVAL, DEFAULT_WIFI_POLL_INTERVAL),
            entry.options.get(
                CONF_WIFI_AWAY_GRACE_PERIOD, DEFAULT_WIFI_AWAY_GRACE_PERIOD
            ),
            tracked_ssid_filters_from_options(entry.options),
            network_store,
            tracked_names,
            owners,
            entry.options.get(
                CONF_NETWORK_NEW_DEVICE_DETECTION,
                DEFAULT_NETWORK_NEW_DEVICE_DETECTION,
            ),
            entry.options.get(
                CONF_RECENT_CLIENT_RETENTION_DAYS,
                DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
            ),
        )
    entry.runtime_data = FortiGateRuntimeData(
        policy_coordinators, wifi_coordinator, network_store
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if wifi_coordinator is not None:
        # This is intentionally after device tracker setup: RestoreEntity can
        # seed a former home/not_home state before the first valid monitor
        # response begins its grace timer. A Wi-Fi error never prevents the
        # firewall switch from loading.
        await wifi_coordinator.async_refresh()
    return True


async def async_migrate_entry(
    hass: HomeAssistant, entry: FortiGatePolicyConfigEntry
) -> bool:
    """Migrate entries without changing entity identity."""
    if entry.version > 8:
        return False
    data = dict(entry.data)
    migrated_options: dict[str, Any] | None = None
    if entry.version == 1:
        data = migrate_v1_data(data)
    if entry.version < 4:
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            title=fortigate_entry_title(data),
            version=4,
        )
    if entry.version < 5:
        options = dict(entry.options)
        tracked_macs = tracked_macs_from_options(options)
        raw_users = options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(raw_users, Mapping) or not raw_users:
            raw_users = migrate_tracker_rules_to_users(options, tracked_macs)
        options[CONF_PRESENCE_USERS] = serialize_presence_users(
            raw_users,
            tracked_macs,
            {policy.policy_id for policy in configured_policies(data)},
        )
        options.pop(CONF_PRESENCE_POLICY_RULES, None)
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            options=options,
            title=fortigate_entry_title(data),
            version=5,
        )
        migrated_options = options
    if entry.version < 6:
        options = dict(
            migrated_options if migrated_options is not None else entry.options
        )
        raw_users = options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(raw_users, Mapping):
            raw_users = {}
        policy_ids = {policy.policy_id for policy in configured_policies(data)}
        options[CONF_PRESENCE_USERS] = serialize_presence_users(
            raw_users,
            tracked_macs_from_options(options),
            policy_ids,
        )
        hass.config_entries.async_update_entry(entry, options=options, version=6)
        migrated_options = options
    if entry.version < 7:
        options = dict(
            migrated_options if migrated_options is not None else entry.options
        )
        # Preserve the previous 180-second behavior for existing installations;
        # 300 seconds is only the default for new entries.
        options.setdefault(CONF_WIFI_AWAY_GRACE_PERIOD, 180)
        options.setdefault(
            CONF_NETWORK_TRACK_FORTIAP_CLIENTS, DEFAULT_NETWORK_TRACK_FORTIAP_CLIENTS
        )
        options.setdefault(
            CONF_NETWORK_CREATE_TRACKER_ENTITIES,
            DEFAULT_NETWORK_CREATE_TRACKER_ENTITIES,
        )
        options.setdefault(
            CONF_NETWORK_NEW_DEVICE_DETECTION,
            DEFAULT_NETWORK_NEW_DEVICE_DETECTION,
        )
        hass.config_entries.async_update_entry(entry, options=options, version=7)
        migrated_options = options
    if entry.version < 8:
        options = dict(
            migrated_options if migrated_options is not None else entry.options
        )
        for key in (
            "policy_rules_v2",
            "presence_policy_rules",
            "policy_automation_enabled",
            "policy_automation_dry_run",
            "default_override_minutes",
        ):
            options.pop(key, None)
        raw_users = options.get(CONF_PRESENCE_USERS, {})
        options[CONF_PRESENCE_USERS] = serialize_presence_users(
            raw_users if isinstance(raw_users, Mapping) else {},
            tracked_macs_from_options(options),
            set(),
        )
        hass.config_entries.async_update_entry(entry, options=options, version=8)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: FortiGatePolicyConfigEntry
) -> bool:
    """Unload the config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.network_store is not None:
        await entry.runtime_data.network_store.async_save()
    return unloaded
