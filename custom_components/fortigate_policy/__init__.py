"""Set up the FortiGate Policy Presence config entry."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FortiGatePolicyApi
from .const import (
    CONF_API_TOKEN,
    CONF_DEFAULT_OVERRIDE_MINUTES,
    CONF_POLICY_AUTOMATION_DRY_RUN,
    CONF_POLICY_AUTOMATION_ENABLED,
    CONF_POLICY_RULES_V2,
    CONF_POLL_INTERVAL,
    CONF_PRESENCE_POLICY_RULES,
    CONF_PRESENCE_USERS,
    CONF_TRACKED_CLIENTS,
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
    DEFAULT_WIFI_AWAY_GRACE_PERIOD,
    DEFAULT_WIFI_POLL_INTERVAL,
    DEFAULT_WIFI_TRACKING_ENABLED,
    DOMAIN,
    OVERRIDE_MODES,
)
from .coordinator import FortiGatePolicyCoordinator, FortiGateWifiCoordinator
from .policy_config import configured_policies, fortigate_entry_title, migrate_v1_data
from .policy_rules import (
    PresencePolicyRuleManager,
    configured_policy_rules,
    configured_presence_rules,
    migrate_user_intents_to_policy_rules,
    serialize_policy_rules,
)
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
    Platform.SELECT,
]


@dataclass(slots=True)
class FortiGateRuntimeData:
    """Runtime state shared by the policy and optional Wi-Fi platforms."""

    policy_coordinators: dict[str, FortiGatePolicyCoordinator]
    wifi_coordinator: FortiGateWifiCoordinator | None
    rule_manager: PresencePolicyRuleManager | None


type FortiGatePolicyConfigEntry = ConfigEntry[FortiGateRuntimeData]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the duration-aware manual override action."""

    async def async_set_policy_override(call: ServiceCall) -> None:
        entry_id = call.data["config_entry_id"]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError("FortiGate config entry was not found")
        runtime = getattr(entry, "runtime_data", None)
        manager = getattr(runtime, "rule_manager", None)
        if manager is None:
            raise ServiceValidationError(
                "This entry has no active presence policy rules"
            )
        await manager.async_set_override(
            call.data["policy_id"],
            call.data["mode"],
            call.data.get("duration_minutes"),
        )

    hass.services.async_register(
        DOMAIN,
        "set_policy_override",
        async_set_policy_override,
        schema=vol.Schema(
            {
                vol.Required("config_entry_id"): str,
                vol.Required("policy_id"): str,
                vol.Required("mode"): vol.In(OVERRIDE_MODES),
                vol.Optional("duration_minutes"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=1440)
                ),
            }
        ),
    )
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


def _cleanup_stale_wifi_registry_entries(
    hass: HomeAssistant,
    entry_id: str,
    tracked_macs: set[str],
    presence_user_ids: set[str] | None = None,
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
            entity.platform == DOMAIN
            and entity.unique_id.startswith(prefix)
            and entity.unique_id not in retained_entity_unique_ids
        ) or (
            entity.platform == DOMAIN
            and entity.unique_id.startswith(user_prefix)
            and entity.unique_id not in retained_user_unique_ids
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
    presence_users = configured_presence_users(
        entry.options, tracked_macs, set(policy_coordinators)
    )
    _cleanup_stale_wifi_registry_entries(
        hass,
        entry.entry_id,
        tracked_macs,
        {user.user_id for user in presence_users},
    )
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
    rule_manager: PresencePolicyRuleManager | None = None
    if wifi_coordinator is not None and policy_coordinators:
        legacy_rules = configured_presence_rules(
            entry.options,
            tracked_macs,
            set(policy_coordinators),
        )
        policy_rules = configured_policy_rules(
            entry.options,
            {user.user_id for user in presence_users},
            set(policy_coordinators),
        )
        if legacy_rules or policy_rules:
            rule_manager = PresencePolicyRuleManager(
                hass,
                wifi_coordinator,
                policy_coordinators,
                legacy_rules,
                users=presence_users,
                policy_rules=policy_rules,
                automation_enabled=entry.options.get(
                    CONF_POLICY_AUTOMATION_ENABLED,
                    DEFAULT_POLICY_AUTOMATION_ENABLED,
                ),
                dry_run=entry.options.get(
                    CONF_POLICY_AUTOMATION_DRY_RUN,
                    DEFAULT_POLICY_AUTOMATION_DRY_RUN,
                ),
                default_override_minutes=entry.options.get(
                    CONF_DEFAULT_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES
                ),
            )
    entry.runtime_data = FortiGateRuntimeData(
        policy_coordinators, wifi_coordinator, rule_manager
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if rule_manager is not None:
        for unsubscribe in rule_manager.async_start():
            entry.async_on_unload(unsubscribe)
        entry.async_on_unload(rule_manager.async_stop)
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
    if entry.version > 6:
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
    if entry.version < 6:
        options = dict(entry.options)
        raw_users = options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(raw_users, Mapping):
            raw_users = {}
        policy_ids = {policy.policy_id for policy in configured_policies(data)}
        migrated_rules = migrate_user_intents_to_policy_rules(raw_users, policy_ids)
        existing_rules = options.get(CONF_POLICY_RULES_V2, {})
        if isinstance(existing_rules, Mapping):
            migrated_rules.update(existing_rules)
        users_without_intents = {
            user_id: {
                **raw_user,
                "home_enable_policies": [],
                "home_disable_policies": [],
                "away_enable_policies": [],
                "away_disable_policies": [],
            }
            for user_id, raw_user in raw_users.items()
            if isinstance(user_id, str) and isinstance(raw_user, Mapping)
        }
        options[CONF_PRESENCE_USERS] = serialize_presence_users(
            users_without_intents,
            tracked_macs_from_options(options),
            policy_ids,
        )
        users = configured_presence_users(
            options,
            tracked_macs_from_options(options),
            policy_ids,
        )
        options[CONF_POLICY_RULES_V2] = serialize_policy_rules(
            migrated_rules,
            {user.user_id for user in users},
            policy_ids,
        )
        hass.config_entries.async_update_entry(entry, options=options, version=6)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: FortiGatePolicyConfigEntry
) -> bool:
    """Unload the config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
