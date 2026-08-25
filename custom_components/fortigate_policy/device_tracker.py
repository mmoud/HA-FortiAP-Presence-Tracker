"""Wi-Fi device trackers backed by FortiGate's associated-client monitor API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.const import CONF_HOST, STATE_HOME, STATE_NOT_HOME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import (
    FortiGatePolicyConfigEntry,
    tracked_macs_from_options,
    tracked_ssid_filters_from_options,
)
from .const import (
    CONF_FRIENDLY_NAME,
    CONF_NETWORK_CREATE_TRACKER_ENTITIES,
    CONF_TRACKED_CLIENTS,
    CONF_VDOM,
    DEFAULT_NETWORK_CREATE_TRACKER_ENTITIES,
)
from .coordinator import FortiGateWifiCoordinator
from .network_device import network_client_device_info
from .policy_config import configured_policies
from .presence_users import PresenceUser, aggregate_presence, configured_presence_users
from .wifi import utcnow


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FortiGatePolicyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create trackers only for MACs the user explicitly selected in Options."""
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
    client_entities = (
        [
            FortiGateWifiClientTracker(
                entry,
                coordinator,
                mac,
                _friendly_name(tracked_clients.get(mac)),
            )
            for mac in sorted(tracked_macs)
        ]
        if entry.options.get(
            CONF_NETWORK_CREATE_TRACKER_ENTITIES,
            DEFAULT_NETWORK_CREATE_TRACKER_ENTITIES,
        )
        else []
    )
    async_add_entities(
        client_entities
        + [FortiGatePresenceUserTracker(entry, coordinator, user) for user in users]
    )


def _friendly_name(value: object) -> str | None:
    if isinstance(value, Mapping):
        name = value.get(CONF_FRIENDLY_NAME)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


class FortiGateWifiClientTracker(
    CoordinatorEntity[FortiGateWifiCoordinator], RestoreEntity, ScannerEntity
):
    """A MAC-stable network ScannerEntity with conservative away detection."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:wifi-marker"
    _attr_entity_registry_enabled_default = True
    _attr_suggested_object_id = "fortigate_wifi_client"

    def __init__(
        self,
        entry: FortiGatePolicyConfigEntry,
        coordinator: FortiGateWifiCoordinator,
        mac: str,
        friendly_name: str | None,
    ) -> None:
        """Initialize a tracker without performing I/O."""
        super().__init__(coordinator)
        self._entry = entry
        self._mac = mac
        self._allowed_ssids = tracked_ssid_filters_from_options(entry.options).get(
            mac, frozenset()
        )
        self._attr_unique_id = f"{entry.entry_id}_wifi_{mac.replace(':', '')}"
        self._attr_name = friendly_name or mac
        self._attr_suggested_object_id = (
            friendly_name.lower().replace(" ", "_")
            if friendly_name
            else mac.replace(":", "_")
        )
        self._attr_mac_address = mac
        self._attr_device_info = network_client_device_info(
            entry, mac, friendly_name or mac
        )
        self._restored_connected: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Seed prior valid presence before the first post-restart monitor poll."""
        await super().async_added_to_hass()
        previous = await self.async_get_last_state()
        if previous is None:
            return
        if previous.state == STATE_HOME:
            self._restored_connected = True
        elif previous.state == STATE_NOT_HOME:
            self._restored_connected = False
        else:
            return
        self.coordinator.restore_presence(
            self._mac, self._restored_connected, previous.last_updated
        )

    @property
    def is_connected(self) -> bool | None:
        """Return home/not_home only from a valid, grace-aware coordinator poll."""
        presence = self.coordinator.presence_for(self._mac)
        if presence is not None:
            return presence.is_connected
        return self._restored_connected

    @property
    def hostname(self) -> str | None:
        presence = self.coordinator.presence_for(self._mac)
        return presence.client.hostname if presence and presence.client else None

    @property
    def ip_address(self) -> str | None:
        presence = self.coordinator.presence_for(self._mac)
        return presence.client.ip if presence and presence.client else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose stable useful diagnostics without high-churn signal readings."""
        presence = self.coordinator.presence_for(self._mac)
        attributes: dict[str, Any] = {
            "mac": self._mac,
            "vdom": self._entry.data[CONF_VDOM],
            "fortigate": self._entry.data[CONF_HOST],
            "presence_source": "fortiap_association",
        }
        if self._allowed_ssids:
            attributes["allowed_ssids"] = sorted(self._allowed_ssids)
        if presence is None:
            return self._stored_attributes(attributes)
        if presence.last_seen:
            attributes["last_seen"] = presence.last_seen.isoformat()
        if presence.missing_since:
            attributes["missing_since"] = presence.missing_since.isoformat()
        if presence.client:
            client = presence.client
            for key, value in (
                ("ssid", client.ssid),
                ("access_point", client.ap_name),
                ("access_point_serial", client.ap_serial),
                ("band", client.band),
                ("radio", client.radio),
                ("channel", client.channel),
                ("association_time", client.association_time),
                ("vlan", client.vlan),
                ("username", client.username),
                ("interface", client.interface),
                ("manufacturer", client.manufacturer),
                ("connection_type", client.connection_type),
            ):
                if value is not None:
                    attributes[key] = value
        return self._stored_attributes(attributes)

    def _stored_attributes(self, attributes: dict[str, Any]) -> dict[str, Any]:
        store = self._entry.runtime_data.network_store
        record = store.records.get(self._mac) if store else None
        if record:
            attributes["first_seen"] = record.first_seen.isoformat()
            attributes["last_seen"] = record.last_seen.isoformat()
            if record.connected_since:
                attributes["connected_since"] = record.connected_since.isoformat()
            if record.owner:
                attributes["owner"] = record.owner
        return attributes


class FortiGatePresenceUserTracker(
    CoordinatorEntity[FortiGateWifiCoordinator], ScannerEntity
):
    """Aggregate tracker: any device home means home; every device away means away."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:account-multiple-check"
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        entry: FortiGatePolicyConfigEntry,
        coordinator: FortiGateWifiCoordinator,
        user: PresenceUser,
    ) -> None:
        super().__init__(coordinator)
        self._user = user
        self._attr_unique_id = f"{entry.entry_id}_presence_user_{user.user_id}"
        self._attr_name = user.name
        self._attr_suggested_object_id = user.name.lower().replace(" ", "_")

    @property
    def is_connected(self) -> bool | None:
        """Aggregate only already grace-aware member states."""
        if self.coordinator.data is None:
            return None
        return aggregate_presence(self._user, self.coordinator.data.presence, utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose stable group information without high-churn Wi-Fi data."""
        return {
            "device_count": len(self._user.macs),
            "home_device_count": (
                sum(
                    self.coordinator.data.presence.get(mac) is not None
                    and self.coordinator.data.presence[mac].is_connected is True
                    for mac in self._user.macs
                )
                if self.coordinator.data is not None
                else None
            ),
        }
