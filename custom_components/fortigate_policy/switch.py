"""The FortiGate policy switch entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import FortiGatePolicyConfigEntry
from .api import FortiGateError
from .const import (
    CONF_LEGACY_PRIMARY_POLICY_ID,
    CONF_VDOM,
    DOMAIN,
    STATUS_ENABLE,
)
from .coordinator import FortiGatePolicyCoordinator
from .policy_config import fortigate_entry_title


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FortiGatePolicyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one independently verified switch per configured policy."""
    async_add_entities(
        [
            FortiGatePolicySwitch(entry, coordinator, policy_id)
            for policy_id, coordinator in entry.runtime_data.policy_coordinators.items()
        ]
    )


class FortiGatePolicySwitch(
    CoordinatorEntity[FortiGatePolicyCoordinator], SwitchEntity
):
    """Switch backed solely by actual, identity-checked FortiGate state."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:shield-switch"

    def __init__(
        self,
        entry: FortiGatePolicyConfigEntry,
        coordinator: FortiGatePolicyCoordinator,
        policy_id: str,
    ) -> None:
        """Initialize the policy switch."""
        super().__init__(coordinator)
        self._entry = entry
        self._policy_id = policy_id
        legacy_primary = entry.data.get(CONF_LEGACY_PRIMARY_POLICY_ID)
        self._attr_unique_id = (
            entry.entry_id
            if legacy_primary == policy_id
            else f"{entry.entry_id}_policy_{policy_id}"
        )
        policy_name = coordinator.data.name if coordinator.data else ""
        self._attr_name = policy_name or f"Policy {policy_id}"
        self._attr_suggested_object_id = f"fortigate_policy_{policy_id}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": fortigate_entry_title(entry.data),
            "manufacturer": "Fortinet",
            "model": "FortiGate",
        }

    @property
    def is_on(self) -> bool | None:
        """Return FortiGate's actual policy status; never assume a command worked."""
        policy = self.coordinator.data
        return policy.status == STATUS_ENABLE if policy is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose useful non-sensitive policy metadata."""
        policy = self.coordinator.data
        checked = self.coordinator.last_successful_check
        return {
            "policy_id": policy.policy_id if policy else self._policy_id,
            "policy_name": policy.name if policy else None,
            "vdom": self._entry.data[CONF_VDOM],
            "fortigate": self._entry.data[CONF_HOST],
            "last_successful_check": checked.isoformat() if checked else None,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable only the configured policy's status property."""
        await self._async_set_status(STATUS_ENABLE)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable only the configured policy's status property."""
        await self._async_set_status("disable")

    async def _async_set_status(self, desired_status: str) -> None:
        """Run the complete preflight/write/readback transaction."""
        try:
            await self.coordinator.async_set_policy_status(desired_status)
        except FortiGateError as err:
            # A fresh coordinator poll preserves actual state if possible, or
            # marks this entity unavailable if FortiGate cannot be reached.
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                "FortiGate policy command was not accepted or verified"
            ) from err
