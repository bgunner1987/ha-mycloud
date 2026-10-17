"""Tests for non-waking hdparm parsing and command construction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import asyncssh
import asyncssh.connection as ssh_connection
import pytest

from custom_components.mycloud.const import PROBE_METHOD_SMARTCTL_SAT
from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    PowerProbeError,
    SSHPowerStateClient,
    build_smartctl_command,
    parse_drive_devices,
    parse_hdparm_state,
    parse_hdparm_states,
    parse_smartctl_states,
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


def hdparm_output(states):
    """Return realistic multi-device hdparm output in supplied order."""
    return "\n".join(
        f"{device}:\n drive state is:  {state}" for device, state in states.items()
    )


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (
            {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE},
            {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_ACTIVE},
        ),
        (
            {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_STANDBY},
            {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_STANDBY},
        ),
        (
            {"/dev/sdc": POWER_STANDBY, "/dev/sda": POWER_ACTIVE},
            {"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_STANDBY},
        ),
        (
            {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_ACTIVE},
            {"/dev/sda": POWER_UNKNOWN, "/dev/sdc": POWER_ACTIVE},
        ),
    ],
)
def test_parse_multi_device_hdparm_output_by_exact_header(states, expected):
    assert parse_hdparm_states(
        hdparm_output(states), ("/dev/sda", "/dev/sdc")
    ) == expected


@pytest.mark.parametrize(
    "output",
    [
        "/dev/sda:\n drive state is: active/idle",
        "/dev/sda:\n drive state is: active/idle\n/dev/sda:\n drive state is: standby",
        "/dev/sda:\n no state here\n/dev/sdc:\n drive state is: standby",
        (
            "/dev/sda:\n drive state is: active/idle\n drive state is: standby\n"
            "/dev/sdc:\n drive state is: standby"
        ),
        "/dev/sda:\n drive state is: active/idle\n/dev/sdb:\n drive state is: standby",
    ],
)
def test_parse_multi_device_hdparm_rejects_incomplete_or_ambiguous_output(output):
    with pytest.raises(ValueError):
        parse_hdparm_states(output, ("/dev/sda", "/dev/sdc"))


def smartctl_output(results):
    """Return framed output emitted by the fixed smartctl shell wrapper."""
    lines = []
    for device, (state_line, status) in results.items():
        lines.extend((
            f"__MYCLOUD_DISK__ {device}",
            "smartctl 7.2 synthetic test banner",
            state_line,
            f"__MYCLOUD_STATUS__ {status}",
        ))
    return "\n".join(lines)


def test_build_smartctl_command_uses_only_validated_paths():
    command = build_smartctl_command(("/dev/sda", "/dev/sdb"))

    for device in ("/dev/sda", "/dev/sdb"):
        assert f'"$mycloud_smartctl" -d sat -n standby,3 {device} 2>&1' in command
        assert f"test -b {device}" in command
        assert f"__MYCLOUD_DISK__ {device}" in command
    assert "/usr/sbin/smartctl /usr/bin/smartctl /sbin/smartctl /bin/smartctl" in command
    assert 'else mycloud_status=$?; fi' in command
    assert "\r" not in command
    assert " -a " not in command
    assert " --scan" not in command
    with pytest.raises(ValueError):
        build_smartctl_command(("/dev/sda; reboot",))


@pytest.mark.parametrize(
    "smart_state",
    ["ACTIVE or IDLE", "IDLE", "IDLE_A", "IDLE_B", "IDLE_C"],
)
def test_parse_smartctl_awake_states(smart_state):
    output = smartctl_output({
        "/dev/sda": (f"Device is in {smart_state} mode", 0),
    })
    assert parse_smartctl_states(output, ("/dev/sda",)) == {
        "/dev/sda": POWER_ACTIVE,
    }


@pytest.mark.parametrize(
    "smart_state",
    ["STANDBY", "STANDBY_Y", "STANDBY_Z", "SLEEP"],
)
def test_parse_smartctl_sleeping_states(smart_state):
    output = smartctl_output({
        "/dev/sda": (f"Device is in {smart_state} mode, exit(3)", 3),
    })
    assert parse_smartctl_states(output, ("/dev/sda",)) == {
        "/dev/sda": POWER_STANDBY,
    }


@pytest.mark.parametrize(
    "message",
    [
        "CHECK POWER MODE returned unknown value 0x81, ignoring -n option",
        "CHECK POWER MODE not implemented, ignoring -n option",
    ],
)
def test_parse_smartctl_explicit_unknown_is_fail_closed(message):
    output = smartctl_output({"/dev/sda": (message, 0)})
    assert parse_smartctl_states(output, ("/dev/sda",)) == {
        "/dev/sda": POWER_UNKNOWN,
    }


def test_parse_smartctl_matches_devices_by_exact_framed_path():
    output = smartctl_output({
        "/dev/sdb": ("Device is in STANDBY mode, exit(3)", 3),
        "/dev/sda": ("Device is in IDLE_A mode", 0),
    })
    assert parse_smartctl_states(output, ("/dev/sda", "/dev/sdb")) == {
        "/dev/sda": POWER_ACTIVE,
        "/dev/sdb": POWER_STANDBY,
    }


@pytest.mark.parametrize(
    "output",
    [
        "Device is in IDLE_A mode\n__MYCLOUD_STATUS__ 0",
        "__MYCLOUD_DISK__ /dev/sda\nDevice is in IDLE_A mode",
        (
            "__MYCLOUD_DISK__ /dev/sda\nDevice is in IDLE_A mode\n"
            "Device is in STANDBY mode, exit(3)\n__MYCLOUD_STATUS__ 0"
        ),
        smartctl_output({
            "/dev/sda": ("Device is in IDLE_A mode, exit(3)", 3),
        }),
        smartctl_output({
            "/dev/sda": ("Device is in STANDBY mode, exit(3)", 0),
        }),
        smartctl_output({
            "/dev/sda": ("Device is in VENDOR_PRIVATE mode", 0),
        }),
    ],
)
def test_parse_smartctl_rejects_incomplete_ambiguous_or_inconsistent_output(output):
    with pytest.raises(ValueError):
        parse_smartctl_states(output, ("/dev/sda",))


def test_parse_smartctl_non_power_exit_is_a_safe_command_failure():
    output = smartctl_output({
        "/dev/sda": ("smartctl: permission denied", 2),
    })
    with pytest.raises(PowerProbeError) as error:
        parse_smartctl_states(output, ("/dev/sda",))
    failure = describe_probe_error(error.value)
    assert failure.error_type == "command_failed"
    assert failure.probe_type == "smartctl_sat_power_state"
    assert "exit_status=2" in failure.summary


class FakeProcess:
    def __init__(self, connection, command):
        self.connection = connection
        self.command = command
        self.closed = False
        self.terminated = False
        self.wait_closed_called = False

    async def wait(self, check=False):
        return await self.connection.run(self.command, check=check)

    def terminate(self):
        self.terminated = True

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.wait_closed_called = True


class FakeConnection:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.commands = []
        self.processes = []
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

    async def create_process(self, command):
        process = FakeProcess(self, command)
        self.processes.append(process)
        return process

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def use_connections(monkeypatch, *connections):
    """Return supplied fake connections from the real connect boundary."""
    pending = iter(connections)

    async def connect(*args, **kwargs):
        return next(pending)

    monkeypatch.setattr(asyncssh, "connect", connect)


@pytest.mark.asyncio
async def test_probe_runs_only_fixed_hdparm_commands(monkeypatch):
    connection = FakeConnection(
        [hdparm_output({"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_ACTIVE})]
    )
    client = SSHPowerStateClient(
        "nas", 22, "root", "not-a-credential", ("/dev/sda", "/dev/sdc")
    )
    use_connections(monkeypatch, connection)

    states = await client.async_check()

    assert states == {"/dev/sda": POWER_STANDBY, "/dev/sdc": POWER_ACTIVE}
    assert connection.commands == ["/usr/bin/hdparm -C /dev/sda /dev/sdc"]
    assert connection.closed
    assert connection.processes[0].closed
    assert connection.processes[0].wait_closed_called
    assert not connection.processes[0].terminated
    assert client._connection is None
    assert client._process is None


@pytest.mark.asyncio
async def test_probe_runs_fixed_smartctl_wrapper_and_reports_probe_type(monkeypatch):
    connection = FakeConnection([smartctl_output({
        "/dev/sda": ("Device is in IDLE_A mode", 0),
        "/dev/sdb": ("Device is in STANDBY mode, exit(3)", 3),
    })])
    client = SSHPowerStateClient(
        "nas",
        22,
        "root",
        "not-a-credential",
        ("/dev/sda", "/dev/sdb"),
        probe_method=PROBE_METHOD_SMARTCTL_SAT,
    )
    use_connections(monkeypatch, connection)

    states = await client.async_check()

    assert states == {"/dev/sda": POWER_ACTIVE, "/dev/sdb": POWER_STANDBY}
    assert connection.commands == [
        build_smartctl_command(("/dev/sda", "/dev/sdb"))
    ]
    assert client.probe_type == "smartctl_sat_power_state"
    assert connection.closed
    assert client._connection is None
    assert client._process is None


@pytest.mark.asyncio
async def test_smartctl_parser_failure_keeps_allowlisted_probe_diagnostics(monkeypatch):
    connection = FakeConnection(["synthetic malformed output"])
    client = SSHPowerStateClient(
        "nas",
        22,
        "root",
        "not-a-credential",
        ("/dev/sda",),
        probe_method=PROBE_METHOD_SMARTCTL_SAT,
    )
    use_connections(monkeypatch, connection)

    with pytest.raises(PowerProbeError) as error:
        await client.async_check()

    failure = describe_probe_error(error.value)
    assert failure.error_type == "parse_failed"
    assert failure.probe_type == "smartctl_sat_power_state"
    assert "unrecognized_smartctl_output" in failure.summary


def test_probe_rejects_unknown_method_before_any_ssh_use():
    with pytest.raises(ValueError, match="Unsupported power probe method"):
        SSHPowerStateClient(
            "nas", 22, "root", "not-a-credential", ("/dev/sda",),
            probe_method="untrusted-method",
        )


class LocalSSHServer(asyncssh.SSHServer):
    """No-auth test server bound only to loopback, never a real NAS."""

    def begin_auth(self, username):
        return False


async def start_local_server(key, commands):
    def handle_process(process):
        # Record the request, but never execute any command on the test host.
        commands.append(process.command)
        devices = process.command.split()[2:]
        process.stdout.write(
            hdparm_output({device: POWER_ACTIVE for device in devices}) + "\n"
        )
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
        assert client._connection is None
        await client.async_close()
        # A newly created client simulates using the persisted TOFU pin.
        client = SSHPowerStateClient(
            "127.0.0.1", server.get_port(), "test", "unused-test-value",
            ("/dev/sda", "/dev/sdc"), expected_fingerprint=pin, timeout=3,
        )
        await client.async_check()
        assert client.fingerprint == pin
        assert client._connection is None
        assert commands == ["/usr/bin/hdparm -C /dev/sda /dev/sdc"] * 2
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
    (1, "/dev/sda:\n drive state is: active/idle", "command_failed"),
    (0, "synthetic-private-detail", "parse_failed"),
])
async def test_command_and_parser_failure_are_classified(
    monkeypatch, exit_status, stdout, expected
):
    connection = FakeConnection([])

    async def run(*args, **kwargs):
        return SimpleNamespace(
            exit_status=exit_status, stdout=stdout, stderr="synthetic-private-stderr",
        )

    connection.run = run
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    use_connections(monkeypatch, connection)
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
async def test_command_timeout_is_not_masked_by_cleanup_error(monkeypatch):
    connection = FakeConnection([])

    async def run(*args, **kwargs):
        raise TimeoutError("synthetic-private-detail")

    async def wait_closed():
        raise RuntimeError("synthetic-private-cleanup")

    connection.run = run
    connection.wait_closed = wait_closed
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    use_connections(monkeypatch, connection)
    with pytest.raises(PowerProbeError) as error:
        await client.async_check()
    failure = describe_probe_error(error.value)
    assert failure.error_type == "timeout"
    assert failure.stage == "command"
    assert failure.probe_type == "hdparm_power_state"
    assert failure.duration_seconds is not None
    assert "synthetic-private" not in failure.summary
    assert connection.processes[0].closed
    assert connection.processes[0].wait_closed_called
    assert client._process is None


@pytest.mark.asyncio
async def test_hanging_command_timeout_cancels_wait_and_closes_all_resources(
    monkeypatch,
):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    connection = FakeConnection([])

    async def run(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    connection.run = run
    client = SSHPowerStateClient(
        "nas", 22, "test", "unused-test-value", ("/dev/sda",), timeout=0.01
    )
    use_connections(monkeypatch, connection)

    with pytest.raises(PowerProbeError) as error:
        await client.async_check()

    failure = describe_probe_error(error.value)
    process = connection.processes[0]
    assert entered.is_set()
    assert cancelled.is_set()
    assert failure.error_type == "timeout"
    assert failure.stage == "command"
    assert failure.duration_seconds is not None
    assert process.terminated
    assert process.closed
    assert process.wait_closed_called
    assert connection.closed
    assert client._process is None
    assert client._connection is None


@pytest.mark.asyncio
async def test_explicit_hdparm_unknown_is_not_a_parser_error(monkeypatch):
    connection = FakeConnection([hdparm_output({"/dev/sda": POWER_UNKNOWN})])
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    use_connections(monkeypatch, connection)
    assert await client.async_check() == {"/dev/sda": POWER_UNKNOWN}
    assert connection.closed
    assert client._connection is None


@pytest.mark.asyncio
async def test_two_probes_use_two_distinct_short_lived_connections(monkeypatch):
    connections = [
        FakeConnection([hdparm_output({"/dev/sda": POWER_ACTIVE})]),
        FakeConnection([hdparm_output({"/dev/sda": POWER_ACTIVE})]),
    ]
    use_connections(monkeypatch, *connections)
    client = SSHPowerStateClient(
        "nas", 22, "test", "unused-test-value", ("/dev/sda",)
    )

    await client.async_check()
    await client.async_check()

    assert connections[0] is not connections[1]
    assert all(connection.closed for connection in connections)
    assert [connection.commands for connection in connections] == [
        ["/usr/bin/hdparm -C /dev/sda"],
        ["/usr/bin/hdparm -C /dev/sda"],
    ]
    assert client._connection is None


@pytest.mark.asyncio
async def test_probe_succeeds_when_server_rejects_a_second_exec_channel(monkeypatch):
    connection = FakeConnection(
        [hdparm_output({"/dev/sda": POWER_ACTIVE, "/dev/sdc": POWER_STANDBY})]
    )
    calls = 0
    original_run = connection.run

    async def reject_second_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncssh.ChannelOpenError(1, "second exec rejected")
        return await original_run(*args, **kwargs)

    connection.run = reject_second_run
    use_connections(monkeypatch, connection)
    client = SSHPowerStateClient(
        "nas", 22, "test", "unused-test-value", ("/dev/sda", "/dev/sdc")
    )

    assert await client.async_check() == {
        "/dev/sda": POWER_ACTIVE,
        "/dev/sdc": POWER_STANDBY,
    }
    assert calls == 1
    assert connection.closed


@pytest.mark.asyncio
async def test_cleanup_timeout_aborts_connection_and_fails_probe(monkeypatch):
    never_closed = asyncio.Event()
    connection = FakeConnection([hdparm_output({"/dev/sda": POWER_ACTIVE})])
    connection.aborted = False

    async def wait_closed():
        await never_closed.wait()

    def abort():
        connection.aborted = True

    connection.wait_closed = wait_closed
    connection.abort = abort
    use_connections(monkeypatch, connection)
    client = SSHPowerStateClient(
        "nas", 22, "test", "unused-test-value", ("/dev/sda",), timeout=0.01
    )

    with pytest.raises(PowerProbeError) as error:
        await client.async_check()

    failure = describe_probe_error(error.value)
    assert failure.error_type == "timeout"
    assert failure.stage == "cleanup"
    assert connection.aborted
    assert client._connection is None


@pytest.mark.asyncio
async def test_cancellation_closes_probe_connection(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    connection = FakeConnection([])

    async def run(*args, **kwargs):
        entered.set()
        await release.wait()

    connection.run = run
    use_connections(monkeypatch, connection)
    client = SSHPowerStateClient(
        "nas", 22, "test", "unused-test-value", ("/dev/sda",)
    )
    task = asyncio.create_task(client.async_check())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.closed
    assert connection.processes[0].closed
    assert client._connection is None
    assert client._process is None


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
async def test_probe_lock_prevents_overlapping_commands(monkeypatch):
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
                exit_status=0,
                stdout=hdparm_output({"/dev/sda": POWER_ACTIVE}),
                stderr="",
            )

    connections = [SlowConnection([]), SlowConnection([])]
    use_connections(monkeypatch, *connections)
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    first = asyncio.create_task(client.async_check())
    await entered.wait()
    second = asyncio.create_task(client.async_check())
    await asyncio.sleep(0)
    assert maximum == 1
    release.set()
    await first
    await second
    assert maximum == 1
    assert all(connection.closed for connection in connections)
    assert client._connection is None


@pytest.mark.asyncio
async def test_connection_failure_does_not_create_an_immediate_login_retry(monkeypatch):
    attempts = 0

    async def fail_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionRefusedError("synthetic-private-detail")

    monkeypatch.setattr(asyncssh, "connect", fail_connect)
    client = SSHPowerStateClient("nas", 22, "test", "unused-test-value", ("/dev/sda",))
    with pytest.raises(PowerProbeError):
        await client.async_check()
    assert attempts == 1


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
