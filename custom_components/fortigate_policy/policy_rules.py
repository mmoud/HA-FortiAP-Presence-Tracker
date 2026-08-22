"""Presence policy evaluation, overrides, and verified reconciliation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    CONF_AWAY_DISABLE_POLICIES,
    CONF_AWAY_ENABLE_POLICIES,
    CONF_HOME_DISABLE_POLICIES,
    CONF_HOME_ENABLE_POLICIES,
    CONF_POLICY_RULE_ACTION,
    CONF_POLICY_RULE_MATCH,
    CONF_POLICY_RULE_NAME,
    CONF_POLICY_RULE_POLICIES,
    CONF_POLICY_RULE_PRESENCE,
    CONF_POLICY_RULE_PRIORITY,
    CONF_POLICY_RULE_SCHEDULE,
    CONF_POLICY_RULE_USERS,
    CONF_POLICY_RULES_V2,
    CONF_PRESENCE_USER_NAME,
    OVERRIDE_AUTOMATIC,
    OVERRIDE_FORCE_DISABLE,
    OVERRIDE_FORCE_ENABLE,
    OVERRIDE_MODES,
    OVERRIDE_PAUSED,
    RULE_MATCH_ALL,
    RULE_MATCH_ANY,
    RULE_PRESENCE_AWAY,
    RULE_PRESENCE_HOME,
    STATUS_DISABLE,
    STATUS_ENABLE,
)
from .coordinator import FortiGatePolicyCoordinator, FortiGateWifiCoordinator
from .presence_users import (
    PresenceUser,
    aggregate_presence,
    configured_presence_users,
    migrate_tracker_rules_to_users,
)
from .wifi import WifiPresence, utcnow

_LOGGER = logging.getLogger(__name__)
PresencePolicyRule = PresenceUser


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One policy-centric condition configured in the native options flow."""

    rule_id: str
    name: str
    user_ids: frozenset[str]
    match: str
    presence: str
    action: str
    policy_ids: frozenset[str]
    priority: int
    schedule_entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyOverride:
    """Runtime-only override; restarts safely return to automatic control."""

    mode: str = OVERRIDE_AUTOMATIC
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResolvedPolicyIntents:
    """Conflict-resolved targets and explanations."""

    desired: dict[str, str]
    blocked_unknown: frozenset[str]
    conflicts: frozenset[str]
    reasons: dict[str, str]


def configured_presence_rules(
    options: Mapping[str, Any],
    tracked_macs: set[str],
    valid_policy_ids: set[str],
) -> tuple[PresencePolicyRule, ...]:
    """Parse legacy user-attached rules for backwards compatibility."""
    users = configured_presence_users(options, tracked_macs, valid_policy_ids)
    if users:
        return tuple(user for user in users if user.affected_policies)
    legacy = migrate_tracker_rules_to_users(options, tracked_macs)
    return tuple(
        user
        for user in configured_presence_users(
            {"presence_users": legacy}, tracked_macs, valid_policy_ids
        )
        if user.affected_policies
    )


def _valid_set(value: object, valid: set[str]) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(item) for item in value if str(item) in valid)


def configured_policy_rules(
    options: Mapping[str, Any],
    valid_user_ids: set[str],
    valid_policy_ids: set[str],
) -> tuple[PolicyRule, ...]:
    """Parse policy-centric rules, ignoring malformed or stale references."""
    raw_rules = options.get(CONF_POLICY_RULES_V2, {})
    if not isinstance(raw_rules, Mapping):
        return ()
    result: list[PolicyRule] = []
    for rule_id, raw in raw_rules.items():
        if not isinstance(rule_id, str) or not isinstance(raw, Mapping):
            continue
        users = _valid_set(raw.get(CONF_POLICY_RULE_USERS), valid_user_ids)
        policies = _valid_set(raw.get(CONF_POLICY_RULE_POLICIES), valid_policy_ids)
        name = raw.get(CONF_POLICY_RULE_NAME)
        match = raw.get(CONF_POLICY_RULE_MATCH)
        presence = raw.get(CONF_POLICY_RULE_PRESENCE)
        action = raw.get(CONF_POLICY_RULE_ACTION)
        schedule = raw.get(CONF_POLICY_RULE_SCHEDULE)
        if (
            not users
            or not policies
            or not isinstance(name, str)
            or not name.strip()
            or match not in (RULE_MATCH_ANY, RULE_MATCH_ALL)
            or presence not in (RULE_PRESENCE_HOME, RULE_PRESENCE_AWAY)
            or action not in (STATUS_ENABLE, STATUS_DISABLE)
        ):
            continue
        try:
            priority = max(0, min(100, int(raw.get(CONF_POLICY_RULE_PRIORITY, 50))))
        except (TypeError, ValueError):
            priority = 50
        result.append(
            PolicyRule(
                rule_id,
                name.strip(),
                users,
                match,
                presence,
                action,
                policies,
                priority,
                schedule.strip()
                if isinstance(schedule, str) and schedule.strip()
                else None,
            )
        )
    return tuple(result)


def serialize_policy_rules(
    rules: Mapping[str, object], valid_user_ids: set[str], valid_policy_ids: set[str]
) -> dict[str, dict[str, object]]:
    """Return normalized JSON-safe rules."""
    return {
        rule.rule_id: {
            CONF_POLICY_RULE_NAME: rule.name,
            CONF_POLICY_RULE_USERS: sorted(rule.user_ids),
            CONF_POLICY_RULE_MATCH: rule.match,
            CONF_POLICY_RULE_PRESENCE: rule.presence,
            CONF_POLICY_RULE_ACTION: rule.action,
            CONF_POLICY_RULE_POLICIES: sorted(rule.policy_ids),
            CONF_POLICY_RULE_PRIORITY: rule.priority,
            CONF_POLICY_RULE_SCHEDULE: rule.schedule_entity_id or "",
        }
        for rule in configured_policy_rules(
            {CONF_POLICY_RULES_V2: rules}, valid_user_ids, valid_policy_ids
        )
    }


def migrate_user_intents_to_policy_rules(
    raw_users: Mapping[str, object], valid_policy_ids: set[str]
) -> dict[str, dict[str, object]]:
    """Convert v1.9 user-attached intents to editable policy-centric rules."""
    migrated: dict[str, dict[str, object]] = {}
    fields = (
        (CONF_HOME_ENABLE_POLICIES, RULE_PRESENCE_HOME, STATUS_ENABLE),
        (CONF_HOME_DISABLE_POLICIES, RULE_PRESENCE_HOME, STATUS_DISABLE),
        (CONF_AWAY_ENABLE_POLICIES, RULE_PRESENCE_AWAY, STATUS_ENABLE),
        (CONF_AWAY_DISABLE_POLICIES, RULE_PRESENCE_AWAY, STATUS_DISABLE),
    )
    for user_id, raw_user in raw_users.items():
        if not isinstance(user_id, str) or not isinstance(raw_user, Mapping):
            continue
        name = raw_user.get(CONF_PRESENCE_USER_NAME)
        if not isinstance(name, str) or not name.strip():
            continue
        for field, presence, action in fields:
            policies = sorted(_valid_set(raw_user.get(field), valid_policy_ids))
            if not policies:
                continue
            migrated[f"migrated_{user_id}_{presence}_{action}"] = {
                CONF_POLICY_RULE_NAME: f"{name.strip()}: {presence} → {action}",
                CONF_POLICY_RULE_USERS: [user_id],
                CONF_POLICY_RULE_MATCH: RULE_MATCH_ANY,
                CONF_POLICY_RULE_PRESENCE: presence,
                CONF_POLICY_RULE_ACTION: action,
                CONF_POLICY_RULE_POLICIES: policies,
                CONF_POLICY_RULE_PRIORITY: 50,
                CONF_POLICY_RULE_SCHEDULE: "",
            }
    return migrated


def _condition_matches(
    rule: PolicyRule, user_states: Mapping[str, bool | None]
) -> bool | None:
    target = rule.presence == RULE_PRESENCE_HOME
    matches = [user_states.get(user_id) for user_id in rule.user_ids]
    if rule.match == RULE_MATCH_ANY:
        if any(state is target for state in matches):
            return True
        if all(state is not None for state in matches):
            return False
        return None
    if any(state is not None and state is not target for state in matches):
        return False
    if matches and all(state is target for state in matches):
        return True
    return None


def resolve_policy_intents(
    rules: tuple[PresencePolicyRule, ...],
    presence: Mapping[str, WifiPresence],
    *,
    policy_rules: tuple[PolicyRule, ...] = (),
    users: Mapping[str, PresenceUser] | None = None,
    schedule_states: Mapping[str, bool | None] | None = None,
    now: datetime | None = None,
) -> ResolvedPolicyIntents:
    """Resolve known intents by priority; unknown blocks and disable wins ties."""
    now = now or utcnow()
    intents: dict[str, list[tuple[int, str, str]]] = {}
    blocked: set[str] = set()
    for user in rules:
        is_home = aggregate_presence(user, presence, now)
        if is_home is None:
            blocked.update(user.affected_policies)
            continue
        enable, disable = user.intents_for(is_home)
        for policy_id in enable:
            intents.setdefault(policy_id, []).append((0, STATUS_ENABLE, user.name))
        for policy_id in disable:
            intents.setdefault(policy_id, []).append((0, STATUS_DISABLE, user.name))

    states = {
        user_id: aggregate_presence(user, presence, now)
        for user_id, user in (users or {}).items()
    }
    schedules = schedule_states or {}
    for rule in policy_rules:
        if rule.schedule_entity_id:
            schedule_state = schedules.get(rule.schedule_entity_id)
            if schedule_state is None:
                blocked.update(rule.policy_ids)
                continue
            if not schedule_state:
                continue
        matched = _condition_matches(rule, states)
        if matched is None:
            blocked.update(rule.policy_ids)
            continue
        if not matched:
            continue
        for policy_id in rule.policy_ids:
            intents.setdefault(policy_id, []).append(
                (rule.priority, rule.action, rule.name)
            )

    desired: dict[str, str] = {}
    conflicts: set[str] = set()
    reasons: dict[str, str] = {}
    for policy_id, values in intents.items():
        if policy_id in blocked:
            reasons[policy_id] = (
                "Blocked: required presence or schedule state is unknown"
            )
            continue
        priority = max(item[0] for item in values)
        winners = [item for item in values if item[0] == priority]
        statuses = {item[1] for item in winners}
        if len(statuses) > 1:
            conflicts.add(policy_id)
        desired[policy_id] = (
            STATUS_DISABLE if STATUS_DISABLE in statuses else STATUS_ENABLE
        )
        reasons[policy_id] = f"Priority {priority}: " + ", ".join(
            sorted({item[2] for item in winners})
        )
    return ResolvedPolicyIntents(
        desired,
        frozenset(blocked),
        frozenset(conflicts),
        reasons,
    )


class PresencePolicyRuleManager:
    """Calculate decisions and serialize verified policy writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        wifi_coordinator: FortiGateWifiCoordinator,
        policy_coordinators: Mapping[str, FortiGatePolicyCoordinator],
        rules: tuple[PresencePolicyRule, ...],
        *,
        users: tuple[PresenceUser, ...] = (),
        policy_rules: tuple[PolicyRule, ...] = (),
        automation_enabled: bool = True,
        dry_run: bool = False,
        default_override_minutes: int = 60,
    ) -> None:
        self.hass = hass
        self.wifi_coordinator = wifi_coordinator
        self.policy_coordinators = policy_coordinators
        self.rules = rules
        self.users = {user.user_id: user for user in users}
        self.policy_rules = policy_rules
        self.automation_enabled = automation_enabled
        self.dry_run = dry_run
        self.default_override_minutes = default_override_minutes
        self.last_reconcile: datetime | None = None
        self.last_error: str | None = None
        self.last_result: ResolvedPolicyIntents | None = None
        self.last_applied: dict[str, datetime] = {}
        self.overrides: dict[str, PolicyOverride] = {}
        self._pending = False
        self._task = None
        self._stopped = False
        self._last_conflicts: frozenset[str] = frozenset()
        self._override_timers: dict[str, Callable[[], None]] = {}
        self._state_listeners: list[Callable[[], None]] = []
        self._consecutive_failures = 0

    def async_start(self) -> list[Callable[[], None]]:
        """Listen to presence, policy, and referenced schedule changes."""
        unsubscribers = [self.wifi_coordinator.async_add_listener(self._schedule)]
        unsubscribers.extend(
            coordinator.async_add_listener(self._schedule)
            for coordinator in self.policy_coordinators.values()
        )
        schedules = {
            rule.schedule_entity_id
            for rule in self.policy_rules
            if rule.schedule_entity_id
        }
        if schedules:
            unsubscribers.append(
                async_track_state_change_event(self.hass, schedules, self._schedule)
            )
        self._schedule()
        return unsubscribers

    @callback
    def async_add_state_listener(
        self, listener: Callable[[], None]
    ) -> Callable[[], None]:
        self._state_listeners.append(listener)

        def remove() -> None:
            if listener in self._state_listeners:
                self._state_listeners.remove(listener)

        return remove

    @callback
    def _notify_state(self) -> None:
        for listener in tuple(self._state_listeners):
            listener()

    @callback
    def _schedule(self, *_args: Any) -> None:
        if self._stopped:
            return
        self._pending = True
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(
                self._async_run(), "FortiAP presence policy reconciliation"
            )

    async def _async_run(self) -> None:
        while self._pending:
            self._pending = False
            try:
                await self.async_reconcile()
            except Exception:
                self.last_error = "Policy reconciliation failed"
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3 and hasattr(self.hass, "data"):
                    ir.async_create_issue(
                        self.hass,
                        "fortigate_policy",
                        "policy_automation_failed",
                        is_fixable=False,
                        is_persistent=False,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="policy_automation_failed",
                    )
                self._notify_state()
                _LOGGER.exception("FortiAP presence policy reconciliation failed")

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        self._pending = False
        for cancel in self._override_timers.values():
            cancel()
        self._override_timers.clear()

    def override_for(self, policy_id: str) -> PolicyOverride:
        return self.overrides.get(policy_id, PolicyOverride())

    async def async_set_override(
        self, policy_id: str, mode: str, minutes: int | None = None
    ) -> None:
        """Set a runtime override and optionally return to automatic mode."""
        if policy_id not in self.policy_coordinators or mode not in OVERRIDE_MODES:
            raise ValueError("Invalid policy override")
        cancel = self._override_timers.pop(policy_id, None)
        if cancel:
            cancel()
        duration = self.default_override_minutes if minutes is None else minutes
        expires_at = None
        if mode != OVERRIDE_AUTOMATIC and duration > 0:
            expires_at = datetime.now(UTC) + timedelta(minutes=duration)

            @callback
            def expire(_now: datetime) -> None:
                self.overrides.pop(policy_id, None)
                self._override_timers.pop(policy_id, None)
                self._notify_state()
                self._schedule()

            self._override_timers[policy_id] = async_call_later(
                self.hass, duration * 60, expire
            )
        if mode == OVERRIDE_AUTOMATIC:
            self.overrides.pop(policy_id, None)
        else:
            self.overrides[policy_id] = PolicyOverride(mode, expires_at)
        self._notify_state()
        await self.async_reconcile()

    def _schedule_states(self) -> dict[str, bool | None]:
        result: dict[str, bool | None] = {}
        for rule in self.policy_rules:
            entity_id = rule.schedule_entity_id
            if not entity_id or entity_id in result:
                continue
            state = self.hass.states.get(entity_id)
            result[entity_id] = (
                True
                if state and state.state == "on"
                else False
                if state and state.state == "off"
                else None
            )
        return result

    async def async_reconcile(self) -> None:
        """Apply overrides first, then known rule decisions with verification."""
        override_desired: dict[str, str] = {}
        paused: set[str] = set()
        for policy_id, override in self.overrides.items():
            if override.mode == OVERRIDE_FORCE_ENABLE:
                override_desired[policy_id] = STATUS_ENABLE
            elif override.mode == OVERRIDE_FORCE_DISABLE:
                override_desired[policy_id] = STATUS_DISABLE
            elif override.mode == OVERRIDE_PAUSED:
                paused.add(policy_id)

        if (
            not self.wifi_coordinator.last_update_success
            and not override_desired
            and not paused
        ):
            return

        result: ResolvedPolicyIntents | None = None
        if self.wifi_coordinator.last_update_success and self.wifi_coordinator.data:
            result = resolve_policy_intents(
                self.rules,
                self.wifi_coordinator.data.presence,
                policy_rules=self.policy_rules,
                users=self.users,
                schedule_states=self._schedule_states(),
            )
            self.last_result = result
            if result.conflicts and result.conflicts != self._last_conflicts:
                _LOGGER.warning(
                    "Presence rules conflict for %s policy/policies; disable wins",
                    len(result.conflicts),
                )
            self._last_conflicts = result.conflicts

        desired = dict(result.desired) if result else {}
        desired.update(override_desired)
        for policy_id in paused:
            desired.pop(policy_id, None)
        if not self.automation_enabled:
            desired.clear()

        for policy_id, desired_status in desired.items():
            coordinator = self.policy_coordinators.get(policy_id)
            if coordinator is None or not coordinator.last_update_success:
                continue
            if self.dry_run and policy_id not in override_desired:
                continue
            if (
                coordinator.data is not None
                and coordinator.data.status == desired_status
            ):
                continue
            await coordinator.async_set_policy_status(desired_status)
            self.last_applied[policy_id] = datetime.now(UTC)
            reason = (
                f"manual {self.overrides[policy_id].mode} override"
                if policy_id in override_desired
                else result.reasons.get(policy_id, "presence rule")
                if result
                else "presence rule"
            )
            _LOGGER.info(
                "Verified policy %s status %s (%s)", policy_id, desired_status, reason
            )
            if hasattr(self.hass, "data"):
                async_log_entry(
                    self.hass,
                    "FortiAP Presence Tracker",
                    f"Policy {policy_id} verified {desired_status}: {reason}",
                    domain="fortigate_policy",
                )
            if hasattr(self.hass, "bus"):
                self.hass.bus.async_fire(
                    "fortigate_policy_decision",
                    {
                        "policy_id": policy_id,
                        "status": desired_status,
                        "reason": reason,
                    },
                )
        self.last_reconcile = datetime.now(UTC)
        self.last_error = None
        self._consecutive_failures = 0
        if hasattr(self.hass, "data"):
            ir.async_delete_issue(
                self.hass, "fortigate_policy", "policy_automation_failed"
            )
        self._notify_state()
