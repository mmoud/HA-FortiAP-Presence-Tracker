"""UI setup, reconfiguration, and options flows for FortiGate Policy Presence."""

from __future__ import annotations

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
    CONF_API_TOKEN,
    CONF_AWAY_DISABLE_POLICIES,
    CONF_AWAY_ENABLE_POLICIES,
    CONF_FRIENDLY_NAME,
    CONF_HOME_DISABLE_POLICIES,
    CONF_HOME_ENABLE_POLICIES,
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_POLICIES,
    CONF_POLICY_IDS,
    CONF_POLL_INTERVAL,
    CONF_PRESENCE_USER_ID,
    CONF_PRESENCE_USER_MACS,
    CONF_PRESENCE_USER_NAME,
    CONF_PRESENCE_USERS,
    CONF_PRESENCE_USERS_TO_REMOVE,
    CONF_RECENT_WIFI_CLIENTS,
    CONF_TRACKED_CLIENTS,
    CONF_VDOM,
    CONF_VERIFY_SSL,
    CONF_WIFI_AWAY_GRACE_PERIOD,
    CONF_WIFI_CLIENT_COUNT_SENSOR,
    CONF_WIFI_POLL_INTERVAL,
    CONF_WIFI_TRACKING_ENABLED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_VDOM,
    DEFAULT_VERIFY_SSL,
    DEFAULT_WIFI_AWAY_GRACE_PERIOD,
    DEFAULT_WIFI_CLIENT_COUNT_SENSOR,
    DEFAULT_WIFI_POLL_INTERVAL,
    DEFAULT_WIFI_TRACKING_ENABLED,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MAX_RECENT_WIFI_CLIENTS,
    MAX_WIFI_AWAY_GRACE_PERIOD,
    MAX_WIFI_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_WIFI_AWAY_GRACE_PERIOD,
    MIN_WIFI_POLL_INTERVAL,
)
from .policy_config import (
    PolicyDefinition,
    configured_policies,
    fortigate_entry_title,
    parse_optional_policy_ids,
    serialize_policies,
)
from .presence_users import RULE_FIELDS, serialize_presence_users
from .wifi import FortiGateWifiClient, normalize_mac, utcnow

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


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
) -> dict[str, dict[str, str]]:
    """Keep friendly names for trackers that remain selected."""
    if not isinstance(tracked, dict):
        return {}
    preserved: dict[str, dict[str, str]] = {}
    for mac, metadata in tracked.items():
        normalized = normalize_mac(mac)
        if normalized not in selected_macs or not isinstance(metadata, dict):
            continue
        name = metadata.get(CONF_FRIENDLY_NAME)
        if isinstance(name, str) and name.strip():
            preserved[normalized] = {CONF_FRIENDLY_NAME: name.strip()}
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

    VERSION = 4

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

    def __init__(self) -> None:
        """Keep state during the select-then-name native options flow."""
        self._new_options: dict[str, Any] = {}
        self._recent_clients: dict[str, dict[str, str]] = {}
        self._selected_macs: list[str] = []
        self._named_clients: dict[str, dict[str, str]] = {}
        self._presence_user_id: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show clear entry points for trackers and advanced settings."""
        tracked = self.config_entry.options.get(CONF_TRACKED_CLIENTS, {})
        tracked_count = len(tracked) if isinstance(tracked, dict) else 0
        menu_options = ["firewall_policies", "wifi_clients"]
        if tracked_count:
            menu_options.append("presence_users")
        if tracked_count:
            menu_options.append("remove_wifi_trackers")
        menu_options.append("wifi_settings")
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders={"tracked": str(tracked_count)},
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
            home_enable = set(user_input.get(CONF_HOME_ENABLE_POLICIES, []))
            home_disable = set(user_input.get(CONF_HOME_DISABLE_POLICIES, []))
            away_enable = set(user_input.get(CONF_AWAY_ENABLE_POLICIES, []))
            away_disable = set(user_input.get(CONF_AWAY_DISABLE_POLICIES, []))
            if not name:
                errors[CONF_PRESENCE_USER_NAME] = "required"
            elif not selected_macs:
                errors[CONF_PRESENCE_USER_MACS] = "select_user_device"
            elif home_enable & home_disable or away_enable & away_disable:
                errors["base"] = "rule_state_conflict"
            else:
                existing = self.config_entry.options.get(CONF_PRESENCE_USERS, {})
                updated_users = dict(existing) if isinstance(existing, dict) else {}
                updated_users[self._presence_user_id] = {
                    CONF_PRESENCE_USER_NAME: name,
                    CONF_PRESENCE_USER_MACS: sorted(selected_macs),
                    **{
                        field: sorted(set(user_input.get(field, [])))
                        for field in RULE_FIELDS
                    },
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

        policy_options = [
            {
                "value": policy.policy_id,
                "label": f"{policy.expected_name or 'Policy'} ({policy.policy_id})",
            }
            for policy in configured_policies(self.config_entry.data)
        ]
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
        }
        if policy_options:
            fields.update(
                {
                    vol.Optional(field, default=current.get(field, [])): SelectSelector(
                        SelectSelectorConfig(options=policy_options, multiple=True)
                    )
                    for field in RULE_FIELDS
                }
            )
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
                return self.async_create_entry(
                    data={
                        **dict(self.config_entry.options),
                        CONF_PRESENCE_USERS: {
                            user_id: user
                            for user_id, user in users.items()
                            if user_id not in remove
                        },
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
        options = [
            {"value": mac, "label": self._client_label(mac, metadata)}
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
            for mac, metadata in existing.items():
                normalized = normalize_mac(mac)
                if normalized and isinstance(metadata, dict):
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
