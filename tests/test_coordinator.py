"""Tests for sleep-aware polling, caching, retry, and cleanup."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.mycloud import coordinator as coordinator_module
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
from custom_components.mycloud.sensor import MyCloudDiskTempSensor

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
        self.checks = 0

    async def async_check(self):
        self.checks += 1
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
async def test_all_active_runs_full_refresh_with_cache():
    states = {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}
    api = FakeAPI()
    coordinator, store = make_coordinator(api, FakeProbe(states))

    result = await coordinator._async_update_data()

    assert api.calls == [
        "enter", "system_info", "system_status", "device_info", "system_version"
    ]
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


ALL_ACTIVE = {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}
BLOCKED_STATES = [
    {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_STANDBY},
    {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_STANDBY},
    {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_UNKNOWN},
    {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_UNKNOWN},
    {"/dev/sda": POWER_ACTIVE},
    {},
    None,  # SSH error: the coordinator must treat it as unknown.
]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_cache", [False, True])
async def test_continuous_all_active_polls_api_only_once(with_cache):
    api = FakeAPI()
    probe = FakeProbe(ALL_ACTIVE)
    coordinator, store = make_coordinator(api, probe, with_cache=with_cache)

    first = await coordinator._async_update_data()
    calls_after_first = list(api.calls)
    saves_after_first = len(store.saved)
    assert first["data_stale"] is False
    for _ in range(4):
        result = await coordinator._async_update_data()
        assert api.calls == calls_after_first
        assert len(store.saved) == saves_after_first
        assert result["last_full_update"] == first["last_full_update"]
        assert result["data_stale"] is True
        assert result["power_states"] == ALL_ACTIVE
        for key in SAMPLE_DATA:
            assert result[key] == first[key]

    assert probe.checks == 5
    assert api.calls == [
        "enter", "system_info", "system_status", "device_info", "system_version"
    ]
    # The wake-phase allowance is intentionally not part of persistent storage.
    assert set(store.saved[-1]) == {"data", "last_full_update", "ssh_host_key"}


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_states", BLOCKED_STATES)
async def test_blocked_probe_rearms_exactly_one_full_wake_poll(blocked_states):
    api = FakeAPI()
    probe = FakeProbe(ALL_ACTIVE)
    coordinator, store = make_coordinator(api, probe)
    first = await coordinator._async_update_data()
    calls_after_first = list(api.calls)
    saves_after_first = len(store.saved)

    probe.states = blocked_states
    probe.error = blocked_states is None
    for _ in range(2):
        blocked = await coordinator._async_update_data()
        assert api.calls == calls_after_first
        assert len(store.saved) == saves_after_first
        assert blocked["data_stale"] is True
        assert blocked["last_full_update"] == first["last_full_update"]
        for key in SAMPLE_DATA:
            assert blocked[key] == first[key]

    probe.states = ALL_ACTIVE
    probe.error = False
    awake = await coordinator._async_update_data()
    assert awake["data_stale"] is False
    assert api.calls.count("enter") == 1
    assert api.calls.count("login") == 0
    for endpoint in SAMPLE_DATA:
        assert api.calls.count(endpoint) == 2
    assert len(store.saved) == saves_after_first + 1

    for _ in range(3):
        cached = await coordinator._async_update_data()
        assert cached["last_full_update"] == awake["last_full_update"]
        for endpoint in SAMPLE_DATA:
            assert api.calls.count(endpoint) == 2
            assert cached[endpoint] == awake[endpoint]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_states", BLOCKED_STATES)
async def test_no_cache_waits_for_all_active_before_first_poll(blocked_states):
    api = FakeAPI()
    probe = FakeProbe(blocked_states, error=blocked_states is None)
    coordinator, _ = make_coordinator(api, probe, with_cache=False)

    for _ in range(2):
        with pytest.raises(UpdateFailed, match="Wake the NAS disks once"):
            await coordinator._async_update_data()
        assert api.calls == []

    probe.states = ALL_ACTIVE
    probe.error = False
    result = await coordinator._async_update_data()
    for endpoint, value in SAMPLE_DATA.items():
        assert result[endpoint] == value
        assert api.calls.count(endpoint) == 1
    await coordinator._async_update_data()
    assert api.calls.count("system_info") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_cache", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError("unavailable"), ForbiddenError(403)])
async def test_failed_api_attempt_is_not_repeated_until_next_wake_phase(with_cache, failure):
    api = FakeAPI([failure])
    probe = FakeProbe(ALL_ACTIVE)
    coordinator, _ = make_coordinator(api, probe, with_cache=with_cache)

    with pytest.raises(UpdateFailed, match="Error fetching data"):
        await coordinator._async_update_data()
    for _ in range(3):
        if with_cache:
            result = await coordinator._async_update_data()
            assert result["data_stale"] is True
            for key, value in SAMPLE_DATA.items():
                assert result[key] == value
        else:
            with pytest.raises(UpdateFailed, match="poll for this wake phase failed"):
                await coordinator._async_update_data()
        assert api.calls.count("system_info") == 1
        assert api.calls.count("login") == 0

    probe.states = BLOCKED_STATES[0]
    if with_cache:
        await coordinator._async_update_data()
    else:
        with pytest.raises(UpdateFailed, match="Wake the NAS disks once"):
            await coordinator._async_update_data()
    probe.states = ALL_ACTIVE
    await coordinator._async_update_data()
    assert api.calls.count("system_info") == 2
    assert api.calls.count("login") == int(isinstance(failure, ForbiddenError))


@pytest.mark.asyncio
async def test_restart_allows_one_new_poll_with_persisted_snapshot():
    api = FakeAPI()
    coordinator, store = make_coordinator(api, FakeProbe(ALL_ACTIVE))
    await coordinator._async_update_data()
    restarted = MyCloudDataUpdateCoordinator(
        hass=object(),
        logger=logging.getLogger("test"),
        api_client=FakeAPI(),
        store=FakeStore(),
        update_interval=timedelta(seconds=600),
        config_entry=object(),
        cached_envelope=store.saved[-1],
        power_client=FakeProbe(ALL_ACTIVE),
    )

    fresh = await restarted._async_update_data()
    cached = await restarted._async_update_data()
    assert fresh["data_stale"] is False
    assert cached["data_stale"] is True
    assert restarted._api_client.calls.count("system_info") == 1


@pytest.mark.asyncio
async def test_legacy_mode_still_polls_every_interval():
    api = FakeAPI()
    coordinator, _ = make_coordinator(api)
    for _ in range(3):
        result = await coordinator._async_update_data()
        assert result["data_stale"] is False
    assert api.calls == ["enter"] + ["system_info", "system_status"] * 3


@pytest.mark.parametrize("sleep_aware,probe_seconds,expected_seconds", [
    (True, 60, 60), (True, 30, 30), (False, 60, 600),
])
def test_coordinator_scheduler_uses_probe_interval_only_in_sleep_aware_mode(
    sleep_aware, probe_seconds, expected_seconds
):
    coordinator = MyCloudDataUpdateCoordinator(
        hass=object(), logger=logging.getLogger("test"), api_client=FakeAPI(),
        store=FakeStore(), update_interval=timedelta(seconds=600),
        power_client=FakeProbe(ALL_ACTIVE) if sleep_aware else None,
        power_probe_interval=timedelta(seconds=probe_seconds),
    )
    assert coordinator.update_interval == timedelta(seconds=expected_seconds)


@pytest.mark.asyncio
async def test_fast_probe_observes_short_phases_and_refreshes_values_and_timestamp(monkeypatch):
    """Drive the real coordinator on its configured cadence, with a virtual clock.

    At the old 600-second cadence, none of the states between t=60 and t=480
    would be observed. No sleeps, mocked coordinator, or timing heuristics.
    """
    clock = {"seconds": 0}
    base_time = datetime(2026, 9, 4, tzinfo=timezone.utc)
    monkeypatch.setattr(
        coordinator_module, "_utc_now",
        lambda: (base_time + timedelta(seconds=clock["seconds"])).isoformat(),
    )

    class TimelineProbe(FakeProbe):
        async def async_check(self):
            self.checks += 1
            now = clock["seconds"]
            if now == 360:
                raise PowerProbeError("simulated SSH error")
            if now in (60, 240):
                state = POWER_STANDBY if now == 60 else POWER_UNKNOWN
                return dict.fromkeys(self.drive_devices, state)
            return dict(ALL_ACTIVE)

    class ChangingAPI(FakeAPI):
        async def system_info(self):
            data = await super().system_info()
            data["disks"][0]["temp"] = 31 + clock["seconds"] // 60
            data["size"]["used"] = 25 + clock["seconds"] // 60
            return data

    api = ChangingAPI()
    probe = TimelineProbe()
    coordinator, store = make_coordinator(api, probe)
    assert coordinator.update_interval == timedelta(seconds=60)
    sensor = MyCloudDiskTempSensor(coordinator, {}, "test", "Disk", {"name": "1"})
    records = {}
    interval = int(coordinator.update_interval.total_seconds())
    for seconds in range(0, 481, interval):
        clock["seconds"] = seconds
        coordinator.data = await coordinator._async_update_data()
        records[seconds] = (
            sensor.native_value, sensor.extra_state_attributes["last_successful_update"],
            api.calls.count("system_info"),
        )

    assert probe.checks == 9
    assert records[60] == records[0]  # Standby: cache and timestamp unchanged.
    assert records[120][0] == 33  # Short wake phase between the old 600s ticks.
    assert records[120][1] != records[0][1]
    assert records[120][2] == 2
    assert records[180] == records[120]  # Continuing active cannot poll again.
    assert records[240] == records[120]  # Unknown cannot call the API.
    assert records[300][2] == 3
    assert records[360] == records[300]  # SSH error arms, but does not poll.
    assert records[420][0] == 38
    assert records[420][2] == 4
    assert records[480] == records[420]
    assert coordinator.data["system_info"]["size"]["used"] == 32
    assert len(store.saved) == 4
    for endpoint in SAMPLE_DATA:
        assert api.calls.count(endpoint) == 4
