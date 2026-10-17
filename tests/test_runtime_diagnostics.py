"""Runtime diagnostics and options-to-reloaded-client regressions."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_coordinator import FakeAPI, FakeProbe, make_coordinator
from test_sensor_setup import make_snapshot, setup_platform

from custom_components.mycloud import async_reload_entry, async_setup_entry
from custom_components.mycloud.config_flow import MyCloudOptionsFlowHandler
from custom_components.mycloud.const import (
    CONF_DRIVE_DEVICES,
    CONF_POWER_PROBE_METHOD,
    CONF_SLEEP_AWARE_ENABLED,
    INTEGRATION_VERSION,
    PROBE_METHOD_HDPARM,
    PROBE_METHOD_SMARTCTL_SAT,
)
from custom_components.mycloud.diagnostics import async_get_config_entry_diagnostics
from custom_components.mycloud.power_probe import POWER_ACTIVE, POWER_UNKNOWN
from custom_components.mycloud.sensor import MyCloudProbeDiagnosticSensor


def test_running_version_matches_manifest():
    manifest = Path(__file__).parents[1] / "custom_components/mycloud/manifest.json"
    assert json.loads(manifest.read_text())["version"] == INTEGRATION_VERSION == "1.3.12"


@pytest.mark.asyncio
async def test_diagnostics_available_without_cache_and_do_not_export_options(monkeypatch):
    async def unknown(_client):
        return {"/dev/sda": POWER_UNKNOWN, "/dev/sdb": POWER_UNKNOWN}

    entities, coordinator, api = await setup_platform(
        monkeypatch, make_snapshot(["sda", "sdb"]),
        {CONF_SLEEP_AWARE_ENABLED: True, CONF_DRIVE_DEVICES: "/dev/sda,/dev/sdb",
         CONF_POWER_PROBE_METHOD: PROBE_METHOD_SMARTCTL_SAT},
        cached=False, power_check=unknown, settle=False,
    )
    coordinator._probe_retry_delays = ()
    try:
        for _ in range(20):
            if coordinator.last_power_check:
                break
            await asyncio.sleep(0)
        diagnostic = next(e for e in entities if isinstance(e, MyCloudProbeDiagnosticSensor))
        assert diagnostic.available
        safe = diagnostic.extra_state_attributes
        assert safe["integration_version"] == INTEGRATION_VERSION
        assert safe["configured_power_probe_method"] == PROBE_METHOD_SMARTCTL_SAT
        assert safe["effective_power_probe_method"] == PROBE_METHOD_SMARTCTL_SAT
        assert safe["checked_devices"] == ["/dev/sda", "/dev/sdb"]
        assert safe["has_api_cache"] is False
        assert safe["probe_loop_running"] is True
        assert safe["api_block_reason"] == "unknown"
        assert diagnostic.native_value == "unknown"
        entry = SimpleNamespace(entry_id="test-entry", data={"Password": "private-marker"}, options={"ssh_password": "private-marker"})
        downloaded = await async_get_config_entry_diagnostics(coordinator.hass, entry)
        assert downloaded == safe
        assert "private-marker" not in json.dumps(downloaded)
        assert "unused-test-value" not in json.dumps(downloaded)
        for key in make_snapshot([]):
            getattr(api, key).assert_not_awaited()
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_option_switch_persists_then_reload_uses_new_client(monkeypatch):
    listeners = []
    entry = SimpleNamespace(
        entry_id="test-entry",
        options={CONF_SLEEP_AWARE_ENABLED: True, CONF_DRIVE_DEVICES: "/dev/sda,/dev/sdc",
                 CONF_POWER_PROBE_METHOD: PROBE_METHOD_HDPARM},
        add_update_listener=lambda callback: listeners.append(callback),
        async_on_unload=lambda _remove: None,
    )
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(
        async_forward_entry_setups=AsyncMock(), async_reload=AsyncMock(),
    ))
    await async_setup_entry(hass, entry)
    assert listeners == [async_reload_entry]
    flow = MyCloudOptionsFlowHandler()
    flow.config_entry = entry
    form = await flow.async_step_init()
    saved = await flow.async_step_init(form["data_schema"]({
        CONF_POWER_PROBE_METHOD: PROBE_METHOD_SMARTCTL_SAT,
        CONF_DRIVE_DEVICES: " /dev/sda, /dev/sdb ",
        CONF_SLEEP_AWARE_ENABLED: True,
    }))
    # HA commits the returned OptionsFlow entry, then invokes the registered
    # listener. Exercise that boundary explicitly, without a fake parser/client.
    entry.options = saved["data"]
    await listeners[0](hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("test-entry")
    entities, coordinator, _ = await setup_platform(monkeypatch, make_snapshot(["sda", "sdb"]), entry.options)
    try:
        assert coordinator.power_client.probe_method == PROBE_METHOD_SMARTCTL_SAT
        assert coordinator.drive_devices == ("/dev/sda", "/dev/sdb")
        assert coordinator.runtime_diagnostics["configured_power_probe_method"] == PROBE_METHOD_SMARTCTL_SAT
        assert any(isinstance(e, MyCloudProbeDiagnosticSensor) for e in entities)
    finally:
        await coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_api_failure_and_interval_wait_have_separate_diagnostics():
    api = FakeAPI(fail_system_info=[TimeoutError("private-marker")])
    coordinator, _ = make_coordinator(api, FakeProbe({"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}))
    with pytest.raises(Exception, match="WD API snapshot failed"):
        await coordinator._async_update_data()
    failed = coordinator.runtime_diagnostics
    assert failed["api_stage"] == "system_info"
    assert failed["api_poll_decision"] == "api_failed"
    assert coordinator.api_poll_allowed is True
    await coordinator._async_update_data()
    waiting = coordinator.runtime_diagnostics
    assert waiting["api_poll_decision"] == "interval_wait"
    assert waiting["api_interval_remaining_seconds"] > 0
    assert waiting["next_periodic_api_poll_at"]
    assert "private-marker" not in json.dumps(waiting)
    assert api.calls.count("system_info") == 1


@pytest.mark.asyncio
async def test_pending_api_stage_is_published_before_snapshot_exists():
    entered = asyncio.Event()

    class WaitingAPI(FakeAPI):
        async def system_info(self):
            entered.set()
            await asyncio.Future()

    api = WaitingAPI()
    coordinator, _ = make_coordinator(api, FakeProbe({"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE}), with_cache=False)
    published = []
    coordinator.async_add_listener(lambda: published.append(coordinator.runtime_diagnostics["api_stage"]))
    task = asyncio.create_task(coordinator._async_update_data())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert "opening_client" in published
        assert published[-1] == "system_info"
        assert coordinator.runtime_diagnostics["api_stage"] == "system_info"
        assert coordinator.last_api_attempt_status == "in_progress"
        assert coordinator.data is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await coordinator.async_shutdown()
    assert api.calls[-1] == "exit"
