"""Manual policy-automation override controls."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import FortiGatePolicyConfigEntry
from .const import DOMAIN, OVERRIDE_MODES
from .policy_rules import PresencePolicyRuleManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FortiGatePolicyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one override selector for every rule-managed policy."""
    manager = entry.runtime_data.rule_manager
    if manager is None:
        return
    managed = {
        policy_id for rule in manager.rules for policy_id in rule.affected_policies
    } | {policy_id for rule in manager.policy_rules for policy_id in rule.policy_ids}
    async_add_entities(
        [
            FortiGatePolicyOverrideSelect(entry, manager, policy_id)
            for policy_id in sorted(managed)
        ]
    )


class FortiGatePolicyOverrideSelect(SelectEntity):
    """Automatic, forced, or paused control with optional automatic expiry."""

    _attr_has_entity_name = True
    _attr_translation_key = "policy_override"
    _attr_icon = "mdi:shield-edit"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options: ClassVar[list[str]] = list(OVERRIDE_MODES)

    def __init__(
        self,
        entry: FortiGatePolicyConfigEntry,
        manager: PresencePolicyRuleManager,
        policy_id: str,
    ) -> None:
        self._entry = entry
        self._manager = manager
        self._policy_id = policy_id
        self._attr_unique_id = f"{entry.entry_id}_policy_{policy_id}_override"
        self._attr_name = f"Policy {policy_id} automation override"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._manager.async_add_state_listener(self.async_write_ha_state)
        )

    @property
    def current_option(self) -> str:
        return self._manager.override_for(self._policy_id).mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        override = self._manager.override_for(self._policy_id)
        return {
            "expires_at": (
                override.expires_at.isoformat() if override.expires_at else None
            ),
            "default_duration_minutes": self._manager.default_override_minutes,
        }

    async def async_select_option(self, option: str) -> None:
        await self._manager.async_set_override(self._policy_id, option)
