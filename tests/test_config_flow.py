"""Flow tests using the real voluptuous_serialize converter, not a serializer stub.

Home Assistant flow lifecycle/password selectors are isolated by conftest; the
actual Voluptuous schema and its drive validator go through the real converter.
"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import voluptuous as vol
from voluptuous_serialize import convert

from custom_components.mycloud.config_flow import (
    MyCloudConfigFlow,
    MyCloudOptionsFlowHandler,
    _validate_drive_devices,
)
from custom_components.mycloud.const import (
    CONF_DRIVE_DEVICES,
    CONF_POWER_PROBE_INTERVAL,
    CONF_SLEEP_AWARE_ENABLED,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_UPDATE_INTERVAL,
    HOST,
    PASSWORD,
    USERNAME,
    VERSION,
)


@pytest.mark.asyncio
async def test_initial_flow_stores_sleep_aware_settings_as_options():
    flow = MyCloudConfigFlow()
    result = await flow.async_step_user(
        {
            HOST: "192.0.2.10",
            USERNAME: "admin",
            PASSWORD: "not-an-api-credential",
            VERSION: 5,
            CONF_UPDATE_INTERVAL: 600,
            CONF_SLEEP_AWARE_ENABLED: True,
            CONF_SSH_PORT: 22,
            CONF_SSH_USERNAME: "root",
            CONF_SSH_PASSWORD: "not-an-ssh-credential",
            CONF_DRIVE_DEVICES: "/dev/sda,/dev/sdc",
        }
    )

    assert result["data"] == {
        HOST: "192.0.2.10",
        USERNAME: "admin",
        PASSWORD: "not-an-api-credential",
        VERSION: 5,
    }
    assert result["options"][CONF_SLEEP_AWARE_ENABLED] is True
    assert result["options"][CONF_DRIVE_DEVICES] == "/dev/sda,/dev/sdc"
    assert result["options"][CONF_POWER_PROBE_INTERVAL] == 60


def make_flow(kind):
    if kind == "config":
        flow = MyCloudConfigFlow()
        return flow, flow.async_step_user
    flow = MyCloudOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(options={
        CONF_UPDATE_INTERVAL: 900,
        CONF_SLEEP_AWARE_ENABLED: True,
        CONF_DRIVE_DEVICES: "/dev/sdb",
        "preserved_option": "kept",
    })
    return flow, flow.async_step_init


def submitted_data(kind, devices):
    data = {
        CONF_UPDATE_INTERVAL: 600,
        CONF_SLEEP_AWARE_ENABLED: True,
        CONF_SSH_PORT: 22,
        CONF_SSH_USERNAME: "root",
        CONF_SSH_PASSWORD: "not-an-ssh-credential",
        CONF_DRIVE_DEVICES: devices,
    }
    if kind == "config":
        data.update({
            HOST: "nas.example.invalid",
            USERNAME: "admin",
            PASSWORD: "not-an-api-credential",
            VERSION: 5,
        })
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["config", "options"])
async def test_form_serializes_with_real_voluptuous_serialize(kind):
    _, step = make_flow(kind)
    form = await step()
    serialized = convert(form["data_schema"])
    # Exercise the JSON response too: no raw function or validator may leak.
    serialized = json.loads(json.dumps(serialized))
    drive_field = next(item for item in serialized if item["name"] == CONF_DRIVE_DEVICES)
    assert drive_field["type"] == "string"
    probe_field = next(item for item in serialized if item["name"] == CONF_POWER_PROBE_INTERVAL)
    assert probe_field["default"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["config", "options"])
@pytest.mark.parametrize("interval", [10, 30, 60])
async def test_probe_interval_is_validated_and_saved_independently(kind, interval):
    _, step = make_flow(kind)
    form = await step()
    data = submitted_data(kind, "/dev/sda,/dev/sdc")
    data[CONF_POWER_PROBE_INTERVAL] = str(interval)
    result = await step(form["data_schema"](data))
    saved = result["options"] if kind == "config" else result["data"]
    assert saved[CONF_POWER_PROBE_INTERVAL] == interval
    assert saved[CONF_UPDATE_INTERVAL] == 600
    data[CONF_POWER_PROBE_INTERVAL] = 9
    with pytest.raises(vol.Invalid):
        form["data_schema"](data)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["config", "options"])
async def test_real_serializer_reproduces_original_free_validator_failure(kind):
    _, step = make_flow(kind)
    form = await step()
    original_bug = vol.Schema({
        marker: _validate_drive_devices
        if marker.schema == CONF_DRIVE_DEVICES else validator
        for marker, validator in form["data_schema"].schema.items()
    })
    with pytest.raises(ValueError, match="Unable to convert schema"):
        convert(original_bug)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["config", "options"])
@pytest.mark.parametrize("devices", ["/dev/sda,/dev/sdc", " /dev/sda , /dev/sdc "])
async def test_submitted_drive_paths_are_normalized_before_saving(kind, devices):
    flow, step = make_flow(kind)
    form = await step()
    submitted = form["data_schema"](submitted_data(kind, devices))
    original = deepcopy(submitted)
    # The visible schema must not perform the custom path normalization.
    assert submitted[CONF_DRIVE_DEVICES] == devices
    result = await step(submitted)
    assert submitted == original
    saved = result["options"] if kind == "config" else result["data"]
    assert saved[CONF_DRIVE_DEVICES] == "/dev/sda,/dev/sdc"
    assert saved[CONF_SLEEP_AWARE_ENABLED] is True
    if kind == "options":
        assert saved["preserved_option"] == "kept"
        assert flow.config_entry.options[CONF_DRIVE_DEVICES] == "/dev/sdb"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["config", "options"])
@pytest.mark.parametrize("devices", [
    "", "   ", "/dev/sda1", "/dev/sda,/dev/sda", "/dev/sda,",
    "/dev/sda;id", "/dev/sda && id", "$(id)", "../dev/sda", "/tmp/disk",
])
async def test_invalid_submitted_paths_return_serializable_field_error(kind, devices):
    flow, step = make_flow(kind)
    form = await step()
    submitted = form["data_schema"](submitted_data(kind, devices))
    original = deepcopy(submitted)
    result = await step(submitted)
    assert result["errors"] == {CONF_DRIVE_DEVICES: "invalid_drive_devices"}
    assert result["step_id"] == ("user" if kind == "config" else "init")
    assert "data" not in result
    assert "options" not in result
    assert submitted == original
    fields = convert(result["data_schema"])
    json.dumps(fields)
    drive_field = next(item for item in fields if item["name"] == CONF_DRIVE_DEVICES)
    assert drive_field["default"] == devices
    if kind == "options":
        assert flow.config_entry.options[CONF_DRIVE_DEVICES] == "/dev/sdb"

    # Correcting the field completes the same flow without losing other options.
    submitted[CONF_DRIVE_DEVICES] = " /dev/sda, /dev/sdc "
    corrected = await step(submitted)
    saved = corrected["options"] if kind == "config" else corrected["data"]
    assert saved[CONF_DRIVE_DEVICES] == "/dev/sda,/dev/sdc"
    assert saved[CONF_SLEEP_AWARE_ENABLED] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["config", "options"])
@pytest.mark.parametrize("devices", [None, 42, ["/dev/sda"]])
async def test_non_text_direct_submission_is_a_field_error(kind, devices):
    _, step = make_flow(kind)
    result = await step(submitted_data(kind, devices))
    assert result["errors"] == {CONF_DRIVE_DEVICES: "invalid_drive_devices"}
    json.dumps(convert(result["data_schema"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["config", "options"])
async def test_disabled_sleep_awareness_does_not_bypass_path_validation(kind):
    _, step = make_flow(kind)
    data = submitted_data(kind, "/dev/sda;id")
    data[CONF_SLEEP_AWARE_ENABLED] = False
    result = await step(data)
    assert result["errors"] == {CONF_DRIVE_DEVICES: "invalid_drive_devices"}
