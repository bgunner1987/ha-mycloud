"""Classify real AsyncSSH exceptions and prove logs never retain raw failures."""

import errno
import json
import logging
import socket

import asyncssh
import pytest

from custom_components.mycloud.probe_diagnostics import (
    PowerProbeError,
    describe_probe_error,
    log_probe_failure,
)

# Deliberately synthetic markers, never real credentials or host keys.
SECRET = "test-password-marker test-user-marker test-host-key-marker"


def wrapped(cause, stage="connect"):
    try:
        raise cause
    except type(cause) as error:
        try:
            raise PowerProbeError(SECRET, stage=stage) from error
        except PowerProbeError as result:
            return result


@pytest.mark.parametrize("cause,expected", [
    (asyncssh.PermissionDenied(SECRET), "authentication_failed"),
    (asyncssh.IllegalUserName(SECRET), "authentication_failed"),
    (asyncssh.HostKeyNotVerifiable(SECRET), "host_key_mismatch"),
    (asyncssh.KeyExchangeFailed(SECRET), "algorithm_negotiation_failed"),
    (ConnectionRefusedError(errno.ECONNREFUSED, SECRET), "connection_failed"),
    (socket.gaierror(-2, SECRET), "connection_failed"),
    (asyncssh.ConnectionLost(SECRET), "connection_failed"),
    (asyncssh.ConnectionLost("Login timeout expired"), "timeout"),
    (TimeoutError(SECRET), "timeout"),
    (asyncssh.ChannelOpenError(1, SECRET), "command_failed"),
    (PowerProbeError(SECRET, error_type="parse_failed"), "parse_failed"),
])
def test_real_exception_classification_is_safe(cause, expected, caplog):
    failure = describe_probe_error(wrapped(cause))
    assert failure.error_type == expected
    assert len(failure.chain) == 2
    with caplog.at_level(logging.WARNING):
        log_probe_failure(logging.getLogger("test.probe"), failure)
    record = caplog.records[-1]
    assert record.exc_info is None
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps(failure.__dict__)
    assert record.args == (expected, "connect")


def test_implicit_context_and_all_chain_nodes_are_sanitized(caplog):
    secret_type = type("test-secret-class-marker", (Exception,), {})
    try:
        raise secret_type(SECRET)
    except secret_type:
        try:
            raise asyncssh.PermissionDenied(SECRET)
        except asyncssh.PermissionDenied:
            try:
                raise PowerProbeError(SECRET, stage="connect")
            except PowerProbeError as error:
                failure = describe_probe_error(error)
                # Log while the raw exception is active, as the coordinator does.
                log_probe_failure(logging.getLogger("test.probe"), failure)
    assert failure.error_type == "authentication_failed"
    assert len(failure.chain) == 3
    assert "Traceback" not in caplog.text
    assert SECRET not in caplog.text
    assert "test-secret-class-marker" not in caplog.text
    assert caplog.records[-1].exc_info is None


@pytest.mark.parametrize("kind", ["key exchange", "encryption", "MAC", "compression"])
def test_negotiation_subtype_is_retained_without_server_algorithm_list(kind):
    cause = asyncssh.KeyExchangeFailed(f"No matching {kind} algorithm found, sent {SECRET}")
    summary = describe_probe_error(wrapped(cause)).summary
    assert "no_matching_" + kind.replace(" ", "_").lower() in summary
    assert SECRET not in summary


def test_changed_safe_cause_has_new_signature_but_secret_text_does_not():
    first = describe_probe_error(wrapped(ConnectionRefusedError(errno.ECONNREFUSED, SECRET)))
    different_text = describe_probe_error(wrapped(ConnectionRefusedError(errno.ECONNREFUSED, "other")))
    changed_cause = describe_probe_error(wrapped(OSError(errno.EHOSTUNREACH, SECRET)))
    assert first == different_text
    assert first != changed_cause


def test_structured_fields_cannot_inject_secrets():
    failure = describe_probe_error(PowerProbeError(
        SECRET, stage=SECRET, error_type=SECRET, detail=SECRET, exit_status=SECRET,
    ))
    assert SECRET not in failure.summary
    assert failure.stage == "probe"
    assert failure.error_type == "connection_failed"


def test_structured_connection_causes_are_distinguishable_without_raw_reasons():
    first = describe_probe_error(wrapped(asyncssh.ChannelOpenError(1, SECRET)))
    second = describe_probe_error(wrapped(asyncssh.ChannelOpenError(4, SECRET)))
    assert first != second
    assert "ssh_channel_code=1" in first.summary
    first = describe_probe_error(wrapped(socket.gaierror(socket.EAI_AGAIN, SECRET)))
    second = describe_probe_error(wrapped(socket.gaierror(socket.EAI_NONAME, SECRET)))
    assert first != second
    assert "EAI_AGAIN" in first.summary
    assert SECRET not in first.summary + second.summary


def test_generated_key_contents_and_individual_credentials_never_reach_logs(caplog):
    key = asyncssh.generate_private_key("ssh-ed25519")
    canaries = [
        key.export_private_key().decode().strip(),
        key.export_public_key().decode().strip(),
        key.get_fingerprint("sha256"),
        "synthetic password with spaces",
        "synthetic-user@example.invalid",
    ]
    failure = describe_probe_error(wrapped(asyncssh.HostKeyNotVerifiable("\n".join(canaries))))
    log_probe_failure(logging.getLogger("test.probe"), failure)
    rendered = caplog.text + json.dumps(failure.__dict__)
    record = caplog.records[-1]
    rendered += repr(record.args) + repr(record.exc_info)
    for value in canaries:
        assert value not in rendered
