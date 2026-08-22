"""UI setup, reconfiguration, and options flows for FortiGate Policy Presence."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    FortiGateAuthError,
    FortiGateConnectionError,
    FortiGateError,
    FortiGateNotFoundError,
    FortiGatePolicyApi,
)
from .const import (
    CONF_ALLOWED_SSIDS,
    CONF_API_TOKEN,
    CONF_DEFAULT_OVERRIDE_MINUTES,
    CONF_FRIENDLY_NAME,
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_POLICIES,
    CONF_POLICY_AUTOMATION_DRY_RUN,
    CONF_POLICY_AUTOMATION_ENABLED,
    CONF_POLICY_IDS,
    CONF_POLICY_RULE_ACTION,
    CONF_POLICY_RULE_ID,
    CONF_POLICY_RULE_MATCH,
    CONF_POLICY_RULE_NAME,
    CONF_POLICY_RULE_POLICIES,
    CONF_POLICY_RULE_PRESENCE,
    CONF_POLICY_RULE_PRIORITY,
    CONF_POLICY_RULE_SCHEDULE,
    CONF_POLICY_RULE_USERS,
    CONF_POLICY_RULES_TO_REMOVE,
    CONF_POLICY_RULES_V2,
    CONF_POLL_INTERVAL,
    CONF_PRESENCE_USER_ID,
    CONF_PRESENCE_USER_MACS,
    CONF_PRESENCE_USER_NAME,
    CONF_PRESENCE_USERS,
    CONF_PRESENCE_USERS_TO_REMOVE,
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
    DEFAULT_PORT,
    DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
    DEFAULT_USER_AWAY_GRACE_PERIOD,
    DEFAULT_VDOM,
    DEFAULT_VERIFY_SSL,
    DEFAULT_WIFI_AWAY_GRACE_PERIOD,
    DEFAULT_WIFI_CLIENT_COUNT_SENSOR,
    DEFAULT_WIFI_POLL_INTERVAL,
    DEFAULT_WIFI_TRACKING_ENABLED,
    DOMAIN,
    MAX_OVERRIDE_MINUTES,
    MAX_POLL_INTERVAL,
    MAX_RECENT_CLIENT_RETENTION_DAYS,
    MAX_RECENT_WIFI_CLIENTS,
    MAX_USER_AWAY_GRACE_PERIOD,
    MAX_WIFI_AWAY_GRACE_PERIOD,
    MAX_WIFI_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_USER_AWAY_GRACE_PERIOD,
    MIN_WIFI_AWAY_GRACE_PERIOD,
    MIN_WIFI_POLL_INTERVAL,
    RULE_MATCH_ALL,
    RULE_MATCH_ANY,
    RULE_PRESENCE_AWAY,
    RULE_PRESENCE_HOME,
    STATUS_DISABLE,
    STATUS_ENABLE,
)
from .policy_config import (
    PolicyDefinition,
    configured_policies,
    fortigate_entry_title,
    parse_optional_policy_ids,
    serialize_policies,
)
from .policy_rules import serialize_policy_rules
from .presence_users import (
    aggregate_presence,
    configured_presence_users,
    serialize_presence_users,
)
from .wifi import FortiGateWifiClient, normalize_mac, utcnow

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _options_hub_summary(
    data: dict[str, Any], options: dict[str, Any]
) -> dict[str, str]:
    """Return bounded, non-sensitive counts for the configuration hub."""
    tracked = options.get(CONF_TRACKED_CLIENTS, {})
    users = options.get(CONF_PRESENCE_USERS, {})
    rules = options.get(CONF_POLICY_RULES_V2, {})
    automation_enabled = options.get(
        CONF_POLICY_AUTOMATION_ENABLED, DEFAULT_POLICY_AUTOMATION_ENABLED
    )
    dry_run = options.get(
        CONF_POLICY_AUTOMATION_DRY_RUN, DEFAULT_POLICY_AUTOMATION_DRY_RUN
    )
    automation = "Off" if not automation_enabled else "Dry run" if dry_run else "Active"
    return {
        "tracked": str(len(tracked) if isinstance(tracked, dict) else 0),
        "users": str(len(users) if isinstance(users, dict) else 0),
        "rules": str(len(rules) if isinstance(rules, dict) else 0),
        "policies": str(len(configured_policies(data))),
        "automation": automation,
    }


def _people_overview(options: dict[str, Any]) -> dict[str, str]:
    """Build a readable, non-editable summary of people and assigned devices."""
    tracked = options.get(CONF_TRACKED_CLIENTS, {})
    users = options.get(CONF_PRESENCE_USERS, {})
    if not isinstance(tracked, dict):
        tracked = {}
    if not isinstance(users, dict):
        users = {}

    def clean(value: object) -> str:
        return " ".join(str(value).split())

    tracked_by_mac = {
        normalized: metadata
        for raw_mac, metadata in tracked.items()
        if (normalized := normalize_mac(raw_mac)) is not None
    }

    lines: list[str] = []
    sortable_users = sorted(
        (user for user in users.values() if isinstance(user, dict)),
        key=lambda user: clean(user.get(CONF_PRESENCE_USER_NAME, "")).casefold(),
    )
    for user in sortable_users:
        name = clean(user.get(CONF_PRESENCE_USER_NAME, "Unnamed person"))
        device_names: list[str] = []
        for raw_mac in user.get(CONF_PRESENCE_USER_MACS, []):
            mac = normalize_mac(raw_mac)
            if mac is None:
                continue
            metadata = tracked_by_mac.get(mac, {})
            friendly_name = (
                metadata.get(CONF_FRIENDLY_NAME) if isinstance(metadata, dict) else None
            )
            device_names.append(clean(friendly_name or mac))
        devices = ", ".join(device_names) if device_names else "No assigned devices"
        try:
            grace = int(
                user.get(CONF_USER_AWAY_GRACE_PERIOD, DEFAULT_USER_AWAY_GRACE_PERIOD)
            )
        except (TypeError, ValueError):
            grace = DEFAULT_USER_AWAY_GRACE_PERIOD
        lines.append(f"• {name}: {devices} · Away grace {grace}s")

    return {
        "count": str(len(sortable_users)),
        "people": "\n\n".join(lines) if lines else "No people are configured.",
    }


def _connection_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the UI form for all connection-critical settings."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(CONF_VDOM, default=defaults.get(CONF_VDOM, DEFAULT_VDOM)): str,
            vol.Optional(
                CONF_POLICY_IDS, default=defaults.get(CONF_POLICY_IDS, "")
            ): str,
            vol.Required(
                CONF_API_TOKEN, default=defaults.get(CONF_API_TOKEN, "")
            ): TOKEN_SELECTOR,
            vol.Required(
                CONF_VERIFY_SSL,
                default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): bool,
        }
    )


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Return polling and diagnostic sensor options."""
    return vol.Schema(
        {
            vol.Required(
                CONF_POLL_INTERVAL,
                default=defaults.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
            ),
            vol.Required(
                CONF_WIFI_POLL_INTERVAL,
                default=defaults.get(
                    CONF_WIFI_POLL_INTERVAL, DEFAULT_WIFI_POLL_INTERVAL
                ),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_WIFI_POLL_INTERVAL, max=MAX_WIFI_POLL_INTERVAL),
            ),
            vol.Required(
                CONF_WIFI_AWAY_GRACE_PERIOD,
                default=defaults.get(
                    CONF_WIFI_AWAY_GRACE_PERIOD, DEFAULT_WIFI_AWAY_GRACE_PERIOD
                ),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(
                    min=MIN_WIFI_AWAY_GRACE_PERIOD,
                    max=MAX_WIFI_AWAY_GRACE_PERIOD,
                ),
            ),
            vol.Required(
                CONF_WIFI_CLIENT_COUNT_SENSOR,
                default=defaults.get(
                    CONF_WIFI_CLIENT_COUNT_SENSOR,
                    DEFAULT_WIFI_CLIENT_COUNT_SENSOR,
                ),
            ): bool,
            vol.Required(
                CONF_POLICY_AUTOMATION_ENABLED,
                default=defaults.get(
                    CONF_POLICY_AUTOMATION_ENABLED,
                    DEFAULT_POLICY_AUTOMATION_ENABLED,
                ),
            ): bool,
            vol.Required(
                CONF_POLICY_AUTOMATION_DRY_RUN,
                default=defaults.get(
                    CONF_POLICY_AUTOMATION_DRY_RUN,
                    DEFAULT_POLICY_AUTOMATION_DRY_RUN,
                ),
            ): bool,
            vol.Required(
                CONF_DEFAULT_OVERRIDE_MINUTES,
                default=defaults.get(
                    CONF_DEFAULT_OVERRIDE_MINUTES, DEFAULT_OVERRIDE_MINUTES
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_OVERRIDE_MINUTES)),
            vol.Required(
                CONF_RECENT_CLIENT_RETENTION_DAYS,
                default=defaults.get(
                    CONF_RECENT_CLIENT_RETENTION_DAYS,
                    DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
                ),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=1, max=MAX_RECENT_CLIENT_RETENTION_DAYS),
            ),
        }
    )


def _policy_options_schema(data: dict[str, Any]) -> vol.Schema:
    """Return the firewall-policy selection form."""
    policy_ids = ", ".join(policy.policy_id for policy in configured_policies(data))
    return vol.Schema(
        {
            vol.Optional(CONF_POLICY_IDS, default=policy_ids): TextSelector(
                TextSelectorConfig()
            )
        }
    )


def _selected_wifi_macs(selected: object, manual: object) -> list[str]:
    """Normalize and deduplicate selected and manually entered MAC addresses."""
    values: list[object] = []
    if isinstance(selected, list):
        values.extend(selected)
    if isinstance(manual, str):
        values.extend(
            value for value in manual.replace(";", ",").split(",") if value.strip()
        )
    result: list[str] = []
    for value in values:
        normalized = normalize_mac(value)
        if normalized is not None and normalized not in result:
            result.append(normalized)
    return result


def _preserved_client_names(
    selected_macs: list[str], tracked: object
) -> dict[str, dict[str, Any]]:
    """Keep tracker metadata for clients that remain selected."""
    if not isinstance(tracked, dict):
        return {}
    preserved: dict[str, dict[str, Any]] = {}
    for mac, metadata in tracked.items():
        normalized = normalize_mac(mac)
        if normalized not in selected_macs or not isinstance(metadata, dict):
            continue
        settings: dict[str, Any] = {}
        name = metadata.get(CONF_FRIENDLY_NAME)
        if isinstance(name, str) and name.strip():
            settings[CONF_FRIENDLY_NAME] = name.strip()
        allowed_ssids = metadata.get(CONF_ALLOWED_SSIDS)
        if isinstance(allowed_ssids, list):
            normalized_ssids = sorted(
                {ssid for ssid in allowed_ssids if isinstance(ssid, str) and ssid},
                key=str.casefold,
            )
            if normalized_ssids:
                settings[CONF_ALLOWED_SSIDS] = normalized_ssids
        if settings:
            preserved[normalized] = settings
    return preserved


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize and reject values that cannot safely form the CMDB URL."""
    data = dict(user_input)
    data[CONF_HOST] = data[CONF_HOST].strip()
    data[CONF_VDOM] = data[CONF_VDOM].strip()
    policy_ids = parse_optional_policy_ids(data.get(CONF_POLICY_IDS, ""))
    data[CONF_POLICY_IDS] = ", ".join(policy_ids)
    data[CONF_API_TOKEN] = data[CONF_API_TOKEN].strip()
    if (
        not data[CONF_HOST]
        or "://" in data[CONF_HOST]
        or "/" in data[CONF_HOST]
        or not data[CONF_VDOM]
        or not data[CONF_API_TOKEN]
    ):
        raise ValueError("Invalid connection input")
    return data


def _api_for_policy(
    hass: HomeAssistant,
    data: dict[str, Any],
    policy_id: str,
    expected_name: str,
) -> FortiGatePolicyApi:
    return FortiGatePolicyApi(
        async_get_clientsession(hass),
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        vdom=data[CONF_VDOM],
        policy_id=policy_id,
        expected_policy_name=expected_name,
        token=data[CONF_API_TOKEN],
        verify_ssl=data[CONF_VERIFY_SSL],
    )


async def _async_validate_input(
    hass: HomeAssistant, data: dict[str, Any]
) -> tuple[tuple[PolicyDefinition, ...], str]:
    """Validate every requested policy and capture its current name guard."""
    policies: list[PolicyDefinition] = []
    policy_ids = parse_optional_policy_ids(data.get(CONF_POLICY_IDS, ""))
    for policy_id in policy_ids:
        policy = await _api_for_policy(hass, data, policy_id, "").async_get_policy()
        policies.append(PolicyDefinition(policy.policy_id, policy.name))
    if not policy_ids:
        # A tracker-only entry still proves host, TLS, VDOM, token, and Wi-Fi
        # monitor access before it is saved.
        await _api_for_policy(hass, data, "", "").async_get_wifi_clients()
    return tuple(policies), fortigate_entry_title(data)


async def _async_validate_entry_data(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate all saved policy identity guards, for token reauthentication."""
    policies = configured_policies(data)
    for policy in policies:
        await _api_for_policy(
            hass, data, policy.policy_id, policy.expected_name
        ).async_get_policy()
    if not policies:
        await _api_for_policy(hass, data, "", "").async_get_wifi_clients()


def _entry_data(
    normalized: dict[str, Any], policies: tuple[PolicyDefinition, ...]
) -> dict[str, Any]:
    data = dict(normalized)
    data.pop(CONF_POLICY_IDS, None)
    data[CONF_POLICIES] = serialize_policies(policies)
    return data


def _error_key(err: Exception) -> str:
    """Translate expected setup failures into safe, actionable UI errors."""
    if isinstance(err, FortiGateAuthError):
        return "invalid_auth"
    if isinstance(err, FortiGateNotFoundError):
        return "not_found"
    if isinstance(err, FortiGateConnectionError):
        return "cannot_connect"
    if isinstance(err, ValueError):
        return "invalid_input"
    return "unknown"


class FortiGatePolicyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure the integration entirely through Home Assistant's UI."""

    VERSION = 6

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial configuration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = _normalize(user_input)
                policies, title = await _async_validate_input(self.hass, data)
            except (FortiGateError, ValueError) as err:
                errors["base"] = _error_key(err)
            else:
                unique_id = (
                    f"{data[CONF_HOST].lower()}:{data[CONF_PORT]}:"
                    f"{data[CONF_VDOM]}:{','.join(p.policy_id for p in policies)}"
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=title, data=_entry_data(data, policies)
                )

        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow the host, policy guard, TLS choice, and token to be changed in UI."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = _normalize(user_input)
                policies, _title = await _async_validate_input(self.hass, data)
            except (FortiGateError, ValueError) as err:
                errors["base"] = _error_key(err)
            else:
                identity = (
                    f"{data[CONF_HOST].lower()}:{data[CONF_PORT]}:"
                    f"{data[CONF_VDOM]}:{','.join(p.policy_id for p in policies)}"
                )
                if any(
                    configured.unique_id == identity
                    and configured.entry_id != entry.entry_id
                    for configured in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                # Keep the existing config-entry unique ID: FortiGate's CMDB
                # policy response does not provide a hardware-stable identifier.
                # The initial flow prevents duplicate endpoint/VDOM/policy tuples.
                updated_data = _entry_data(data, policies)
                legacy_primary = entry.data.get(CONF_LEGACY_PRIMARY_POLICY_ID)
                if legacy_primary and any(
                    policy.policy_id == legacy_primary for policy in policies
                ):
                    updated_data[CONF_LEGACY_PRIMARY_POLICY_ID] = legacy_primary
                self.hass.config_entries.async_update_entry(
                    entry, title=fortigate_entry_title(updated_data)
                )
                return self.async_update_reload_and_abort(
                    entry, data_updates=updated_data
                )

        # The saved token is deliberately never placed back into the UI form.
        defaults = dict(entry.data)
        defaults[CONF_POLICY_IDS] = ", ".join(
            policy.policy_id for policy in configured_policies(entry.data)
        )
        defaults[CONF_API_TOKEN] = ""
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(defaults),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start a token-only reauthentication flow after a 401/403 response."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate and save a replacement token without displaying the old one."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_API_TOKEN: user_input[CONF_API_TOKEN].strip()}
            try:
                if not data[CONF_API_TOKEN]:
                    raise ValueError("Empty API token")
                await _async_validate_entry_data(self.hass, data)
            except (FortiGateError, ValueError) as err:
                errors["base"] = _error_key(err)
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return a reloading options flow for polling cadence."""
        return FortiGatePolicyOptionsFlow()


class FortiGatePolicyOptionsFlow(OptionsFlowWithReload):
    """Configure policy polling and opt-in Wi-Fi presence without YAML."""

    _SELECTED_CLIENTS = "selected_wifi_clients"
    _MANUAL_MACS = "manual_wifi_macs"
    _TRACKERS_TO_REMOVE = "wifi_trackers_to_remove"
    _FILTER_TRACKER = "wifi_filter_tracker"

    def __init__(self) -> None:
        """Keep state during the select-then-name native options flow."""
        self._new_options: dict[str, Any] = {}
        self._recent_clients: dict[str, dict[str, str]] = {}
        self._selected_macs: list[str] = []
        self._named_clients: dict[str, dict[str, Any]] = {}
        self._presence_user_id: str | None = None
        self._policy_rule_id: str | None = None
        self._pending_policy_rule: dict[str, Any] | None = None
        self._pending_guided_user: dict[str, Any] | None = None
        self._tracker_filter_mac: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a person-centered configuration hub with a status summary."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "guided_parental_control",
                "people_devices",
                "parental_controls",
                "firewall_policies",
                "advanced_settings",
            ],
            description_placeholders=_options_hub_summary(
                dict(self.config_entry.data), dict(self.config_entry.options)
            ),
        )

    async def async_step_people_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Group tracker and person management in one focused submenu."""
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        tracked_count = len(tracked) if isinstance(tracked, dict) else 0
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        user_count = len(users) if isinstance(users, dict) else 0
        menu = ["wifi_clients"]
        if tracked_count:
            menu.extend(
                ["wifi_tracker_filters", "presence_users", "remove_wifi_trackers"]
            )
        overview = _people_overview(dict(self.config_entry.options))
        return self.async_show_menu(
            step_id="people_devices",
            menu_options=menu,
            description_placeholders={
                "tracked": str(tracked_count),
                "users": str(user_count),
                "people": overview["people"],
            },
        )

    async def async_step_parental_controls(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Group guided setup and rule management away from device discovery."""
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
        menu = ["guided_parental_control"]
        if (
            isinstance(users, dict)
            and users
            and configured_policies(self.config_entry.data)
        ):
            menu.append("policy_rules")
        return self.async_show_menu(
            step_id="parental_controls",
            menu_options=menu,
            description_placeholders={
                "users": str(len(users) if isinstance(users, dict) else 0),
                "rules": str(len(rules) if isinstance(rules, dict) else 0),
            },
        )

    async def async_step_advanced_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep polling and automation safety controls out of the main path."""
        return self.async_show_menu(
            step_id="advanced_settings", menu_options=["wifi_settings"]
        )

    async def async_step_guided_parental_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start the shortest safe path to a person and one policy rule."""
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        if not isinstance(tracked, dict) or not tracked:
            return await self.async_step_wifi_clients()
        if not configured_policies(self.config_entry.data):
            return await self.async_step_guided_policy_required()
        if not self._unassigned_tracker_options():
            users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
            if isinstance(users, dict) and users:
                return await self.async_step_guided_existing_person(user_input)
            return await self.async_step_wifi_clients()
        self._presence_user_id = self._presence_user_id or uuid4().hex
        return await self.async_step_guided_person(user_input)

    async def async_step_guided_policy_required(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain why policy control is required by the automation wizard."""
        return self.async_show_menu(
            step_id="guided_policy_required",
            menu_options=["firewall_policies", "presence_users"],
        )

    async def async_step_guided_existing_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use an existing person when every tracker is already assigned."""
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(users, dict) or not users:
            return await self.async_step_presence_users()
        if user_input is not None:
            selected = user_input.get(CONF_PRESENCE_USER_ID)
            if isinstance(selected, str) and selected in users:
                self._presence_user_id = selected
                self._pending_guided_user = None
                return await self.async_step_guided_policy()
        return self.async_show_form(
            step_id="guided_existing_person",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRESENCE_USER_ID): SelectSelector(
                        SelectSelectorConfig(options=self._user_options())
                    )
                }
            ),
        )

    def _guided_person_name(self) -> str | None:
        """Return the new or existing person selected by guided setup."""
        if self._pending_guided_user is not None:
            return str(self._pending_guided_user[CONF_PRESENCE_USER_NAME])
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(users, dict) or self._presence_user_id is None:
            return None
        user = users.get(self._presence_user_id)
        if not isinstance(user, dict):
            return None
        name = str(user.get(CONF_PRESENCE_USER_NAME, "")).strip()
        return name or None

    async def async_step_guided_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the stable person identity and all of their Wi-Fi devices."""
        if self._presence_user_id is None:
            return await self.async_step_guided_parental_control()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_PRESENCE_USER_NAME, "")).strip()
            selected_macs = {
                normalized
                for mac in user_input.get(CONF_PRESENCE_USER_MACS, [])
                if (normalized := normalize_mac(mac)) is not None
            }
            assigned = self._assigned_tracker_macs()
            if not name:
                errors[CONF_PRESENCE_USER_NAME] = "required"
            elif not selected_macs:
                errors[CONF_PRESENCE_USER_MACS] = "select_user_device"
            elif selected_macs & assigned:
                errors[CONF_PRESENCE_USER_MACS] = "device_already_assigned"
            else:
                self._pending_guided_user = {
                    CONF_PRESENCE_USER_NAME: name,
                    CONF_PRESENCE_USER_MACS: sorted(selected_macs),
                    CONF_USER_AWAY_GRACE_PERIOD: int(
                        user_input.get(
                            CONF_USER_AWAY_GRACE_PERIOD,
                            DEFAULT_USER_AWAY_GRACE_PERIOD,
                        )
                    ),
                }
                return await self.async_step_guided_policy()
        return self.async_show_form(
            step_id="guided_person",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRESENCE_USER_NAME): TextSelector(
                        TextSelectorConfig()
                    ),
                    vol.Required(CONF_PRESENCE_USER_MACS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=self._unassigned_tracker_options(), multiple=True
                        )
                    ),
                    vol.Required(
                        CONF_USER_AWAY_GRACE_PERIOD,
                        default=self.config_entry.options.get(
                            CONF_WIFI_AWAY_GRACE_PERIOD,
                            DEFAULT_USER_AWAY_GRACE_PERIOD,
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_USER_AWAY_GRACE_PERIOD,
                            max=MAX_USER_AWAY_GRACE_PERIOD,
                            step=15,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_guided_policy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect policy behavior before using the normal verified preview."""
        person = self._guided_person_name()
        if self._presence_user_id is None or person is None:
            return await self.async_step_guided_parental_control()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not str(user_input.get(CONF_POLICY_RULE_NAME, "")).strip():
                errors[CONF_POLICY_RULE_NAME] = "required"
            elif not user_input.get(CONF_POLICY_RULE_POLICIES):
                errors[CONF_POLICY_RULE_POLICIES] = "select_rule_policy"
            else:
                self._policy_rule_id = self._policy_rule_id or uuid4().hex
                self._pending_policy_rule = {
                    CONF_POLICY_RULE_NAME: str(
                        user_input[CONF_POLICY_RULE_NAME]
                    ).strip(),
                    CONF_POLICY_RULE_USERS: [self._presence_user_id],
                    CONF_POLICY_RULE_MATCH: RULE_MATCH_ANY,
                    CONF_POLICY_RULE_PRESENCE: user_input[CONF_POLICY_RULE_PRESENCE],
                    CONF_POLICY_RULE_ACTION: user_input[CONF_POLICY_RULE_ACTION],
                    CONF_POLICY_RULE_POLICIES: list(
                        user_input[CONF_POLICY_RULE_POLICIES]
                    ),
                    CONF_POLICY_RULE_PRIORITY: int(
                        user_input[CONF_POLICY_RULE_PRIORITY]
                    ),
                    CONF_POLICY_RULE_SCHEDULE: str(
                        user_input.get(CONF_POLICY_RULE_SCHEDULE, "")
                    ),
                }
                return await self.async_step_guided_policy_rule_preview()
        policy_options = [
            {
                "value": policy.policy_id,
                "label": f"{policy.expected_name or 'Policy'} ({policy.policy_id})",
            }
            for policy in configured_policies(self.config_entry.data)
        ]
        return self.async_show_form(
            step_id="guided_policy",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLICY_RULE_NAME, default=f"{person} policy"
                    ): TextSelector(TextSelectorConfig()),
                    vol.Required(
                        CONF_POLICY_RULE_PRESENCE, default=RULE_PRESENCE_AWAY
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[RULE_PRESENCE_HOME, RULE_PRESENCE_AWAY],
                            translation_key="policy_rule_presence",
                        )
                    ),
                    vol.Required(
                        CONF_POLICY_RULE_ACTION, default=STATUS_DISABLE
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[STATUS_ENABLE, STATUS_DISABLE],
                            translation_key="policy_rule_action",
                        )
                    ),
                    vol.Required(CONF_POLICY_RULE_POLICIES): SelectSelector(
                        SelectSelectorConfig(options=policy_options, multiple=True)
                    ),
                    vol.Required(CONF_POLICY_RULE_PRIORITY, default=50): NumberSelector(
                        NumberSelectorConfig(
                            min=0, max=100, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_POLICY_RULE_SCHEDULE): EntitySelector(
                        EntitySelectorConfig(domain="schedule")
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_remove_wifi_trackers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Permanently remove selected tracker entities and devices on reload."""
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        if not isinstance(tracked, dict) or not tracked:
            return self.async_abort(reason="no_trackers")
        errors: dict[str, str] = {}
        if user_input is not None:
            remove = {
                normalized
                for value in user_input.get(self._TRACKERS_TO_REMOVE, [])
                if (normalized := normalize_mac(value)) is not None
            }
            if not remove:
                errors["base"] = "select_tracker_to_remove"
            else:
                remaining = {
                    mac: metadata
                    for mac, metadata in tracked.items()
                    if normalize_mac(mac) not in remove
                }
                current_users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
                users = serialize_presence_users(
                    current_users if isinstance(current_users, dict) else {},
                    {
                        normalized
                        for mac in remaining
                        if (normalized := normalize_mac(mac)) is not None
                    },
                    {
                        policy.policy_id
                        for policy in configured_policies(self.config_entry.data)
                    },
                )
                return self.async_create_entry(
                    data={
                        **dict(self.config_entry.options),
                        CONF_TRACKED_CLIENTS: remaining,
                        CONF_PRESENCE_USERS: users,
                        CONF_POLICY_RULES_V2: serialize_policy_rules(
                            self.config_entry.options.get(CONF_POLICY_RULES_V2, {}),
                            set(users),
                            {
                                policy.policy_id
                                for policy in configured_policies(
                                    self.config_entry.data
                                )
                            },
                        ),
                    }
                )

        options = [
            {
                "value": mac,
                "label": (
                    f"{metadata.get(CONF_FRIENDLY_NAME, mac)} ({mac})"
                    if isinstance(metadata, dict)
                    else mac
                ),
            }
            for mac, metadata in sorted(tracked.items())
        ]
        return self.async_show_form(
            step_id="remove_wifi_trackers",
            data_schema=vol.Schema(
                {
                    vol.Optional(self._TRACKERS_TO_REMOVE, default=[]): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_firewall_policies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add or remove policy switches using the saved connection settings."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = _normalize({**entry.data, **user_input})
                policies, _title = await _async_validate_input(self.hass, data)
            except (FortiGateError, ValueError) as err:
                errors["base"] = _error_key(err)
            else:
                updated_data = _entry_data(data, policies)
                legacy_primary = entry.data.get(CONF_LEGACY_PRIMARY_POLICY_ID)
                if legacy_primary and any(
                    policy.policy_id == legacy_primary for policy in policies
                ):
                    updated_data[CONF_LEGACY_PRIMARY_POLICY_ID] = legacy_primary
                self.hass.config_entries.async_update_entry(
                    entry,
                    data=updated_data,
                    title=fortigate_entry_title(updated_data),
                )
                options = dict(entry.options)
                current_users = options.get(CONF_PRESENCE_USERS, {})
                options[CONF_PRESENCE_USERS] = serialize_presence_users(
                    current_users if isinstance(current_users, dict) else {},
                    set(self._selected_from_options()),
                    {policy.policy_id for policy in policies},
                )
                options[CONF_POLICY_RULES_V2] = serialize_policy_rules(
                    options.get(CONF_POLICY_RULES_V2, {}),
                    set(options[CONF_PRESENCE_USERS]),
                    {policy.policy_id for policy in policies},
                )
                return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="firewall_policies",
            data_schema=_policy_options_schema(dict(entry.data)),
            errors=errors,
        )

    async def async_step_presence_users(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer native UI actions for multi-device user profiles."""
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        if not isinstance(tracked, dict) or not tracked:
            return self.async_abort(reason="no_trackers")
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        menu_options = ["add_presence_user"]
        if isinstance(users, dict) and users:
            menu_options.extend(["edit_presence_user", "remove_presence_users"])
        return self.async_show_menu(
            step_id="presence_users",
            menu_options=menu_options,
        )

    def _tracker_options(self) -> list[dict[str, str]]:
        """Return selected trackers as labels suitable for a multi-selector."""
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        if not isinstance(tracked, dict):
            return []
        return [
            {
                "value": mac,
                "label": (
                    f"{metadata.get(CONF_FRIENDLY_NAME, mac)} ({mac})"
                    if isinstance(metadata, dict)
                    else mac
                ),
            }
            for mac, metadata in sorted(tracked.items())
            if normalize_mac(mac) is not None
        ]

    def _assigned_tracker_macs(self) -> set[str]:
        """Return MACs already owned by a configured person."""
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        return {
            normalized
            for raw_user in (users.values() if isinstance(users, dict) else [])
            if isinstance(raw_user, dict)
            for mac in raw_user.get(CONF_PRESENCE_USER_MACS, [])
            if (normalized := normalize_mac(mac)) is not None
        }

    def _unassigned_tracker_options(self) -> list[dict[str, str]]:
        """Offer only devices that can safely belong to a new person."""
        assigned = self._assigned_tracker_macs()
        return [
            option
            for option in self._tracker_options()
            if normalize_mac(option["value"]) not in assigned
        ]

    def _user_options(self) -> list[dict[str, str]]:
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(users, dict):
            return []
        return [
            {
                "value": user_id,
                "label": str(user.get(CONF_PRESENCE_USER_NAME, user_id)),
            }
            for user_id, user in sorted(users.items())
            if isinstance(user_id, str) and isinstance(user, dict)
        ]

    async def async_step_add_presence_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create one user from one or more selected device MACs."""
        self._presence_user_id = self._presence_user_id or uuid4().hex
        return await self._async_presence_user_form(user_input, {})

    async def async_step_edit_presence_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose an existing user before opening its profile form."""
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(users, dict) or not users:
            return self.async_abort(reason="no_presence_users")
        if user_input is not None:
            selected = user_input.get(CONF_PRESENCE_USER_ID)
            if isinstance(selected, str) and selected in users:
                self._presence_user_id = selected
                return await self.async_step_edit_presence_user_profile()
        return self.async_show_form(
            step_id="edit_presence_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRESENCE_USER_ID): SelectSelector(
                        SelectSelectorConfig(options=self._user_options())
                    )
                }
            ),
        )

    async def async_step_edit_presence_user_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit devices, name, and policy behavior without changing user identity."""
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        if (
            self._presence_user_id is None
            or not isinstance(users, dict)
            or not isinstance(users.get(self._presence_user_id), dict)
        ):
            return await self.async_step_edit_presence_user()
        return await self._async_presence_user_form(
            user_input, users[self._presence_user_id]
        )

    async def _async_presence_user_form(
        self, user_input: dict[str, Any] | None, current: dict[str, Any]
    ) -> ConfigFlowResult:
        """Save a stable user profile and its optional policy state rules."""
        if self._presence_user_id is None:
            return await self.async_step_presence_users()
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_PRESENCE_USER_NAME, "")).strip()
            selected_macs = {
                normalized
                for mac in user_input.get(CONF_PRESENCE_USER_MACS, [])
                if (normalized := normalize_mac(mac)) is not None
            }
            existing = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
            if not isinstance(existing, dict):
                existing = {}
            assigned_elsewhere = {
                normalized
                for user_id, raw_user in existing.items()
                if user_id != self._presence_user_id and isinstance(raw_user, dict)
                for mac in raw_user.get(CONF_PRESENCE_USER_MACS, [])
                if (normalized := normalize_mac(mac)) is not None
            }
            if not name:
                errors[CONF_PRESENCE_USER_NAME] = "required"
            elif not selected_macs:
                errors[CONF_PRESENCE_USER_MACS] = "select_user_device"
            elif selected_macs & assigned_elsewhere:
                errors[CONF_PRESENCE_USER_MACS] = "device_already_assigned"
            else:
                updated_users = dict(existing) if isinstance(existing, dict) else {}
                updated_users[self._presence_user_id] = {
                    CONF_PRESENCE_USER_NAME: name,
                    CONF_PRESENCE_USER_MACS: sorted(selected_macs),
                    CONF_USER_AWAY_GRACE_PERIOD: int(
                        user_input.get(
                            CONF_USER_AWAY_GRACE_PERIOD,
                            DEFAULT_USER_AWAY_GRACE_PERIOD,
                        )
                    ),
                }
                updated_users = serialize_presence_users(
                    updated_users,
                    set(self._selected_from_options()),
                    {
                        policy.policy_id
                        for policy in configured_policies(self.config_entry.data)
                    },
                )
                return self.async_create_entry(
                    data={
                        **dict(self.config_entry.options),
                        CONF_PRESENCE_USERS: updated_users,
                    }
                )

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_PRESENCE_USER_NAME,
                default=current.get(CONF_PRESENCE_USER_NAME, ""),
            ): TextSelector(TextSelectorConfig()),
            vol.Required(
                CONF_PRESENCE_USER_MACS,
                default=current.get(CONF_PRESENCE_USER_MACS, []),
            ): SelectSelector(
                SelectSelectorConfig(options=self._tracker_options(), multiple=True)
            ),
            vol.Required(
                CONF_USER_AWAY_GRACE_PERIOD,
                default=current.get(
                    CONF_USER_AWAY_GRACE_PERIOD,
                    self.config_entry.options.get(
                        CONF_WIFI_AWAY_GRACE_PERIOD,
                        DEFAULT_USER_AWAY_GRACE_PERIOD,
                    ),
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_USER_AWAY_GRACE_PERIOD,
                    max=MAX_USER_AWAY_GRACE_PERIOD,
                    step=15,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
        }
        return self.async_show_form(
            step_id=("edit_presence_user_profile" if current else "add_presence_user"),
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    async def async_step_remove_presence_users(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove selected aggregate users and their entities on reload."""
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        if not isinstance(users, dict) or not users:
            return self.async_abort(reason="no_presence_users")
        errors: dict[str, str] = {}
        if user_input is not None:
            remove = set(user_input.get(CONF_PRESENCE_USERS_TO_REMOVE, []))
            if not remove:
                errors["base"] = "select_presence_user_to_remove"
            else:
                remaining_users = {
                    user_id: user
                    for user_id, user in users.items()
                    if user_id not in remove
                }
                policy_ids = {
                    policy.policy_id
                    for policy in configured_policies(self.config_entry.data)
                }
                return self.async_create_entry(
                    data={
                        **dict(self.config_entry.options),
                        CONF_PRESENCE_USERS: remaining_users,
                        CONF_POLICY_RULES_V2: serialize_policy_rules(
                            self.config_entry.options.get(CONF_POLICY_RULES_V2, {}),
                            set(remaining_users),
                            policy_ids,
                        ),
                    }
                )
        return self.async_show_form(
            step_id="remove_presence_users",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRESENCE_USERS_TO_REMOVE): SelectSelector(
                        SelectSelectorConfig(
                            options=self._user_options(), multiple=True
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_policy_rules(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage policy-centric multi-user rules."""
        raw_rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
        menu = ["add_policy_rule"]
        if isinstance(raw_rules, dict) and raw_rules:
            menu.extend(["edit_policy_rule", "remove_policy_rules"])
        return self.async_show_menu(step_id="policy_rules", menu_options=menu)

    def _policy_rule_options(self) -> list[dict[str, str]]:
        rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
        if not isinstance(rules, dict):
            return []
        return [
            {
                "value": rule_id,
                "label": str(rule.get(CONF_POLICY_RULE_NAME, rule_id)),
            }
            for rule_id, rule in sorted(rules.items())
            if isinstance(rule_id, str) and isinstance(rule, dict)
        ]

    async def async_step_add_policy_rule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._policy_rule_id = self._policy_rule_id or uuid4().hex
        return await self._async_policy_rule_form(user_input, {})

    async def async_step_edit_policy_rule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
        if not isinstance(rules, dict) or not rules:
            return self.async_abort(reason="no_policy_rules")
        if user_input is not None:
            selected = user_input.get(CONF_POLICY_RULE_ID)
            if isinstance(selected, str) and selected in rules:
                self._policy_rule_id = selected
                return await self.async_step_edit_policy_rule_details()
        return self.async_show_form(
            step_id="edit_policy_rule",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLICY_RULE_ID): SelectSelector(
                        SelectSelectorConfig(options=self._policy_rule_options())
                    )
                }
            ),
        )

    async def async_step_edit_policy_rule_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
        if (
            self._policy_rule_id is None
            or not isinstance(rules, dict)
            or not isinstance(rules.get(self._policy_rule_id), dict)
        ):
            return await self.async_step_edit_policy_rule()
        return await self._async_policy_rule_form(
            user_input, rules[self._policy_rule_id]
        )

    async def _async_policy_rule_form(
        self, user_input: dict[str, Any] | None, current: dict[str, Any]
    ) -> ConfigFlowResult:
        if self._policy_rule_id is None:
            return await self.async_step_policy_rules()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not str(user_input.get(CONF_POLICY_RULE_NAME, "")).strip():
                errors[CONF_POLICY_RULE_NAME] = "required"
            elif not user_input.get(CONF_POLICY_RULE_USERS):
                errors[CONF_POLICY_RULE_USERS] = "select_rule_user"
            elif not user_input.get(CONF_POLICY_RULE_POLICIES):
                errors[CONF_POLICY_RULE_POLICIES] = "select_rule_policy"
            else:
                self._pending_policy_rule = {
                    CONF_POLICY_RULE_NAME: str(
                        user_input[CONF_POLICY_RULE_NAME]
                    ).strip(),
                    CONF_POLICY_RULE_USERS: list(user_input[CONF_POLICY_RULE_USERS]),
                    CONF_POLICY_RULE_MATCH: user_input[CONF_POLICY_RULE_MATCH],
                    CONF_POLICY_RULE_PRESENCE: user_input[CONF_POLICY_RULE_PRESENCE],
                    CONF_POLICY_RULE_ACTION: user_input[CONF_POLICY_RULE_ACTION],
                    CONF_POLICY_RULE_POLICIES: list(
                        user_input[CONF_POLICY_RULE_POLICIES]
                    ),
                    CONF_POLICY_RULE_PRIORITY: int(
                        user_input[CONF_POLICY_RULE_PRIORITY]
                    ),
                    CONF_POLICY_RULE_SCHEDULE: str(
                        user_input.get(CONF_POLICY_RULE_SCHEDULE, "")
                    ),
                }
                return await self.async_step_policy_rule_preview()

        policy_options = [
            {
                "value": policy.policy_id,
                "label": f"{policy.expected_name or 'Policy'} ({policy.policy_id})",
            }
            for policy in configured_policies(self.config_entry.data)
        ]
        schedule_default = current.get(CONF_POLICY_RULE_SCHEDULE)
        schedule_field = (
            vol.Optional(CONF_POLICY_RULE_SCHEDULE, default=schedule_default)
            if isinstance(schedule_default, str) and schedule_default
            else vol.Optional(CONF_POLICY_RULE_SCHEDULE)
        )
        return self.async_show_form(
            step_id=("edit_policy_rule_details" if current else "add_policy_rule"),
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLICY_RULE_NAME,
                        default=current.get(CONF_POLICY_RULE_NAME, ""),
                    ): TextSelector(TextSelectorConfig()),
                    vol.Required(
                        CONF_POLICY_RULE_USERS,
                        default=current.get(CONF_POLICY_RULE_USERS, []),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=self._user_options(), multiple=True
                        )
                    ),
                    vol.Required(
                        CONF_POLICY_RULE_MATCH,
                        default=current.get(CONF_POLICY_RULE_MATCH, RULE_MATCH_ANY),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[RULE_MATCH_ANY, RULE_MATCH_ALL],
                            translation_key="policy_rule_match",
                        )
                    ),
                    vol.Required(
                        CONF_POLICY_RULE_PRESENCE,
                        default=current.get(
                            CONF_POLICY_RULE_PRESENCE, RULE_PRESENCE_HOME
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[RULE_PRESENCE_HOME, RULE_PRESENCE_AWAY],
                            translation_key="policy_rule_presence",
                        )
                    ),
                    vol.Required(
                        CONF_POLICY_RULE_ACTION,
                        default=current.get(CONF_POLICY_RULE_ACTION, STATUS_DISABLE),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[STATUS_ENABLE, STATUS_DISABLE],
                            translation_key="policy_rule_action",
                        )
                    ),
                    vol.Required(
                        CONF_POLICY_RULE_POLICIES,
                        default=current.get(CONF_POLICY_RULE_POLICIES, []),
                    ): SelectSelector(
                        SelectSelectorConfig(options=policy_options, multiple=True)
                    ),
                    vol.Required(
                        CONF_POLICY_RULE_PRIORITY,
                        default=current.get(CONF_POLICY_RULE_PRIORITY, 50),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=0, max=100, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    schedule_field: EntitySelector(
                        EntitySelectorConfig(domain="schedule")
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_policy_rule_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Require an explicit confirmation after showing the exact rule effect."""
        return await self._async_policy_rule_preview(user_input, "policy_rule_preview")

    async def async_step_guided_policy_rule_preview(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the final step of the guided person-and-rule workflow."""
        return await self._async_policy_rule_preview(
            user_input, "guided_policy_rule_preview"
        )

    async def _async_policy_rule_preview(
        self, user_input: dict[str, Any] | None, step_id: str
    ) -> ConfigFlowResult:
        """Validate and persist a normal or guided rule after confirmation."""
        if self._policy_rule_id is None or self._pending_policy_rule is None:
            return await self.async_step_policy_rules()
        errors: dict[str, str] = {}
        if user_input is not None and user_input.get("confirm") is True:
            rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
            updated = dict(rules) if isinstance(rules, dict) else {}
            updated[self._policy_rule_id] = self._pending_policy_rule
            options = dict(self.config_entry.options)
            user_ids = set(self._user_ids())
            if self._pending_guided_user is not None:
                users = options.get(CONF_PRESENCE_USERS, {})
                updated_users = dict(users) if isinstance(users, dict) else {}
                updated_users[self._presence_user_id] = self._pending_guided_user
                updated_users = serialize_presence_users(
                    updated_users,
                    set(self._selected_from_options()),
                    {
                        policy.policy_id
                        for policy in configured_policies(self.config_entry.data)
                    },
                )
                options[CONF_PRESENCE_USERS] = updated_users
                user_ids = set(updated_users)
            policy_ids = {
                policy.policy_id
                for policy in configured_policies(self.config_entry.data)
            }
            normalized = serialize_policy_rules(updated, user_ids, policy_ids)
            return self.async_create_entry(
                data={
                    **options,
                    CONF_POLICY_RULES_V2: normalized,
                }
            )
        if user_input is not None:
            errors["confirm"] = "confirmation_required"
        pending = self._pending_policy_rule
        users = ", ".join(
            option["label"]
            for option in self._user_options()
            if option["value"] in pending[CONF_POLICY_RULE_USERS]
        )
        if self._pending_guided_user is not None:
            users = str(self._pending_guided_user[CONF_PRESENCE_USER_NAME])
        policies = ", ".join(pending[CONF_POLICY_RULE_POLICIES])
        schedule = pending.get(CONF_POLICY_RULE_SCHEDULE) or "Always"
        current_states = (
            "New user will be evaluated after save"
            if self._pending_guided_user is not None
            else "Waiting for a valid Wi-Fi update"
        )
        runtime = getattr(self.config_entry, "runtime_data", None)
        wifi = getattr(runtime, "wifi_coordinator", None)
        if (
            self._pending_guided_user is None
            and wifi is not None
            and wifi.last_update_success
            and wifi.data is not None
        ):
            configured_users = configured_presence_users(
                self.config_entry.options,
                set(self._selected_from_options()),
                {
                    policy.policy_id
                    for policy in configured_policies(self.config_entry.data)
                },
            )
            selected_users = set(pending[CONF_POLICY_RULE_USERS])
            current_states = (
                ", ".join(
                    f"{user.name}="
                    + (
                        "home"
                        if (
                            state := aggregate_presence(
                                user, wifi.data.presence, utcnow()
                            )
                        )
                        is True
                        else "away"
                        if state is False
                        else "unknown"
                    )
                    for user in configured_users
                    if user.user_id in selected_users
                )
                or current_states
            )
        raw_rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
        conflict = "None detected"
        if isinstance(raw_rules, dict) and any(
            isinstance(rule, dict)
            and rule.get(CONF_POLICY_RULE_PRIORITY)
            == pending[CONF_POLICY_RULE_PRIORITY]
            and rule.get(CONF_POLICY_RULE_ACTION) != pending[CONF_POLICY_RULE_ACTION]
            and set(rule.get(CONF_POLICY_RULE_POLICIES, []))
            & set(pending[CONF_POLICY_RULE_POLICIES])
            for rule_id, rule in raw_rules.items()
            if rule_id != self._policy_rule_id
        ):
            conflict = (
                "Possible equal-priority conflict; disable will win if both match"
            )
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={
                "name": pending[CONF_POLICY_RULE_NAME],
                "users": users,
                "match": pending[CONF_POLICY_RULE_MATCH],
                "presence": pending[CONF_POLICY_RULE_PRESENCE],
                "action": pending[CONF_POLICY_RULE_ACTION],
                "policies": policies,
                "priority": str(pending[CONF_POLICY_RULE_PRIORITY]),
                "schedule": str(schedule),
                "current_states": current_states,
                "conflict": conflict,
            },
            errors=errors,
        )

    def _user_ids(self) -> list[str]:
        users = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
        return list(users) if isinstance(users, dict) else []

    async def async_step_remove_policy_rules(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        rules = self.config_entry.options.get(CONF_POLICY_RULES_V2, {})
        if not isinstance(rules, dict) or not rules:
            return self.async_abort(reason="no_policy_rules")
        errors: dict[str, str] = {}
        if user_input is not None:
            remove = set(user_input.get(CONF_POLICY_RULES_TO_REMOVE, []))
            if not remove:
                errors["base"] = "select_policy_rule_to_remove"
            else:
                return self.async_create_entry(
                    data={
                        **dict(self.config_entry.options),
                        CONF_POLICY_RULES_V2: {
                            rule_id: rule
                            for rule_id, rule in rules.items()
                            if rule_id not in remove
                        },
                    }
                )
        return self.async_show_form(
            step_id="remove_policy_rules",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLICY_RULES_TO_REMOVE): SelectSelector(
                        SelectSelectorConfig(
                            options=self._policy_rule_options(), multiple=True
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_wifi_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage polling cadence and the optional client-count sensor."""
        if user_input is not None:
            return self.async_create_entry(
                data={**dict(self.config_entry.options), **user_input}
            )
        return self.async_show_form(
            step_id="wifi_settings",
            data_schema=_settings_schema(dict(self.config_entry.options)),
        )

    async def async_step_wifi_tracker_filters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a tracked wireless client whose SSID scope should change."""
        if not self._tracker_options():
            return self.async_abort(reason="no_trackers")
        if user_input is not None:
            selected = normalize_mac(user_input.get(self._FILTER_TRACKER))
            if selected in set(self._selected_from_options()):
                self._tracker_filter_mac = selected
                return await self.async_step_wifi_tracker_filter()
        return self.async_show_form(
            step_id="wifi_tracker_filters",
            data_schema=vol.Schema(
                {
                    vol.Required(self._FILTER_TRACKER): SelectSelector(
                        SelectSelectorConfig(options=self._tracker_options())
                    )
                }
            ),
        )

    async def async_step_wifi_tracker_filter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Limit one tracker to associations on selected SSIDs."""
        mac = self._tracker_filter_mac
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        tracked_key = (
            next((key for key in tracked if normalize_mac(key) == mac), None)
            if mac is not None and isinstance(tracked, dict)
            else None
        )
        if mac is None or not isinstance(tracked, dict) or tracked_key is None:
            return await self.async_step_wifi_tracker_filters()
        metadata = tracked.get(tracked_key)
        current = dict(metadata) if isinstance(metadata, dict) else {}
        if user_input is not None:
            raw_ssids = user_input.get(CONF_ALLOWED_SSIDS, [])
            if not isinstance(raw_ssids, list):
                raw_ssids = []
            allowed_ssids = sorted(
                {ssid for ssid in raw_ssids if isinstance(ssid, str) and ssid},
                key=str.casefold,
            )
            if allowed_ssids:
                current[CONF_ALLOWED_SSIDS] = allowed_ssids
            else:
                current.pop(CONF_ALLOWED_SSIDS, None)
            updated = dict(tracked)
            if tracked_key != mac:
                updated.pop(tracked_key, None)
            updated[mac] = current
            return self.async_create_entry(
                data={
                    **dict(self.config_entry.options),
                    CONF_TRACKED_CLIENTS: updated,
                }
            )

        current_ssids = current.get(CONF_ALLOWED_SSIDS, [])
        if not isinstance(current_ssids, list):
            current_ssids = []
        return self.async_show_form(
            step_id="wifi_tracker_filter",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ALLOWED_SSIDS, default=current_ssids
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=self._known_ssids(),
                            multiple=True,
                            custom_value=True,
                        )
                    )
                }
            ),
            description_placeholders={"tracker": self._tracker_name(mac)},
        )

    async def async_step_wifi_clients(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover associated stations and select only the MACs to track."""
        errors: dict[str, str] = {}
        if not self._new_options:
            self._new_options = dict(self.config_entry.options)
        if user_input is not None:
            enabled = user_input[CONF_WIFI_TRACKING_ENABLED]
            self._new_options[CONF_WIFI_TRACKING_ENABLED] = enabled
            if not enabled:
                self._new_options[CONF_RECENT_WIFI_CLIENTS] = self._recent_clients
                return self.async_create_entry(data=self._new_options)
            self._selected_macs = _selected_wifi_macs(
                user_input.get(self._SELECTED_CLIENTS, []),
                user_input.get(self._MANUAL_MACS, ""),
            )
            if not self._selected_macs:
                errors["base"] = "select_at_least_one"
                return self._wifi_clients_form(errors)
            self._named_clients = _preserved_client_names(
                self._selected_macs,
                self.config_entry.options.get(CONF_TRACKED_CLIENTS, {}),
            )
            if len(self._named_clients) == len(self._selected_macs):
                return self._finish_wifi_clients()
            return await self.async_step_wifi_client_name()

        try:
            clients, _skipped, _version = await self._async_discover_wifi_clients()
        except FortiGateError as err:
            errors["base"] = _error_key(err)
            clients = {}
        self._recent_clients = self._merged_recent_clients(clients)
        return self._wifi_clients_form(errors)

    def _wifi_clients_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Build the discovery form with an explicit manual-MAC fallback."""
        name_counts: dict[str, int] = {}
        for metadata in self._recent_clients.values():
            hostname = metadata.get("hostname", "").strip().lower()
            if hostname:
                name_counts[hostname] = name_counts.get(hostname, 0) + 1
        options = [
            {
                "value": mac,
                "label": self._client_label(mac, metadata)
                + (
                    " — duplicate device name"
                    if name_counts.get(metadata.get("hostname", "").strip().lower(), 0)
                    > 1
                    else ""
                ),
            }
            for mac, metadata in sorted(self._recent_clients.items())
        ]
        return self.async_show_form(
            step_id="wifi_clients",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_WIFI_TRACKING_ENABLED,
                        default=self._new_options.get(
                            CONF_WIFI_TRACKING_ENABLED,
                            bool(self._selected_from_options())
                            or DEFAULT_WIFI_TRACKING_ENABLED,
                        ),
                    ): bool,
                    vol.Required(
                        self._SELECTED_CLIENTS,
                        default=self._selected_from_options(),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            custom_value=True,
                        )
                    ),
                    vol.Optional(self._MANUAL_MACS, default=""): TextSelector(
                        TextSelectorConfig()
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"count": str(len(options))},
        )

    async def async_step_wifi_client_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Assign a friendly name without using it as the stable MAC identity."""
        mac = next(mac for mac in self._selected_macs if mac not in self._named_clients)
        if user_input is not None:
            name = user_input[CONF_FRIENDLY_NAME].strip() or self._default_name(mac)
            self._named_clients[mac] = {CONF_FRIENDLY_NAME: name}
            if len(self._named_clients) == len(self._selected_macs):
                return self._finish_wifi_clients()
            mac = next(
                mac for mac in self._selected_macs if mac not in self._named_clients
            )

        return self.async_show_form(
            step_id="wifi_client_name",
            data_schema=vol.Schema(
                {vol.Required(CONF_FRIENDLY_NAME, default=self._default_name(mac)): str}
            ),
            description_placeholders={"mac": mac, "client": self._default_name(mac)},
        )

    def _finish_wifi_clients(self) -> ConfigFlowResult:
        """Persist the exact selected tracker set and bounded discovery cache."""
        self._new_options[CONF_WIFI_TRACKING_ENABLED] = True
        self._new_options[CONF_TRACKED_CLIENTS] = self._named_clients
        self._new_options[CONF_RECENT_WIFI_CLIENTS] = self._recent_clients
        current_users = self._new_options.get(CONF_PRESENCE_USERS, {})
        self._new_options[CONF_PRESENCE_USERS] = serialize_presence_users(
            current_users if isinstance(current_users, dict) else {},
            set(self._selected_macs),
            {
                policy.policy_id
                for policy in configured_policies(self.config_entry.data)
            },
        )
        self._new_options[CONF_POLICY_RULES_V2] = serialize_policy_rules(
            self._new_options.get(CONF_POLICY_RULES_V2, {}),
            set(self._new_options[CONF_PRESENCE_USERS]),
            {
                policy.policy_id
                for policy in configured_policies(self.config_entry.data)
            },
        )
        return self.async_create_entry(data=self._new_options)

    async def _async_discover_wifi_clients(
        self,
    ) -> tuple[dict[str, FortiGateWifiClient], int, str | None]:
        """Reuse the integration's authenticated FortiGate API client."""
        data = self.config_entry.data
        api = _api_for_policy(self.hass, data, "", "")
        return await api.async_get_wifi_client_catalog()

    def _selected_from_options(self) -> list[str]:
        """Retain explicitly selected offline devices in the discovery UI."""
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        if not isinstance(tracked, dict):
            return []
        return [
            normalized
            for mac in tracked
            if (normalized := normalize_mac(mac)) is not None
        ]

    def _merged_recent_clients(
        self, clients: dict[str, FortiGateWifiClient]
    ) -> dict[str, dict[str, str]]:
        """Keep a bounded, serializable discovery cache and offline selections."""
        existing = self.config_entry.options.get(CONF_RECENT_WIFI_CLIENTS, {})
        recent: dict[str, dict[str, str]] = {}
        if isinstance(existing, dict):
            cutoff = utcnow() - timedelta(
                days=int(
                    self.config_entry.options.get(
                        CONF_RECENT_CLIENT_RETENTION_DAYS,
                        DEFAULT_RECENT_CLIENT_RETENTION_DAYS,
                    )
                )
            )
            for mac, metadata in existing.items():
                normalized = normalize_mac(mac)
                if normalized and isinstance(metadata, dict):
                    last_seen = metadata.get("last_seen")
                    if isinstance(last_seen, str):
                        try:
                            parsed_seen = datetime.fromisoformat(last_seen)
                            if parsed_seen.tzinfo is not None and parsed_seen < cutoff:
                                continue
                        except ValueError:
                            pass
                    recent[normalized] = {
                        key: value
                        for key, value in metadata.items()
                        if isinstance(value, str)
                    }
        seen_at = utcnow()
        for mac, client in clients.items():
            recent[mac] = client.as_recent_metadata(seen_at)
        for mac in self._selected_from_options():
            recent.setdefault(mac, {})
        return dict(
            sorted(
                recent.items(),
                key=lambda item: item[1].get("last_seen", ""),
                reverse=True,
            )[:MAX_RECENT_WIFI_CLIENTS]
        )

    def _client_label(self, mac: str, metadata: dict[str, str]) -> str:
        name = metadata.get("hostname") or mac
        details = ", ".join(
            value
            for value in (
                metadata.get("ip"),
                metadata.get("ssid"),
                metadata.get("ap_name"),
            )
            if value
        )
        if metadata.get("last_seen"):
            details = ", ".join(
                value
                for value in (
                    details,
                    f"last seen {metadata['last_seen'][:16].replace('T', ' ')}",
                )
                if value
            )
        return f"{name} ({mac})" + (f" — {details}" if details else "")

    def _default_name(self, mac: str) -> str:
        return self._recent_clients.get(mac, {}).get("hostname") or mac

    def _tracker_name(self, mac: str) -> str:
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        if isinstance(tracked, dict):
            metadata = tracked.get(mac)
            if isinstance(metadata, dict):
                name = metadata.get(CONF_FRIENDLY_NAME)
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return mac

    def _known_ssids(self) -> list[str]:
        """Return recently observed and already configured wireless networks."""
        ssids = {
            metadata["ssid"]
            for metadata in self._recent_clients.values()
            if isinstance(metadata.get("ssid"), str) and metadata["ssid"]
        }
        recent = self.config_entry.options.get(CONF_RECENT_WIFI_CLIENTS, {})
        if isinstance(recent, dict):
            ssids.update(
                metadata["ssid"]
                for metadata in recent.values()
                if isinstance(metadata, dict)
                and isinstance(metadata.get("ssid"), str)
                and metadata["ssid"]
            )
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        if isinstance(tracked, dict):
            for metadata in tracked.values():
                if not isinstance(metadata, dict):
                    continue
                allowed = metadata.get(CONF_ALLOWED_SSIDS, [])
                if isinstance(allowed, list):
                    ssids.update(
                        ssid for ssid in allowed if isinstance(ssid, str) and ssid
                    )
        return sorted(ssids, key=str.casefold)
