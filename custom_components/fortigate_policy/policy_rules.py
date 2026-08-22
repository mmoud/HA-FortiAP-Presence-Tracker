"""Presence-driven policy rules and conflict-safe reconciliation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_AWAY_DISABLE_POLICIES,
    CONF_AWAY_ENABLE_POLICIES,
    CONF_HOME_DISABLE_POLICIES,
    CONF_HOME_ENABLE_POLICIES,
    CONF_PRESENCE_POLICY_RULES,
    STATUS_DISABLE,
    STATUS_ENABLE,
)
from .coordinator import FortiGatePolicyCoordinator, FortiGateWifiCoordinator
from .wifi import WifiPresence, normalize_mac

_LOGGER = logging.getLogger(__name__)

RULE_FIELDS = (
    CONF_HOME_ENABLE_POLICIES,
    CONF_HOME_DISABLE_POLICIES,
    CONF_AWAY_ENABLE_POLICIES,
    CONF_AWAY_DISABLE_POLICIES,
)


@dataclass(frozen=True, slots=True)
class PresencePolicyRule:
    """Policy states requested by one tracked MAC for each presence state."""

    mac: str
    home_enable: frozenset[str]
    home_disable: frozenset[str]
    away_enable: frozenset[str]
    away_disable: frozenset[str]

    @property
    def affected_policies(self) -> frozenset[str]:
        """Return every policy whose state may depend on this tracker."""
        return frozenset().union(
            self.home_enable,
            self.home_disable,
            self.away_enable,
            self.away_disable,
        )

    def intents_for(self, is_home: bool) -> tuple[frozenset[str], frozenset[str]]:
        """Return the enable and disable intents for a known presence state."""
        if is_home:
            return self.home_enable, self.home_disable
        return self.away_enable, self.away_disable


@dataclass(frozen=True, slots=True)
class ResolvedPolicyIntents:
    """Conflict-resolved policy targets for one valid Wi-Fi update."""

    desired: dict[str, str]
    blocked_unknown: frozenset[str]
    conflicts: frozenset[str]


def _policy_set(value: object, valid_policy_ids: set[str]) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(
        str(policy_id) for policy_id in value if str(policy_id) in valid_policy_ids
    )


def configured_presence_rules(
    options: Mapping[str, Any],
    tracked_macs: set[str],
    valid_policy_ids: set[str],
) -> tuple[PresencePolicyRule, ...]:
    """Parse persisted rules while ignoring stale trackers and policy IDs."""
    raw_rules = options.get(CONF_PRESENCE_POLICY_RULES, {})
    if not isinstance(raw_rules, Mapping):
        return ()
    rules: list[PresencePolicyRule] = []
    for raw_mac, raw_rule in raw_rules.items():
        mac = normalize_mac(raw_mac)
        if mac is None or mac not in tracked_macs or not isinstance(raw_rule, Mapping):
            continue
        rule = PresencePolicyRule(
            mac=mac,
            home_enable=_policy_set(
                raw_rule.get(CONF_HOME_ENABLE_POLICIES), valid_policy_ids
            ),
            home_disable=_policy_set(
                raw_rule.get(CONF_HOME_DISABLE_POLICIES), valid_policy_ids
            ),
            away_enable=_policy_set(
                raw_rule.get(CONF_AWAY_ENABLE_POLICIES), valid_policy_ids
            ),
            away_disable=_policy_set(
                raw_rule.get(CONF_AWAY_DISABLE_POLICIES), valid_policy_ids
            ),
        )
        if rule.affected_policies:
            rules.append(rule)
    return tuple(rules)


def serialize_presence_rules(
    rules: Mapping[str, object],
    tracked_macs: set[str],
    valid_policy_ids: set[str],
) -> dict[str, dict[str, list[str]]]:
    """Normalize and prune rule options after trackers or policies change."""
    parsed = configured_presence_rules(
        {CONF_PRESENCE_POLICY_RULES: rules}, tracked_macs, valid_policy_ids
    )
    return {
        rule.mac: {
            CONF_HOME_ENABLE_POLICIES: sorted(rule.home_enable),
            CONF_HOME_DISABLE_POLICIES: sorted(rule.home_disable),
            CONF_AWAY_ENABLE_POLICIES: sorted(rule.away_enable),
            CONF_AWAY_DISABLE_POLICIES: sorted(rule.away_disable),
        }
        for rule in parsed
    }


def resolve_policy_intents(
    rules: tuple[PresencePolicyRule, ...],
    presence: Mapping[str, WifiPresence],
) -> ResolvedPolicyIntents:
    """Resolve tracker rules; unknown blocks writes and disable wins conflicts."""
    enable: set[str] = set()
    disable: set[str] = set()
    blocked: set[str] = set()
    for rule in rules:
        state = presence.get(rule.mac)
        if state is None or state.is_connected is None:
            blocked.update(rule.affected_policies)
            continue
        enable_intents, disable_intents = rule.intents_for(state.is_connected)
        enable.update(enable_intents)
        disable.update(disable_intents)

    conflicts = enable & disable
    desired = {
        policy_id: STATUS_DISABLE if policy_id in disable else STATUS_ENABLE
        for policy_id in enable | disable
        if policy_id not in blocked
    }
    return ResolvedPolicyIntents(
        desired=desired,
        blocked_unknown=frozenset(blocked),
        conflicts=frozenset(conflicts - blocked),
    )


class PresencePolicyRuleManager:
    """Reconcile presence rules without issuing redundant FortiGate writes."""

    def __init__(
        self,
        hass: HomeAssistant,
        wifi_coordinator: FortiGateWifiCoordinator,
        policy_coordinators: Mapping[str, FortiGatePolicyCoordinator],
        rules: tuple[PresencePolicyRule, ...],
    ) -> None:
        self.hass = hass
        self.wifi_coordinator = wifi_coordinator
        self.policy_coordinators = policy_coordinators
        self.rules = rules
        self.last_reconcile: datetime | None = None
        self.last_error: str | None = None
        self.last_result: ResolvedPolicyIntents | None = None
        self._pending = False
        self._task = None
        self._stopped = False
        self._last_conflicts: frozenset[str] = frozenset()

    def async_start(self) -> list[Callable[[], None]]:
        """Listen to both presence and policy state changes."""
        unsubscribers = [self.wifi_coordinator.async_add_listener(self._schedule)]
        unsubscribers.extend(
            coordinator.async_add_listener(self._schedule)
            for coordinator in self.policy_coordinators.values()
        )
        self._schedule()
        return unsubscribers

    @callback
    def _schedule(self) -> None:
        """Coalesce coordinator callbacks into one serialized reconciliation task."""
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
                _LOGGER.exception("FortiAP presence policy reconciliation failed")

    @callback
    def async_stop(self) -> None:
        """Prevent new reconciliation work after config-entry unload."""
        self._stopped = True
        self._pending = False

    async def async_reconcile(self) -> None:
        """Apply known state-based intents and verify every required change."""
        wifi_data = self.wifi_coordinator.data
        if not self.wifi_coordinator.last_update_success or wifi_data is None:
            return
        result = resolve_policy_intents(self.rules, wifi_data.presence)
        self.last_result = result
        if result.conflicts and result.conflicts != self._last_conflicts:
            _LOGGER.warning(
                "Presence rules conflict for %s policy/policies; disable wins",
                len(result.conflicts),
            )
        self._last_conflicts = result.conflicts
        for policy_id, desired_status in result.desired.items():
            coordinator = self.policy_coordinators.get(policy_id)
            if coordinator is None or not coordinator.last_update_success:
                continue
            if (
                coordinator.data is not None
                and coordinator.data.status == desired_status
            ):
                continue
            await coordinator.async_set_policy_status(desired_status)
            _LOGGER.info(
                "Presence rules verified policy %s status %s",
                policy_id,
                desired_status,
            )
        self.last_reconcile = datetime.now(UTC)
        self.last_error = None
