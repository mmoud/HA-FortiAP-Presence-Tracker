"""Tests for multi-device user presence and policy reconciliation."""

from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.fortigate_policy.const import STATUS_DISABLE, STATUS_ENABLE
from custom_components.fortigate_policy.policy_rules import (
    PolicyRule,
    PresencePolicyRuleManager,
    migrate_user_intents_to_policy_rules,
    resolve_policy_intents,
)
from custom_components.fortigate_policy.presence_users import (
    PresenceUser,
    aggregate_presence,
    configured_presence_users,
    migrate_tracker_rules_to_users,
    serialize_presence_users,
)
from custom_components.fortigate_policy.wifi import WifiPresence

MAC_1 = "aa:bb:cc:dd:ee:ff"
MAC_2 = "11:22:33:44:55:66"
MAC_3 = "22:33:44:55:66:77"
NOW = datetime(2026, 8, 21, 13, 0, tzinfo=UTC)


def _presence(value: bool | None) -> WifiPresence:
    return WifiPresence(value, None, NOW if value else None, None)


def _user(
    user_id: str = "user-1",
    macs: frozenset[str] = frozenset({MAC_1, MAC_2}),
    *,
    home_enable: frozenset[str] = frozenset(),
    home_disable: frozenset[str] = frozenset(),
    away_enable: frozenset[str] = frozenset(),
    away_disable: frozenset[str] = frozenset(),
) -> PresenceUser:
    return PresenceUser(
        user_id,
        "User 1",
        macs,
        home_enable,
        home_disable,
        away_enable,
        away_disable,
    )


class FakePolicyCoordinator:
    def __init__(self, status: str, available: bool = True) -> None:
        self.data = SimpleNamespace(status=status)
        self.last_update_success = available
        self.commands: list[str] = []

    async def async_set_policy_status(self, status: str) -> None:
        self.commands.append(status)
        self.data = SimpleNamespace(status=status)


class TestPresenceUserModel(unittest.TestCase):
    def test_any_device_home_marks_user_home(self) -> None:
        self.assertIs(
            aggregate_presence(
                _user(), {MAC_1: _presence(False), MAC_2: _presence(True)}
            ),
            True,
        )

    def test_every_device_away_marks_user_away(self) -> None:
        self.assertIs(
            aggregate_presence(
                _user(), {MAC_1: _presence(False), MAC_2: _presence(False)}
            ),
            False,
        )

    def test_unknown_member_prevents_false_away(self) -> None:
        self.assertIsNone(
            aggregate_presence(
                _user(), {MAC_1: _presence(False), MAC_2: _presence(None)}
            )
        )

    def test_home_evidence_wins_over_unknown_member(self) -> None:
        self.assertIs(
            aggregate_presence(
                _user(), {MAC_1: _presence(True), MAC_2: _presence(None)}
            ),
            True,
        )

    def test_profiles_are_normalized_and_pruned(self) -> None:
        raw = {
            "stable-id": {
                "presence_user_name": "Household member",
                "presence_user_macs": ["AA-BB-CC-DD-EE-FF", MAC_2, MAC_3],
                "home_enable_policies": ["1", "999"],
                "away_disable_policies": ["2"],
            }
        }
        serialized = serialize_presence_users(raw, {MAC_1, MAC_2}, {"1", "2"})
        parsed = configured_presence_users(
            {"presence_users": serialized}, {MAC_1, MAC_2}, {"1", "2"}
        )

        self.assertEqual({"stable-id"}, set(serialized))
        self.assertEqual(frozenset({MAC_1, MAC_2}), parsed[0].macs)
        self.assertEqual(frozenset({"1"}), parsed[0].home_enable)
        self.assertEqual(frozenset({"2"}), parsed[0].away_disable)

    def test_legacy_tracker_rule_migrates_without_losing_behavior(self) -> None:
        migrated = migrate_tracker_rules_to_users(
            {
                "tracked_clients": {MAC_1: {"friendly_name": "Phone"}},
                "presence_policy_rules": {MAC_1: {"away_disable_policies": ["61"]}},
            },
            {MAC_1},
        )

        profile = next(iter(migrated.values()))
        self.assertEqual("Phone", profile["presence_user_name"])
        self.assertEqual([MAC_1], profile["presence_user_macs"])
        self.assertEqual(["61"], profile["away_disable_policies"])

    def test_user_attached_intents_migrate_to_policy_rules(self) -> None:
        migrated = migrate_user_intents_to_policy_rules(
            {
                "user-1": {
                    "presence_user_name": "Example user",
                    "home_enable_policies": ["61"],
                    "away_disable_policies": ["61", "999"],
                }
            },
            {"61"},
        )
        self.assertEqual(2, len(migrated))
        self.assertEqual(
            ["61"], migrated["migrated_user-1_home_enable"]["policy_rule_policies"]
        )
        self.assertEqual(
            "away", migrated["migrated_user-1_away_disable"]["policy_rule_presence"]
        )


class TestPolicyResolution(unittest.TestCase):
    def test_users_control_independent_policy_sets(self) -> None:
        rules = (
            _user("one", frozenset({MAC_1}), home_disable=frozenset({"1"})),
            _user(
                "two",
                frozenset({MAC_2}),
                away_disable=frozenset({"1", "2"}),
            ),
        )
        result = resolve_policy_intents(
            rules, {MAC_1: _presence(True), MAC_2: _presence(False)}
        )
        self.assertEqual({"1": STATUS_DISABLE, "2": STATUS_DISABLE}, result.desired)

    def test_disable_wins_conflict_between_users(self) -> None:
        rules = (
            _user("one", frozenset({MAC_1}), home_enable=frozenset({"1"})),
            _user("two", frozenset({MAC_2}), home_disable=frozenset({"1"})),
        )
        result = resolve_policy_intents(
            rules, {MAC_1: _presence(True), MAC_2: _presence(True)}
        )
        self.assertEqual({"1": STATUS_DISABLE}, result.desired)
        self.assertEqual(frozenset({"1"}), result.conflicts)

    def test_unknown_user_blocks_affected_policy(self) -> None:
        rules = (
            _user("one", frozenset({MAC_1}), home_enable=frozenset({"1"})),
            _user("two", frozenset({MAC_2}), home_disable=frozenset({"1"})),
        )
        result = resolve_policy_intents(
            rules, {MAC_1: _presence(True), MAC_2: _presence(None)}
        )
        self.assertEqual({}, result.desired)
        self.assertEqual(frozenset({"1"}), result.blocked_unknown)

    def test_policy_rule_any_user_home_matches(self) -> None:
        users = {
            "one": _user("one", frozenset({MAC_1})),
            "two": _user("two", frozenset({MAC_2})),
        }
        rule = PolicyRule(
            "rule-1",
            "Family arrived",
            frozenset(users),
            "any",
            "home",
            STATUS_ENABLE,
            frozenset({"1"}),
            50,
        )
        result = resolve_policy_intents(
            (),
            {MAC_1: _presence(False), MAC_2: _presence(True)},
            policy_rules=(rule,),
            users=users,
        )
        self.assertEqual({"1": STATUS_ENABLE}, result.desired)
        self.assertIn("Family arrived", result.reasons["1"])

    def test_policy_rule_all_users_away_waits_for_unknown(self) -> None:
        users = {
            "one": _user("one", frozenset({MAC_1})),
            "two": _user("two", frozenset({MAC_2})),
        }
        rule = PolicyRule(
            "rule-1",
            "Everyone left",
            frozenset(users),
            "all",
            "away",
            STATUS_DISABLE,
            frozenset({"1"}),
            50,
        )
        result = resolve_policy_intents(
            (),
            {MAC_1: _presence(False), MAC_2: _presence(None)},
            policy_rules=(rule,),
            users=users,
        )
        self.assertEqual({}, result.desired)
        self.assertEqual(frozenset({"1"}), result.blocked_unknown)

    def test_higher_priority_wins_and_equal_priority_disable_wins(self) -> None:
        user = _user("one", frozenset({MAC_1}))
        rules = (
            PolicyRule(
                "low",
                "Low priority",
                frozenset({"one"}),
                "any",
                "home",
                STATUS_DISABLE,
                frozenset({"1"}),
                10,
            ),
            PolicyRule(
                "high-enable",
                "High enable",
                frozenset({"one"}),
                "any",
                "home",
                STATUS_ENABLE,
                frozenset({"1"}),
                80,
            ),
            PolicyRule(
                "high-disable",
                "High disable",
                frozenset({"one"}),
                "any",
                "home",
                STATUS_DISABLE,
                frozenset({"1"}),
                80,
            ),
        )
        result = resolve_policy_intents(
            (),
            {MAC_1: _presence(True)},
            policy_rules=rules,
            users={"one": user},
        )
        self.assertEqual({"1": STATUS_DISABLE}, result.desired)
        self.assertEqual(frozenset({"1"}), result.conflicts)

    def test_inactive_schedule_skips_rule_and_unknown_schedule_blocks(self) -> None:
        user = _user("one", frozenset({MAC_1}))
        rule = PolicyRule(
            "scheduled",
            "School hours",
            frozenset({"one"}),
            "any",
            "home",
            STATUS_DISABLE,
            frozenset({"1"}),
            50,
            "schedule.school_hours",
        )
        inactive = resolve_policy_intents(
            (),
            {MAC_1: _presence(True)},
            policy_rules=(rule,),
            users={"one": user},
            schedule_states={"schedule.school_hours": False},
        )
        unknown = resolve_policy_intents(
            (),
            {MAC_1: _presence(True)},
            policy_rules=(rule,),
            users={"one": user},
            schedule_states={"schedule.school_hours": None},
        )
        self.assertEqual({}, inactive.desired)
        self.assertEqual(frozenset(), inactive.blocked_unknown)
        self.assertEqual(frozenset({"1"}), unknown.blocked_unknown)

    def test_user_grace_can_extend_device_grace(self) -> None:
        user = PresenceUser(
            "one",
            "User 1",
            frozenset({MAC_1}),
            frozenset(),
            frozenset(),
            frozenset(),
            frozenset(),
            300,
        )
        missing = WifiPresence(False, NOW - timedelta(seconds=200), NOW, None)
        self.assertTrue(aggregate_presence(user, {MAC_1: missing}, NOW))
        self.assertFalse(
            aggregate_presence(user, {MAC_1: missing}, NOW + timedelta(seconds=101))
        )


class TestPresencePolicyRuleManager(unittest.TestCase):
    def test_reconcile_changes_only_policy_that_needs_it(self) -> None:
        policy_1 = FakePolicyCoordinator(STATUS_ENABLE)
        policy_2 = FakePolicyCoordinator(STATUS_DISABLE)
        wifi = SimpleNamespace(
            last_update_success=True,
            data=SimpleNamespace(
                presence={MAC_1: _presence(False), MAC_2: _presence(True)}
            ),
        )
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy_1, "2": policy_2},  # type: ignore[arg-type]
            (_user(home_disable=frozenset({"1", "2"})),),
        )

        asyncio.run(manager.async_reconcile())

        self.assertEqual([STATUS_DISABLE], policy_1.commands)
        self.assertEqual([], policy_2.commands)
        self.assertIsNotNone(manager.last_reconcile)

    def test_failed_wifi_poll_never_changes_policy(self) -> None:
        policy = FakePolicyCoordinator(STATUS_ENABLE)
        wifi = SimpleNamespace(last_update_success=False, data=None)
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy},  # type: ignore[arg-type]
            (_user(away_disable=frozenset({"1"})),),
        )

        asyncio.run(manager.async_reconcile())

        self.assertEqual([], policy.commands)
        self.assertIsNone(manager.last_reconcile)

    def test_unknown_member_never_executes_away_rule(self) -> None:
        policy = FakePolicyCoordinator(STATUS_ENABLE)
        wifi = SimpleNamespace(
            last_update_success=True,
            data=SimpleNamespace(
                presence={MAC_1: _presence(False), MAC_2: _presence(None)}
            ),
        )
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy},  # type: ignore[arg-type]
            (_user(away_disable=frozenset({"1"})),),
        )

        asyncio.run(manager.async_reconcile())

        self.assertEqual([], policy.commands)
        self.assertEqual(frozenset({"1"}), manager.last_result.blocked_unknown)

    def test_dry_run_calculates_but_does_not_write(self) -> None:
        policy = FakePolicyCoordinator(STATUS_ENABLE)
        wifi = SimpleNamespace(
            last_update_success=True,
            data=SimpleNamespace(presence={MAC_1: _presence(False)}),
        )
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy},  # type: ignore[arg-type]
            (_user(macs=frozenset({MAC_1}), away_disable=frozenset({"1"})),),
            dry_run=True,
        )

        asyncio.run(manager.async_reconcile())

        self.assertEqual({"1": STATUS_DISABLE}, manager.last_result.desired)
        self.assertEqual([], policy.commands)

    def test_manual_override_works_during_wifi_outage(self) -> None:
        policy = FakePolicyCoordinator(STATUS_ENABLE)
        wifi = SimpleNamespace(last_update_success=False, data=None)
        bus = SimpleNamespace(events=[])
        bus.async_fire = lambda event, data: bus.events.append((event, data))
        manager = PresencePolicyRuleManager(
            SimpleNamespace(bus=bus),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy},  # type: ignore[arg-type]
            (),
        )

        asyncio.run(manager.async_set_override("1", "force_disable", minutes=0))

        self.assertEqual([STATUS_DISABLE], policy.commands)
        self.assertEqual("force_disable", manager.override_for("1").mode)
        self.assertEqual("fortigate_policy_decision", bus.events[0][0])

    def test_paused_override_prevents_rule_write(self) -> None:
        policy = FakePolicyCoordinator(STATUS_ENABLE)
        wifi = SimpleNamespace(
            last_update_success=True,
            data=SimpleNamespace(presence={MAC_1: _presence(False)}),
        )
        manager = PresencePolicyRuleManager(
            SimpleNamespace(),  # type: ignore[arg-type]
            wifi,  # type: ignore[arg-type]
            {"1": policy},  # type: ignore[arg-type]
            (_user(macs=frozenset({MAC_1}), away_disable=frozenset({"1"})),),
        )

        asyncio.run(manager.async_set_override("1", "paused", minutes=0))

        self.assertEqual([], policy.commands)
