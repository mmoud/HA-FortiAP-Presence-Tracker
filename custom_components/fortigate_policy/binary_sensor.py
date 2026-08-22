"""Presence binary sensors for selected FortiGate Wi-Fi clients."""

from __future__ import annotations

from collections.abc import Mapping

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FortiGatePolicyConfigEntry, tracked_macs_from_options
from .const import CONF_FRIENDLY_NAME, CONF_TRACKED_CLIENTS, DOMAIN
from .coordinator import FortiGateWifiCoordinator
from .policy_config import configured_policies
from .presence_users import PresenceUser, aggregate_presence, configured_presence_users


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FortiGatePolicyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one presence binary sensor for every selected tracker."""
    coordinator = entry.runtime_data.wifi_coordinator
    if coordinator is None:
        return
    tracked_clients = entry.options.get(CONF_TRACKED_CLIENTS, {})
    if not isinstance(tracked_clients, Mapping):
        tracked_clients = {}
    tracked_macs = tracked_macs_from_options(entry.options)
    users = configured_presence_users(
        entry.options,
        tracked_macs,
        {policy.policy_id for policy in configured_policies(entry.data)},
    )
    async_add_entities(
        [
            FortiGateWifiPresenceBinarySensor(
                entry,
                coordinator,
                mac,
                _friendly_name(tracked_clients.get(mac)),
            )
            for mac in sorted(tracked_macs)
        ]
        + [
            FortiGatePresenceUserBinarySensor(entry, coordinator, user)
            for user in users
        ]
    )


def _friendly_name(value: object) -> str | None:
    if isinstance(value, Mapping):
        name = value.get(CONF_FRIENDLY_NAME)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


class FortiGateWifiPresenceBinarySensor(
    CoordinatorEntity[FortiGateWifiCoordinator], BinarySensorEntity
):
    """Boolean presence view of the same grace-aware tracker state."""

    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True
    _attr_translation_key = "presence"

    def __init__(
        self,
        entry: FortiGatePolicyConfigEntry,
        coordinator: FortiGateWifiCoordinator,
        mac: str,
        friendly_name: str | None,
    ) -> None:
        super().__init__(coordinator)
        compact_mac = mac.replace(":", "")
        device_name = friendly_name or mac
        self._mac = mac
        self._attr_unique_id = f"{entry.entry_id}_wifi_{compact_mac}_presence"
        self._attr_suggested_object_id = (
            f"{friendly_name.lower().replace(' ', '_')}_presence"
            if friendly_name
            else f"{mac.replace(':', '_')}_presence"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_wifi_{compact_mac}")},
            "connections": {(dr.CONNECTION_NETWORK_MAC, mac)},
            "name": device_name,
            "via_device": (DOMAIN, entry.entry_id),
        }

    @property
    def is_on(self) -> bool | None:
        """Return ON for home, OFF for away, and unknown before valid data."""
        presence = self.coordinator.presence_for(self._mac)
        return presence.is_connected if presence is not None else None


class FortiGatePresenceUserBinarySensor(
    CoordinatorEntity[FortiGateWifiCoordinator], BinarySensorEntity
):
    """Boolean view of a multi-device user's aggregate presence."""

    _attr_device_class = BinarySensorDeviceClass.PRESENCE
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True
    _attr_icon = "mdi:account-check"

    def __init__(
        self,
        entry: FortiGatePolicyConfigEntry,
        coordinator: FortiGateWifiCoordinator,
        user: PresenceUser,
    ) -> None:
        super().__init__(coordinator)
        self._user = user
        self._attr_unique_id = f"{entry.entry_id}_presence_user_{user.user_id}_presence"
        self._attr_name = f"{user.name} Presence"
        self._attr_suggested_object_id = (
            f"{user.name.lower().replace(' ', '_')}_presence"
        )

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return aggregate_presence(self._user, self.coordinator.data.presence)
