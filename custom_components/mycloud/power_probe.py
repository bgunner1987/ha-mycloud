"""Non-waking SSH power-state probe for WD My Cloud disks."""

from __future__ import annotations

import asyncio
import hmac
import re
from collections.abc import Callable, Sequence
from typing import Any

import asyncssh

from .const import (
    DEFAULT_POWER_PROBE_METHOD,
    PROBE_METHOD_HDPARM,
    PROBE_METHOD_SMARTCTL_SAT,
)
from .probe_diagnostics import PowerProbeError

HDPARM_PATH = "/usr/bin/hdparm"
POWER_ACTIVE = "active/idle"
POWER_STANDBY = "standby"
POWER_UNKNOWN = "unknown"

_DRIVE_DEVICE_PATTERN = re.compile(r"^/dev/(?:sd|hd)[a-z]+$")
_STATE_PATTERN = re.compile(
    r"drive\s+state\s+is\s*:\s*(standby|active/idle|unknown)\b",
    re.IGNORECASE,
)
_DEVICE_HEADER_PATTERN = re.compile(r"^\s*(/dev/(?:sd|hd)[a-z]+):\s*$")
_SMARTCTL_DEVICE = "__MYCLOUD_DISK__ "
_SMARTCTL_STATUS = "__MYCLOUD_STATUS__ "
_SMARTCTL_ERROR = "__MYCLOUD_ERROR__ "
_SMARTCTL_STATE = re.compile(r"^Device is in (.+) mode(?:, exit\((\d+)\))?$")
_SMARTCTL_UNKNOWN = re.compile(
    r"^CHECK POWER MODE (?:returned unknown value 0x[0-9a-fA-F]+|not implemented), "
    r"ignoring -n option$"
)
_SMARTCTL_AWAKE = frozenset({"ACTIVE or IDLE", "IDLE", "IDLE_A", "IDLE_B", "IDLE_C"})
_SMARTCTL_ASLEEP = frozenset({"STANDBY", "STANDBY_Y", "STANDBY_Z", "SLEEP"})


def build_smartctl_command(drive_devices: Sequence[str]) -> str:
    """Build one framed SAT CHECK POWER MODE command for validated disks.

    smartctl 7.2 exits after the power check when no further display/test
    options are provided. Each result includes its validated path and its own
    exit status; status 3 represents an asleep drive.
    """
    # Noninteractive SSH sessions often omit sbin from PATH. Only look for
    # executable files; this does not open disks or run smartctl discovery.
    lines = [
        "mycloud_smartctl=",
        (
            "for mycloud_candidate in /usr/sbin/smartctl /usr/bin/smartctl "
            "/sbin/smartctl /bin/smartctl; do "
            'if test -x "$mycloud_candidate"; then '
            'mycloud_smartctl=$mycloud_candidate; break; fi; done'
        ),
        (
            'if test -z "$mycloud_smartctl"; then '
            'mycloud_smartctl=$(command -v smartctl 2>/dev/null) || mycloud_smartctl=; fi'
        ),
    ]
    for device in parse_drive_devices(drive_devices):
        lines.extend((
            f"printf '{_SMARTCTL_DEVICE}{device}\\n'",
            (
                f"if ! test -b {device}; then "
                f"printf '{_SMARTCTL_ERROR}device_missing\\n'; mycloud_status=2; "
                'elif test -z "$mycloud_smartctl"; then '
                f"printf '{_SMARTCTL_ERROR}program_missing\\n'; mycloud_status=127; "
                f'elif LC_ALL=C "$mycloud_smartctl" -d sat -n standby,3 {device} 2>&1; '
                'then mycloud_status=0; else mycloud_status=$?; fi'
            ),
            f"printf '\\n{_SMARTCTL_STATUS}%s\\n' \"$mycloud_status\"",
        ))
    return "\n".join(lines)


def parse_smartctl_states(
    output: str, drive_devices: Sequence[str],
    *, diagnostics: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Require a complete, unambiguous per-disk result; never infer awake."""
    devices = parse_drive_devices(drive_devices)
    sections: dict[str, tuple[list[str], int | None]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith(_SMARTCTL_DEVICE):
            device = line.removeprefix(_SMARTCTL_DEVICE)
            if device not in devices or device in sections:
                raise ValueError("Unexpected or duplicate smartctl device")
            if current is not None and sections[current][1] is None:
                raise ValueError("Missing smartctl status")
            current = device
            sections[device] = ([], None)
            if diagnostics is not None:
                diagnostics[device] = {
                    "state": POWER_UNKNOWN, "exit_status": None, "error_type": None,
                }
        elif line.startswith(_SMARTCTL_STATUS):
            if current is None or sections[current][1] is not None:
                raise ValueError("Unexpected smartctl status")
            status_text = line.removeprefix(_SMARTCTL_STATUS)
            if not status_text.isascii() or not status_text.isdecimal():
                raise ValueError("Invalid smartctl status")
            status = int(status_text)
            if not 0 <= status <= 255:
                raise ValueError("Invalid smartctl status")
            sections[current] = (sections[current][0], status)
            if diagnostics is not None:
                diagnostics[current]["exit_status"] = status
        elif current is not None:
            if sections[current][1] is not None:
                if line.strip():
                    raise ValueError("Unexpected output after smartctl status")
            else:
                sections[current][0].append(line)
        elif line.strip():
            raise ValueError("Unframed smartctl output")
    if set(sections) != set(devices):
        raise ValueError("Missing smartctl device")

    states = {}
    failure = None
    for device in devices:
        lines, status = sections[device]
        device_diagnostic = {
            "state": POWER_UNKNOWN, "exit_status": status, "error_type": None,
        }
        if diagnostics is not None:
            diagnostics[device] = device_diagnostic
        error_markers = [
            line.removeprefix(_SMARTCTL_ERROR)
            for line in lines if line.startswith(_SMARTCTL_ERROR)
        ]
        if status not in (None, 0, 3):
            error_type = "command_failed"
            if error_markers == ["device_missing"] and status == 2:
                error_type = "device_missing"
            elif status == 127:
                error_type = "program_missing"
            device_diagnostic["error_type"] = error_type
            failure = failure or PowerProbeError(
                "smartctl power-state command failed", stage="command",
                error_type=error_type,
                detail=error_type if error_type != "command_failed" else "nonzero_exit",
                exit_status=status, probe_type="smartctl_sat_power_state",
            )
            continue
        if error_markers:
            raise ValueError("Inconsistent smartctl error marker")
        matches = [
            match for line in lines if (match := _SMARTCTL_STATE.fullmatch(line))
        ]
        unknowns = [line for line in lines if _SMARTCTL_UNKNOWN.fullmatch(line)]
        if len(matches) + len(unknowns) != 1 or status is None:
            raise ValueError("Missing or ambiguous smartctl power state")
        if unknowns:
            if status != 0:
                raise ValueError("Unexpected smartctl unknown-state status")
            states[device] = POWER_UNKNOWN
            continue
        state, printed_exit = matches[0].groups()
        if state in _SMARTCTL_AWAKE and status == 0 and printed_exit is None:
            states[device] = POWER_ACTIVE
        elif state in _SMARTCTL_ASLEEP and status == 3 and printed_exit == "3":
            states[device] = POWER_STANDBY
        else:
            raise ValueError("Unrecognized or inconsistent smartctl power state")
        device_diagnostic["state"] = states[device]
    if failure is not None:
        raise failure
    return states


def parse_drive_devices(value: str | Sequence[str]) -> tuple[str, ...]:
    """Validate and normalize whole-disk device paths accepted by hdparm."""
    raw_devices = value.split(",") if isinstance(value, str) else value
    devices = tuple(str(device).strip() for device in raw_devices)

    if not devices or any(not device for device in devices):
        raise ValueError("At least one drive device is required")
    if len(set(devices)) != len(devices):
        raise ValueError("Drive devices must not contain duplicates")
    if any(_DRIVE_DEVICE_PATTERN.fullmatch(device) is None for device in devices):
        raise ValueError(
            "Drive devices must be whole-disk paths such as /dev/sda or /dev/sdc"
        )

    return devices


def parse_hdparm_state(output: str) -> str:
    """Parse the three hdparm states supported by the fail-closed policy."""
    match = _STATE_PATTERN.search(output)
    if match is None:
        return POWER_UNKNOWN
    state = match.group(1).lower()
    return state if state in (POWER_ACTIVE, POWER_STANDBY) else POWER_UNKNOWN


def parse_hdparm_states(
    output: str, drive_devices: Sequence[str]
) -> dict[str, str]:
    """Parse one multi-device hdparm response without positional assumptions.

    ``hdparm`` prefixes each device result with the device path. Requiring exactly
    one named section and exactly one state per configured path makes partial,
    duplicated, reordered, or unexpected output fail closed.
    """
    devices = parse_drive_devices(drive_devices)
    expected = set(devices)
    sections: dict[str, list[str]] = {}
    current_device: str | None = None

    for line in output.splitlines():
        header = _DEVICE_HEADER_PATTERN.fullmatch(line)
        if header is not None:
            current_device = header.group(1)
            if current_device not in expected or current_device in sections:
                raise ValueError("Unexpected or duplicate hdparm device section")
            sections[current_device] = []
            continue
        if current_device is None:
            if _STATE_PATTERN.search(line) is not None:
                raise ValueError("hdparm state has no device section")
            continue
        sections[current_device].append(line)

    if set(sections) != expected:
        raise ValueError("hdparm response is missing a configured device")

    states: dict[str, str] = {}
    for device in devices:
        matches = _STATE_PATTERN.findall("\n".join(sections[device]))
        if len(matches) != 1:
            raise ValueError("hdparm device section has no unique power state")
        state = matches[0].lower()
        states[device] = (
            state if state in (POWER_ACTIVE, POWER_STANDBY) else POWER_UNKNOWN
        )
    return states


class _PinnedSSHClient(asyncssh.SSHClient):
    """Validate a server key against a pin, accepting one key for TOFU."""

    def __init__(
        self,
        expected_fingerprint: str | None,
        fingerprint_seen: Callable[[str], None],
    ) -> None:
        self._expected_fingerprint = expected_fingerprint
        self._fingerprint_seen = fingerprint_seen

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        """Accept the first key and require an exact match thereafter."""
        fingerprint = key.get_fingerprint("sha256")
        if self._expected_fingerprint is None:
            self._expected_fingerprint = fingerprint
            self._fingerprint_seen(fingerprint)
            return True
        return hmac.compare_digest(self._expected_fingerprint, fingerprint)


class SSHPowerStateClient:
    """Use one short-lived SSH connection per serialized hdparm probe."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        drive_devices: Sequence[str],
        expected_fingerprint: str | None = None,
        timeout: float = 10.0,
        *,
        probe_method: str = DEFAULT_POWER_PROBE_METHOD,
    ) -> None:
        if probe_method not in (PROBE_METHOD_HDPARM, PROBE_METHOD_SMARTCTL_SAT):
            raise ValueError("Unsupported power probe method")
        self._probe_method = probe_method
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._drive_devices = parse_drive_devices(drive_devices)
        self._expected_fingerprint = expected_fingerprint
        self._fingerprint = expected_fingerprint
        self._observed_fingerprint: str | None = None
        self._timeout = timeout
        self._connection: Any | None = None
        self._process: Any | None = None
        self._process_running = False
        self._probe_lock = asyncio.Lock()
        self.last_command_exit_status: int | None = None
        self.device_diagnostics: dict[str, dict[str, Any]] = {}

    @property
    def probe_method(self) -> str:
        """Return the method actually used by this client instance."""
        return self._probe_method

    @property
    def probe_type(self) -> str:
        """Return an allowlisted diagnostic name for the selected probe."""
        return (
            "smartctl_sat_power_state"
            if self._probe_method == PROBE_METHOD_SMARTCTL_SAT
            else "hdparm_power_state"
        )

    def _probe_error(self, *args, **kwargs) -> PowerProbeError:
        return PowerProbeError(*args, probe_type=self.probe_type, **kwargs)

    @property
    def fingerprint(self) -> str | None:
        """Return the pinned or newly observed SHA-256 host-key fingerprint."""
        return self._fingerprint

    @property
    def drive_devices(self) -> tuple[str, ...]:
        """Return validated drive paths in configured order."""
        return self._drive_devices

    def _fingerprint_seen(self, fingerprint: str) -> None:
        self._observed_fingerprint = fingerprint

    async def _async_connect(self):
        started = asyncio.get_running_loop().time()
        validator = _PinnedSSHClient(
            self._expected_fingerprint,
            self._fingerprint_seen,
        )
        self._observed_fingerprint = None
        try:
            connection = await asyncio.wait_for(
                asyncssh.connect(
                    self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    client_keys=None,
                    # b"" can fall back to ~/.ssh/known_hosts and bypass our pin
                    # callback. None disables checking. A truthy empty store
                    # always delegates host-key trust to the pinned client.
                    known_hosts=asyncssh.import_known_hosts(""),
                    server_host_key_algs="default",
                    client_factory=lambda: validator,
                ),
                timeout=self._timeout,
            )
        except Exception as err:
            raise self._probe_error(
                "SSH connection or host-key validation failed",
                stage="connect", detail="connect_failed",
                duration_seconds=asyncio.get_running_loop().time() - started,
            ) from err

        self._connection = connection

        if self._expected_fingerprint is None and self._observed_fingerprint:
            self._fingerprint = self._observed_fingerprint
            self._expected_fingerprint = self._observed_fingerprint

        return connection

    async def _async_check_once(self) -> dict[str, str]:
        """Read every configured drive through one connection and one exec channel."""
        states: dict[str, str] = {}
        self.last_command_exit_status = None
        self.device_diagnostics = {
            device: {"state": POWER_UNKNOWN, "exit_status": None, "error_type": None}
            for device in self._drive_devices
        }
        failure: BaseException | None = None
        try:
            connection = await self._async_connect()
            use_smartctl = self._probe_method == PROBE_METHOD_SMARTCTL_SAT
            command = (
                build_smartctl_command(self._drive_devices)
                if use_smartctl
                else f"{HDPARM_PATH} -C {' '.join(self._drive_devices)}"
            )
            started = asyncio.get_running_loop().time()

            async def run_command():
                self._process = await connection.create_process(command)
                self._process_running = True
                result = await self._process.wait(check=False)
                self._process_running = False
                return result

            try:
                # One budget covers both opening the exec channel and waiting for
                # hdparm. On timeout, async_close() explicitly terminates and closes
                # the retained process before closing the SSH connection.
                result = await asyncio.wait_for(run_command(), timeout=self._timeout)
            except TimeoutError as err:
                raise self._probe_error(
                    "SSH power-state command timed out",
                    stage="command",
                    error_type="timeout",
                    detail="command_timeout",
                    duration_seconds=(
                        asyncio.get_running_loop().time() - started
                    ),
                ) from err
            self.last_command_exit_status = (
                result.exit_status
                if type(result.exit_status) is int and 0 <= result.exit_status <= 255
                else None
            )
            if result.exit_status != 0:
                raise self._probe_error(
                    "SSH power-state command failed", stage="command",
                    error_type="command_failed", detail="nonzero_exit",
                    exit_status=result.exit_status,
                    duration_seconds=(
                        asyncio.get_running_loop().time() - started
                    ),
                )
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            try:
                if use_smartctl:
                    states = parse_smartctl_states(
                        output, self._drive_devices, diagnostics=self.device_diagnostics
                    )
                else:
                    states = parse_hdparm_states(output, self._drive_devices)
                    self.device_diagnostics = {
                        device: {"state": state, "exit_status": None, "error_type": None}
                        for device, state in states.items()
                    }
            except ValueError as err:
                raise self._probe_error(
                    "Unrecognized power-state response", stage="command",
                    error_type="parse_failed",
                    detail=(
                        "unrecognized_smartctl_output"
                        if use_smartctl else "unrecognized_hdparm_output"
                    ),
                    duration_seconds=(
                        asyncio.get_running_loop().time() - started
                    ),
                ) from err
        except (PowerProbeError, asyncio.CancelledError) as err:
            failure = err
        except Exception as err:  # noqa: BLE001 - AsyncSSH exposes varied errors
            failure = self._probe_error(
                "SSH power-state command failed", stage="command", detail="command_failed",
            )
            failure.__cause__ = err
            failure.__suppress_context__ = True

        # A successful result is returned only after the connection has been
        # closed. The same bounded cleanup covers parser failures and cancellation.
        try:
            close_task = asyncio.create_task(self.async_close())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                try:
                    await close_task
                except Exception:  # noqa: BLE001 - cancellation must win
                    # Preserve cancellation after bounded best-effort cleanup.
                    close_task.cancel()
                raise
        except PowerProbeError as cleanup_error:
            if failure is None:
                failure = cleanup_error
        except Exception as cleanup_error:  # noqa: BLE001 - normalize SSH cleanup
            if failure is None:
                failure = self._probe_error(
                    "SSH connection cleanup failed", stage="cleanup",
                    error_type="connection_failed", detail="cleanup_failed",
                )
                failure.__cause__ = cleanup_error
                failure.__suppress_context__ = True

        if failure is not None:
            safe_failure = (
                failure.error_type if isinstance(failure, PowerProbeError) else None
            )
            for diagnostic in self.device_diagnostics.values():
                if diagnostic["state"] == POWER_UNKNOWN and not diagnostic["error_type"]:
                    diagnostic["error_type"] = safe_failure
            raise failure

        return states

    async def async_check(self) -> dict[str, str]:
        """Serialize probes; each cycle opens exactly one short connection."""
        async with self._probe_lock:
            return await self._async_check_once()

    async def async_close(self) -> None:
        """Boundedly terminate the current exec channel and SSH connection."""
        process, self._process = self._process, None
        process_running, self._process_running = self._process_running, False
        connection, self._connection = self._connection, None
        cleanup_failure: PowerProbeError | None = None

        if process is not None:
            try:
                terminate = getattr(process, "terminate", None)
                if process_running and callable(terminate):
                    try:
                        terminate()
                    except (OSError, asyncssh.Error):
                        # Some embedded SSH servers don't support process signals.
                        # Closing the channel and connection remains authoritative.
                        pass
                process.close()
                await asyncio.wait_for(
                    process.wait_closed(), timeout=self._timeout
                )
            except TimeoutError as err:
                cleanup_failure = self._probe_error(
                    "SSH process cleanup timed out", stage="cleanup",
                    error_type="timeout", detail="process_cleanup_timeout",
                )
                cleanup_failure.__cause__ = err
                cleanup_failure.__suppress_context__ = True
            except Exception as err:  # noqa: BLE001 - normalize SSH cleanup
                cleanup_failure = self._probe_error(
                    "SSH process cleanup failed", stage="cleanup",
                    error_type="connection_failed", detail="cleanup_failed",
                )
                cleanup_failure.__cause__ = err
                cleanup_failure.__suppress_context__ = True

        if connection is not None:
            connection.close()
            try:
                await asyncio.wait_for(
                    connection.wait_closed(), timeout=self._timeout
                )
            except TimeoutError as err:
                abort = getattr(connection, "abort", None)
                if callable(abort):
                    abort()
                connection_failure = self._probe_error(
                    "SSH connection cleanup timed out", stage="cleanup",
                    error_type="timeout", detail="cleanup_timeout",
                )
                connection_failure.__cause__ = err
                connection_failure.__suppress_context__ = True
                cleanup_failure = cleanup_failure or connection_failure
            except Exception as err:  # noqa: BLE001 - normalize SSH cleanup
                connection_failure = self._probe_error(
                    "SSH connection cleanup failed", stage="cleanup",
                    error_type="connection_failed", detail="cleanup_failed",
                )
                connection_failure.__cause__ = err
                connection_failure.__suppress_context__ = True
                cleanup_failure = cleanup_failure or connection_failure

        if cleanup_failure is not None:
            raise cleanup_failure
