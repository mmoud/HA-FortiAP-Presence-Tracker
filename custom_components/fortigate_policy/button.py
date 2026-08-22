"""Diagnostic controls for a FortiGate config entry."""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FortiGatePolicyConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FortiGatePolicyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one refresh button for the FortiGate entry."""
    async_add_entities([FortiGateRefreshButton(entry)])


class FortiGateRefreshButton(ButtonEntity):
    """Request an immediate refresh without changing FortiGate configuration."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh_data"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: FortiGatePolicyConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_refresh_data"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def available(self) -> bool:
        """Return whether this entry has data that can be refreshed."""
        return bool(
            self._entry.runtime_data.policy_coordinators
            or self._entry.runtime_data.wifi_coordinator is not None
        )

    async def async_press(self) -> None:
        """Refresh policy and Wi-Fi coordinators in parallel."""
        coordinators = list(self._entry.runtime_data.policy_coordinators.values())
        if self._entry.runtime_data.wifi_coordinator is not None:
            coordinators.append(self._entry.runtime_data.wifi_coordinator)
        await asyncio.gather(
            *(coordinator.async_request_refresh() for coordinator in coordinators)
        )
