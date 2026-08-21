"""Policy-list parsing and config-entry compatibility helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_POLICIES,
    CONF_POLICY_ID,
    CONF_POLICY_NAME,
)


@dataclass(frozen=True, slots=True)
class PolicyDefinition:
    """One configured firewall policy and its identity guard."""

    policy_id: str
    expected_name: str


def parse_policy_ids(value: object) -> tuple[str, ...]:
    """Parse a comma-separated policy list, preserving order and removing duplicates."""
    if not isinstance(value, str):
        raise TypeError("Policy IDs must be text")
    policy_ids: list[str] = []
    for item in value.split(","):
        policy_id = item.strip()
        if not policy_id or not policy_id.isdigit():
            raise ValueError("Policy IDs must be comma-separated numbers")
        if policy_id not in policy_ids:
            policy_ids.append(policy_id)
    if not policy_ids:
        raise ValueError("At least one policy ID is required")
    return tuple(policy_ids)


def configured_policies(data: Mapping[str, Any]) -> tuple[PolicyDefinition, ...]:
    """Read the current policy list or a version-1 single-policy entry."""
    raw_policies = data.get(CONF_POLICIES)
    policies: list[PolicyDefinition] = []
    if isinstance(raw_policies, list):
        for raw in raw_policies:
            if not isinstance(raw, Mapping):
                continue
            policy_id = str(raw.get(CONF_POLICY_ID, "")).strip()
            expected_name = raw.get(CONF_POLICY_NAME, "")
            if policy_id.isdigit() and isinstance(expected_name, str):
                policies.append(PolicyDefinition(policy_id, expected_name.strip()))
    if policies:
        return tuple(policies)

    policy_id = str(data.get(CONF_POLICY_ID, "")).strip()
    expected_name = data.get(CONF_POLICY_NAME, "")
    if policy_id.isdigit() and isinstance(expected_name, str):
        return (PolicyDefinition(policy_id, expected_name.strip()),)
    raise ValueError("Config entry has no valid firewall policies")


def serialize_policies(
    policies: tuple[PolicyDefinition, ...],
) -> list[dict[str, str]]:
    """Return a config-entry-safe policy list."""
    return [
        {
            CONF_POLICY_ID: policy.policy_id,
            CONF_POLICY_NAME: policy.expected_name,
        }
        for policy in policies
    ]


def migrate_v1_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a version-1 entry and retain the original switch unique ID."""
    policies = configured_policies(data)
    migrated = dict(data)
    migrated[CONF_POLICIES] = serialize_policies(policies)
    migrated[CONF_LEGACY_PRIMARY_POLICY_ID] = policies[0].policy_id
    return migrated
