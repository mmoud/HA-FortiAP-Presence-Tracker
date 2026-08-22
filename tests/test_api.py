"""FortiOS REST contract tests using an in-memory HTTP session."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any, Self

sys.path.insert(0, str(Path(__file__).parents[1]))

from custom_components.fortigate_policy.api import (
    FortiGateAuthError,
    FortiGateCommandError,
    FortiGateNotFoundError,
    FortiGatePolicyApi,
)


class FakeResponse:
    """Minimal async context-manager response used by the REST client."""

    def __init__(self, status: int, payload: Any, raw_payload: bytes = b"") -> None:
        self.status = status
        self._payload = payload
        self._raw_payload = raw_payload
        self.content_type = "application/json"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def json(self, **_: object) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def read(self) -> bytes:
        return self._raw_payload


class FakeSession:
    """Record requests and provide queued FortiGate responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, object, dict[str, Any]]] = []

    def request(self, method: str, url: object, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        return self._responses.pop(0)


def api(session: FakeSession) -> FortiGatePolicyApi:
    """Build a client with deliberately non-secret test inputs."""
    return FortiGatePolicyApi(
        session,  # type: ignore[arg-type]
        host="fortigate.example.test",
        port=9443,
        vdom="root",
        policy_id="123",
        expected_policy_name="Example policy",
        token="test-token",
        verify_ssl=True,
    )


class TestFortiGatePolicyApi(unittest.TestCase):
    """Verify resource paths, bounded writes, and error translation."""

    def test_get_policy_uses_cmdb_path_and_bearer_header(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": {
                            "policyid": 123,
                            "name": "Example policy",
                            "status": "enable",
                        },
                    },
                )
            ]
        )

        result = asyncio.run(api(session).async_get_policy())

        self.assertEqual("enable", result.status)
        method, url, kwargs = session.requests[0]
        self.assertEqual("GET", method)
        self.assertEqual(
            "https://fortigate.example.test:9443/api/v2/cmdb/firewall/policy/123?vdom=root",
            str(url),
        )
        self.assertEqual("Bearer test-token", kwargs["headers"]["Authorization"])
        self.assertNotIn("test-token", str(url))
        self.assertTrue(kwargs["ssl"])

    def test_get_policy_accepts_fortios_results_list(self) -> None:
        """FortiOS 7.6 returns the MKEY lookup as a single-item list."""
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [
                            {
                                "policyid": 123,
                                "name": "Example policy",
                                "status": "disable",
                            }
                        ],
                    },
                )
            ]
        )

        result = asyncio.run(api(session).async_get_policy())

        self.assertEqual("disable", result.status)

    def test_status_write_contains_no_policy_fields_except_status(self) -> None:
        session = FakeSession([FakeResponse(200, {"status": "success"})])

        asyncio.run(api(session).async_set_status("disable"))

        method, url, kwargs = session.requests[0]
        self.assertEqual("PUT", method)
        self.assertEqual(
            "https://fortigate.example.test:9443/api/v2/cmdb/firewall/policy/123?vdom=root",
            str(url),
        )
        self.assertEqual({"data": {"status": "disable"}}, kwargs["json"])

    def test_unsuccessful_write_response_is_rejected(self) -> None:
        session = FakeSession([FakeResponse(200, {"status": "error"})])

        with self.assertRaises(FortiGateCommandError):
            asyncio.run(api(session).async_set_status("enable"))

    def test_auth_and_not_found_responses_are_distinct(self) -> None:
        with self.assertRaises(FortiGateAuthError):
            asyncio.run(api(FakeSession([FakeResponse(401, {})])).async_get_policy())
        with self.assertRaises(FortiGateNotFoundError):
            asyncio.run(api(FakeSession([FakeResponse(404, {})])).async_get_policy())

    def test_fortios_json_error_envelope_is_translated(self) -> None:
        """Some FortiOS versions report authorization errors with HTTP 200."""
        with self.assertRaises(FortiGateAuthError):
            asyncio.run(
                api(
                    FakeSession(
                        [
                            FakeResponse(
                                200,
                                {"status": "error", "http_status": 401},
                            )
                        ]
                    )
                ).async_get_policy()
            )
        with self.assertRaises(FortiGateNotFoundError):
            asyncio.run(
                api(
                    FakeSession(
                        [
                            FakeResponse(
                                200,
                                {"status": "error", "http_status": "404"},
                            )
                        ]
                    )
                ).async_get_policy()
            )

    def test_wifi_monitor_uses_configured_vdom(self) -> None:
        session = FakeSession([FakeResponse(200, {"status": "success", "results": []})])

        clients, skipped, _version = asyncio.run(api(session).async_get_wifi_clients())

        self.assertEqual({}, clients)
        self.assertEqual(0, skipped)
        method, url, _kwargs = session.requests[0]
        self.assertEqual("GET", method)
        self.assertEqual(
            "https://fortigate.example.test:9443/api/v2/monitor/wifi/client?vdom=root",
            str(url),
        )

    def test_system_status_version_uses_configured_vdom(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"status": "success", "results": {"version": "v7.4.8"}},
                )
            ]
        )

        version = asyncio.run(api(session).async_get_fortios_version())

        self.assertEqual("v7.4.8", version)
        method, url, _kwargs = session.requests[0]
        self.assertEqual("GET", method)
        self.assertEqual(
            "https://fortigate.example.test:9443/api/v2/monitor/system/status?vdom=root",
            str(url),
        )

    def test_wifi_monitor_tolerates_invalid_utf8_in_optional_label(self) -> None:
        """A bad optional hostname byte must not hide valid MAC presence."""
        raw_payload = (
            b'{"status":"success","results":[{"mac":"AA-BB-CC-DD-EE-FF",'
            b'"hostname":"phone-\xff"}]}'
        )
        session = FakeSession(
            [FakeResponse(200, ValueError("strict decode failed"), raw_payload)]
        )

        clients, skipped, _version = asyncio.run(api(session).async_get_wifi_clients())

        self.assertEqual(0, skipped)
        self.assertIn("aa:bb:cc:dd:ee:ff", clients)
        self.assertEqual("phone-\ufffd", clients["aa:bb:cc:dd:ee:ff"].hostname)

    def test_wifi_catalog_enriches_by_mac_from_detected_devices(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [{"mac": "AA:BB:CC:DD:EE:FF"}],
                    },
                ),
                FakeResponse(
                    200,
                    {"status": "success", "results": []},
                ),
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [
                            {
                                "mac_address": "aa-bb-cc-dd-ee-ff",
                                "hostname": "Example iPhone",
                            }
                        ],
                    },
                ),
                FakeResponse(200, {"status": "success", "results": []}),
                FakeResponse(200, {"status": "success", "results": []}),
            ]
        )

        clients, _skipped, _version = asyncio.run(
            api(session).async_get_wifi_client_catalog()
        )

        self.assertEqual("Example iPhone", clients["aa:bb:cc:dd:ee:ff"].hostname)
        self.assertEqual("/api/v2/monitor/dhcp", session.requests[1][1].path)
        self.assertEqual(
            "/api/v2/monitor/user/device/query", session.requests[2][1].path
        )
        self.assertEqual("/api/v2/cmdb/system.dhcp/server", session.requests[3][1].path)
        self.assertEqual("/api/v2/cmdb/user/device", session.requests[4][1].path)

    def test_wifi_catalog_optional_http_errors_do_not_warn(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [{"mac": "AA:BB:CC:DD:EE:FF"}],
                    },
                ),
                FakeResponse(400, {}),
                FakeResponse(400, {}),
                FakeResponse(400, {}),
                FakeResponse(400, {}),
            ]
        )

        with self.assertNoLogs(
            "custom_components.fortigate_policy.api", level="WARNING"
        ):
            clients, _skipped, _version = asyncio.run(
                api(session).async_get_wifi_client_catalog()
            )

        self.assertIn("aa:bb:cc:dd:ee:ff", clients)

    def test_wifi_catalog_falls_back_across_fortios_endpoint_names(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [{"mac": "AA:BB:CC:DD:EE:FF"}],
                    },
                ),
                FakeResponse(404, {}),
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [
                            {
                                "mac": "aa-bb-cc-dd-ee-ff",
                                "hostname": "Fallback device",
                            }
                        ],
                    },
                ),
                FakeResponse(404, {}),
                FakeResponse(200, {"status": "success", "results": []}),
                FakeResponse(404, {}),
                FakeResponse(
                    200,
                    {
                        "status": "success",
                        "results": [
                            {
                                "mac": "aa-bb-cc-dd-ee-ff",
                                "alias": "Configured device",
                            },
                            {
                                "mac": "11:22:33:44:55:66",
                                "alias": "Known offline device",
                            },
                        ],
                    },
                ),
            ]
        )

        clients, _skipped, _version = asyncio.run(
            api(session).async_get_wifi_client_catalog()
        )

        self.assertEqual("Configured device", clients["aa:bb:cc:dd:ee:ff"].hostname)
        self.assertEqual("Known offline device", clients["11:22:33:44:55:66"].hostname)
        self.assertEqual(
            [
                "/api/v2/monitor/wifi/client",
                "/api/v2/monitor/dhcp",
                "/api/v2/monitor/system/dhcp",
                "/api/v2/monitor/user/device/query",
                "/api/v2/monitor/user/detected-device",
                "/api/v2/cmdb/system.dhcp/server",
                "/api/v2/cmdb/user/device",
            ],
            [request[1].path for request in session.requests],
        )


if __name__ == "__main__":
    unittest.main()
