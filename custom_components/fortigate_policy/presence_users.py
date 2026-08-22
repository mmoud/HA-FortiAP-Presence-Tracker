"""Persisted multi-device user profiles and conservative presence aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .const import (
    CONF_AWAY_DISABLE_POLICIES,
    CONF_AWAY_ENABLE_POLICIES,
    CONF_FRIENDLY_NAME,
    CONF_HOME_DISABLE_POLICIES,
    CONF_HOME_ENABLE_POLICIES,
    CONF_PRESENCE_POLICY_RULES,
    CONF_PRESENCE_USER_MACS,
    CONF_PRESENCE_USER_NAME,
    CONF_PRESENCE_USERS,
    CONF_USER_AWAY_GRACE_PERIOD,
    DEFAULT_USER_AWAY_GRACE_PERIOD,
    MAX_USER_AWAY_GRACE_PERIOD,
    MIN_USER_AWAY_GRACE_PERIOD,
)
from .wifi import WifiPresence, normalize_mac

RULE_FIELDS = (
    CONF_HOME_ENABLE_POLICIES,
    CONF_HOME_DISABLE_POLICIES,
    CONF_AWAY_ENABLE_POLICIES,
    CONF_AWAY_DISABLE_POLICIES,
)


@dataclass(frozen=True, slots=True)
class PresenceUser:
    """A stable user identity backed by one or more tracked Wi-Fi devices."""

    user_id: str
    name: str
    macs: frozenset[str]
    home_enable: frozenset[str]
    home_disable: frozenset[str]
    away_enable: frozenset[str]
    away_disable: frozenset[str]
    away_grace_period: int = DEFAULT_USER_AWAY_GRACE_PERIOD

    @property
    def affected_policies(self) -> frozenset[str]:
        """Return every policy whose state may depend on this user."""
        return frozenset().union(
            self.home_enable,
            self.home_disable,
            self.away_enable,
            self.away_disable,
        )

    def intents_for(self, is_home: bool) -> tuple[frozenset[str], frozenset[str]]:
        """Return enable and disable intents for a known aggregate state."""
        if is_home:
            return self.home_enable, self.home_disable
        return self.away_enable, self.away_disable


def aggregate_presence(
    user: PresenceUser,
    presence: Mapping[str, WifiPresence],
    now: datetime | None = None,
) -> bool | None:
    """Return conservative aggregate state with an optional longer user grace."""
    members = [presence.get(mac) for mac in user.macs]
    states = [member.is_connected if member is not None else None for member in members]
    if any(state is True for state in states):
        return True
    if any(state is None for state in states):
        return None
    if states and all(state is False for state in states):
        if now is not None:
            for member in members:
                if (
                    member is not None
                    and member.missing_since is not None
                    and (now - member.missing_since).total_seconds()
                    < user.away_grace_period
                ):
                    return True
        return False
    return None


def _policy_set(value: object, valid_policy_ids: set[str]) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(
        str(policy_id) for policy_id in value if str(policy_id) in valid_policy_ids
    )


def _away_grace(value: object) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_USER_AWAY_GRACE_PERIOD
    return max(MIN_USER_AWAY_GRACE_PERIOD, min(MAX_USER_AWAY_GRACE_PERIOD, seconds))


def configured_presence_users(
    options: Mapping[str, Any],
    tracked_macs: set[str],
    valid_policy_ids: set[str],
) -> tuple[PresenceUser, ...]:
    """Parse valid profiles, pruning stale devices and policy references."""
    raw_users = options.get(CONF_PRESENCE_USERS, {})
    if not isinstance(raw_users, Mapping):
        return ()
    users: list[PresenceUser] = []
    for raw_id, raw_user in raw_users.items():
        if (
            not isinstance(raw_id, str)
            or not raw_id
            or not isinstance(raw_user, Mapping)
        ):
            continue
        raw_macs = raw_user.get(CONF_PRESENCE_USER_MACS, [])
        if not isinstance(raw_macs, list):
            continue
        macs = frozenset(
            normalized
            for mac in raw_macs
            if (normalized := normalize_mac(mac)) is not None
            and normalized in tracked_macs
        )
        name = raw_user.get(CONF_PRESENCE_USER_NAME)
        if not macs or not isinstance(name, str) or not name.strip():
            continue
        users.append(
            PresenceUser(
                user_id=raw_id,
                name=name.strip(),
                macs=macs,
                home_enable=_policy_set(
                    raw_user.get(CONF_HOME_ENABLE_POLICIES), valid_policy_ids
                ),
                home_disable=_policy_set(
                    raw_user.get(CONF_HOME_DISABLE_POLICIES), valid_policy_ids
                ),
                away_enable=_policy_set(
                    raw_user.get(CONF_AWAY_ENABLE_POLICIES), valid_policy_ids
                ),
                away_disable=_policy_set(
                    raw_user.get(CONF_AWAY_DISABLE_POLICIES), valid_policy_ids
                ),
                away_grace_period=_away_grace(
                    raw_user.get(
                        CONF_USER_AWAY_GRACE_PERIOD,
                        DEFAULT_USER_AWAY_GRACE_PERIOD,
                    )
                ),
            )
        )
    return tuple(users)


def serialize_presence_users(
    users: Mapping[str, object],
    tracked_macs: set[str],
    valid_policy_ids: set[str],
) -> dict[str, dict[str, object]]:
    """Normalize profiles after trackers or firewall policies change."""
    parsed = configured_presence_users(
        {CONF_PRESENCE_USERS: users}, tracked_macs, valid_policy_ids
    )
    return {
        user.user_id: {
            CONF_PRESENCE_USER_NAME: user.name,
            CONF_PRESENCE_USER_MACS: sorted(user.macs),
            CONF_HOME_ENABLE_POLICIES: sorted(user.home_enable),
            CONF_HOME_DISABLE_POLICIES: sorted(user.home_disable),
            CONF_AWAY_ENABLE_POLICIES: sorted(user.away_enable),
            CONF_AWAY_DISABLE_POLICIES: sorted(user.away_disable),
            CONF_USER_AWAY_GRACE_PERIOD: user.away_grace_period,
        }
        for user in parsed
    }


def migrate_tracker_rules_to_users(
    options: Mapping[str, Any], tracked_macs: set[str]
) -> dict[str, dict[str, object]]:
    """Convert v1.8 per-MAC rules into one-device user profiles."""
    raw_rules = options.get(CONF_PRESENCE_POLICY_RULES, {})
    tracked = options.get("tracked_clients", {})
    if not isinstance(raw_rules, Mapping):
        return {}
    migrated: dict[str, dict[str, object]] = {}
    for raw_mac, raw_rule in raw_rules.items():
        mac = normalize_mac(raw_mac)
        if mac is None or mac not in tracked_macs or not isinstance(raw_rule, Mapping):
            continue
        metadata = tracked.get(mac, {}) if isinstance(tracked, Mapping) else {}
        friendly_name = (
            metadata.get(CONF_FRIENDLY_NAME) if isinstance(metadata, Mapping) else None
        )
        migrated[f"device_{mac.replace(':', '')}"] = {
            CONF_PRESENCE_USER_NAME: (
                friendly_name.strip()
                if isinstance(friendly_name, str) and friendly_name.strip()
                else mac
            ),
            CONF_PRESENCE_USER_MACS: [mac],
            **{
                field: list(raw_rule.get(field, []))
                if isinstance(raw_rule.get(field), list)
                else []
                for field in RULE_FIELDS
            },
        }
    return migrated
