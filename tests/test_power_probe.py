"""Tests for non-waking hdparm parsing and command construction."""

from __future__ import annotations

import asyncio
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
from custom_components.mycloud.probe_diagnostics import describe_probe_error


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
            with pytest.raises(PowerProbeError, match="host-key validation failed") as error:
                await client.async_check()
            assert describe_probe_error(error.value).error_type == "host_key_mismatch"
            assert commands == []
            assert client.fingerprint == different_pin
    finally:
        await client.async_close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause,expected", [
    (asyncssh.PermissionDenied("synthetic-private-detail"), "authentication_failed"),
    (asyncssh.KeyExchangeFailed("synthetic-private-detail"), "algorithm_negotiation_failed"),
    (ConnectionRefusedError("synthetic-private-detail"), "connection_failed"),
    (TimeoutError("synthetic-private-detail"), "timeout"),
])
async def test_connect_preserves_classifiable_cause(monkeypatch, cause, expected):
    async def fail_connect(*args, **kwargs):
        raise cause

    monkeypatch.setattr(asyncssh, "connect", fail_connect)
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    with pytest.raises(PowerProbeError) as error:
        await client.async_check()
    failure = describe_probe_error(error.value)
    assert failure.error_type == expected
    assert failure.stage == "connect"
    assert error.value.__cause__ is cause
    assert "synthetic-private-detail" not in failure.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_status,stdout,expected", [
    (127, "synthetic-private-detail", "command_failed"),
    (1, "drive state is: active/idle", "command_failed"),
    (0, "synthetic-private-detail", "parse_failed"),
])
async def test_command_and_parser_failure_are_classified(exit_status, stdout, expected):
    connection = FakeConnection([])

    async def run(*args, **kwargs):
        return SimpleNamespace(
            exit_status=exit_status, stdout=stdout, stderr="synthetic-private-stderr",
        )

    connection.run = run
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    client._connection = connection
    with pytest.raises(PowerProbeError) as error:
        await client.async_check()
    failure = describe_probe_error(error.value)
    assert failure.error_type == expected
    assert failure.stage == "command"
    assert "synthetic-private" not in failure.summary
    assert connection.closed
    if exit_status:
        assert f"exit_status={exit_status}" in failure.summary


@pytest.mark.asyncio
async def test_command_timeout_is_not_masked_by_cleanup_error():
    connection = FakeConnection([])

    async def run(*args, **kwargs):
        raise TimeoutError("synthetic-private-detail")

    async def wait_closed():
        raise RuntimeError("synthetic-private-cleanup")

    connection.run = run
    connection.wait_closed = wait_closed
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    client._connection = connection
    with pytest.raises(PowerProbeError) as error:
        await client.async_check()
    failure = describe_probe_error(error.value)
    assert failure.error_type == "timeout"
    assert failure.stage == "command"
    assert "synthetic-private" not in failure.summary


@pytest.mark.asyncio
async def test_explicit_hdparm_unknown_is_not_a_parser_error():
    connection = FakeConnection(["drive state is: unknown"])
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    client._connection = connection
    assert await client.async_check() == {"/dev/sda": POWER_UNKNOWN}


@pytest.mark.asyncio
async def test_real_asyncssh_authentication_failure_is_classified():
    class RejectSSHServer(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return False

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=RejectSSHServer,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
    )
    client = SSHPowerStateClient(
        "127.0.0.1", server.get_port(), "test", "unused-test-value", ("/dev/sda",), timeout=3,
    )
    try:
        with pytest.raises(PowerProbeError) as error:
            await client.async_check()
        failure = describe_probe_error(error.value)
        assert failure.error_type == "authentication_failed"
        assert any("PermissionDenied" in node for node in failure.chain)
        assert "unused-test-value" not in failure.summary
    finally:
        await client.async_close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_real_asyncssh_algorithm_negotiation_failure_is_classified(monkeypatch):
    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=LocalSSHServer,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        kex_algs=["curve25519-sha256"],
    )
    connect = asyncssh.connect

    def incompatible_connect(*args, **kwargs):
        # Two modern, deliberately disjoint test sets. Production is unchanged.
        return connect(*args, **kwargs, kex_algs=["ecdh-sha2-nistp256"])

    monkeypatch.setattr(asyncssh, "connect", incompatible_connect)
    client = SSHPowerStateClient(
        "127.0.0.1", server.get_port(), "test", "unused-test-value", ("/dev/sda",), timeout=3,
    )
    try:
        with pytest.raises(PowerProbeError) as error:
            await client.async_check()
        failure = describe_probe_error(error.value)
        assert failure.error_type == "algorithm_negotiation_failed"
        assert "no_matching_key_exchange" in failure.summary
        assert "unused-test-value" not in failure.summary
    finally:
        await client.async_close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_lock_prevents_overlapping_commands():
    active = 0
    maximum = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowConnection(FakeConnection):
        async def run(self, command, check=False):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1
            return SimpleNamespace(
                exit_status=0, stdout="drive state is: active/idle", stderr=""
            )

    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    client._connection = SlowConnection([])
    first = asyncio.create_task(client.async_check())
    await entered.wait()
    second = asyncio.create_task(client.async_check())
    await asyncio.sleep(0)
    assert maximum == 1
    release.set()
    await first
    await second
    assert maximum == 1


@pytest.mark.asyncio
async def test_connection_failure_gets_only_one_controlled_reconnect(monkeypatch):
    attempts = 0

    async def fail_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("synthetic-private-detail")

    monkeypatch.setattr(asyncssh, "connect", fail_connect)
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    with pytest.raises(PowerProbeError):
        await client.async_check()
    assert attempts == 2


@pytest.mark.asyncio
async def test_authentication_and_host_key_failures_are_not_retried(monkeypatch):
    for cause in (
        asyncssh.PermissionDenied("synthetic-private-detail"),
        asyncssh.HostKeyNotVerifiable("synthetic-private-detail"),
    ):
        attempts = 0
        current_cause = cause

        async def fail_connect(*args, error=current_cause, **kwargs):
            nonlocal attempts
            attempts += 1
            raise error

        monkeypatch.setattr(asyncssh, "connect", fail_connect)
        client = SSHPowerStateClient(
            "nas", 22, "test", "unused-test-value", ("/dev/sda",)
        )
        with pytest.raises(PowerProbeError):
            await client.async_check()
        assert attempts == 1
