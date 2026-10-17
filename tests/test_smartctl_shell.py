"""Execute the entire generated command in a POSIX shell and real AsyncSSH.

Only disk/program availability and the smartctl executable are simulated. The
command, quoting, redirection, markers, statuses, SSH client, parser and API gate
are production code. No test accesses a real block device.
"""

import asyncio
import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import asyncssh
import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_coordinator import FakeAPI, make_coordinator
from test_power_probe import LocalSSHServer

from custom_components.mycloud.const import PROBE_METHOD_SMARTCTL_SAT
from custom_components.mycloud.power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    PowerProbeError,
    SSHPowerStateClient,
    build_smartctl_command,
    parse_smartctl_states,
)
from custom_components.mycloud.probe_diagnostics import describe_probe_error

DEVICES = ("/dev/sda", "/dev/sdb")
FIXTURES = json.loads((Path(__file__).parent / "fixtures/smartctl_sat.json").read_text())
SHELL = shutil.which("sh")
if SHELL is None and os.name == "nt":
    candidate = Path("C:/Program Files/Git/usr/bin/sh.exe")
    SHELL = str(candidate) if candidate.is_file() else None
pytestmark = pytest.mark.skipif(SHELL is None, reason="POSIX shell required")


def shell_script(tmp_path, cases, *, errexit=False):
    """Hermetic smartctl replacement: exact argv, no device I/O of any kind."""
    program = tmp_path / "smartctl"
    branches = []
    missing = []
    program_missing = any(FIXTURES[case].get("program_missing") for case in cases)
    for device, case in zip(DEVICES, cases, strict=True):
        fixture = FIXTURES[case]
        output = fixture["output"].replace("'", "'\"'\"'")
        redirection = " >&2" if fixture.get("stderr") else ""
        branches.append(
            f"{device}) printf '%s' '{output}'{redirection}; "
            f"exit {fixture['exit_status']} ;;"
        )
        if fixture.get("missing"):
            missing.append(device)
    program.write_text(
        "#!/bin/sh\n"
        '[ "$#" = 5 ] && [ "$1 $2 $3 $4" = "-d sat -n standby,3" ] || exit 99\n'
        'case "$5" in\n' + "\n".join(branches) + "\n*) exit 99 ;;\nesac\n",
        encoding="utf-8", newline="\n",
    )
    program.chmod(0o700)
    prefix_path = tmp_path.as_posix()
    if os.name == "nt":
        prefix_path = "/" + prefix_path[0].lower() + prefix_path[2:]
    # Force all absolute program candidates unavailable in the test sandbox;
    # fallback resolves ONLY our fixture. Never execute a host's real smartctl.
    missing_test = "|".join(missing) or "__none__"
    prefix = (
        ("set -e\n" if errexit else "")
        + f"PATH='{prefix_path if not program_missing else '/__mycloud_empty_path__'}'\n"
        + 'test() { case "$1" in -x) return 1 ;; '
        + f'-b) case "$2" in {missing_test}) return 1 ;; '
        + '/dev/sda|/dev/sdb) return 0 ;; *) return 1 ;; esac ;; '
        + '*) command test "$@" ;; esac; }\n'
    )
    return prefix


def run_shell(prefix, command):
    return subprocess.run(
        [SHELL, "-s"], input=prefix + command + "\n", text=True,
        capture_output=True, timeout=5, check=False,
    )


@pytest.mark.parametrize("errexit", [False, True])
@pytest.mark.parametrize("cases,expected", [
    (("idle_a", "idle_a"), (POWER_ACTIVE, POWER_ACTIVE)),
    (("idle_a_stderr", "idle_a"), (POWER_ACTIVE, POWER_ACTIVE)),
    (("standby", "standby"), (POWER_STANDBY, POWER_STANDBY)),
    (("idle_a", "standby"), (POWER_ACTIVE, POWER_STANDBY)),
    (("unknown", "idle_a"), (POWER_UNKNOWN, POWER_ACTIVE)),
])
def test_generated_shell_command_frames_both_results(tmp_path, cases, expected, errexit):
    result = run_shell(shell_script(tmp_path, cases, errexit=errexit), build_smartctl_command(DEVICES))
    assert result.returncode == 0
    assert result.stderr == ""
    diagnostics = {}
    assert parse_smartctl_states(result.stdout, DEVICES, diagnostics=diagnostics) == dict(zip(DEVICES, expected))
    assert [diagnostics[d]["exit_status"] for d in DEVICES] == [FIXTURES[c]["exit_status"] for c in cases]
    # CRLF transport is accepted without loosening marker matching.
    assert parse_smartctl_states(result.stdout.replace("\n", "\r\n"), DEVICES) == dict(zip(DEVICES, expected))


@pytest.mark.parametrize("case,error_type,status", [
    ("missing", "device_missing", 2),
    ("program_missing", "program_missing", 127),
    ("command_failed", "command_failed", 2),
])
def test_generated_shell_command_keeps_per_device_failure(tmp_path, case, error_type, status):
    result = run_shell(shell_script(tmp_path, ("idle_a", case)), build_smartctl_command(DEVICES))
    assert result.returncode == 0  # wrapper status is not a disk's status
    diagnostics = {}
    with pytest.raises(PowerProbeError) as caught:
        parse_smartctl_states(result.stdout, DEVICES, diagnostics=diagnostics)
    assert describe_probe_error(caught.value).error_type == error_type
    assert diagnostics["/dev/sdb"]["exit_status"] == status
    assert diagnostics["/dev/sdb"]["state"] == POWER_UNKNOWN
    assert "synthetic-private-output" not in json.dumps(diagnostics)


def test_old_wrapper_reproduces_missing_path_and_errexit_failures(tmp_path):
    old_command = "\n".join(
        f"printf '__MYCLOUD_DISK__ {device}\\n'\n"
        f"LC_ALL=C smartctl -d sat -n standby,3 {device} 2>&1\n"
        "printf '__MYCLOUD_STATUS__ %s\\n' \"$?\""
        for device in DEVICES
    )
    missing = run_shell("PATH=/__mycloud_empty_path__\n", old_command)
    assert missing.returncode == 0
    assert missing.stdout.count("__MYCLOUD_STATUS__ 127") == 2
    standby = run_shell(shell_script(tmp_path, ("standby", "standby"), errexit=True), old_command)
    assert standby.returncode == 3
    assert "__MYCLOUD_STATUS__" not in standby.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("cases,allowed,reason", [
    (("idle_a", "idle_a"), True, None),
    (("standby", "standby"), False, "standby"),
    (("idle_a", "standby"), False, "mixed"),
    (("unknown", "idle_a"), False, "unknown"),
    (("idle_a", "missing"), False, "probe_error"),
    (("program_missing", "program_missing"), False, "probe_error"),
    (("command_failed", "idle_a"), False, "probe_error"),
])
async def test_real_ssh_shell_result_to_first_api_snapshot(tmp_path, cases, allowed, reason):
    prefix = shell_script(tmp_path, cases, errexit=True)
    commands = []

    async def handle(process):
        commands.append(process.command)
        result = await asyncio.to_thread(run_shell, prefix, process.command)
        process.stdout.write(result.stdout)
        process.stderr.write(result.stderr)
        process.exit(result.returncode)

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=LocalSSHServer,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=handle,
    )
    client = SSHPowerStateClient(
        "127.0.0.1", server.get_port(), "test", "unused-test-value", DEVICES,
        probe_method=PROBE_METHOD_SMARTCTL_SAT, timeout=3,
    )
    api = FakeAPI()
    coordinator, _ = make_coordinator(api, client, with_cache=False)
    try:
        if allowed:
            result = await coordinator._async_update_data()
            assert result["data_stale"] is False
            assert result["last_full_update"]
            assert coordinator.runtime_diagnostics["api_poll_decision"] == "snapshot_updated"
            for method in ("system_info", "system_status", "device_info", "system_version"):
                assert api.calls.count(method) == 1
            await coordinator._async_update_data()
            assert api.calls.count("system_info") == 1
            assert coordinator.runtime_diagnostics["api_poll_decision"] == "interval_wait"
        else:
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
            assert api.calls == []
        assert coordinator.api_poll_allowed is allowed
        assert coordinator.api_block_reason == reason
        assert all(c == build_smartctl_command(DEVICES) for c in commands)
        assert client.last_command_exit_status == 0
        assert set(client.device_diagnostics) == set(DEVICES)
        assert client._connection is None and client._process is None
    finally:
        await coordinator.async_shutdown()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_standby_then_idle_a_refreshes_cache_from_full_ssh_path(tmp_path):
    cases = ("standby", "standby")

    async def handle(process):
        result = await asyncio.to_thread(run_shell, shell_script(tmp_path, cases), process.command)
        process.stdout.write(result.stdout)
        process.exit(result.returncode)

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=LocalSSHServer,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")], process_factory=handle,
    )
    client = SSHPowerStateClient("127.0.0.1", server.get_port(), "test", "", DEVICES, probe_method=PROBE_METHOD_SMARTCTL_SAT)
    api = FakeAPI()
    coordinator, _ = make_coordinator(api, client)
    try:
        stale = await coordinator._async_update_data()
        assert api.calls == []
        cases = ("idle_a", "idle_a")
        fresh = await coordinator._async_update_data()
        assert fresh["last_full_update"] != stale["last_full_update"]
        assert not fresh["data_stale"]
        assert api.calls.count("system_info") == 1
        await coordinator._async_update_data()
        assert api.calls.count("system_info") == 1
        assert coordinator._api_update_interval == timedelta(seconds=600)
    finally:
        await coordinator.async_shutdown()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_disk", "missing_status", "corrupt_marker", "stderr", "process_exit"])
async def test_damaged_ssh_protocol_never_opens_api(tmp_path, damage):
    prefix = shell_script(tmp_path, ("idle_a", "idle_a"))

    async def handle(process):
        result = await asyncio.to_thread(run_shell, prefix, process.command)
        output = result.stdout
        if damage == "missing_disk":
            output = output.replace("__MYCLOUD_DISK__ /dev/sdb", "")
        elif damage == "missing_status":
            output = output.replace("__MYCLOUD_STATUS__ 0", "", 1)
        elif damage == "corrupt_marker":
            output = output.replace("__MYCLOUD_DISK__", "__DAMAGED__", 1)
        elif damage == "stderr":
            process.stderr.write("synthetic-private-output")
        process.stdout.write(output)
        process.exit(1 if damage == "process_exit" else 0)

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=LocalSSHServer,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")], process_factory=handle,
    )
    client = SSHPowerStateClient("127.0.0.1", server.get_port(), "test", "", DEVICES, probe_method=PROBE_METHOD_SMARTCTL_SAT)
    api = FakeAPI()
    coordinator, _ = make_coordinator(api, client)
    try:
        result = await coordinator._async_update_data()
        assert result["data_stale"]
        assert coordinator.api_block_reason == "probe_error"
        assert coordinator.power_probe_error_type == ("command_failed" if damage == "process_exit" else "parse_failed")
        assert set(coordinator.power_states.values()) == {POWER_UNKNOWN}
        assert api.calls == []
        assert "synthetic-private-output" not in json.dumps(coordinator.runtime_diagnostics)
        assert client._connection is None and client._process is None
    finally:
        await coordinator.async_shutdown()
        server.close()
        await server.wait_closed()
