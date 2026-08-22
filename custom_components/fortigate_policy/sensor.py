"""Optional FortiGate Wi-Fi client count sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FortiGatePolicyConfigEntry
from .const import CONF_WIFI_CLIENT_COUNT_SENSOR
from .coordinator import FortiGateWifiCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FortiGatePolicyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the opt-in associated-client count sensor."""
    entities: list[SensorEntity] = []
    coordinator = entry.runtime_data.wifi_coordinator
    if coordinator is not None and entry.options.get(
        CONF_WIFI_CLIENT_COUNT_SENSOR, False
    ):
        entities.append(FortiGateWifiClientCount(coordinator, entry))
    manager = entry.runtime_data.rule_manager
    if manager is not None:
        managed = {
            policy_id for rule in manager.rules for policy_id in rule.affected_policies
        } | {
            policy_id for rule in manager.policy_rules for policy_id in rule.policy_ids
        }
        entities.extend(
            FortiGatePolicyDecisionSensor(entry, manager, policy_id)
            for policy_id in sorted(managed)
        )
    async_add_entities(entities)


class FortiGateWifiClientCount(
    CoordinatorEntity[FortiGateWifiCoordinator], SensorEntity
):
    """Count currently associated clients from the one shared monitor poll."""

    _attr_has_entity_name = True
    _attr_name = "Wi-Fi clients"
    _attr_icon = "mdi:wifi"
    _attr_suggested_object_id = "fortigate_wifi_clients"

    def __init__(
        self, coordinator: FortiGateWifiCoordinator, entry: FortiGatePolicyConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_wifi_client_count"
        self._attr_device_info = {
            "identifiers": {("fortigate_policy", entry.entry_id)},
        }

    @property
    def native_value(self) -> int | None:
        return len(self.coordinator.data.clients) if self.coordinator.data else None


class FortiGatePolicyDecisionSensor(SensorEntity):
    """Explain automation intent without changing the actual policy switch state."""

    _attr_has_entity_name = True
    _attr_translation_key = "policy_decision"
    _attr_icon = "mdi:shield-search"

    def __init__(self, entry, manager, policy_id: str) -> None:
        self._manager = manager
        self._policy_id = policy_id
        self._attr_unique_id = f"{entry.entry_id}_policy_{policy_id}_decision"
        self._attr_name = f"Policy {policy_id} automation decision"
        self._attr_device_info = {"identifiers": {("fortigate_policy", entry.entry_id)}}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._manager.async_add_state_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> str:
        override = self._manager.override_for(self._policy_id)
        if override.mode != "automatic":
            return override.mode
        result = self._manager.last_result
        if result is None:
            return "waiting"
        if self._policy_id in result.blocked_unknown:
            return "blocked_unknown"
        return result.desired.get(self._policy_id, "no_action")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._manager.last_result
        override = self._manager.override_for(self._policy_id)
        return {
            "reason": result.reasons.get(self._policy_id) if result else None,
            "conflict": bool(result and self._policy_id in result.conflicts),
            "automation_enabled": self._manager.automation_enabled,
            "dry_run": self._manager.dry_run,
            "override": override.mode,
            "override_expires_at": (
                override.expires_at.isoformat() if override.expires_at else None
            ),
            "last_applied": (
                self._manager.last_applied[self._policy_id].isoformat()
                if self._policy_id in self._manager.last_applied
                else None
            ),
            "last_error": self._manager.last_error,
        }
