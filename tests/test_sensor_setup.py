"""Exercise real platform setup, coordinator and entities with synthetic NAS I/O."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.mycloud import sensor as platform
from custom_components.mycloud.const import (
    CONF_DRIVE_DEVICES,
    CONF_POWER_PROBE_INTERVAL,
    CONF_SLEEP_AWARE_ENABLED,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    HOST,
    PASSWORD,
    USERNAME,
    VERSION,
)
from custom_components.mycloud.power_probe import POWER_ACTIVE, POWER_STANDBY


def make_snapshot(names):
    disks = []
    for name in names:
        disks.append({
            "name": name, "sn": f"test-{name}", "model": "Test disk", "rev": "test",
            "temp": 31 if name == "sda" else 36, "size": 100,
            "healthy": name == "sda", "failed": name == "sdc",
            "over_temp": False, "sleep": False,
        })
    return {
        "system_info": {
            "disks": disks, "volumes": [],
            "size": {"total": 200, "used": 50, "unused": 150},
        },
        "system_status": {"cpu": 3, "memory": {"total": 100, "unused": 50}},
        "device_info": {"serial_number": "test-nas", "name": "Test NAS", "description": "EX2 Ultra"},
        "system_version": {"firmware": "test"},
    }


async def setup_platform(monkeypatch, snapshot, options):
    api = SimpleNamespace(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(), session=None,
        **{key: AsyncMock(return_value=deepcopy(value)) for key, value in snapshot.items()},
    )
    store = SimpleNamespace(
        async_load=AsyncMock(return_value={
            "data": deepcopy(snapshot), "last_full_update": "2026-09-04T00:00:00+00:00",
        }),
        async_save=AsyncMock(),
    )
    monkeypatch.setattr(platform, "nas_client", lambda *args: api)
    monkeypatch.setattr(platform, "Store", lambda *args: store)

    async def check_power(client):
        return {
            device: POWER_STANDBY if device == "/dev/sda" else POWER_ACTIVE
            for device in client.drive_devices
        }

    monkeypatch.setattr(platform.SSHPowerStateClient, "async_check", check_power)
    hass = SimpleNamespace(data={DOMAIN: {"test-entry": {}}})
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={HOST: "nas.example.invalid", USERNAME: "test", PASSWORD: "unused-test-value", VERSION: 5},
        options=options,
    )
    entities = []
    await platform.async_setup_entry(hass, entry, entities.extend)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    return entities, coordinator, api


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("sda", "sdb", "sdc"), ("sdc", "sdb", "sda")])
@pytest.mark.parametrize("devices", ["/dev/sda,/dev/sdc", "/dev/sdc,/dev/sda"])
async def test_platform_matches_exact_names_not_positions(monkeypatch, names, devices):
    snapshot = make_snapshot(names)
    # Real stale API rows may be incomplete: skip before reading sn/model/etc.
    snapshot["system_info"]["disks"] = [
        {"name": "sdb"} if disk["name"] == "sdb" else disk
        for disk in snapshot["system_info"]["disks"]
    ]
    entities, coordinator, api = await setup_platform(monkeypatch, snapshot, {
        CONF_SLEEP_AWARE_ENABLED: True, CONF_DRIVE_DEVICES: devices,
        CONF_UPDATE_INTERVAL: 600,
    })
    try:
        assert coordinator.update_interval == timedelta(seconds=60)
        sleeping = {e._disk_name: e for e in entities if isinstance(e, platform.MyCloudDiskSleepSensor)}
        assert set(sleeping) == {"sda", "sdc"}
        assert sleeping["sda"]._drive_device == "/dev/sda"
        assert sleeping["sdc"]._drive_device == "/dev/sdc"
        assert sleeping["sda"].is_on is True
        assert sleeping["sdc"].is_on is False
        assert all(e.available for e in sleeping.values())
        temperatures = {e._disk_name: e.native_value for e in entities if isinstance(e, platform.MyCloudDiskTempSensor)}
        healthy = {e._disk_name: e.is_on for e in entities if isinstance(e, platform.MyCloudDiskHealthySensor)}
        failed = {e._disk_name: e.is_on for e in entities if isinstance(e, platform.MyCloudDiskFailedSensor)}
        assert temperatures == {"sda": 31.0, "sdc": 36.0}
        assert healthy == {"sda": True, "sdc": False}
        assert failed == {"sda": False, "sdc": True}
        assert len([e for e in entities if hasattr(e, "_disk_name")]) == 12
        for key in snapshot:
            getattr(api, key).assert_not_awaited()
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", ["sdb", "/dev/sdc", "sdc1", " sdc", "sdc;id", "../sdc", None, ["sdc"]])
async def test_unconfigured_or_untrusted_api_name_never_maps_to_a_drive(monkeypatch, bad_name):
    snapshot = make_snapshot(["sda", "sdc"])
    snapshot["system_info"]["disks"].insert(1, {"name": bad_name})
    entities, coordinator, _ = await setup_platform(monkeypatch, snapshot, {
        CONF_SLEEP_AWARE_ENABLED: True, CONF_DRIVE_DEVICES: "/dev/sda,/dev/sdc",
        CONF_UPDATE_INTERVAL: 600, CONF_POWER_PROBE_INTERVAL: 30,
    })
    try:
        assert coordinator.update_interval == timedelta(seconds=30)
        disk_entities = [e for e in entities if hasattr(e, "_disk_name")]
        assert len(disk_entities) == 12
        assert {e._disk_name for e in disk_entities} == {"sda", "sdc"}
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_legacy_platform_preserves_api_disks_and_update_interval(monkeypatch):
    entities, coordinator, _ = await setup_platform(monkeypatch, make_snapshot(["sda", "sdb", "sdc"]), {
        CONF_SLEEP_AWARE_ENABLED: False, CONF_UPDATE_INTERVAL: 600,
        CONF_POWER_PROBE_INTERVAL: 30,
    })
    try:
        assert coordinator.update_interval == timedelta(seconds=600)
        assert len([e for e in entities if hasattr(e, "_disk_name")]) == 18
    finally:
        await coordinator.async_shutdown()
