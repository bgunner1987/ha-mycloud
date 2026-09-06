"""Allowlisted SSH diagnostics: never pass raw exceptions to a log handler."""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
from dataclasses import dataclass

import asyncssh

_ERROR_TYPES = {
    "authentication_failed", "host_key_mismatch", "algorithm_negotiation_failed",
    "connection_failed", "command_failed", "timeout", "parse_failed",
}
_DETAILS = {
    "probe_failed", "connect_failed", "command_failed", "nonzero_exit",
    "unrecognized_hdparm_output", "cleanup_timeout", "cleanup_failed",
}


class PowerProbeError(Exception):
    """Carry structured operation context; the message is never logged directly."""

    def __init__(
        self, message: str, *, stage="probe", error_type=None, detail=None, exit_status=None
    ):
        super().__init__(message)
        self.stage = (
            stage if stage in {"probe", "connect", "command", "cleanup"} else "probe"
        )
        self.error_type = error_type if error_type in _ERROR_TYPES else None
        self.detail = detail if detail in _DETAILS else "probe_failed"
        self.exit_status = (
            exit_status if type(exit_status) is int and 0 <= exit_status <= 255 else None
        )


@dataclass(frozen=True)
class ProbeFailure:
    """Only fixed vocabulary, safe to store or pass to logging handlers."""

    error_type: str
    stage: str
    chain: tuple[str, ...]

    @property
    def summary(self) -> str:
        return f"{self.error_type}; stage={self.stage}; " + " <- ".join(self.chain)


def _describe(error: BaseException) -> tuple[str | None, str]:
    """Classify by exception type; never copy reason/args/output/host/key values."""
    if isinstance(error, PowerProbeError):
        label = f"PowerProbeError: {error.detail}"
        if error.exit_status is not None:
            label += f" (exit_status={error.exit_status})"
        return error.error_type, label
    if isinstance(error, asyncssh.ConnectionLost) and error.reason == "Login timeout expired":
        return "timeout", "ConnectionLost: login_timeout"
    known = (
        (TimeoutError, "timeout", "TimeoutError"),
        (asyncssh.PermissionDenied, "authentication_failed", "PermissionDenied"),
        (asyncssh.IllegalUserName, "authentication_failed", "IllegalUserName"),
        (asyncssh.HostKeyNotVerifiable, "host_key_mismatch", "HostKeyNotVerifiable"),
        (asyncssh.KeyExchangeFailed, "algorithm_negotiation_failed", "KeyExchangeFailed"),
        (asyncssh.ChannelOpenError, "command_failed", "ChannelOpenError"),
        (asyncssh.ProcessError, "command_failed", "ProcessError"),
        (asyncssh.ConnectionLost, "connection_failed", "ConnectionLost"),
        (asyncssh.ProtocolError, "connection_failed", "ProtocolError"),
        (asyncssh.DisconnectError, "connection_failed", "DisconnectError"),
        (socket.gaierror, "connection_failed", "AddressResolutionError"),
        (ConnectionRefusedError, "connection_failed", "ConnectionRefusedError"),
        (ConnectionResetError, "connection_failed", "ConnectionResetError"),
        (OSError, "connection_failed", "OSError"),
    )
    for cls, category, label in known:
        if isinstance(error, cls):
            if isinstance(error, socket.gaierror):
                names = {
                    value: name for name, value in vars(socket).items()
                    if name.startswith("EAI_") and type(value) is int
                }
                label += ": " + names.get(error.errno, "unspecified")
            elif isinstance(error, OSError):
                # Export a symbolic, known errno only, never strerror/filename.
                label += ": " + errno.errorcode.get(error.errno, "unspecified")
            if (
                isinstance(error, asyncssh.DisconnectError)
                and type(error.code) is int and 1 <= error.code <= 15
            ):
                label += f": ssh_disconnect_code={error.code}"
            if (
                isinstance(error, asyncssh.ChannelOpenError)
                and type(error.code) is int and 1 <= error.code <= 4
            ):
                label += f": ssh_channel_code={error.code}"
            if isinstance(error, asyncssh.KeyExchangeFailed):
                # Do not export server-supplied algorithm lists or free text.
                reason = error.reason
                if isinstance(reason, str):
                    for kind in ("key exchange", "encryption", "MAC", "compression"):
                        if reason.startswith(f"No matching {kind} algorithm"):
                            label += f": no_matching_{kind.replace(' ', '_').lower()}"
                            break
                    else:
                        if reason.startswith("Unable to find compatible server host key"):
                            label += ": no_compatible_host_key_algorithm"
            return category, label
    if isinstance(error, asyncio.CancelledError):
        return None, "CancelledError"
    for cls, label in ((ValueError, "ValueError"), (TypeError, "TypeError"), (RuntimeError, "RuntimeError")):
        if isinstance(error, cls):
            return None, label
    # Even custom class names can contain sensitive data. Never interpolate them.
    return None, "Exception: details withheld"


def describe_probe_error(error: PowerProbeError) -> ProbeFailure:
    """Preserve every node in the causal chain, without retaining raw exceptions."""
    chain = []
    categories = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        category, description = _describe(current)
        chain.append(description)
        if category is not None:
            categories.append(category)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    fallback = "command_failed" if error.stage == "command" else "connection_failed"
    return ProbeFailure(categories[-1] if categories else fallback, error.stage, tuple(chain))


def log_probe_failure(logger: logging.Logger, failure: ProbeFailure) -> None:
    """Log one concise allowlisted warning without raw exception tracebacks."""
    logger.warning(
        "SSH power probe failed (%s, stage=%s); WD API blocked for this cycle",
        failure.error_type,
        failure.stage,
    )
