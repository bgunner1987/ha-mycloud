"""Non-waking SSH power-state probe for WD My Cloud disks."""

from __future__ import annotations

import asyncio
import hmac
import re
from collections.abc import Callable, Sequence
from typing import Any

import asyncssh

HDPARM_PATH = "/usr/bin/hdparm"
POWER_ACTIVE = "active/idle"
POWER_STANDBY = "standby"
POWER_UNKNOWN = "unknown"

_DRIVE_DEVICE_PATTERN = re.compile(r"^/dev/(?:sd|hd)[a-z]+$")
_STATE_PATTERN = re.compile(
    r"drive\s+state\s+is\s*:\s*(standby|active/idle|unknown)\b",
    re.IGNORECASE,
)


class PowerProbeError(Exception):
    """Raised when a power-state check cannot be completed safely."""


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
    """Maintain and supervise one SSH connection used only for hdparm -C."""

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
        if self._connection is not None and not self._connection.is_closed():
            return self._connection

        validator = _PinnedSSHClient(
            self._expected_fingerprint,
            self._fingerprint_seen,
        )
        self._observed_fingerprint = None
        try:
            self._connection = await asyncio.wait_for(
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
            await self.async_close()
            raise PowerProbeError("SSH connection or host-key validation failed") from err

        if self._expected_fingerprint is None and self._observed_fingerprint:
            self._fingerprint = self._observed_fingerprint
            self._expected_fingerprint = self._observed_fingerprint

        return self._connection

    async def async_check(self) -> dict[str, str]:
        """Read every configured drive state sequentially without other commands."""
        connection = await self._async_connect()
        states: dict[str, str] = {}

        try:
            for device in self._drive_devices:
                result = await asyncio.wait_for(
                    connection.run(f"{HDPARM_PATH} -C {device}", check=False),
                    timeout=self._timeout,
                )
                if result.exit_status != 0:
                    states[device] = POWER_UNKNOWN
                    continue
                states[device] = parse_hdparm_state(
                    f"{result.stdout or ''}\n{result.stderr or ''}"
                )
        except Exception as err:
            await self.async_close()
            raise PowerProbeError("SSH power-state command failed") from err

        return states

    async def async_close(self) -> None:
        """Close the current SSH connection, if any."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()
            await connection.wait_closed()
