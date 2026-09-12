"""Non-waking SSH power-state probe for WD My Cloud disks."""

from __future__ import annotations

import asyncio
import hmac
import re
from collections.abc import Callable, Sequence
from typing import Any

import asyncssh

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
    ) -> None:
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
            raise PowerProbeError(
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
        failure: BaseException | None = None
        try:
            connection = await self._async_connect()
            command = f"{HDPARM_PATH} -C {' '.join(self._drive_devices)}"
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
                raise PowerProbeError(
                    "SSH power-state command timed out",
                    stage="command",
                    error_type="timeout",
                    detail="command_timeout",
                    duration_seconds=(
                        asyncio.get_running_loop().time() - started
                    ),
                ) from err
            if result.exit_status != 0:
                raise PowerProbeError(
                    "SSH power-state command failed", stage="command",
                    error_type="command_failed", detail="nonzero_exit",
                    exit_status=result.exit_status,
                    duration_seconds=(
                        asyncio.get_running_loop().time() - started
                    ),
                )
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            try:
                states = parse_hdparm_states(output, self._drive_devices)
            except ValueError as err:
                raise PowerProbeError(
                    "Unrecognized hdparm response", stage="command",
                    error_type="parse_failed", detail="unrecognized_hdparm_output",
                    duration_seconds=(
                        asyncio.get_running_loop().time() - started
                    ),
                ) from err
        except (PowerProbeError, asyncio.CancelledError) as err:
            failure = err
        except Exception as err:  # noqa: BLE001 - AsyncSSH exposes varied errors
            failure = PowerProbeError(
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
                failure = PowerProbeError(
                    "SSH connection cleanup failed", stage="cleanup",
                    error_type="connection_failed", detail="cleanup_failed",
                )
                failure.__cause__ = cleanup_error
                failure.__suppress_context__ = True

        if failure is not None:
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
                cleanup_failure = PowerProbeError(
                    "SSH process cleanup timed out", stage="cleanup",
                    error_type="timeout", detail="process_cleanup_timeout",
                )
                cleanup_failure.__cause__ = err
                cleanup_failure.__suppress_context__ = True
            except Exception as err:  # noqa: BLE001 - normalize SSH cleanup
                cleanup_failure = PowerProbeError(
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
                connection_failure = PowerProbeError(
                    "SSH connection cleanup timed out", stage="cleanup",
                    error_type="timeout", detail="cleanup_timeout",
                )
                connection_failure.__cause__ = err
                connection_failure.__suppress_context__ = True
                cleanup_failure = cleanup_failure or connection_failure
            except Exception as err:  # noqa: BLE001 - normalize SSH cleanup
                connection_failure = PowerProbeError(
                    "SSH connection cleanup failed", stage="cleanup",
                    error_type="connection_failed", detail="cleanup_failed",
                )
                connection_failure.__cause__ = err
                connection_failure.__suppress_context__ = True
                cleanup_failure = cleanup_failure or connection_failure

        if cleanup_failure is not None:
            raise cleanup_failure
