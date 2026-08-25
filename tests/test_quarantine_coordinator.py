"""Verified quarantine transaction tests without a Home Assistant event loop."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.fortigate_policy.api import (
    FortiGateAuthError,
    FortiGateConnectionError,
)
from custom_components.fortigate_policy.coordinator import (
    FortiGateQuarantineCoordinator,
)
from custom_components.fortigate_policy.quarantine import FortiGateQuarantineState

MAC = "aa:bb:cc:dd:ee:ff"


class FakeApi:
    def __init__(self, reads, update_error: Exception | None = None) -> None:
        self.reads = list(reads)
        self.update_error = update_error
        self.updates: list[tuple[str, bool, str]] = []

    async def async_get_quarantine_state(self):
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def async_update_quarantine(self, mac, desired, friendly_name):
        self.updates.append((mac, desired, friendly_name))
        if self.update_error:
            raise self.update_error
        return True


def coordinator(api: FakeApi):
    captured = []
    return SimpleNamespace(
        api=api,
        _command_lock=asyncio.Lock(),
        last_successful_update=None,
        async_set_updated_data=captured.append,
        captured=captured,
    )


class TestQuarantineCoordinator(unittest.TestCase):
    def test_success_requires_post_write_readback(self) -> None:
        off = FortiGateQuarantineState(True, frozenset(), 0)
        on = FortiGateQuarantineState(True, frozenset({MAC}), 1)
        api = FakeApi([off, on])
        instance = coordinator(api)
        asyncio.run(
            FortiGateQuarantineCoordinator.async_set_mac_quarantine(
                instance, MAC, True, "Example phone"
            )
        )
        self.assertEqual([(MAC, True, "Example phone")], api.updates)
        self.assertEqual(on, instance.captured[-1])

    def test_already_on_is_idempotent(self) -> None:
        on = FortiGateQuarantineState(True, frozenset({MAC}), 1)
        api = FakeApi([on])
        instance = coordinator(api)
        asyncio.run(
            FortiGateQuarantineCoordinator.async_set_mac_quarantine(
                instance, MAC, True, "Example phone"
            )
        )
        self.assertEqual([], api.updates)

    def test_permission_denied_is_not_reported_as_success(self) -> None:
        off = FortiGateQuarantineState(True, frozenset(), 0)
        api = FakeApi([off], FortiGateAuthError("denied"))
        with self.assertRaises(FortiGateAuthError):
            asyncio.run(
                FortiGateQuarantineCoordinator.async_set_mac_quarantine(
                    coordinator(api), MAC, True, "Example phone"
                )
            )

    def test_api_success_without_state_change_is_rejected(self) -> None:
        off = FortiGateQuarantineState(True, frozenset(), 0)
        api = FakeApi([off, off])
        with (
            patch("custom_components.fortigate_policy.coordinator.UPDATE_RETRIES", 1),
            self.assertRaises(FortiGateConnectionError),
        ):
            asyncio.run(
                FortiGateQuarantineCoordinator.async_set_mac_quarantine(
                    coordinator(api), MAC, True, "Example phone"
                )
            )

    def test_failed_readback_never_invents_requested_state(self) -> None:
        off = FortiGateQuarantineState(True, frozenset(), 0)
        api = FakeApi([off, FortiGateConnectionError("offline")])
        instance = coordinator(api)
        with (
            patch("custom_components.fortigate_policy.coordinator.UPDATE_RETRIES", 1),
            self.assertRaises(FortiGateConnectionError),
        ):
            asyncio.run(
                FortiGateQuarantineCoordinator.async_set_mac_quarantine(
                    instance, MAC, True, "Example phone"
                )
            )
        self.assertEqual([off], instance.captured)


if __name__ == "__main__":
    unittest.main()
