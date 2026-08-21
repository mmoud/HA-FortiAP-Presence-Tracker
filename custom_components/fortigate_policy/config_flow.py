"""UI setup, reconfiguration, and options flows for FortiGate Policy Presence."""

from __future__ import annotations

from typing import Any

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
    CONF_FRIENDLY_NAME,
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_POLICIES,
    CONF_POLICY_IDS,
    CONF_POLL_INTERVAL,
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
    parse_policy_ids,
    serialize_policies,
)
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
            vol.Required(
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


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Return adjustable policy and Wi-Fi presence options."""
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
                CONF_WIFI_TRACKING_ENABLED,
                default=defaults.get(
                    CONF_WIFI_TRACKING_ENABLED, DEFAULT_WIFI_TRACKING_ENABLED
                ),
            ): bool,
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


def _normalize(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize and reject values that cannot safely form the CMDB URL."""
    data = dict(user_input)
    data[CONF_HOST] = data[CONF_HOST].strip()
    data[CONF_VDOM] = data[CONF_VDOM].strip()
    policy_ids = parse_policy_ids(data[CONF_POLICY_IDS])
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
    for policy_id in parse_policy_ids(data[CONF_POLICY_IDS]):
        policy = await _api_for_policy(hass, data, policy_id, "").async_get_policy()
        policies.append(PolicyDefinition(policy.policy_id, policy.name))
    title = (
        policies[0].expected_name or f"FortiGate Policy {policies[0].policy_id}"
        if len(policies) == 1
        else f"FortiGate ({len(policies)} policies)"
    )
    return tuple(policies), title


async def _async_validate_entry_data(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate all saved policy identity guards, for token reauthentication."""
    for policy in configured_policies(data):
        await _api_for_policy(
            hass, data, policy.policy_id, policy.expected_name
        ).async_get_policy()


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

    VERSION = 2

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
                if legacy_primary := entry.data.get(CONF_LEGACY_PRIMARY_POLICY_ID):
                    updated_data[CONF_LEGACY_PRIMARY_POLICY_ID] = legacy_primary
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

    def __init__(self) -> None:
        """Keep state during the select-then-name native options flow."""
        self._new_options: dict[str, Any] = {}
        self._recent_clients: dict[str, dict[str, str]] = {}
        self._selected_macs: list[str] = []
        self._named_clients: dict[str, dict[str, str]] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage polling cadence."""
        if user_input is not None:
            self._new_options = {**dict(self.config_entry.options), **user_input}
            if not user_input[CONF_WIFI_TRACKING_ENABLED]:
                return self.async_create_entry(data=self._new_options)
            return await self.async_step_wifi_clients()
        return self.async_show_form(
            step_id="init", data_schema=_options_schema(dict(self.config_entry.options))
        )

    async def async_step_wifi_clients(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover associated stations and select only the MACs to track."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(self._SELECTED_CLIENTS, [])
            self._selected_macs = [
                mac for value in selected if (mac := normalize_mac(value)) is not None
            ]
            self._named_clients = {}
            if not self._selected_macs:
                self._new_options[CONF_TRACKED_CLIENTS] = {}
                self._new_options[CONF_RECENT_WIFI_CLIENTS] = self._recent_clients
                return self.async_create_entry(data=self._new_options)
            return await self.async_step_wifi_client_name()

        try:
            clients, _skipped, _version = await self._async_discover_wifi_clients()
        except FortiGateError as err:
            errors["base"] = _error_key(err)
            clients = {}
        self._recent_clients = self._merged_recent_clients(clients)
        options = [
            {"value": mac, "label": self._client_label(mac, metadata)}
            for mac, metadata in sorted(self._recent_clients.items())
        ]
        if not options and not errors:
            errors["base"] = "no_wifi_clients"
        existing = self._selected_from_options()
        return self.async_show_form(
            step_id="wifi_clients",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        self._SELECTED_CLIENTS,
                        default=existing,
                    ): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"count": str(len(options))},
        )

    async def async_step_wifi_client_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Assign a friendly name without using it as the stable MAC identity."""
        mac = self._selected_macs[len(self._named_clients)]
        if user_input is not None:
            name = user_input[CONF_FRIENDLY_NAME].strip() or self._default_name(mac)
            self._named_clients[mac] = {CONF_FRIENDLY_NAME: name}
            if len(self._named_clients) == len(self._selected_macs):
                self._new_options[CONF_TRACKED_CLIENTS] = self._named_clients
                self._new_options[CONF_RECENT_WIFI_CLIENTS] = self._recent_clients
                return self.async_create_entry(data=self._new_options)
            mac = self._selected_macs[len(self._named_clients)]

        return self.async_show_form(
            step_id="wifi_client_name",
            data_schema=vol.Schema(
                {vol.Required(CONF_FRIENDLY_NAME, default=self._default_name(mac)): str}
            ),
            description_placeholders={"mac": mac, "client": self._default_name(mac)},
        )

    async def _async_discover_wifi_clients(
        self,
    ) -> tuple[dict[str, FortiGateWifiClient], int, str | None]:
        """Reuse the integration's authenticated FortiGate API client."""
        data = self.config_entry.data
        policy = configured_policies(data)[0]
        api = _api_for_policy(self.hass, data, policy.policy_id, policy.expected_name)
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
