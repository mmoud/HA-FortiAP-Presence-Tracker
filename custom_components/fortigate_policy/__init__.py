"""Set up the FortiGate Policy Presence config entry."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FortiGatePolicyApi
from .const import (
    CONF_API_TOKEN,
    CONF_POLL_INTERVAL,
    CONF_TRACKED_CLIENTS,
    CONF_VDOM,
    CONF_VERIFY_SSL,
    CONF_WIFI_AWAY_GRACE_PERIOD,
    CONF_WIFI_CLIENT_COUNT_SENSOR,
    CONF_WIFI_POLL_INTERVAL,
    CONF_WIFI_TRACKING_ENABLED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WIFI_AWAY_GRACE_PERIOD,
    DEFAULT_WIFI_POLL_INTERVAL,
    DEFAULT_WIFI_TRACKING_ENABLED,
    DOMAIN,
)
from .coordinator import FortiGatePolicyCoordinator, FortiGateWifiCoordinator
from .policy_config import configured_policies, fortigate_entry_title, migrate_v1_data
from .wifi import normalize_mac

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.DEVICE_TRACKER,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
]


@dataclass(slots=True)
class FortiGateRuntimeData:
    """Runtime state shared by the policy and optional Wi-Fi platforms."""

    policy_coordinators: dict[str, FortiGatePolicyCoordinator]
    wifi_coordinator: FortiGateWifiCoordinator | None


type FortiGatePolicyConfigEntry = ConfigEntry[FortiGateRuntimeData]


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


def _cleanup_stale_wifi_registry_entries(
    hass: HomeAssistant, entry_id: str, tracked_macs: set[str]
) -> None:
    """Remove tracker entities and devices no longer selected in Options."""
    prefix = f"{entry_id}_wifi_"
    retained_entity_unique_ids = {
        unique_id
        for mac in tracked_macs
        for unique_id in (
            f"{prefix}{mac.replace(':', '')}",
            f"{prefix}{mac.replace(':', '')}_presence",
        )
    }
    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry_id):
        if (
            entity.platform == DOMAIN
            and entity.unique_id.startswith(prefix)
            and entity.unique_id not in retained_entity_unique_ids
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
    _cleanup_stale_wifi_registry_entries(hass, entry.entry_id, tracked_macs)
    if entry.options.get(
        CONF_WIFI_TRACKING_ENABLED, DEFAULT_WIFI_TRACKING_ENABLED
    ) and (tracked_macs or entry.options.get(CONF_WIFI_CLIENT_COUNT_SENSOR, False)):
        wifi_coordinator = FortiGateWifiCoordinator(
            hass,
            entry,
            wifi_api,
            tracked_macs,
            entry.options.get(CONF_WIFI_POLL_INTERVAL, DEFAULT_WIFI_POLL_INTERVAL),
            entry.options.get(
                CONF_WIFI_AWAY_GRACE_PERIOD, DEFAULT_WIFI_AWAY_GRACE_PERIOD
            ),
        )
    entry.runtime_data = FortiGateRuntimeData(policy_coordinators, wifi_coordinator)
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
    if entry.version > 4:
        return False
    data = dict(entry.data)
    if entry.version == 1:
        data = migrate_v1_data(data)
    if entry.version < 4:
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            title=fortigate_entry_title(data),
            version=4,
        )
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: FortiGatePolicyConfigEntry
) -> bool:
    """Unload the config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
