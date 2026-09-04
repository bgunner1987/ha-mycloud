"""Update coordination and persistent caching for My Cloud."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_POWER_PROBE_INTERVAL
from .power_probe import (
    POWER_ACTIVE,
    POWER_UNKNOWN,
    PowerProbeError,
    SSHPowerStateClient,
)

FIRST_SETUP_MESSAGE = (
    "Sleep-aware polling found sleeping or unknown disks and no cached data. "
    "Wake the NAS disks once to complete the first setup; no WD API request was sent."
)

_API_KEYS = ("system_info", "system_status", "device_info", "system_version")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_http_403(err: Exception) -> bool:
    status = getattr(err, "status", None)
    if status == 403:
        return True
    return any(arg == 403 or str(arg) == "403" for arg in getattr(err, "args", ()))


class MyCloudDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fail closed before WD API access and retain the last full snapshot."""

    def __init__(
        self,
        hass,
        logger: logging.Logger,
        api_client,
        store,
        update_interval,
        config_entry=None,
        cached_envelope: dict[str, Any] | None = None,
        power_client: SSHPowerStateClient | None = None,
        power_probe_interval: timedelta = timedelta(seconds=DEFAULT_POWER_PROBE_INTERVAL),
    ) -> None:
        # The scheduler checks power independently of the legacy API interval.
        # The wake-phase gate below remains the only authority for API access.
        super().__init__(
            hass,
            logger,
            config_entry=config_entry,
            name="mycloud_coordinator",
            update_interval=power_probe_interval if power_client is not None else update_interval,
            update_method=self._async_update_data,
        )
        envelope = cached_envelope or {}
        cached_data = envelope.get("data")
        self._cached_data = (
            deepcopy(cached_data)
            if isinstance(cached_data, dict)
            and all(key in cached_data for key in _API_KEYS)
            else None
        )
        self._last_full_update = envelope.get("last_full_update")
        self._persisted_fingerprint = envelope.get("ssh_host_key")
        self._api_client = api_client
        self._integration_logger = logger
        self._api_started = False
        self._api_needs_login = False
        self._store = store
        self.power_client = power_client
        self.drive_devices = power_client.drive_devices if power_client else ()
        self._last_probe_summary: tuple[str, ...] | None = None
        # Runtime-only: allow one attempt after startup or a non-active probe.
        # Consume before API access, so API errors cannot keep the disks awake.
        self._wake_poll_pending = True
        self._closed = False

    async def _async_save_cache(self) -> None:
        await self._store.async_save(
            {
                "data": deepcopy(self._cached_data),
                "last_full_update": self._last_full_update,
                "ssh_host_key": self._persisted_fingerprint,
            }
        )

    async def _async_persist_new_fingerprint(self) -> None:
        if self.power_client is None:
            return
        fingerprint = self.power_client.fingerprint
        if fingerprint and fingerprint != self._persisted_fingerprint:
            self._persisted_fingerprint = fingerprint
            await self._async_save_cache()

    async def _async_start_api(self) -> None:
        if self._api_started:
            return
        try:
            await self._api_client.__aenter__()
        except Exception:
            session = getattr(self._api_client, "session", None)
            if session is not None and not getattr(session, "closed", False):
                await session.close()
            raise
        self._api_started = True

    async def _async_fetch_once(self) -> dict[str, Any]:
        # A wake-phase poll refreshes the entire snapshot. Legacy polling keeps
        # its existing dynamic-only refresh when a snapshot is already cached.
        include_static = self.power_client is not None or self._cached_data is None
        result = {
            "system_info": await self._api_client.system_info(),
            "system_status": await self._api_client.system_status(),
        }
        if include_static:
            result["device_info"] = await self._api_client.device_info()
            result["system_version"] = await self._api_client.system_version()
        else:
            result["device_info"] = deepcopy(self._cached_data["device_info"])
            result["system_version"] = deepcopy(self._cached_data["system_version"])
        return result

    async def _async_fetch_with_reauth(self) -> dict[str, Any]:
        try:
            if self._api_needs_login:
                await self._api_client.login()
                self._api_needs_login = False
            return await self._async_fetch_once()
        except Exception as err:
            if not _is_http_403(err):
                raise
            if self.power_client is not None:
                # Defer reauthentication until a new observed wake phase.
                # Even a 403 must not cause a second snapshot attempt now.
                self._api_needs_login = True
                raise

        await self._api_client.login()
        return await self._async_fetch_once()

    def _cached_result(
        self,
        power_states: dict[str, str],
        last_power_check: str | None,
        data_stale: bool,
    ) -> dict[str, Any]:
        assert self._cached_data is not None
        result = deepcopy(self._cached_data)
        result.update(
            {
                "power_states": power_states,
                "data_stale": data_stale,
                "last_full_update": self._last_full_update,
                "last_power_check": last_power_check,
                "sleep_aware_enabled": self.power_client is not None,
            }
        )
        return result

    def _log_probe_transition(self, states: dict[str, str]) -> None:
        summary = tuple(states.get(device, POWER_UNKNOWN) for device in self.drive_devices)
        if summary == self._last_probe_summary:
            return
        self._last_probe_summary = summary
        if all(state == POWER_ACTIVE for state in summary):
            self._integration_logger.debug(
                "All configured drives are active; checking wake-phase poll allowance"
            )
        else:
            self._integration_logger.info(
                "WD API polling skipped by sleep-aware fail-closed policy; drive states: %s",
                ", ".join(summary),
            )

    async def _async_update_data(self) -> dict[str, Any]:
        power_states: dict[str, str] = {}
        last_power_check: str | None = None

        if self.power_client is not None:
            last_power_check = _utc_now()
            try:
                power_states = await self.power_client.async_check()
            except PowerProbeError:
                power_states = {
                    device: POWER_UNKNOWN for device in self.drive_devices
                }
            all_active = bool(self.drive_devices) and all(
                power_states.get(device) == POWER_ACTIVE
                for device in self.drive_devices
            )
            if not all_active:
                self._wake_poll_pending = True

            await self._async_persist_new_fingerprint()
            self._log_probe_transition(power_states)

            if not all_active:
                if self._cached_data is None:
                    raise UpdateFailed(FIRST_SETUP_MESSAGE)
                return self._cached_result(power_states, last_power_check, True)

            if not self._wake_poll_pending:
                if self._cached_data is None:
                    raise UpdateFailed(
                        "The WD API poll for this wake phase failed and no cached "
                        "data is available. Waiting for the next observed wake "
                        "phase or an integration restart before trying again."
                    )
                return self._cached_result(power_states, last_power_check, True)

            self._wake_poll_pending = False

        try:
            await self._async_start_api()
            fresh_data = await self._async_fetch_with_reauth()
        except Exception as err:
            raise UpdateFailed(f"Error fetching data from the WD API: {err}") from err

        self._cached_data = fresh_data
        self._last_full_update = _utc_now()
        await self._async_save_cache()
        return self._cached_result(power_states, last_power_check, False)

    async def async_shutdown(self) -> None:
        """Close each owned resource at most once."""
        if self._closed:
            return
        self._closed = True
        await super().async_shutdown()
        try:
            if self.power_client is not None:
                await self.power_client.async_close()
        finally:
            if self._api_started:
                self._api_started = False
                await self._api_client.__aexit__(None, None, None)
