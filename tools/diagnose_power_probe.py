"""Run the installed source's real AsyncSSH probe without Home Assistant.

Usage: python tools/diagnose_power_probe.py --host NAS --username sshd
Requires asyncssh. Prompts for the password; never prints raw remote output or
credentials. Does not make WD API calls or alter Home Assistant's host-key pin.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import sys
import types
from pathlib import Path

# Load the actual source package without running its HA-specific __init__.py.
package = types.ModuleType("_mycloud_probe_runtime")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/mycloud")]
sys.modules[package.__name__] = package
probe = importlib.import_module(f"{package.__name__}.power_probe")
diagnostics = importlib.import_module(f"{package.__name__}.probe_diagnostics")
const = importlib.import_module(f"{package.__name__}.const")


async def run(args, password):
    client = probe.SSHPowerStateClient(
        args.host, args.port, args.username, password,
        probe.parse_drive_devices(args.devices),
        expected_fingerprint=args.expected_fingerprint,
        probe_method=args.method,
    )
    error = None
    try:
        states = await client.async_check()
    except probe.PowerProbeError as caught:
        failure = diagnostics.describe_probe_error(caught)
        states = dict.fromkeys(client.drive_devices, probe.POWER_UNKNOWN)
        error = {"type": failure.error_type, "summary": failure.summary}
    print(json.dumps({
        "integration_version": const.INTEGRATION_VERSION,
        "effective_power_probe_method": client.probe_method,
        "checked_devices": list(client.drive_devices),
        "power_states": states,
        "device_probe_results": client.device_diagnostics,
        "ssh_command_exit_status": client.last_command_exit_status,
        "api_power_gate_allowed": error is None and all(
            state == probe.POWER_ACTIVE for state in states.values()
        ),
        "error": error,
    }, indent=2))
    return 1 if error else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", required=True)
    parser.add_argument("--devices", default="/dev/sda,/dev/sdb")
    parser.add_argument("--method", choices=("hdparm", "smartctl_sat"), default="smartctl_sat")
    parser.add_argument("--expected-fingerprint", help="Optional existing SHA256 host-key pin; never printed")
    args = parser.parse_args()
    probe.parse_drive_devices(args.devices)
    return asyncio.run(run(args, getpass.getpass("SSH password: ")))


if __name__ == "__main__":
    raise SystemExit(main())
