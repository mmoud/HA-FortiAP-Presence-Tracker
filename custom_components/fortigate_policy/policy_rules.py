"""Presence-driven policy rules and conflict-safe reconciliation."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import (
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
from .wifi import WifiPresence

_LOGGER = logging.getLogger(__name__)

PresencePolicyRule = PresenceUser


@dataclass(frozen=True, slots=True)
class ResolvedPolicyIntents:
    """Conflict-resolved policy targets for one valid Wi-Fi update."""

    desired: dict[str, str]
    blocked_unknown: frozenset[str]
    conflicts: frozenset[str]


def configured_presence_rules(
    options: Mapping[str, Any],
    tracked_macs: set[str],
    valid_policy_ids: set[str],
) -> tuple[PresencePolicyRule, ...]:
    """Parse user rules, with a runtime fallback for pre-migration entries."""
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


def resolve_policy_intents(
    rules: tuple[PresencePolicyRule, ...],
    presence: Mapping[str, WifiPresence],
) -> ResolvedPolicyIntents:
    """Resolve tracker rules; unknown blocks writes and disable wins conflicts."""
    enable: set[str] = set()
    disable: set[str] = set()
    blocked: set[str] = set()
    for rule in rules:
        is_home = aggregate_presence(rule, presence)
        if is_home is None:
            blocked.update(rule.affected_policies)
            continue
        enable_intents, disable_intents = rule.intents_for(is_home)
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
