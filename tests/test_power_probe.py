"""Tests for non-waking hdparm parsing and command construction."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import asyncssh
import asyncssh.connection as ssh_connection
import pytest

from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    PowerProbeError,
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


class LocalSSHServer(asyncssh.SSHServer):
    """No-auth test server bound only to loopback, never a real NAS."""

    def begin_auth(self, username):
        return False


async def start_local_server(key, commands):
    def handle_process(process):
        # Record the request, but never execute any command on the test host.
        commands.append(process.command)
        process.stdout.write("drive state is: active/idle\n")
        process.exit(0)

    return await asyncssh.listen(
        "127.0.0.1", 0, server_factory=LocalSSHServer,
        server_host_keys=[key], process_factory=handle_process,
    )


@pytest.mark.asyncio
async def test_real_asyncssh_tofu_then_pinned_reconnect():
    key = asyncssh.generate_private_key("ssh-ed25519")
    commands = []
    server = await start_local_server(key, commands)
    client = SSHPowerStateClient(
        "127.0.0.1", server.get_port(), "test", "unused-test-value",
        ("/dev/sda", "/dev/sdc"), timeout=3,
    )
    try:
        assert await client.async_check() == {
            "/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE,
        }
        pin = client.fingerprint
        assert pin == key.get_fingerprint("sha256")
        await client.async_close()
        # A newly created client simulates using the persisted TOFU pin.
        client = SSHPowerStateClient(
            "127.0.0.1", server.get_port(), "test", "unused-test-value",
            ("/dev/sda", "/dev/sdc"), expected_fingerprint=pin, timeout=3,
        )
        await client.async_check()
        assert client.fingerprint == pin
        assert commands == [
            "/usr/bin/hdparm -C /dev/sda", "/usr/bin/hdparm -C /dev/sdc",
        ] * 2
    finally:
        await client.async_close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_empty_bytes", [False, True])
async def test_real_asyncssh_pin_cannot_be_bypassed_by_ambient_known_hosts(
    monkeypatch, tmp_path, old_empty_bytes
):
    key = asyncssh.generate_private_key("ssh-ed25519")
    different_pin = asyncssh.generate_private_key("ssh-ed25519").get_fingerprint("sha256")
    commands = []
    server = await start_local_server(key, commands)
    ambient_file = tmp_path / "known_hosts"
    # Only an ephemeral public key is written, never a real key or credential.
    ambient_file.write_text(
        f"[127.0.0.1]:{server.get_port()} " + key.export_public_key().decode(),
        encoding="utf-8",
    )

    def test_path(*parts):
        if parts == ("~", ".ssh", "known_hosts"):
            return ambient_file
        return Path(*parts)

    monkeypatch.setattr(ssh_connection, "Path", test_path)
    if old_empty_bytes:
        # Negative control: reproduce the old argument with REAL AsyncSSH.
        monkeypatch.setattr(asyncssh, "import_known_hosts", lambda _: b"")
    client = SSHPowerStateClient(
        "127.0.0.1", server.get_port(), "test", "unused-test-value",
        ("/dev/sda",), expected_fingerprint=different_pin, timeout=3,
    )
    try:
        if old_empty_bytes:
            await client.async_check()  # Old behavior incorrectly trusts the file.
            assert commands == ["/usr/bin/hdparm -C /dev/sda"]
        else:
            with pytest.raises(PowerProbeError, match="host-key validation failed"):
                await client.async_check()
            assert commands == []
            assert client.fingerprint == different_pin
    finally:
        await client.async_close()
        server.close()
        await server.wait_closed()
