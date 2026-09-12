"""Exercise coordinator diagnostics, available system entities and log suppression."""

import json
import logging
from datetime import datetime, timedelta, timezone
from itertools import count

import asyncssh
import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_coordinator import (
    ALL_ACTIVE,
    BLOCKED_STATES,
    FakeAPI,
    FakeProbe,
    make_coordinator,
)
from test_probe_diagnostics import SECRET, wrapped

from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    PowerProbeError,
)
from custom_components.mycloud.sensor import (
    MyCloudCPUSensor,
    MyCloudDiskSleepSensor,
    MyCloudMemorySensor,
)


class ErrorProbe(FakeProbe):
    def __init__(self, cause):
        super().__init__(ALL_ACTIVE)
        self.cause = cause

    async def async_check(self):
        self.checks += 1
        if self.cause is not None:
            raise wrapped(self.cause)
        return dict(self.states)


class CommandTimeoutProbe(FakeProbe):
    def __init__(self, states):
        super().__init__(states)
        self.timed_out = False

    async def async_check(self):
        self.checks += 1
        if self.timed_out:
            try:
                raise TimeoutError(SECRET)
            except TimeoutError as err:
                raise PowerProbeError(
                    SECRET,
                    stage="command",
                    error_type="timeout",
                    detail="command_timeout",
                    duration_seconds=10.004,
                ) from err
        return dict(self.states)


def warnings(caplog):
    return [
        record for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "test"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause,expected", [
    (asyncssh.PermissionDenied(SECRET), "authentication_failed"),
    (asyncssh.HostKeyNotVerifiable(SECRET), "host_key_mismatch"),
    (asyncssh.KeyExchangeFailed(SECRET), "algorithm_negotiation_failed"),
    (ConnectionRefusedError(SECRET), "connection_failed"),
    (PowerProbeError(SECRET, error_type="command_failed"), "command_failed"),
    (TimeoutError(SECRET), "timeout"),
    (PowerProbeError(SECRET, error_type="parse_failed"), "parse_failed"),
])
async def test_error_diagnostics_on_available_system_sensors(cause, expected, caplog):
    api = FakeAPI()
    probe = ErrorProbe(cause)
    coordinator, store = make_coordinator(api, probe)
    cpu = MyCloudCPUSensor(coordinator, {}, "test", "NAS")
    memory = MyCloudMemorySensor(coordinator, {}, "test", "NAS")
    sleeping = MyCloudDiskSleepSensor(coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda")
    for failure_count in range(1, 4):
        coordinator.data = await coordinator._async_update_data()
        for entity in (cpu, memory):
            assert entity.available
            attrs = entity.extra_state_attributes
            assert attrs["power_probe_status"] == "error"
            assert attrs["power_probe_error_type"] == expected
            assert attrs["power_probe_error_stage"] == "connect"
            assert attrs["power_probe_error_duration_seconds"] is None
            assert attrs["power_probe_type"] == "hdparm_power_state"
            assert attrs["last_power_check"] == coordinator.last_power_check
            assert attrs["last_power_check"] is not None
            assert attrs["last_successful_power_check"] is None
            assert attrs["last_conclusive_power_check"] is None
            assert attrs["consecutive_probe_failures"] == failure_count
            assert attrs["consecutive_command_timeouts"] == 0
            assert attrs["power_states"] == dict.fromkeys(probe.drive_devices, POWER_UNKNOWN)
            assert attrs["raw_power_states"] == attrs["power_states"]
            assert attrs["last_confirmed_power_states"] == {}
            assert attrs["power_state_stale"] is True
            assert attrs["api_poll_allowed"] is False
            assert attrs["api_block_reason"] == "probe_error"
            assert expected in attrs["last_power_probe_error"]
            assert attrs["data_stale"] is True
            assert attrs["last_successful_update"] == "2026-09-03T08:00:00+00:00"
            assert SECRET not in json.dumps(attrs)
        assert not sleeping.available
        assert cpu.state == 7
        assert memory.state == 40
    assert api.calls == []
    assert store.saved == []
    assert len(warnings(caplog)) == 1
    assert warnings(caplog)[0].exc_info is None
    assert SECRET not in caplog.text
    # Diagnostics are snapshots, not mutable aliases into the coordinator.
    cpu.extra_state_attributes["power_states"].clear()
    assert coordinator.power_states


@pytest.mark.asyncio
async def test_repeated_timeout_keeps_confirmed_state_but_api_gate_stays_closed(caplog):
    api = FakeAPI()
    probe = CommandTimeoutProbe(ALL_ACTIVE)
    coordinator, _ = make_coordinator(api, probe)
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )

    with caplog.at_level(logging.DEBUG):
        coordinator.data = await coordinator._async_update_data()
    first_success = coordinator.last_successful_power_check
    api_calls_after_success = list(api.calls)
    assert sleeping.available
    assert sleeping.is_on is False

    probe.timed_out = True
    with caplog.at_level(logging.DEBUG):
        coordinator.data = await coordinator._async_update_data()
    assert coordinator.power_states == dict.fromkeys(
        probe.drive_devices, POWER_UNKNOWN
    )
    assert coordinator.power_probe_status == "error"
    assert coordinator.consecutive_probe_failures == 1
    assert coordinator.consecutive_command_timeouts == 1
    assert coordinator.last_successful_power_check == first_success
    assert sleeping.available
    assert sleeping.is_on is False
    assert api.calls == api_calls_after_success

    with caplog.at_level(logging.DEBUG):
        coordinator.data = await coordinator._async_update_data()
    assert coordinator.consecutive_probe_failures == 2
    assert coordinator.consecutive_command_timeouts == 2
    assert sleeping.available
    assert sleeping.is_on is False
    assert coordinator.power_probe_diagnostics["api_block_reason"] == "probe_error"
    assert api.calls == api_calls_after_success
    assert len(warnings(caplog)) == 0

    with caplog.at_level(logging.DEBUG):
        coordinator.data = await coordinator._async_update_data()
    assert coordinator.consecutive_command_timeouts == 3
    assert len(warnings(caplog)) == 1
    timeout_records = [
        record for record in caplog.records
        if record.name == "test" and "consecutive_command_timeouts" in record.message
    ]
    assert [record.levelno for record in timeout_records] == [
        logging.DEBUG, logging.DEBUG, logging.WARNING,
    ]
    diagnostics = coordinator.power_probe_diagnostics
    assert diagnostics["power_probe_error_stage"] == "command"
    assert diagnostics["power_probe_error_duration_seconds"] == 10.004
    assert diagnostics["power_probe_type"] == "hdparm_power_state"
    assert SECRET not in json.dumps(diagnostics)

    probe.timed_out = False
    probe.states = BLOCKED_STATES[0]
    coordinator.data = await coordinator._async_update_data()
    assert sleeping.available
    assert sleeping.is_on is True
    assert coordinator.power_probe_status == "ok"
    assert coordinator.power_probe_error_type is None
    assert coordinator.last_power_probe_error is None
    assert coordinator.consecutive_probe_failures == 0
    assert coordinator.consecutive_command_timeouts == 0
    assert coordinator.last_successful_power_check is not None
    assert api.calls == api_calls_after_success


@pytest.mark.asyncio
async def test_security_failure_keeps_display_state_but_is_diagnosed_and_blocked():
    api = FakeAPI()
    probe = ErrorProbe(None)
    coordinator, _ = make_coordinator(api, probe)
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )
    coordinator.data = await coordinator._async_update_data()
    api_calls_after_success = list(api.calls)

    probe.cause = asyncssh.HostKeyNotVerifiable(SECRET)
    coordinator.data = await coordinator._async_update_data()

    assert coordinator.consecutive_probe_failures == 1
    assert coordinator.power_probe_error_type == "host_key_mismatch"
    assert sleeping.available
    assert sleeping.is_on is False
    assert coordinator.api_poll_allowed is False
    assert coordinator.api_block_reason == "probe_error"
    assert api.calls == api_calls_after_success


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confirmed_state", "expected_is_on"),
    [(POWER_STANDBY, True), (POWER_ACTIVE, False)],
)
async def test_conclusive_state_then_unknown_keeps_per_drive_display(
    confirmed_state, expected_is_on, monkeypatch
):
    ticks = count()
    base = datetime(2026, 9, 6, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "custom_components.mycloud.coordinator._utc_now",
        lambda: (base + timedelta(seconds=next(ticks))).isoformat(),
    )
    api = FakeAPI()
    probe = FakeProbe(
        {"/dev/sda": confirmed_state, "/dev/sdc": confirmed_state}
    )
    coordinator, _ = make_coordinator(api, probe)
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )
    coordinator.data = await coordinator._async_update_data()
    api_calls_after_conclusive = list(api.calls)
    successful_time = coordinator.last_successful_power_check
    conclusive_time = coordinator.last_conclusive_power_check

    probe.states = {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": confirmed_state}
    coordinator.data = await coordinator._async_update_data()

    diagnostics = coordinator.power_probe_diagnostics
    assert sleeping.available
    assert sleeping.is_on is expected_is_on
    assert diagnostics["raw_power_states"]["/dev/sda"] == POWER_UNKNOWN
    assert diagnostics["last_confirmed_power_states"]["/dev/sda"] == confirmed_state
    assert diagnostics["power_probe_status"] == "unknown"
    assert diagnostics["power_state_stale"] is True
    assert diagnostics["last_successful_power_check"] != successful_time
    assert diagnostics["last_conclusive_power_check"] == conclusive_time
    assert diagnostics["consecutive_probe_failures"] == 0
    assert diagnostics["api_poll_allowed"] is False
    assert diagnostics["api_block_reason"] == "unknown"
    assert api.calls == api_calls_after_conclusive


@pytest.mark.asyncio
async def test_unknown_without_any_confirmed_state_is_unavailable():
    probe = FakeProbe(
        {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_UNKNOWN}
    )
    coordinator, _ = make_coordinator(FakeAPI(), probe)
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )

    coordinator.data = await coordinator._async_update_data()

    assert not sleeping.available
    assert sleeping.is_on is None
    assert coordinator.visible_power_states == {}
    assert coordinator.last_successful_power_check is not None
    assert coordinator.last_conclusive_power_check is None
    assert coordinator.consecutive_probe_failures == 0


@pytest.mark.asyncio
async def test_unknown_drive_retains_its_state_while_conclusive_peer_updates():
    api = FakeAPI()
    probe = FakeProbe(
        {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_STANDBY}
    )
    coordinator, _ = make_coordinator(api, probe)
    sda = MyCloudDiskSleepSensor(
        coordinator, {}, "sda", "Disk sda", {"name": "sda"}, "/dev/sda"
    )
    sdc = MyCloudDiskSleepSensor(
        coordinator, {}, "sdc", "Disk sdc", {"name": "sdc"}, "/dev/sdc"
    )
    coordinator.data = await coordinator._async_update_data()

    probe.states = {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_ACTIVE}
    coordinator.data = await coordinator._async_update_data()

    assert sda.available and sda.is_on is True
    assert sdc.available and sdc.is_on is False
    assert coordinator.power_states == {
        "/dev/sda": POWER_UNKNOWN,
        "/dev/sdc": POWER_ACTIVE,
    }
    assert coordinator.visible_power_states == {
        "/dev/sda": POWER_STANDBY,
        "/dev/sdc": POWER_ACTIVE,
    }
    assert coordinator.api_poll_allowed is False
    assert coordinator.api_block_reason == "unknown"
    assert api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("states", "allowed", "reason", "api_polls"),
    [
        (ALL_ACTIVE, True, None, 1),
        (
            {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_STANDBY},
            False,
            "mixed",
            0,
        ),
        (
            {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_UNKNOWN},
            False,
            "unknown",
            0,
        ),
    ],
)
async def test_api_gate_uses_only_same_cycle_raw_states(
    states, allowed, reason, api_polls
):
    api = FakeAPI()
    coordinator, _ = make_coordinator(api, FakeProbe(states))

    coordinator.data = await coordinator._async_update_data()

    assert coordinator.api_poll_allowed is allowed
    assert coordinator.api_block_reason == reason
    assert api.calls.count("system_info") == api_polls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recovered_state", "expected_is_on"),
    [(POWER_ACTIVE, False), (POWER_STANDBY, True)],
)
async def test_unknown_recovers_immediately_to_new_conclusive_state(
    recovered_state, expected_is_on
):
    probe = FakeProbe(
        {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_UNKNOWN}
    )
    coordinator, _ = make_coordinator(FakeAPI(), probe)
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )
    coordinator.data = await coordinator._async_update_data()
    assert not sleeping.available

    probe.states = {
        "/dev/sda": recovered_state,
        "/dev/sdc": POWER_ACTIVE,
    }
    coordinator.data = await coordinator._async_update_data()

    assert sleeping.available
    assert sleeping.is_on is expected_is_on
    assert coordinator.power_states["/dev/sda"] == recovered_state
    assert coordinator.visible_power_states["/dev/sda"] == recovered_state
    assert coordinator.power_state_stale is False
    assert coordinator.last_conclusive_power_check is not None


@pytest.mark.asyncio
async def test_repeated_unknown_never_changes_confirmed_sleeping_state():
    api = FakeAPI()
    probe = FakeProbe(
        {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}
    )
    coordinator, _ = make_coordinator(api, probe)
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )
    coordinator.data = await coordinator._async_update_data()
    api_calls = list(api.calls)

    probe.states = {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_ACTIVE}
    observed = []
    for _ in range(3):
        coordinator.data = await coordinator._async_update_data()
        observed.append((sleeping.available, sleeping.is_on))

    assert observed == [(True, False)] * 3
    assert coordinator.power_states["/dev/sda"] == POWER_UNKNOWN
    assert coordinator.visible_power_states["/dev/sda"] == POWER_ACTIVE
    assert api.calls == api_calls


@pytest.mark.asyncio
async def test_probe_error_after_restart_keeps_api_cache_but_no_power_state():
    api = FakeAPI()
    coordinator, _ = make_coordinator(
        api, ErrorProbe(TimeoutError(SECRET)), with_cache=True
    )
    cpu = MyCloudCPUSensor(coordinator, {}, "test", "NAS")
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )

    coordinator.data = await coordinator._async_update_data()

    assert cpu.available and cpu.state == 7
    assert not sleeping.available
    assert sleeping.is_on is None
    assert coordinator.visible_power_states == {}
    assert coordinator.api_poll_allowed is False
    assert coordinator.api_block_reason == "probe_error"
    assert coordinator.data["data_stale"] is True
    assert api.calls == []


@pytest.mark.asyncio
async def test_changed_cause_logs_once_and_recovery_rearms_logging(caplog, monkeypatch):
    ticks = count()
    base = datetime(2026, 9, 4, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "custom_components.mycloud.coordinator._utc_now",
        lambda: (base + timedelta(seconds=60 * next(ticks))).isoformat(),
    )
    api = FakeAPI()
    probe = ErrorProbe(asyncssh.PermissionDenied(SECRET))
    coordinator, _ = make_coordinator(api, probe)
    first_time = None
    for message in (SECRET, "different synthetic text", SECRET):
        probe.cause = asyncssh.PermissionDenied(message)
        coordinator.data = await coordinator._async_update_data()
        first_time = first_time or coordinator.last_power_check
    assert coordinator.last_power_check != first_time
    assert len(warnings(caplog)) == 1

    for subtype in ("encryption", "MAC"):
        probe.cause = asyncssh.KeyExchangeFailed(f"No matching {subtype} algorithm found, sent {SECRET}")
        for _ in range(2):
            coordinator.data = await coordinator._async_update_data()
    assert len(warnings(caplog)) == 3
    probe.cause = None
    probe.states = BLOCKED_STATES[0]  # Recovery need not wake the NAS.
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.power_probe_status == "ok"
    assert coordinator.power_probe_error_type is None
    assert coordinator.last_power_probe_error is None
    assert coordinator.consecutive_probe_failures == 0
    assert coordinator.last_successful_power_check is not None
    assert api.calls == []
    probe.cause = asyncssh.KeyExchangeFailed(f"No matching MAC algorithm found, sent {SECRET}")
    coordinator.data = await coordinator._async_update_data()
    assert len(warnings(caplog)) == 4  # First error after a successful probe.

    probe.cause = None
    probe.states = ALL_ACTIVE
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.power_probe_status == "ok"
    assert coordinator.power_probe_error_type is None
    assert coordinator.data["data_stale"] is False
    assert coordinator.data["last_full_update"] != "2026-09-03T08:00:00+00:00"
    for _ in range(2):
        coordinator.data = await coordinator._async_update_data()
    assert api.calls.count("system_info") == 1
    assert len(warnings(caplog)) == 4
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_diagnostics_survive_missing_cache(caplog):
    api = FakeAPI()
    coordinator, _ = make_coordinator(api, ErrorProbe(TimeoutError(SECRET)), with_cache=False)
    with pytest.raises(UpdateFailed, match="Wake the NAS"):
        await coordinator._async_update_data()
    assert coordinator.power_probe_diagnostics["power_probe_error_type"] == "timeout"
    assert coordinator.power_probe_diagnostics["last_power_check"]
    assert api.calls == []
    assert len(warnings(caplog)) == 1
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_unknown_states_are_sanitized_before_storage_or_transition_logs(caplog):
    probe = FakeProbe({"/dev/sda": SECRET, SECRET: SECRET})
    api = FakeAPI()
    coordinator, _ = make_coordinator(api, probe)
    with caplog.at_level(logging.INFO):
        coordinator.data = await coordinator._async_update_data()
    assert coordinator.power_probe_status == "unknown"
    assert coordinator.power_probe_error_type is None
    assert coordinator.power_states == dict.fromkeys(probe.drive_devices, POWER_UNKNOWN)
    assert SECRET not in json.dumps(coordinator.power_probe_diagnostics)
    assert SECRET not in caplog.text
    assert api.calls == []


@pytest.mark.asyncio
async def test_legacy_mode_diagnostics_are_disabled():
    coordinator, _ = make_coordinator(FakeAPI())
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.power_probe_diagnostics == {
        "last_power_check": None,
        "last_successful_power_check": None,
        "last_conclusive_power_check": None,
        "consecutive_probe_failures": 0,
        "consecutive_command_timeouts": 0,
        "power_states": {},
        "raw_power_states": {},
        "last_confirmed_power_states": {},
        "power_state_stale": None,
        "power_probe_status": "disabled",
        "power_probe_error_type": None,
        "power_probe_error_stage": None,
        "power_probe_error_duration_seconds": None,
        "power_probe_type": None,
        "last_power_probe_error": None,
        "api_poll_allowed": True,
        "api_block_reason": None,
    }
