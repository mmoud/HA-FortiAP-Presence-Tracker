"""Tests for per-tracker presence policy rules."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from custom_components.fortigate_policy.const import (
    CONF_AWAY_DISABLE_POLICIES,
    CONF_AWAY_ENABLE_POLICIES,
    CONF_HOME_DISABLE_POLICIES,
    CONF_HOME_ENABLE_POLICIES,
    CONF_PRESENCE_POLICY_RULES,
    STATUS_DISABLE,
    STATUS_ENABLE,
)
from custom_components.fortigate_policy.policy_rules import (
    PresencePolicyRule,
    PresencePolicyRuleManager,
    configured_presence_rules,
    resolve_policy_intents,
    serialize_presence_rules,
)
from custom_components.fortigate_policy.wifi import WifiPresence

MAC_1 = "aa:bb:cc:dd:ee:ff"
MAC_2 = "11:22:33:44:55:66"
NOW = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)


def _presence(value: bool | None) -> WifiPresence:
    return WifiPresence(value, None, NOW if value else None, None)


class FakePolicyCoordinator:
    def __init__(self, status: str, available: bool = True) -> None:
        self.data = SimpleNamespace(status=status)
        self.last_update_success = available
        self.commands: list[str] = []

    async def async_set_policy_status(self, status: str) -> None:
        self.commands.append(status)
        self.data = SimpleNamespace(status=status)


class TestPresencePolicyRuleModel(unittest.TestCase):
    def test_multiple_trackers_control_independent_policy_sets(self) -> None:
        rules = (
            PresencePolicyRule(
                MAC_1,
                frozenset(),
                frozenset({"1"}),
                frozenset(),
                frozenset(),
            ),
            PresencePolicyRule(
                MAC_2,
                frozenset(),
                frozenset(),
                frozenset(),
                frozenset({"1", "2"}),
            ),
        )

        result = resolve_policy_intents(
            rules, {MAC_1: _presence(True), MAC_2: _presence(False)}
        )

        self.assertEqual({"1": STATUS_DISABLE, "2": STATUS_DISABLE}, result.desired)

    def test_disable_wins_when_trackers_conflict(self) -> None:
        rules = (
            PresencePolicyRule(
                MAC_1,
                frozenset({"1"}),
                frozenset(),
                frozenset(),
                frozenset(),
            ),
            PresencePolicyRule(
                MAC_2,
                frozenset(),
                frozenset({"1"}),
                frozenset(),
                frozenset(),
            ),
        )

        result = resolve_policy_intents(
            rules, {MAC_1: _presence(True), MAC_2: _presence(True)}
        )

        self.assertEqual({"1": STATUS_DISABLE}, result.desired)
        self.assertEqual(frozenset({"1"}), result.conflicts)

    def test_unknown_tracker_blocks_policy_change(self) -> None:
        rules = (
            PresencePolicyRule(
                MAC_1,
                frozenset({"1"}),
                frozenset(),
                frozenset(),
                frozenset(),
            ),
            PresencePolicyRule(
                MAC_2,
                frozenset(),
                frozenset({"1"}),
                frozenset(),
                frozenset(),
            ),
        )

        result = resolve_policy_intents(
            rules, {MAC_1: _presence(True), MAC_2: _presence(None)}
        )

        self.assertEqual({}, result.desired)
        self.assertEqual(frozenset({"1"}), result.blocked_unknown)

    def test_saved_rules_are_normalized_and_pruned(self) -> None:
        raw = {
            "AA-BB-CC-DD-EE-FF": {
                CONF_HOME_ENABLE_POLICIES: ["1", "999"],
                CONF_HOME_DISABLE_POLICIES: [],
                CONF_AWAY_ENABLE_POLICIES: [],
                CONF_AWAY_DISABLE_POLICIES: ["2"],
            },
            MAC_2: {CONF_HOME_ENABLE_POLICIES: ["1"]},
        }

        serialized = serialize_presence_rules(raw, {MAC_1}, {"1", "2"})
        parsed = configured_presence_rules(
            {CONF_PRESENCE_POLICY_RULES: serialized}, {MAC_1}, {"1", "2"}
        )

        self.assertEqual({MAC_1}, set(serialized))
        self.assertEqual(frozenset({"1"}), parsed[0].home_enable)
        self.assertEqual(frozenset({"2"}), parsed[0].away_disable)


class TestPresencePolicyRuleManager(unittest.TestCase):
    def test_reconcile_changes_only_policies_that_need_it(self) -> None:
        policy_1 = FakePolicyCoordinator(STATUS_ENABLE)
        policy_2 = FakePolicyCoordinator(STATUS_DISABLE)
        wifi = SimpleNamespace(
            last_update_success=True,
            data=SimpleNamespace(presence={MAC_1: _presence(True)}),
        )
        rule = PresencePolicyRule(
            MAC_1,
            frozenset(),
            frozenset({"1", "2"}),
            frozenset(),
            frozenset(),
        )
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy_1, "2": policy_2},  # type: ignore[arg-type]
            (rule,),
        )

        asyncio.run(manager.async_reconcile())

        self.assertEqual([STATUS_DISABLE], policy_1.commands)
        self.assertEqual([], policy_2.commands)
        self.assertIsNotNone(manager.last_reconcile)

    def test_failed_wifi_poll_never_changes_policy(self) -> None:
        policy = FakePolicyCoordinator(STATUS_ENABLE)
        wifi = SimpleNamespace(last_update_success=False, data=None)
        rule = PresencePolicyRule(
            MAC_1,
            frozenset(),
            frozenset(),
            frozenset(),
            frozenset({"1"}),
        )
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy},  # type: ignore[arg-type]
            (rule,),
        )

        asyncio.run(manager.async_reconcile())

        self.assertEqual([], policy.commands)
        self.assertIsNone(manager.last_reconcile)

    def test_unavailable_policy_is_not_written(self) -> None:
        policy = FakePolicyCoordinator(STATUS_ENABLE, available=False)
        wifi = SimpleNamespace(
            last_update_success=True,
            data=SimpleNamespace(presence={MAC_1: _presence(False)}),
        )
        rule = PresencePolicyRule(
            MAC_1,
            frozenset(),
            frozenset(),
            frozenset(),
            frozenset({"1"}),
        )
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy},  # type: ignore[arg-type]
            (rule,),
        )

        asyncio.run(manager.async_reconcile())

        self.assertEqual([], policy.commands)
