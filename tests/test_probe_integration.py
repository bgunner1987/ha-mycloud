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

from custom_components.mycloud.power_probe import POWER_UNKNOWN, PowerProbeError
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
            assert attrs["last_power_check"] == coordinator.last_power_check
            assert attrs["last_power_check"] is not None
            assert attrs["last_successful_power_check"] is None
            assert attrs["consecutive_probe_failures"] == failure_count
            assert attrs["power_states"] == dict.fromkeys(probe.drive_devices, POWER_UNKNOWN)
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
async def test_sleeping_entity_tolerates_one_failure_but_api_gate_does_not(caplog):
    api = FakeAPI()
    probe = ErrorProbe(None)
    coordinator, _ = make_coordinator(api, probe)
    sleeping = MyCloudDiskSleepSensor(
        coordinator, {}, "test", "Disk", {"name": "1"}, "/dev/sda"
    )

    coordinator.data = await coordinator._async_update_data()
    first_success = coordinator.last_successful_power_check
    api_calls_after_success = list(api.calls)
    assert sleeping.available
    assert sleeping.is_on is False

    probe.cause = TimeoutError(SECRET)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.power_states == dict.fromkeys(
        probe.drive_devices, POWER_UNKNOWN
    )
    assert coordinator.power_probe_status == "error"
    assert coordinator.consecutive_probe_failures == 1
    assert coordinator.last_successful_power_check == first_success
    assert sleeping.available
    assert sleeping.is_on is False
    assert api.calls == api_calls_after_success

    coordinator.data = await coordinator._async_update_data()
    assert coordinator.consecutive_probe_failures == 2
    assert not sleeping.available
    assert sleeping.is_on is None
    assert api.calls == api_calls_after_success
    assert len(warnings(caplog)) == 1

    probe.cause = None
    probe.states = BLOCKED_STATES[0]
    coordinator.data = await coordinator._async_update_data()
    assert sleeping.available
    assert sleeping.is_on is True
    assert coordinator.power_probe_status == "ok"
    assert coordinator.power_probe_error_type is None
    assert coordinator.last_power_probe_error is None
    assert coordinator.consecutive_probe_failures == 0
    assert coordinator.last_successful_power_check is not None
    assert api.calls == api_calls_after_success


@pytest.mark.asyncio
async def test_security_failure_is_not_hidden_by_sleeping_display_tolerance():
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
    assert not sleeping.available
    assert sleeping.is_on is None
    assert api.calls == api_calls_after_success


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
        "consecutive_probe_failures": 0,
        "power_states": {}, "power_probe_status": "disabled",
        "power_probe_error_type": None, "last_power_probe_error": None,
    }
