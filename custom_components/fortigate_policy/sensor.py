"""Optional FortiGate Wi-Fi client count sensor."""

from __future__ import annotations

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
    coordinator = entry.runtime_data.wifi_coordinator
    if coordinator is None or not entry.options.get(
        CONF_WIFI_CLIENT_COUNT_SENSOR, False
    ):
        return
    async_add_entities([FortiGateWifiClientCount(coordinator, entry)])


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
