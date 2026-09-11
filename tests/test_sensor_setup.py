"""Exercise real platform setup, coordinator and entities with synthetic NAS I/O."""

import asyncio
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


async def setup_platform(
    monkeypatch,
    snapshot,
    options,
    *,
    cached=True,
    power_check=None,
    settle=True,
):
    api = SimpleNamespace(
        __aenter__=AsyncMock(), __aexit__=AsyncMock(), session=None,
        **{key: AsyncMock(return_value=deepcopy(value)) for key, value in snapshot.items()},
    )
    store = SimpleNamespace(
        async_load=AsyncMock(
            return_value=(
                {
                    "data": deepcopy(snapshot),
                    "last_full_update": "2026-09-04T00:00:00+00:00",
                }
                if cached
                else None
            )
        ),
        async_save=AsyncMock(),
    )
    monkeypatch.setattr(platform, "nas_client", lambda *args: api)
    monkeypatch.setattr(platform, "Store", lambda *args: store)

    async def check_power(client):
        return {
            device: POWER_STANDBY if device == "/dev/sda" else POWER_ACTIVE
            for device in client.drive_devices
        }

    monkeypatch.setattr(
        platform.SSHPowerStateClient,
        "async_check",
        power_check or check_power,
    )
    hass = SimpleNamespace(data={DOMAIN: {"test-entry": {}}})

    def create_background_task(_hass, coroutine, name):
        return asyncio.create_task(coroutine, name=name)

    entry = SimpleNamespace(
        entry_id="test-entry",
        data={HOST: "nas.example.invalid", USERNAME: "test", PASSWORD: "unused-test-value", VERSION: 5},
        options=options,
        async_create_background_task=create_background_task,
    )
    entities = []
    await platform.async_setup_entry(hass, entry, entities.extend)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    if settle:
        await asyncio.sleep(0)
    return entities, coordinator, api


@pytest.mark.asyncio
async def test_sleep_aware_setup_does_not_wait_for_hanging_probe(monkeypatch):
    probe_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def hanging_probe(_client):
        probe_started.set()
        await never_finish.wait()

    entities, coordinator, api = await asyncio.wait_for(
        setup_platform(
            monkeypatch,
            make_snapshot(["sda", "sdc"]),
            {
                CONF_SLEEP_AWARE_ENABLED: True,
                CONF_DRIVE_DEVICES: "/dev/sda,/dev/sdc",
                CONF_UPDATE_INTERVAL: 600,
            },
            power_check=hanging_probe,
            settle=False,
        ),
        timeout=0.1,
    )
    try:
        assert entities
        assert coordinator.data["data_stale"] is True
        await asyncio.wait_for(probe_started.wait(), timeout=0.1)
        assert not coordinator.power_probe_task.done()
        for key in make_snapshot([]):
            getattr(api, key).assert_not_awaited()
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_sleep_aware_setup_without_cache_starts_cleanly(monkeypatch):
    probe_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def hanging_probe(_client):
        probe_started.set()
        await never_finish.wait()

    entities, coordinator, api = await asyncio.wait_for(
        setup_platform(
            monkeypatch,
            make_snapshot(["sda", "sdc"]),
            {
                CONF_SLEEP_AWARE_ENABLED: True,
                CONF_DRIVE_DEVICES: "/dev/sda,/dev/sdc",
                CONF_UPDATE_INTERVAL: 600,
            },
            cached=False,
            power_check=hanging_probe,
            settle=False,
        ),
        timeout=0.1,
    )
    try:
        # Entity identifiers depend on the first real device snapshot, so the
        # platform loads now and adds them later without inventing identities.
        assert entities == []
        assert coordinator.data is None
        await asyncio.wait_for(probe_started.wait(), timeout=0.1)
        assert not coordinator.power_probe_task.done()
        for key in make_snapshot([]):
            getattr(api, key).assert_not_awaited()
    finally:
        task = coordinator.power_probe_task
        await coordinator.async_shutdown()
        assert task.done()


@pytest.mark.asyncio
async def test_first_safe_snapshot_adds_deferred_entities_without_cache(monkeypatch):
    async def active_probe(client):
        return dict.fromkeys(client.drive_devices, POWER_ACTIVE)

    entities, coordinator, api = await setup_platform(
        monkeypatch,
        make_snapshot(["sda", "sdc"]),
        {
            CONF_SLEEP_AWARE_ENABLED: True,
            CONF_DRIVE_DEVICES: "/dev/sda,/dev/sdc",
            CONF_UPDATE_INTERVAL: 600,
        },
        cached=False,
        power_check=active_probe,
    )
    try:
        for _ in range(10):
            if entities:
                break
            await asyncio.sleep(0)
        assert entities
        assert coordinator.data["data_stale"] is False
        assert "remove_startup_listener" not in coordinator.hass.data[DOMAIN][
            "test-entry"
        ]
        for key in make_snapshot([]):
            getattr(api, key).assert_awaited_once()
    finally:
        await coordinator.async_shutdown()


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
        assert coordinator.update_interval is None
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
        assert coordinator.update_interval is None
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
