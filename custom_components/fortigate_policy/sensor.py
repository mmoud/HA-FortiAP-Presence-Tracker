"""FortiGate network-client and policy diagnostic sensors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfSignalStrength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FortiGatePolicyConfigEntry, tracked_macs_from_options
from .const import (
    CONF_FRIENDLY_NAME,
    CONF_NETWORK_CREATE_TRACKER_ENTITIES,
    CONF_NETWORK_NEW_DEVICE_DETECTION,
    CONF_TRACKED_CLIENTS,
    CONF_WIFI_CLIENT_COUNT_SENSOR,
    DEFAULT_NETWORK_CREATE_TRACKER_ENTITIES,
    DEFAULT_NETWORK_NEW_DEVICE_DETECTION,
    DOMAIN,
)
from .coordinator import FortiGateWifiCoordinator
from .network_device import network_client_device_info
from .network_store import NetworkDeviceRecord
from .wifi import WifiPresence, utcnow


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
    tracked = tracked_macs_from_options(entry.options)
    raw_clients = entry.options.get(CONF_TRACKED_CLIENTS, {})
    if coordinator is not None and entry.options.get(
        CONF_NETWORK_CREATE_TRACKER_ENTITIES,
        DEFAULT_NETWORK_CREATE_TRACKER_ENTITIES,
    ):
        for mac in sorted(tracked):
            friendly_name = None
            metadata = (
                raw_clients.get(mac) if isinstance(raw_clients, Mapping) else None
            )
            if isinstance(metadata, Mapping) and isinstance(
                metadata.get(CONF_FRIENDLY_NAME), str
            ):
                friendly_name = metadata[CONF_FRIENDLY_NAME]
            entities.extend(
                FortiGateNetworkClientSensor(
                    entry, coordinator, mac, friendly_name, description
                )
                for description in NETWORK_SENSOR_DESCRIPTIONS
            )
    if coordinator is not None and entry.options.get(
        CONF_NETWORK_NEW_DEVICE_DETECTION, DEFAULT_NETWORK_NEW_DEVICE_DETECTION
    ):
        entities.append(FortiGateUnknownNetworkClientCount(coordinator, entry, tracked))
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


@dataclass(frozen=True, kw_only=True)
class NetworkSensorDescription:
    """A compact sensor exposed on each explicitly selected network client."""

    key: str
    icon: str
    value: Callable[[NetworkDeviceRecord | None, WifiPresence | None], Any]
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    enabled: bool = True


def _metadata(record: NetworkDeviceRecord | None, key: str) -> str | int | None:
    return record.metadata.get(key) if record else None


NETWORK_SENSOR_DESCRIPTIONS = (
    NetworkSensorDescription(
        key="ip_address",
        icon="mdi:ip-network",
        value=lambda record, _presence: _metadata(record, "ip"),
    ),
    NetworkSensorDescription(
        key="connection_type",
        icon="mdi:wifi",
        value=lambda record, _presence: _metadata(record, "connection_type"),
    ),
    NetworkSensorDescription(
        key="ssid",
        icon="mdi:wifi-settings",
        value=lambda record, _presence: _metadata(record, "ssid"),
    ),
    NetworkSensorDescription(
        key="access_point",
        icon="mdi:access-point",
        value=lambda record, _presence: _metadata(record, "ap_name"),
    ),
    NetworkSensorDescription(
        key="first_seen",
        icon="mdi:clock-start",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda record, _presence: record.first_seen if record else None,
    ),
    NetworkSensorDescription(
        key="last_seen",
        icon="mdi:clock-check",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda record, _presence: record.last_seen if record else None,
    ),
    NetworkSensorDescription(
        key="connected_since",
        icon="mdi:connection",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda record, _presence: record.connected_since if record else None,
    ),
    NetworkSensorDescription(
        key="connection_duration",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        unit=UnitOfTime.SECONDS,
        enabled=False,
        value=lambda record, _presence: (
            max(0, int((utcnow() - record.connected_since).total_seconds()))
            if record and record.connected and record.connected_since
            else None
        ),
    ),
    NetworkSensorDescription(
        key="signal_strength",
        icon="mdi:wifi-strength-2",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        unit=UnitOfSignalStrength.DECIBELS_MILLIWATT,
        enabled=False,
        value=lambda _record, presence: (
            presence.client.rssi if presence and presence.client else None
        ),
    ),
)


class FortiGateNetworkClientSensor(
    CoordinatorEntity[FortiGateWifiCoordinator], SensorEntity
):
    """One useful fact on an explicitly selected client's Device Registry page."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: FortiGatePolicyConfigEntry,
        coordinator: FortiGateWifiCoordinator,
        mac: str,
        friendly_name: str | None,
        description: NetworkSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        compact = mac.replace(":", "")
        self._entry = entry
        self._mac = mac
        self._description = description
        self._attr_unique_id = f"{entry.entry_id}_wifi_{compact}_{description.key}"
        self._attr_translation_key = description.key
        self._attr_icon = description.icon
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.unit
        self._attr_entity_registry_enabled_default = description.enabled
        self._attr_device_info = network_client_device_info(
            entry, mac, friendly_name or mac
        )

    @property
    def native_value(self) -> str | int | datetime | None:
        store = self._entry.runtime_data.network_store
        return self._description.value(
            store.records.get(self._mac) if store else None,
            self.coordinator.presence_for(self._mac),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._description.key != "connection_type":
            return None
        store = self._entry.runtime_data.network_store
        record = store.records.get(self._mac) if store else None
        if not record:
            return None
        return {
            key: value
            for key in (
                "vlan",
                "interface",
                "manufacturer",
                "ap_serial",
                "band",
                "channel",
            )
            if (value := record.metadata.get(key)) is not None
        }


class FortiGateUnknownNetworkClientCount(
    CoordinatorEntity[FortiGateWifiCoordinator], SensorEntity
):
    """Count associated MACs that have not been explicitly named/tracked."""

    _attr_has_entity_name = True
    _attr_translation_key = "unknown_network_devices"
    _attr_icon = "mdi:help-network"

    def __init__(
        self,
        coordinator: FortiGateWifiCoordinator,
        entry: FortiGatePolicyConfigEntry,
        tracked_macs: set[str],
    ) -> None:
        super().__init__(coordinator)
        self._tracked_macs = tracked_macs
        self._attr_unique_id = f"{entry.entry_id}_unknown_network_devices"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(set(self.coordinator.data.clients) - self._tracked_macs)


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
