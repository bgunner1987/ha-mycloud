"""Tests for cache metadata and live sleeping entity behavior."""

from types import SimpleNamespace

import pytest

from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
)
from custom_components.mycloud.sensor import (
    MyCloudDiskSleepSensor,
    MyCloudDiskTempSensor,
)


def make_coordinator(power_state):
    return SimpleNamespace(
        last_update_success=True,
        data={
            "system_info": {
                "disks": [{"name": "1", "temp": 31, "sleep": False}]
            },
            "power_states": {"/dev/sda": power_state},
            "sleep_aware_enabled": True,
            "data_stale": power_state != POWER_ACTIVE,
            "last_full_update": "2026-09-03T08:00:00+00:00",
        },
    )


@pytest.mark.parametrize(
    ("power_state", "is_on", "available"),
    [
        (POWER_STANDBY, True, True),
        (POWER_ACTIVE, False, True),
        (POWER_UNKNOWN, None, False),
    ],
)
def test_sleep_sensor_prefers_live_hdparm_state(power_state, is_on, available):
    entity = MyCloudDiskSleepSensor(
        make_coordinator(power_state),
        {},
        "serial",
        "Disk 1",
        {"name": "1"},
        "/dev/sda",
    )

    assert entity.is_on is is_on
    assert entity.available is available


def test_cached_temperature_and_freshness_attributes_are_retained():
    entity = MyCloudDiskTempSensor(
        make_coordinator(POWER_STANDBY),
        {},
        "serial",
        "Disk 1",
        {"name": "1"},
    )

    assert entity.native_value == 31.0
    assert entity.extra_state_attributes == {
        "data_stale": True,
        "last_successful_update": "2026-09-03T08:00:00+00:00",
    }
