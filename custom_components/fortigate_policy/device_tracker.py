"""Wi-Fi device trackers backed by FortiGate's associated-client monitor API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.const import CONF_HOST, STATE_HOME, STATE_NOT_HOME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FortiGatePolicyConfigEntry, tracked_macs_from_options
from .const import CONF_FRIENDLY_NAME, CONF_TRACKED_CLIENTS, CONF_VDOM, DOMAIN
from .coordinator import FortiGateWifiCoordinator


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
    async_add_entities(
        [
            FortiGateWifiClientTracker(
                entry,
                coordinator,
                mac,
                _friendly_name(tracked_clients.get(mac)),
            )
            for mac in sorted(tracked_macs_from_options(entry.options))
        ]
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
        self._attr_unique_id = f"{entry.entry_id}_wifi_{mac.replace(':', '')}"
        self._attr_name = friendly_name or mac
        self._attr_suggested_object_id = (
            friendly_name.lower().replace(" ", "_")
            if friendly_name
            else mac.replace(":", "_")
        )
        self._attr_mac_address = mac
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_wifi_{mac.replace(':', '')}")},
            "connections": {(dr.CONNECTION_NETWORK_MAC, mac)},
            "name": friendly_name or mac,
            "via_device": (DOMAIN, entry.entry_id),
        }
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
        }
        if presence is None:
            return attributes
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
            ):
                if value is not None:
                    attributes[key] = value
        return attributes
