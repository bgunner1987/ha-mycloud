"""Tests for sleep-aware polling, caching, retry, and cleanup."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import timedelta

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.mycloud.coordinator import (
    FIRST_SETUP_MESSAGE,
    MyCloudDataUpdateCoordinator,
)
from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    PowerProbeError,
)

SAMPLE_DATA = {
    "system_info": {
        "disks": [
            {"name": "1", "temp": 31, "sleep": False},
            {"name": "2", "temp": 32, "sleep": False},
        ],
        "volumes": [{"id": "vol1", "size": 100}],
        "size": {"total": 100, "used": 25, "unused": 75},
    },
    "system_status": {
        "cpu": 7,
        "memory": {"total": 100, "unused": 60},
    },
    "device_info": {
        "serial_number": "SERIAL",
        "name": "My Cloud",
        "description": "EX2 Ultra",
    },
    "system_version": {"firmware": "5.33.102"},
}


class ForbiddenError(Exception):
    pass


class FakeStore:
    def __init__(self):
        self.saved = []

    async def async_save(self, data):
        self.saved.append(deepcopy(data))


class FakeAPI:
    def __init__(self, fail_system_info=None):
        self.calls = []
        self.fail_system_info = list(fail_system_info or [])
        self.session = None

    async def __aenter__(self):
        self.calls.append("enter")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.calls.append("exit")

    async def login(self):
        self.calls.append("login")

    async def system_info(self):
        self.calls.append("system_info")
        if self.fail_system_info:
            failure = self.fail_system_info.pop(0)
            if failure is not None:
                raise failure
        return deepcopy(SAMPLE_DATA["system_info"])

    async def system_status(self):
        self.calls.append("system_status")
        return deepcopy(SAMPLE_DATA["system_status"])

    async def device_info(self):
        self.calls.append("device_info")
        return deepcopy(SAMPLE_DATA["device_info"])

    async def system_version(self):
        self.calls.append("system_version")
        return deepcopy(SAMPLE_DATA["system_version"])


class FakeProbe:
    drive_devices = ("/dev/sda", "/dev/sdc")
    fingerprint = "test-fingerprint-placeholder"

    def __init__(self, states=None, error=False):
        self.states = states
        self.error = error
        self.closed = 0

    async def async_check(self):
        if self.error:
            raise PowerProbeError("timeout")
        return dict(self.states)

    async def async_close(self):
        self.closed += 1


def make_coordinator(api, probe=None, with_cache=True):
    envelope = (
        {
            "data": deepcopy(SAMPLE_DATA),
            "last_full_update": "2026-09-03T08:00:00+00:00",
            "ssh_host_key": "test-fingerprint-placeholder",
        }
        if with_cache
        else None
    )
    store = FakeStore()
    coordinator = MyCloudDataUpdateCoordinator(
        hass=object(),
        logger=logging.getLogger("test"),
        api_client=api,
        store=store,
        update_interval=timedelta(seconds=600),
        config_entry=object(),
        cached_envelope=envelope,
        power_client=probe,
    )
    return coordinator, store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "states",
    [
        {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_STANDBY},
        {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_STANDBY},
    ],
)
async def test_standby_or_mixed_state_skips_api_and_keeps_cache(states):
    api = FakeAPI()
    coordinator, store = make_coordinator(api, FakeProbe(states))

    result = await coordinator._async_update_data()

    assert api.calls == []
    assert result["system_info"] == SAMPLE_DATA["system_info"]
    assert result["system_status"] == SAMPLE_DATA["system_status"]
    assert result["data_stale"] is True
    assert result["power_states"] == states
    assert store.saved == []


@pytest.mark.asyncio
async def test_all_active_runs_dynamic_refresh_only_with_cache():
    states = {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}
    api = FakeAPI()
    coordinator, store = make_coordinator(api, FakeProbe(states))

    result = await coordinator._async_update_data()

    assert api.calls == ["enter", "system_info", "system_status"]
    assert result["data_stale"] is False
    assert result["device_info"] == SAMPLE_DATA["device_info"]
    assert len(store.saved) == 1


@pytest.mark.asyncio
async def test_ssh_error_fails_closed_and_keeps_cache():
    api = FakeAPI()
    coordinator, _ = make_coordinator(api, FakeProbe(error=True))

    result = await coordinator._async_update_data()

    assert api.calls == []
    assert result["system_info"] == SAMPLE_DATA["system_info"]
    assert result["power_states"] == {
        "/dev/sda": POWER_UNKNOWN,
        "/dev/sdc": POWER_UNKNOWN,
    }
    assert result["data_stale"] is True


@pytest.mark.asyncio
async def test_403_logs_in_once_without_second_session_and_retries_once():
    api = FakeAPI([ForbiddenError(403), None])
    coordinator, _ = make_coordinator(api, probe=None)

    result = await coordinator._async_update_data()

    assert result["data_stale"] is False
    assert api.calls.count("enter") == 1
    assert api.calls.count("login") == 1
    assert api.calls.count("system_info") == 2


@pytest.mark.asyncio
async def test_second_403_is_not_retried_again():
    api = FakeAPI([ForbiddenError(403), ForbiddenError(403)])
    coordinator, _ = make_coordinator(api, probe=None)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert api.calls.count("enter") == 1
    assert api.calls.count("login") == 1
    assert api.calls.count("system_info") == 2


@pytest.mark.asyncio
async def test_restart_with_cache_and_sleeping_disks_preserves_values():
    api = FakeAPI()
    probe = FakeProbe(
        {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_STANDBY}
    )
    coordinator, _ = make_coordinator(api, probe, with_cache=True)

    result = await coordinator._async_update_data()

    assert result["system_info"]["disks"][0]["temp"] == 31
    assert result["system_info"]["size"]["used"] == 25
    assert result["last_full_update"] == "2026-09-03T08:00:00+00:00"
    assert api.calls == []


@pytest.mark.asyncio
async def test_first_start_without_cache_does_not_wake_disks():
    api = FakeAPI()
    probe = FakeProbe(
        {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_STANDBY}
    )
    coordinator, _ = make_coordinator(api, probe, with_cache=False)

    with pytest.raises(UpdateFailed, match="Wake the NAS disks once") as err:
        await coordinator._async_update_data()

    assert FIRST_SETUP_MESSAGE in str(err.value)
    assert api.calls == []


@pytest.mark.asyncio
async def test_first_active_refresh_loads_static_endpoints():
    api = FakeAPI()
    probe = FakeProbe(
        {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}
    )
    coordinator, _ = make_coordinator(api, probe, with_cache=False)

    await coordinator._async_update_data()

    assert api.calls == [
        "enter",
        "system_info",
        "system_status",
        "device_info",
        "system_version",
    ]


@pytest.mark.asyncio
async def test_shutdown_closes_http_and_ssh_once():
    api = FakeAPI()
    probe = FakeProbe(
        {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}
    )
    coordinator, _ = make_coordinator(api, probe)
    await coordinator._async_update_data()

    await coordinator.async_shutdown()
    await coordinator.async_shutdown()

    assert api.calls.count("exit") == 1
    assert probe.closed == 1
