"""Tests for configuring sleep-aware behavior before the first poll."""

import pytest

from custom_components.mycloud.config_flow import MyCloudConfigFlow
from custom_components.mycloud.const import (
    CONF_DRIVE_DEVICES,
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
