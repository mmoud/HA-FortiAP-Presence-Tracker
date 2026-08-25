"""Coordinators for verified policy control and Wi-Fi client presence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FortiGateAuthError,
    FortiGateConnectionError,
    FortiGateError,
    FortiGateNotFoundError,
    FortiGatePolicyApi,
    Policy,
)
from .const import (
    DOMAIN,
    EVENT_NEW_NETWORK_DEVICE,
    UPDATE_RETRIES,
    UPDATE_RETRY_DELAY,
)
from .network_store import NetworkDeviceStore
from .wifi import (
    FortiGateWifiClient,
    WifiPresence,
    advance_presence,
    client_matches_ssid_filter,
    utcnow,
)

_LOGGER = logging.getLogger(__name__)


class FortiGatePolicyCoordinator(DataUpdateCoordinator[Policy]):
    """Poll one FortiGate policy and serialize policy-status changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: FortiGatePolicyApi,
        poll_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_policy_{api.policy_id}",
            update_interval=timedelta(seconds=poll_interval),
            always_update=False,
        )
        self.api = api
        self.last_successful_check: datetime | None = None
        self._command_lock = asyncio.Lock()

    async def _async_update_data(self) -> Policy:
        """Fetch FortiGate state; errors make coordinator entities unavailable."""
        try:
            policy = await self.api.async_get_policy()
        except FortiGateAuthError as err:
            raise ConfigEntryAuthFailed("FortiGate API authentication failed") from err
        except FortiGateError as err:
            raise UpdateFailed("Unable to determine FortiGate policy state") from err

        self.last_successful_check = datetime.now(UTC)
        return policy

    async def async_set_policy_status(self, desired_status: str) -> None:
        """Preflight, write status only, then prove FortiGate changed it."""
        async with self._command_lock:
            # Mandatory preflight GET, including ID/name protection in the API client.
            preflight = await self._async_read_for_command()
            self.async_set_updated_data(preflight)

            # Avoid an unnecessary write, but preserve the actual read state.
            if preflight.status == desired_status:
                return

            await self.api.async_set_status(desired_status)

            # FortiGate accepted the PUT; that alone is not success. Re-read it.
            last_error: FortiGateError | None = None
            for attempt in range(UPDATE_RETRIES):
                try:
                    policy = await self._async_read_for_command()
                except FortiGateError as err:
                    last_error = err
                else:
                    self.async_set_updated_data(policy)
                    if policy.status == desired_status:
                        return
                if attempt < UPDATE_RETRIES - 1:
                    await asyncio.sleep(UPDATE_RETRY_DELAY.total_seconds())

            if last_error is not None:
                raise last_error
            raise FortiGateConnectionError(
                "FortiGate policy did not reach the requested status"
            )

    async def _async_read_for_command(self) -> Policy:
        """Read state for a command while retaining the specific failure type."""
        policy = await self.api.async_get_policy()
        self.last_successful_check = datetime.now(UTC)
        return policy


@dataclass(frozen=True, slots=True)
class FortiGateWifiData:
    """A single valid monitor poll and state for the selected MAC addresses."""

    clients: dict[str, FortiGateWifiClient]
    presence: dict[str, WifiPresence]
    skipped_clients: int
    fortios_version: str | None
    fortios_version_source: str | None
    system_status_endpoint_supported: bool | None
    updated_at: datetime


class FortiGateWifiCoordinator(DataUpdateCoordinator[FortiGateWifiData]):
    """Poll Wi-Fi clients once and fan the result out to all device trackers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: FortiGatePolicyApi,
        tracked_macs: set[str],
        poll_interval: int,
        away_grace_period: int,
        ssid_filters: Mapping[str, frozenset[str]] | None = None,
        network_store: NetworkDeviceStore | None = None,
        tracked_names: Mapping[str, str] | None = None,
        owners: Mapping[str, str] | None = None,
        detect_new_devices: bool = True,
        retention_days: int = 30,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_wifi_clients",
            update_interval=timedelta(seconds=poll_interval),
            # Client attributes may change while a device remains home.
            always_update=True,
        )
        self.api = api
        self._tracked_macs = tracked_macs
        self._ssid_filters = dict(ssid_filters or {})
        self._grace_period = timedelta(seconds=away_grace_period)
        self.network_store = network_store
        self._tracked_names = dict(tracked_names or {})
        self._owners = dict(owners or {})
        self._detect_new_devices = detect_new_devices
        self._retention_days = retention_days
        self._restored_presence: dict[str, WifiPresence] = {}
        self.last_successful_update: datetime | None = None
        self.fortios_version: str | None = None
        self.fortios_version_source: str | None = None
        self.system_status_endpoint_supported: bool | None = None
        self._system_status_checked = False

    async def _async_update_data(self) -> FortiGateWifiData:
        """Fetch one valid client list; failures preserve tracker presence state."""
        try:
            clients, skipped, response_version = await self.api.async_get_wifi_clients()
        except FortiGateError as err:
            # Crucially, failed polls do not run advance_presence(). Entities
            # become unavailable through CoordinatorEntity instead of away.
            raise UpdateFailed("Unable to determine FortiGate Wi-Fi clients") from err

        now = utcnow()
        if response_version:
            self.fortios_version = response_version
            self.fortios_version_source = "wifi_client_response"
        elif not self._system_status_checked:
            self._system_status_checked = True
            try:
                status_version = await self.api.async_get_fortios_version()
            except FortiGateNotFoundError:
                self.system_status_endpoint_supported = False
            except FortiGateError as err:
                # System status is diagnostic enrichment. It must never make a
                # successful Wi-Fi association poll unavailable.
                _LOGGER.debug(
                    "FortiGate system status enrichment unavailable: %s",
                    type(err).__name__,
                )
            else:
                self.system_status_endpoint_supported = True
                if status_version:
                    self.fortios_version = status_version
                    self.fortios_version_source = "system_status"
        previous = (
            self.data.presence if self.data is not None else self._restored_presence
        )
        if self.network_store is not None:
            for mac, record in self.network_store.records.items():
                if mac not in previous:
                    previous[mac] = WifiPresence(
                        record.connected,
                        None,
                        record.last_seen,
                        None,
                    )
            new_records = self.network_store.process_clients(
                clients,
                now,
                tracked_names=self._tracked_names,
                owners=self._owners,
                retention_days=self._retention_days,
            )
            if self._detect_new_devices:
                for record in new_records:
                    client = clients[record.mac]
                    self.hass.bus.async_fire(
                        EVENT_NEW_NETWORK_DEVICE,
                        {
                            "mac": client.mac,
                            "ip": client.ip,
                            "hostname": client.hostname,
                            "ssid": client.ssid,
                            "ap": client.ap_name,
                            "vendor": client.manufacturer,
                            "connection_type": client.connection_type,
                            "timestamp": now.isoformat(),
                            "fortigate_entry_id": self.config_entry.entry_id,
                        },
                    )
                    _LOGGER.debug(
                        "New network client discovered: %s", _masked_mac(record.mac)
                    )
            for record in new_records:
                self.network_store.mark_announced(record.mac)
        known_macs = set(self._tracked_macs) | set(clients)
        if self.network_store is not None:
            known_macs.update(self.network_store.records)
        presence = {
            mac: advance_presence(
                previous.get(mac),
                client_matches_ssid_filter(
                    clients.get(mac), self._ssid_filters.get(mac, frozenset())
                ),
                now,
                self._grace_period,
            )
            for mac in known_macs
        }
        for mac, state in presence.items():
            old = previous.get(mac)
            if state.is_connected is False and (
                old is None or old.is_connected is not False
            ):
                if self.network_store is not None:
                    self.network_store.mark_away(mac)
                _LOGGER.debug(
                    "Network client %s marked not_home after %s seconds",
                    _masked_mac(mac),
                    int(self._grace_period.total_seconds()),
                )
            elif state.missing_since and (old is None or old.missing_since is None):
                _LOGGER.debug(
                    "Network client %s entered away grace period", _masked_mac(mac)
                )
            elif state.client and old and old.missing_since:
                _LOGGER.debug(
                    "Network client %s returned during away grace period",
                    _masked_mac(mac),
                )
            if (
                state.client
                and old
                and old.client
                and state.client.ap_name != old.client.ap_name
            ):
                _LOGGER.debug("Network client %s moved access point", _masked_mac(mac))
        self.last_successful_update = now
        self._restored_presence = {}
        if skipped:
            _LOGGER.debug(
                "FortiGate Wi-Fi client update skipped %s malformed client records",
                skipped,
            )
        _LOGGER.debug(
            "FortiGate Wi-Fi client update succeeded: %s clients", len(clients)
        )
        return FortiGateWifiData(
            clients,
            presence,
            skipped,
            self.fortios_version,
            self.fortios_version_source,
            self.system_status_endpoint_supported,
            now,
        )

    def presence_for(self, mac: str) -> WifiPresence | None:
        """Return cached state for an explicitly selected MAC address."""
        return self.data.presence.get(mac) if self.data is not None else None

    def restore_presence(
        self, mac: str, is_connected: bool, last_seen: datetime | None
    ) -> None:
        """Seed a saved state so a restart cannot create an instant departure."""
        if self.data is None and mac in self._tracked_macs:
            self._restored_presence[mac] = WifiPresence(
                is_connected, None, last_seen if is_connected else None, None
            )


def _masked_mac(mac: str) -> str:
    """Retain enough of a MAC for debug correlation without logging it whole."""
    return f"{mac[:8]}:xx:xx:xx"
