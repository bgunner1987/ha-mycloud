"""Regression tests for bounded probes, wake phases and quiet HA updates."""

import asyncio
import inspect
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from test_coordinator import SAMPLE_DATA, FakeAPI, FakeStore

from custom_components.mycloud import coordinator as coordinator_module
from custom_components.mycloud.coordinator import MyCloudDataUpdateCoordinator
from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
)

ACTIVE = {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}
STANDBY = {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_ACTIVE}
UNKNOWN = {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_UNKNOWN}
MIXED_UNKNOWN = {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_UNKNOWN}


class SequenceProbe:
    drive_devices = ("/dev/sda", "/dev/sdc")
    fingerprint = "test-fingerprint-placeholder"

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0
        self.closed = 0

    async def async_check(self):
        self.calls += 1
        if len(self.states) > 1:
            return dict(self.states.pop(0))
        return dict(self.states[0])

    async def async_close(self):
        self.closed += 1


class ControlledSleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


class TaskTrackingHass:
    """Model Home Assistant's separate normal and background task buckets."""

    def __init__(self):
        self.normal_tasks = set()
        self.background_tasks = set()
        self.normal_create_calls = 0

    def async_create_task(self, coroutine, name=None):
        self.normal_create_calls += 1
        task = asyncio.create_task(coroutine, name=name)
        self.normal_tasks.add(task)
        task.add_done_callback(self.normal_tasks.discard)
        return task

    def async_create_background_task(self, coroutine, name, eager_start=True):
        task = asyncio.create_task(coroutine, name=name)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def async_block_till_done(self):
        if self.normal_tasks:
            await asyncio.gather(*tuple(self.normal_tasks))


class LifecycleConfigEntry:
    """Mirror ConfigEntry ownership and automatic cancellation on unload."""

    def __init__(self):
        self.background_tasks = set()
        self.create_calls = []

    def async_create_background_task(
        self, hass, coroutine, name, eager_start=True
    ):
        self.create_calls.append((hass, name, eager_start))
        task = hass.async_create_background_task(
            coroutine, name, eager_start=eager_start
        )
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def async_cancel_background_tasks(self):
        tasks = tuple(self.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def make_scheduler(
    states, *, with_cache=True, update_seconds=600, sleep=None,
    hass=None, config_entry=None
):
    envelope = (
        {
            "data": deepcopy(SAMPLE_DATA),
            "last_full_update": "2026-09-05T00:00:00+00:00",
            "ssh_host_key": "test-fingerprint-placeholder",
        }
        if with_cache else None
    )
    probe = SequenceProbe(states)
    api = FakeAPI()
    store = FakeStore()
    sleeper = sleep or ControlledSleep()
    hass = hass or TaskTrackingHass()
    config_entry = config_entry or LifecycleConfigEntry()
    coordinator = MyCloudDataUpdateCoordinator(
        hass=hass,
        logger=logging.getLogger("test"),
        api_client=api,
        store=store,
        update_interval=timedelta(seconds=update_seconds),
        config_entry=config_entry,
        cached_envelope=envelope,
        power_client=probe,
        power_probe_interval=timedelta(seconds=10),
        probe_retry_delays=(2, 3),
        sleep_func=sleeper,
    )
    return coordinator, probe, api, store, sleeper


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [UNKNOWN, MIXED_UNKNOWN])
async def test_unknown_becomes_all_active_during_bounded_followup(first):
    coordinator, probe, api, _, sleeper = make_scheduler(
        [first, ACTIVE], with_cache=False
    )
    result = await coordinator._async_update_data()
    assert result["data_stale"] is False
    assert result["power_states"] == ACTIVE
    assert sleeper.delays == [2]
    assert probe.calls == 2
    assert api.calls.count("system_info") == 1


@pytest.mark.asyncio
async def test_standby_stops_followups_and_blocks_api():
    coordinator, probe, api, _, sleeper = make_scheduler([STANDBY])
    result = await coordinator._async_update_data()
    assert result["data_stale"] is True
    assert sleeper.delays == []
    assert probe.calls == 1
    assert api.calls == []


@pytest.mark.asyncio
async def test_permanent_unknown_uses_cache_after_two_followups():
    coordinator, probe, api, store, sleeper = make_scheduler(
        [UNKNOWN, UNKNOWN, UNKNOWN]
    )
    result = await coordinator._async_update_data()
    assert result["data_stale"] is True
    assert result["system_info"] == SAMPLE_DATA["system_info"]
    assert sleeper.delays == [2, 3]
    assert probe.calls == 3
    assert api.calls == []
    assert store.saved == []


@pytest.mark.asyncio
async def test_no_ssh_connection_is_held_during_followup_waits():
    class LifecycleProbe(SequenceProbe):
        connection_open = False

        async def async_check(self):
            assert not self.connection_open
            self.connection_open = True
            try:
                return await super().async_check()
            finally:
                self.connection_open = False

    probe = LifecycleProbe([UNKNOWN, UNKNOWN, ACTIVE])
    waits = []

    async def assert_closed_while_waiting(delay):
        assert not probe.connection_open
        waits.append(delay)

    coordinator = MyCloudDataUpdateCoordinator(
        hass=object(),
        logger=logging.getLogger("test"),
        api_client=FakeAPI(),
        store=FakeStore(),
        update_interval=timedelta(seconds=600),
        power_client=probe,
        probe_retry_delays=(2, 3),
        sleep_func=assert_closed_while_waiting,
    )

    await coordinator._async_update_data()

    assert waits == [2, 3]
    assert probe.calls == 3
    assert not probe.connection_open


@pytest.mark.asyncio
async def test_transient_unknown_does_not_rearm_current_wake_phase():
    coordinator, probe, api, _, _ = make_scheduler(
        [ACTIVE, UNKNOWN, UNKNOWN, UNKNOWN, ACTIVE]
    )
    first = await coordinator._async_update_data()
    blocked = await coordinator._async_update_data()
    recovered = await coordinator._async_update_data()
    assert first["data_stale"] is False
    assert blocked["data_stale"] is True
    assert recovered["data_stale"] is False
    assert api.calls.count("system_info") == 1
    assert probe.calls == 5


@pytest.mark.asyncio
async def test_definite_standby_rearms_exactly_one_immediate_poll():
    coordinator, _, api, _, _ = make_scheduler([ACTIVE, STANDBY, ACTIVE, ACTIVE])
    for _ in range(4):
        await coordinator._async_update_data()
    assert api.calls.count("system_info") == 2


@pytest.mark.asyncio
async def test_unknown_only_sleep_can_refresh_when_interval_is_due(monkeypatch):
    clock = {"seconds": 0}
    base = datetime(2026, 9, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        coordinator_module,
        "_utc_now",
        lambda: (base + timedelta(seconds=clock["seconds"])).isoformat(),
    )
    coordinator, probe, api, _, _ = make_scheduler(
        [ACTIVE, UNKNOWN, UNKNOWN, UNKNOWN, ACTIVE]
    )
    await coordinator._async_update_data()
    clock["seconds"] = 601
    blocked = await coordinator._async_update_data()
    assert blocked["data_stale"] is True
    assert api.calls.count("system_info") == 1
    clock["seconds"] = 606
    fresh = await coordinator._async_update_data()
    assert fresh["data_stale"] is False
    assert fresh["last_full_update"] != "2026-09-05T00:00:00+00:00"
    assert api.calls.count("system_info") == 2
    assert probe.calls == 5


@pytest.mark.asyncio
async def test_interval_is_a_minimum_and_continuous_active_cannot_flood(monkeypatch):
    clock = {"seconds": 0}
    base = datetime(2026, 9, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        coordinator_module,
        "_utc_now",
        lambda: (base + timedelta(seconds=clock["seconds"])).isoformat(),
    )
    coordinator, _, api, _, _ = make_scheduler([ACTIVE])
    await coordinator._async_update_data()
    for seconds in (10, 100, 599):
        clock["seconds"] = seconds
        result = await coordinator._async_update_data()
        assert result["data_stale"] is False
    assert api.calls.count("system_info") == 1
    clock["seconds"] = 600
    await coordinator._async_update_data()
    clock["seconds"] = 601
    await coordinator._async_update_data()
    assert api.calls.count("system_info") == 2


@pytest.mark.asyncio
async def test_unchanged_ten_second_probes_do_not_notify_or_write_cache():
    class TwoLoopTicks:
        calls = 0

        async def __call__(self, delay):
            self.calls += 1
            assert delay == 10
            if self.calls == 3:
                raise asyncio.CancelledError

    sleep = TwoLoopTicks()
    coordinator, probe, api, store, _ = make_scheduler([ACTIVE], sleep=sleep)
    coordinator.data = await coordinator._async_update_data()
    saves = len(store.saved)
    with pytest.raises(asyncio.CancelledError):
        await coordinator._async_power_probe_loop()
    assert probe.calls == 4
    assert api.calls.count("system_info") == 1
    assert len(store.saved) == saves
    assert coordinator.updated_data_calls == 0
    assert coordinator.update_error_calls == 0


@pytest.mark.asyncio
async def test_relevant_power_transition_notifies_once_without_cache_write():
    class OneLoopTick:
        calls = 0

        async def __call__(self, delay):
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError

    sleep = OneLoopTick()
    coordinator, _, api, store, _ = make_scheduler(
        [ACTIVE, STANDBY], sleep=sleep
    )
    coordinator.data = await coordinator._async_update_data()
    saves = len(store.saved)
    with pytest.raises(asyncio.CancelledError):
        await coordinator._async_power_probe_loop()
    assert coordinator.updated_data_calls == 1
    assert coordinator.data["power_states"] == STANDBY
    assert coordinator.data["data_stale"] is True
    assert len(store.saved) == saves
    assert api.calls.count("system_info") == 1


@pytest.mark.asyncio
async def test_background_api_failure_keeps_cache_and_cannot_retry_quickly(monkeypatch):
    clock = {"seconds": 0}
    base = datetime(2026, 9, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        coordinator_module,
        "_utc_now",
        lambda: (base + timedelta(seconds=clock["seconds"])).isoformat(),
    )

    class OneLoopTick:
        calls = 0

        async def __call__(self, delay):
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError

    sleep = OneLoopTick()
    coordinator, _, api, store, _ = make_scheduler([ACTIVE], sleep=sleep)
    coordinator.data = await coordinator._async_update_data()
    first_update = coordinator.data["last_full_update"]
    saves = len(store.saved)
    api.fail_system_info.append(RuntimeError("synthetic unavailable"))
    clock["seconds"] = 600
    with pytest.raises(asyncio.CancelledError):
        await coordinator._async_power_probe_loop()
    assert coordinator.last_update_success
    assert coordinator.data["data_stale"] is True
    assert coordinator.data["last_full_update"] == first_update
    assert coordinator.data["last_api_attempt"] is not None
    assert coordinator.data["last_api_attempt_status"] == "error"
    assert coordinator.data["last_api_error_type"] == "api_error"
    assert coordinator.data["last_api_error"] == (
        "api_error; stage=snapshot; details_withheld"
    )
    assert coordinator.updated_data_calls == 1
    assert len(store.saved) == saves
    assert api.calls.count("system_info") == 2
    clock["seconds"] = 601
    result = await coordinator._async_update_data()
    assert result["data_stale"] is True
    assert api.calls.count("system_info") == 2


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_timer_and_closes_both_clients():
    coordinator, _, api, _, _ = make_scheduler([ACTIVE])
    coordinator.data = await coordinator._async_update_data()
    coordinator.async_start_power_probe_loop()
    task = coordinator.power_probe_task
    assert task is not None
    await coordinator.async_shutdown()
    await coordinator.async_shutdown()
    assert task.done()
    assert coordinator.power_probe_task is None
    assert coordinator.power_client.closed == 1
    assert api.calls.count("exit") == 1


class BlockingLoopSleep:
    """Keep a background loop pending without wall-clock delays."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, delay):
        self.started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_probe_loop_uses_config_entry_background_task_and_is_idempotent():
    hass = TaskTrackingHass()
    entry = LifecycleConfigEntry()
    sleep = BlockingLoopSleep()
    coordinator, _, _, _, _ = make_scheduler(
        [ACTIVE], sleep=sleep, hass=hass, config_entry=entry
    )

    coordinator.async_start_power_probe_loop()
    task = coordinator.power_probe_task
    await sleep.started.wait()
    coordinator.async_start_power_probe_loop()

    assert task is coordinator.power_probe_task
    assert entry.create_calls == [(hass, "mycloud power probe", True)]
    assert task in entry.background_tasks
    assert task in hass.background_tasks
    assert hass.normal_create_calls == 0
    assert hass.normal_tasks == set()
    assert not task.done()
    await asyncio.wait_for(hass.async_block_till_done(), timeout=0.1)
    assert not task.done()

    await coordinator.async_shutdown()
    await coordinator.async_shutdown()
    assert task.done()
    assert coordinator.power_probe_task is None


def test_failed_background_registration_closes_unowned_coroutine():
    class RejectingEntry:
        coroutine = None

        def async_create_background_task(self, hass, coroutine, name):
            self.coroutine = coroutine
            raise RuntimeError("synthetic registration failure")

    entry = RejectingEntry()
    coordinator, _, _, _, _ = make_scheduler(
        [ACTIVE], hass=TaskTrackingHass(), config_entry=entry
    )

    with pytest.raises(RuntimeError, match="registration failure"):
        coordinator.async_start_power_probe_loop()

    assert coordinator.power_probe_task is None
    assert inspect.getcoroutinestate(entry.coroutine) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_config_entry_auto_cancel_and_manual_cleanup_do_not_collide():
    hass = TaskTrackingHass()
    entry = LifecycleConfigEntry()
    sleep = BlockingLoopSleep()
    coordinator, _, _, _, _ = make_scheduler(
        [ACTIVE], sleep=sleep, hass=hass, config_entry=entry
    )
    coordinator.async_start_power_probe_loop()
    task = coordinator.power_probe_task
    await sleep.started.wait()

    await entry.async_cancel_background_tasks()
    await coordinator.async_shutdown()
    await coordinator.async_shutdown()

    assert task.done()
    assert coordinator.power_probe_task is None
    assert coordinator.power_client.closed == 1


@pytest.mark.asyncio
async def test_reload_replaces_old_loop_with_exactly_one_new_background_task():
    hass = TaskTrackingHass()
    entry = LifecycleConfigEntry()
    first_sleep = BlockingLoopSleep()
    first, _, _, _, _ = make_scheduler(
        [ACTIVE], sleep=first_sleep, hass=hass, config_entry=entry
    )
    first.async_start_power_probe_loop()
    old_task = first.power_probe_task
    await first_sleep.started.wait()
    await first.async_shutdown()
    await asyncio.sleep(0)

    second_sleep = BlockingLoopSleep()
    second, _, _, _, _ = make_scheduler(
        [ACTIVE], sleep=second_sleep, hass=hass, config_entry=entry
    )
    second.async_start_power_probe_loop()
    new_task = second.power_probe_task
    await second_sleep.started.wait()
    second.async_start_power_probe_loop()

    assert old_task.done()
    assert new_task is not old_task
    assert not new_task.done()
    assert len(entry.background_tasks) == 1
    assert len(hass.background_tasks) == 1
    assert len(entry.create_calls) == 2
    assert hass.normal_create_calls == 0

    await second.async_shutdown()
