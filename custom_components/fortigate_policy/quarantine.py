"""Normalize and safely mutate FortiOS native MAC quarantine configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .const import QUARANTINE_TARGET_PREFIX
from .wifi import normalize_mac


@dataclass(frozen=True, slots=True)
class FortiGateQuarantineState:
    """The trustworthy subset of ``config user quarantine``."""

    enabled: bool
    quarantined_macs: frozenset[str]
    target_count: int


def quarantine_results(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Extract the singleton CMDB object returned by supported FortiOS builds."""
    results = payload.get("results")
    if isinstance(results, Sequence) and not isinstance(results, (str, bytes)):
        if len(results) != 1 or not isinstance(results[0], Mapping):
            raise ValueError("quarantine response has an invalid results list")
        results = results[0]
    if not isinstance(results, Mapping):
        raise ValueError("quarantine response lacks a configuration object")
    targets = results.get("targets", [])
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        raise ValueError("quarantine targets are not a list")
    return results


def parse_quarantine_state(payload: Mapping[str, Any]) -> FortiGateQuarantineState:
    """Return all drop-enabled MACs without trusting optional target metadata."""
    results = quarantine_results(payload)
    quarantined: set[str] = set()
    valid_targets = 0
    for target in results.get("targets", []):
        if not isinstance(target, Mapping):
            continue
        valid_targets += 1
        macs = target.get("macs", [])
        if not isinstance(macs, Sequence) or isinstance(macs, (str, bytes)):
            continue
        for item in macs:
            if not isinstance(item, Mapping) or item.get("drop") != "enable":
                continue
            if (mac := normalize_mac(item.get("mac"))) is not None:
                quarantined.add(mac)
    return FortiGateQuarantineState(
        enabled=results.get("quarantine", "enable") == "enable",
        quarantined_macs=frozenset(quarantined),
        target_count=valid_targets,
    )


def quarantine_target_name(mac: str) -> str:
    """Return an immutable integration-owned target name."""
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError("invalid MAC address")
    return f"{QUARANTINE_TARGET_PREFIX}{normalized.replace(':', '').upper()}"


def updated_quarantine_targets(
    results: Mapping[str, Any],
    mac: str,
    desired: bool,
    friendly_name: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Change one MAC while preserving every unrelated target and MAC.

    Empty integration-owned targets are removed. Empty administrator-created
    targets are retained because their existence may itself be intentional.
    """
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError("invalid MAC address")
    raw_targets = results.get("targets", [])
    if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
        raise ValueError("quarantine targets are not a list")

    targets: list[dict[str, Any]] = []
    found = False
    changed = False
    for raw_target in raw_targets:
        if not isinstance(raw_target, Mapping):
            raise ValueError("quarantine target is malformed")
        target = deepcopy(dict(raw_target))
        raw_macs = target.get("macs", [])
        if not isinstance(raw_macs, Sequence) or isinstance(raw_macs, (str, bytes)):
            raise ValueError("quarantine MAC table is malformed")
        kept_macs: list[dict[str, Any]] = []
        for raw_item in raw_macs:
            if not isinstance(raw_item, Mapping):
                raise ValueError("quarantine MAC entry is malformed")
            item = deepcopy(dict(raw_item))
            item.pop("q_origin_key", None)
            item.pop("parent", None)
            item_mac = normalize_mac(item.get("mac"))
            if item_mac != normalized:
                kept_macs.append(item)
                continue
            found = True
            if desired:
                if item.get("drop") != "enable":
                    item["drop"] = "enable"
                    changed = True
                kept_macs.append(item)
            else:
                changed = True
        target["macs"] = kept_macs
        target.pop("q_origin_key", None)
        target_name = str(target.get("entry", ""))
        if kept_macs or target_name != quarantine_target_name(normalized):
            targets.append(target)
        else:
            changed = True

    if desired and not found:
        description = "Managed by Home Assistant"
        clean_name = " ".join(friendly_name.split())
        if clean_name:
            description = f"Home Assistant: {clean_name}"[:63]
        targets.append(
            {
                "entry": quarantine_target_name(normalized),
                "description": description,
                "macs": [
                    {
                        "mac": normalized.upper(),
                        "description": description,
                        "drop": "enable",
                    }
                ],
            }
        )
        changed = True
    return targets, changed
