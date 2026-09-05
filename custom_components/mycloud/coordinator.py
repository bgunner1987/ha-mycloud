"""Update coordination and persistent caching for My Cloud."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_POWER_PROBE_INTERVAL
from .power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    PowerProbeError,
    SSHPowerStateClient,
)
from .probe_diagnostics import ProbeFailure, describe_probe_error, log_probe_failure

FIRST_SETUP_MESSAGE = (
    "Sleep-aware polling found sleeping or unknown disks and no cached data. "
    "Wake the NAS disks once to complete the first setup; no WD API request was sent."
)

_API_KEYS = ("system_info", "system_status", "device_info", "system_version")
_KNOWN_POWER_STATES = (POWER_ACTIVE, POWER_STANDBY)
# Delays are relative: follow-ups occur about 2 and 5 seconds after the first.
_DEFAULT_PROBE_RETRY_DELAYS = (2.0, 3.0)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_http_403(err: Exception) -> bool:
    status = getattr(err, "status", None)
    if status == 403:
        return True
    return any(arg == 403 or str(arg) == "403" for arg in getattr(err, "args", ()))


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


class MyCloudDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Run lightweight power probes and gate complete WD API snapshots."""

    def __init__(
        self,
        hass,
        logger: logging.Logger,
        api_client,
        store,
        update_interval: timedelta,
        config_entry=None,
        cached_envelope: dict[str, Any] | None = None,
        power_client: SSHPowerStateClient | None = None,
        power_probe_interval: timedelta = timedelta(seconds=DEFAULT_POWER_PROBE_INTERVAL),
        *,
        probe_retry_delays: tuple[float, ...] = _DEFAULT_PROBE_RETRY_DELAYS,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # Sleep-aware timing is owned by one lightweight background loop. Passing
        # None prevents DataUpdateCoordinator from independently scheduling a
        # second, overlapping probe/update cycle.
        super().__init__(
            hass,
            logger,
            config_entry=config_entry,
            name="mycloud_coordinator",
            update_interval=None if power_client is not None else update_interval,
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
        self._api_update_interval = update_interval
        self._power_probe_interval = power_probe_interval
        self._probe_retry_delays = probe_retry_delays
        self._sleep = sleep_func
        self.power_client = power_client
        self.drive_devices = power_client.drive_devices if power_client else ()
        self._last_probe_summary: tuple[str, ...] | None = None
        self.last_power_check: str | None = None
        self.power_states: dict[str, str] = {}
        self.power_probe_status = "pending" if power_client else "disabled"
        self.power_probe_error_type: str | None = None
        self.last_power_probe_error: str | None = None
        self._last_logged_probe_failure: ProbeFailure | None = None
        self._wake_poll_pending = True
        self._last_api_attempt_at: str | None = None
        self._last_api_attempt_failed = False
        self._cycle_lock = asyncio.Lock()
        self._probe_task: asyncio.Task | None = None
        self._closed = False

    @property
    def power_probe_diagnostics(self) -> dict[str, Any]:
        """Expose safe runtime diagnostics, including when no snapshot exists."""
        return {
            "last_power_check": self.last_power_check,
            "power_states": dict(self.power_states),
            "power_probe_status": self.power_probe_status,
            "power_probe_error_type": self.power_probe_error_type,
            "last_power_probe_error": self.last_power_probe_error,
        }

    @property
    def power_probe_task(self) -> asyncio.Task | None:
        """Expose the owned task for lifecycle tests."""
        return self._probe_task

    def async_start_power_probe_loop(self) -> None:
        """Start the single owned lightweight probe loop."""
        if self.power_client is None or self._closed or self._probe_task is not None:
            return
        coroutine = self._async_power_probe_loop()
        create_task = getattr(self.hass, "async_create_task", None)
        if create_task is not None:
            self._probe_task = create_task(coroutine, name="mycloud power probe")
        else:
            self._probe_task = asyncio.create_task(coroutine, name="mycloud power probe")

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
        except BaseException:
            session = getattr(self._api_client, "session", None)
            if session is not None and not getattr(session, "closed", False):
                await session.close()
            raise
        self._api_started = True

    async def _async_fetch_once(self) -> dict[str, Any]:
        # Wake-phase polls always refresh the complete snapshot. Legacy mode
        # retains its prior dynamic-only refresh once static data is cached.
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
                # A failed wake-phase attempt is never repeated immediately.
                self._api_needs_login = True
                raise

        await self._api_client.login()
        return await self._async_fetch_once()

    def _cached_result(self, data_stale: bool) -> dict[str, Any]:
        assert self._cached_data is not None
        result = deepcopy(self._cached_data)
        result.update(
            {
                "data_stale": data_stale,
                "last_full_update": self._last_full_update,
                "sleep_aware_enabled": self.power_client is not None,
            }
        )
        result.update(self.power_probe_diagnostics)
        return result

    def _diagnostic_signature(self) -> tuple[Any, ...]:
        return (
            tuple((device, self.power_states.get(device)) for device in self.drive_devices),
            self.power_probe_status,
            self.power_probe_error_type,
            self.last_power_probe_error,
        )

    def _log_probe_transition(self) -> None:
        summary = tuple(
            self.power_states.get(device, POWER_UNKNOWN) for device in self.drive_devices
        )
        if summary == self._last_probe_summary:
            return
        self._last_probe_summary = summary
        if all(state == POWER_ACTIVE for state in summary):
            self._integration_logger.debug(
                "All configured drives are active; checking wake-phase and interval gates"
            )
        else:
            self._integration_logger.info(
                "WD API polling skipped by sleep-aware fail-closed policy; drive states: %s",
                ", ".join(summary),
            )

    def _record_probe_failure(self, err: PowerProbeError) -> None:
        failure = describe_probe_error(err)
        self.power_states = dict.fromkeys(self.drive_devices, POWER_UNKNOWN)
        self.power_probe_status = "error"
        self.power_probe_error_type = failure.error_type
        self.last_power_probe_error = failure.summary
        if failure != self._last_logged_probe_failure:
            log_probe_failure(self._integration_logger, failure)
            self._last_logged_probe_failure = failure

    def _record_probe_success(self, states: dict[str, str]) -> None:
        # Only configured paths and known states leave the power client.
        self.power_states = {
            device: (
                states.get(device)
                if states.get(device) in _KNOWN_POWER_STATES
                else POWER_UNKNOWN
            )
            for device in self.drive_devices
        }
        self.power_probe_status = (
            "unknown"
            if any(state == POWER_UNKNOWN for state in self.power_states.values())
            else "ok"
        )
        self.power_probe_error_type = None
        self._last_logged_probe_failure = None

    async def _async_probe_with_followups(self) -> None:
        assert self.power_client is not None
        try:
            states = await self.power_client.async_check()
            # Retry only the transient all-non-standby unknown case.
            for delay in self._probe_retry_delays:
                normalized = {
                    device: (
                        states.get(device)
                        if states.get(device) in _KNOWN_POWER_STATES
                        else POWER_UNKNOWN
                    )
                    for device in self.drive_devices
                }
                if POWER_STANDBY in normalized.values():
                    break
                if POWER_UNKNOWN not in normalized.values():
                    break
                await self._sleep(delay)
                states = await self.power_client.async_check()
        except PowerProbeError as err:
            self._record_probe_failure(err)
        else:
            self._record_probe_success(states)
        self.last_power_check = _utc_now()
        await self._async_persist_new_fingerprint()
        self._log_probe_transition()

    def _interval_due(self, now: str) -> bool:
        now_dt = _parse_timestamp(now)
        if now_dt is None:
            return True
        references = [
            parsed
            for parsed in (
                _parse_timestamp(self._last_full_update),
                _parse_timestamp(self._last_api_attempt_at),
            )
            if parsed is not None
        ]
        if not references:
            return True
        return now_dt - max(references) >= self._api_update_interval

    async def _async_full_poll(self, attempt_time: str) -> dict[str, Any]:
        self._last_api_attempt_at = attempt_time
        try:
            await self._async_start_api()
            fresh_data = await self._async_fetch_with_reauth()
        except Exception as err:
            self._last_api_attempt_failed = True
            raise UpdateFailed(f"Error fetching data from the WD API: {err}") from err

        self._last_api_attempt_failed = False
        self._cached_data = fresh_data
        self._last_full_update = _utc_now()
        await self._async_save_cache()
        return self._cached_result(False)

    async def _async_sleep_aware_cycle(self) -> tuple[dict[str, Any], bool]:
        before = self._diagnostic_signature()
        await self._async_probe_with_followups()
        changed = before != self._diagnostic_signature()
        all_active = bool(self.drive_devices) and all(
            self.power_states.get(device) == POWER_ACTIVE for device in self.drive_devices
        )
        any_standby = POWER_STANDBY in self.power_states.values()
        if any_standby:
            self._wake_poll_pending = True

        if not all_active:
            if self._cached_data is None:
                raise UpdateFailed(FIRST_SETUP_MESSAGE)
            return self._cached_result(True), changed

        now = self.last_power_check or _utc_now()
        interval_due = self._interval_due(now)
        immediate_wake_poll = (
            self._wake_poll_pending
            and (self._last_api_attempt_at is None or not self._last_api_attempt_failed)
        )
        if immediate_wake_poll or interval_due:
            # Consume before access. Failures cannot create a rapid retry loop.
            self._wake_poll_pending = False
            return await self._async_full_poll(now), True

        if self._cached_data is None:
            raise UpdateFailed(
                "The WD API poll failed and no cached data is available. Waiting for "
                "the configured API interval or an integration restart before retrying."
            )
        # An active, recent snapshot is not stale merely because this lightweight
        # probe intentionally skipped a full API poll.
        return self._cached_result(self._last_api_attempt_failed), changed

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._cycle_lock:
            if self.power_client is not None:
                data, _ = await self._async_sleep_aware_cycle()
                return data
            return await self._async_full_poll(_utc_now())

    async def _async_power_probe_loop(self) -> None:
        while True:
            await self._sleep(self._power_probe_interval.total_seconds())
            try:
                async with self._cycle_lock:
                    data, relevant = await self._async_sleep_aware_cycle()
            except asyncio.CancelledError:
                raise
            except UpdateFailed as err:
                if self._cached_data is None:
                    self.async_set_update_error(err)
                else:
                    # A background API failure keeps the last snapshot available
                    # and marks it stale, while the attempt timestamp throttles
                    # any retry until the configured interval.
                    self.async_set_updated_data(self._cached_result(True))
            else:
                if relevant:
                    self.async_set_updated_data(data)

    async def async_shutdown(self) -> None:
        """Cancel the owned timer and close HTTP/SSH resources exactly once."""
        if self._closed:
            return
        self._closed = True
        task, self._probe_task = self._probe_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await super().async_shutdown()
        try:
            if self.power_client is not None:
                await self.power_client.async_close()
        finally:
            if self._api_started:
                self._api_started = False
                await self._api_client.__aexit__(None, None, None)
