"""Small FortiOS API client for policy control and Wi-Fi presence."""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout
from yarl import URL

from .const import DEFAULT_TIMEOUT, VALID_STATUSES
from .wifi import (
    FortiGateClientIdentity,
    FortiGateWifiClient,
    enrich_wifi_clients,
    parse_client_identities,
    parse_wifi_clients,
)

_LOGGER = logging.getLogger(__name__)


class FortiGateError(Exception):
    """Base FortiGate API error."""


class FortiGateConnectionError(FortiGateError):
    """The FortiGate could not be reached or returned invalid JSON."""


class FortiGateAuthError(FortiGateError):
    """FortiGate rejected the API token or its permissions."""


class FortiGateNotFoundError(FortiGateError):
    """The configured VDOM/policy resource was not found."""


class FortiGateIdentityError(FortiGateError):
    """The returned policy does not match the configured ID/name guard."""


class FortiGateCommandError(FortiGateError):
    """FortiGate did not accept the status-only policy update."""


@dataclass(frozen=True, slots=True)
class Policy:
    """The safe subset of a policy returned by FortiGate."""

    policy_id: str
    name: str
    status: str


class FortiGatePolicyApi:
    """Read one policy and modify only its `status` field."""

    def __init__(
        self,
        session: ClientSession,
        *,
        host: str,
        port: int,
        vdom: str,
        policy_id: str,
        expected_policy_name: str,
        token: str,
        verify_ssl: bool,
    ) -> None:
        self._session = session
        self._policy_id = policy_id
        self._expected_policy_name = expected_policy_name
        self._verify_ssl = verify_ssl
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        self._vdom = vdom
        self._policy_url = URL.build(
            scheme="https",
            host=host,
            port=port,
            path=f"/api/v2/cmdb/firewall/policy/{policy_id}",
            query={"vdom": vdom},
        )
        self._wifi_clients_url = URL.build(
            scheme="https",
            host=host,
            port=port,
            path="/api/v2/monitor/wifi/client",
            query={"vdom": vdom},
        )

        def monitor_url(path: str) -> URL:
            return URL.build(
                scheme="https",
                host=host,
                port=port,
                path=path,
                query={"vdom": vdom},
            )

        # FortiOS has renamed both identity monitor endpoints across releases.
        # Keep each source's candidates together and stop at the first valid
        # response.  These optional calls run only when the user opens the
        # client-selection Options Flow, never during periodic presence polls.
        self._identity_urls = {
            "dhcp": (
                monitor_url("/api/v2/monitor/dhcp"),
                monitor_url("/api/v2/monitor/system/dhcp"),
            ),
            "detected_device": (
                monitor_url("/api/v2/monitor/user/device/query"),
                monitor_url("/api/v2/monitor/user/detected-device"),
            ),
            "dhcp_reservation": (monitor_url("/api/v2/cmdb/system.dhcp/server"),),
            "configured_device": (monitor_url("/api/v2/cmdb/user/device"),),
        }

    @property
    def policy_id(self) -> str:
        """Return the policy ID assigned to this bounded client."""
        return self._policy_id

    async def async_get_policy(self) -> Policy:
        """Fetch and identity-check the configured policy."""
        payload = await self._async_request("GET", self._policy_url)
        return self._parse_policy(payload)

    async def async_set_status(self, status: str) -> None:
        """Set only the policy status after the caller has preflighted it."""
        if status not in VALID_STATUSES:
            raise FortiGateCommandError("Invalid requested policy status")

        payload = await self._async_request(
            "PUT", self._policy_url, json={"data": {"status": status}}
        )
        if payload.get("status") != "success":
            raise FortiGateCommandError("FortiGate did not report a successful update")

    async def async_get_wifi_clients(
        self,
    ) -> tuple[dict[str, FortiGateWifiClient], int, str | None]:
        """Get currently associated FortiAP clients in the configured VDOM.

        FortiOS documents ``GET /api/v2/monitor/wifi/client?vdom=<vdom>``.
        The normalizer accepts documented field variants but rejects a response
        that does not contain a valid client-list container.
        """
        payload = await self._async_request(
            "GET", self._wifi_clients_url, allow_monitor_json_fallback=True
        )
        try:
            return parse_wifi_clients(payload, self._vdom)
        except ValueError as err:
            raise FortiGateConnectionError(
                "FortiGate returned an unexpected Wi-Fi client response"
            ) from err

    async def async_get_wifi_client_catalog(
        self,
    ) -> tuple[dict[str, FortiGateWifiClient], int, str | None]:
        """Get associated clients enriched with FortiGate identity sources.

        This is used by the user-triggered Options Flow, not by the periodic
        presence coordinator, so normal tracking remains one Wi-Fi request per
        polling cycle.  Enrichment is best-effort: an unavailable optional
        endpoint must not hide otherwise valid associated clients.
        """
        clients, skipped, version = await self.async_get_wifi_clients()
        catalog_identities: dict[str, FortiGateClientIdentity] = {}
        for source, urls in self._identity_urls.items():
            for url in urls:
                try:
                    payload = await self._async_request(
                        "GET", url, allow_monitor_json_fallback=True
                    )
                    identities = parse_client_identities(payload, source)
                except FortiGateNotFoundError:
                    _LOGGER.debug(
                        "FortiGate identity endpoint is not available: %s", url.path
                    )
                    continue
                except (FortiGateError, ValueError) as err:
                    _LOGGER.debug(
                        "Optional FortiGate identity enrichment unavailable: %s (%s)",
                        url.path,
                        type(err).__name__,
                    )
                    break
                _LOGGER.debug(
                    "FortiGate identity enrichment completed: %s returned %s "
                    "MAC-keyed records",
                    url.path,
                    len(identities),
                )
                clients = enrich_wifi_clients(
                    clients,
                    identities,
                    prefer_identity_name=source
                    in {"configured_device", "dhcp_reservation"},
                )
                for mac, identity in identities.items():
                    previous = catalog_identities.get(mac)
                    catalog_identities[mac] = FortiGateClientIdentity(
                        mac=mac,
                        hostname=identity.hostname
                        or (previous.hostname if previous else None),
                        ip=identity.ip or (previous.ip if previous else None),
                        source=identity.source,
                    )
                break
        # Catalog-only records let the user select a known offline device.
        # They never prove presence: periodic tracking still calls only
        # async_get_wifi_clients(), and requires a current FortiAP association.
        for mac, identity in catalog_identities.items():
            clients.setdefault(
                mac,
                FortiGateWifiClient(
                    mac=mac,
                    ip=identity.ip,
                    hostname=identity.hostname,
                    vdom=self._vdom,
                ),
            )
        return clients, skipped, version

    async def _async_request(
        self,
        method: str,
        url: URL,
        *,
        json: dict[str, Any] | None = None,
        allow_monitor_json_fallback: bool = False,
    ) -> Mapping[str, Any]:
        """Make one request without logging credentials or response data."""
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers,
                json=json,
                ssl=self._verify_ssl,
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT),
            ) as response:
                if response.status in (401, 403):
                    raise FortiGateAuthError("FortiGate rejected the API credentials")
                if response.status == 404:
                    raise FortiGateNotFoundError(
                        "FortiGate policy resource was not found"
                    )
                if response.status != 200:
                    _LOGGER.warning(
                        "FortiGate API request failed: %s %s returned HTTP %s "
                        "(content type %s)",
                        method,
                        url.path,
                        response.status,
                        response.content_type,
                    )
                    raise FortiGateConnectionError(
                        f"FortiGate returned HTTP {response.status}"
                    )
                try:
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    if allow_monitor_json_fallback:
                        raw_payload = await response.read()
                        try:
                            # Some FortiOS monitor responses contain invalid
                            # UTF-8 or unescaped control bytes in optional
                            # client labels.  Browser JSON viewers tolerate
                            # these values, while aiohttp's strict decoder
                            # rejects the entire otherwise-valid response.
                            # Replacement is safe here: identity is still
                            # established exclusively from a validated MAC.
                            payload = jsonlib.loads(
                                raw_payload.decode("utf-8", errors="replace"),
                                strict=False,
                            )
                        except (UnicodeError, ValueError) as fallback_err:
                            _LOGGER.warning(
                                "FortiGate monitor response was not decodable "
                                "JSON: %s %s (%s, %s bytes)",
                                method,
                                url.path,
                                type(fallback_err).__name__,
                                len(raw_payload),
                            )
                            raise FortiGateConnectionError(
                                "FortiGate returned invalid JSON"
                            ) from fallback_err
                        _LOGGER.debug(
                            "FortiGate monitor response required tolerant JSON "
                            "decoding: %s %s (%s bytes)",
                            method,
                            url.path,
                            len(raw_payload),
                        )
                    else:
                        _LOGGER.warning(
                            "FortiGate API response was not JSON: %s %s "
                            "(content type %s)",
                            method,
                            url.path,
                            response.content_type,
                        )
                        raise FortiGateConnectionError(
                            "FortiGate returned invalid JSON"
                        ) from err
                except asyncio.TimeoutError as err:
                    _LOGGER.warning(
                        "FortiGate API response was not JSON: %s %s (content type %s)",
                        method,
                        url.path,
                        response.content_type,
                    )
                    raise FortiGateConnectionError(
                        "FortiGate returned invalid JSON"
                    ) from err
        except FortiGateError:
            raise
        except (ClientError, TimeoutError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "FortiGate API connection failed: %s %s (%s)",
                method,
                url.path,
                type(err).__name__,
            )
            raise FortiGateConnectionError("Unable to contact FortiGate") from err

        if not isinstance(payload, Mapping):
            _LOGGER.warning(
                "FortiGate API response had unexpected JSON type: %s %s (%s)",
                method,
                url.path,
                type(payload).__name__,
            )
            raise FortiGateConnectionError(
                "FortiGate returned an unexpected JSON value"
            )

        # Some FortiOS builds wrap an API failure in a HTTP 200 response and
        # communicate the real result through the usual response fields.  Do
        # not let that become a generic parsing error: an unavailable policy
        # switch must distinguish invalid credentials from a missing policy.
        if payload.get("status") == "error":
            reported_status = payload.get("http_status")
            if reported_status in (401, 403, "401", "403"):
                raise FortiGateAuthError("FortiGate rejected the API credentials")
            if reported_status in (404, "404"):
                raise FortiGateNotFoundError("FortiGate policy resource was not found")
        return payload

    def _parse_policy(self, payload: Mapping[str, Any]) -> Policy:
        """Extract the documented single-policy CMDB response safely."""
        result = payload.get("results")
        # FortiOS returns a mapping for some CMDB releases and a one-item
        # sequence for others, including FortiOS 7.6.  The request includes
        # the policy MKEY, so a sequence must contain exactly that one object.
        if isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
            if len(result) != 1 or not isinstance(result[0], Mapping):
                raise FortiGateConnectionError(
                    "FortiGate policy response has an invalid results list"
                )
            result = result[0]
        if not isinstance(result, Mapping):
            raise FortiGateConnectionError("FortiGate policy response lacks results")

        policy_id = str(result.get("policyid", ""))
        name = result.get("name")
        status = result.get("status")
        if policy_id != self._policy_id:
            raise FortiGateIdentityError(
                "Returned policy ID does not match configuration"
            )
        if not isinstance(name, str) or not isinstance(status, str):
            raise FortiGateConnectionError("FortiGate policy response is incomplete")
        if status not in VALID_STATUSES:
            raise FortiGateConnectionError("FortiGate policy status is invalid")
        if self._expected_policy_name and name != self._expected_policy_name:
            raise FortiGateIdentityError(
                "Returned policy name does not match configuration"
            )
        return Policy(policy_id=policy_id, name=name, status=status)
