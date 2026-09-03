"""Tests for non-waking hdparm parsing and command construction."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    SSHPowerStateClient,
    parse_drive_devices,
    parse_hdparm_state,
)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("/dev/sda:\n drive state is:  standby", POWER_STANDBY),
        ("/dev/sdc:\n drive state is:  active/idle", POWER_ACTIVE),
        ("/dev/sda:\n drive state is:  unknown", POWER_UNKNOWN),
        ("unexpected output", POWER_UNKNOWN),
    ],
)
def test_parse_hdparm_state(output, expected):
    assert parse_hdparm_state(output) == expected


def test_drive_devices_reject_shell_input():
    with pytest.raises(ValueError):
        parse_drive_devices("/dev/sda; reboot")


class FakeConnection:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.commands = []
        self.closed = False

    def is_closed(self):
        return self.closed

    async def run(self, command, check=False):
        self.commands.append(command)
        return SimpleNamespace(
            exit_status=0,
            stdout=next(self.outputs),
            stderr="",
        )

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_probe_runs_only_fixed_hdparm_commands():
    connection = FakeConnection(
        ["drive state is: standby", "drive state is: active/idle"]
    )
    client = SSHPowerStateClient(
        "nas", 22, "root", "not-a-credential", ("/dev/sda", "/dev/sdc")
    )
    client._connection = connection

    states = await client.async_check()

    assert states == {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_ACTIVE}
    assert connection.commands == [
        "/usr/bin/hdparm -C /dev/sda",
        "/usr/bin/hdparm -C /dev/sdc",
    ]
