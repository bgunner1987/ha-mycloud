# Sleep-aware polling: v1.3.12 repair and diagnosis

## Install and verify the effective settings

1. In HACS, update the custom repository `bgunner1987/ha-mycloud` to **v1.3.12**, then restart Home Assistant. HACS downloading files alone does not replace Python modules already loaded into the running process.
2. Open Settings > Devices & services > My Cloud > Configure. Enable sleep-aware polling, select **smartctl (SAT)**, and enter **only the devices currently present**. For the EX2 Ultra described in the report these are `/dev/sda,/dev/sdb`; remove an old `/dev/sdc` entry. Keep the SSH credentials that work for your NAS.
3. Save. OptionsFlow saves the normalized paths and `power_probe_method` in entry options. The registered update listener reloads the entry, closes its previous probe loop/connections, and constructs a new SSH client with the saved method. The selected default for older entries remains hdparm; updating files does not silently change the user's selection.
4. Inspect **My Cloud Power Probe** under Developer Tools > States, or use **Download diagnostics** on the integration. This works with no successful API snapshot. Check `integration_version=1.3.12`, both method fields equal to `smartctl_sat`, and `checked_devices` containing exactly the expected devices.
5. With both disks explicitly awake, the first probe must show `active/idle`, exit status 0 per disk, `api_poll_allowed=true`, then `api_poll_decision=snapshot_updated`, `last_api_attempt_status=success`, and a new `last_successful_update`. Subsequent recent active checks show `interval_wait`. Never treat unknown as evidence that a disk is awake.

## What was demonstrated, and what remains unverified

The existing IDLE_A/B/C parser, the stored-method handoff, and the options reload listener were already present in v1.3.11. Adding those same branches again would not address an execution or configuration problem. The reported IDLE_A text is recognized by the existing parser.

The old wrapper looked up bare `smartctl` in the noninteractive SSH PATH. Its last `printf` returned 0 even when individual commands returned 127. A shell regression reproduces this behavior. It also reproduces an inherited `errexit` shell aborting after a standby exit 3, before the per-device status marker. These are demonstrated command/protocol weaknesses, **not proof that this NAS has that PATH or shell setting**.

The repaired wrapper searches executable `/usr/sbin/smartctl`, `/usr/bin/smartctl`, `/sbin/smartctl`, and `/bin/smartctl`, then PATH. It checks block-device presence without opening the device, distinguishes missing devices/programs, captures each status in an `if` branch (safe with `errexit`), and places a newline before each status marker. It still uses only `-d sat -n standby,3`. No SMART attributes, discovery, IDENTIFY, self-test, report tracing, or wake command is requested.

The [smartmontools 7.2 implementation](https://github.com/smartmontools/smartmontools/blob/RELEASE_7_2/smartmontools/ataprint.cpp) maps power code 0x81 to IDLE_A and returns after CHECK POWER MODE when no further display/test options are supplied, before the drive-identification code. `-r ataioctl` is not needed for the state line and is deliberately not enabled in production.

The developer environment reached the reported SSH server, but had no verified host-key entry or usable authenticated session. **No successful run on this physical NAS or the user's Home Assistant is claimed.** Installed version, saved device paths, NAS shell/PATH and actual AsyncSSH results must be confirmed with the runtime fields below or the standalone diagnostic. Regression fixtures clearly distinguish the user-supplied IDLE_A evidence from synthetic cases.

## Read the blocking stage

| Fields | Meaning / next check |
| --- | --- |
| `integration_version` differs from 1.3.12 | Old code is still running; check HACS repository/version and restart HA. |
| Configured/effective method differs from `smartctl_sat` | Save the desired option and verify the entry reload. |
| `checked_devices` includes a retired disk | Correct the paths in Configure. Missing disks must block API access. |
| `probe_loop_running=false` | The owned probe task is not running. Reload the integration and inspect setup errors. |
| `probe_loop_status=checking` | A connection, command, bounded follow-up or API request is in progress. Compare `api_stage`, probe times and timeout diagnostics. |
| `polling_block_reason=device_missing` | A configured path is not a current block device. |
| `polling_block_reason=program_missing` | The noninteractive shell cannot find executable smartctl in the fixed paths or PATH. |
| `authentication_failed`, `host_key_mismatch`, `algorithm_negotiation_failed`, `connection_failed`, `timeout` | Transport/trust/authentication failure; no API access. No legacy algorithms are enabled automatically. |
| `command_failed` | Inspect `device_probe_results` and `ssh_command_exit_status`; an individual failure can coexist with wrapper exit 0. |
| `parse_failed` | Missing/damaged/duplicate framing, inconsistent status, or unrecognized output. This differs from an explicit unknown energy state. |
| `unknown`, `mixed`, `standby` | Current raw state blocks the API. Cached values and confirmed Sleeping display remain preserved. |
| `api_poll_decision=interval_wait` | Power gate passed, but an API attempt/snapshot is recent. Inspect remaining seconds and next periodic API time. |
| `api_poll_decision=api_failed` | API was attempted after the power gate passed. Inspect `last_api_error_type`, safe error text, and `api_stage`. |

`device_probe_results` contains only validated paths, normalized states, numeric status codes and allowlisted errors. With hdparm, the process has one shared exit status, so the individual disk exit statuses are null. On a malformed or failed smartctl probe, any successfully parsed peer details are diagnostic only: the coordinator discards partial power states and blocks the entire API cycle. No raw stdout/stderr, password, token, username, address, or host key is exported or logged.

## Exact command protocol

`build_smartctl_command()` sends one LF-delimited POSIX script in one SSH exec channel. For each validated device it prints `__MYCLOUD_DISK__ /dev/sdX`, merges that smartctl process's stderr into its stdout, and prints `__MYCLOUD_STATUS__ N`. The wrapper status normally is 0; it means the script completed, not that all disks passed. The parser independently requires every device and every status exactly once.

`Device is in IDLE_A mode` plus disk status 0 means active/idle. `Device is in STANDBY mode, exit(3)` plus status 3 means standby. Neither a status alone nor missing output permits API access. Unexpected stderr outside the frames and a nonzero SSH process status fail closed. LF and CRLF are covered by tests.

## Targeted live diagnostic (no Home Assistant required)

Download/checkout this repository at v1.3.12. In a terminal with Python 3.12 or newer:

```console
python -m pip install "asyncssh>=2.24.0"
python tools/diagnose_power_probe.py --host NAS_ADDRESS --port 22 --username sshd --devices /dev/sda,/dev/sdb --method smartctl_sat
```

The script prompts for the password and executes the **same command builder, AsyncSSH connection, timeout/cleanup and parser** as the integration. It prints only sanitized JSON, makes no WD API requests, and does not store credentials or modify HA's host-key pin. Like the integration's first connection, it uses TOFU unless `--expected-fingerprint` supplies an existing trusted SHA256 pin. Later HA reconnects still enforce HA's own stored pin. Do not reset that pin to work around a mismatch.

The decisive expected result is both paths mapped to `active/idle`, disk statuses 0, wrapper status 0 and `api_power_gate_allowed=true`. If this succeeds but HA still does not update, use HA's downloaded diagnostics to locate the remaining version/options/loop/interval/API stage. Share the safe JSON, not SSH passwords or raw trace output.

## Automated coverage and limits

Tests execute the generated script in a real POSIX shell with a hermetic smartctl replacement, then over a real loopback AsyncSSH server into the actual coordinator/API gate. They cover both IDLE_A disks, standby status 3, mixed and unknown states, a missing device, missing program, command failure, stderr without a final newline, malformed/missing markers, a nonzero SSH process status, no-cache startup, standby-to-awake refresh, and interval throttling. Separate tests cover SSH authentication, host-key pinning, timeouts, serialized probes and resource cleanup.

Options tests use the real Voluptuous serializer and execute the actual reload callback followed by platform setup. The surrounding Home Assistant lifecycle and WD HTTP calls are isolated by the test harness; this is not a real HA installation or a physical disk sleep/wake experiment. A live standby cycle remains the final deployment check.
