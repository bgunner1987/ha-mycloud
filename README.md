# <img src="images/icon.png" alt="WD My Cloud App Icon" width="100"> ha-mycloud

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bgunner1987&repository=ha-mycloud&category=Integration)

Home Assistant integration for Western Digital My Cloud NAS devices.

This integration is powered by the [wdnas-client](https://github.com/J-shw/wdnas_client) Python library, which handles all communication with the NAS.

---

## Features
- **System Status**: Monitor CPU and memory usage of your My Cloud device.
- **Device Information**: See key details like serial number, name, and firmware version.
- **Disk Information**: See key details about disks, including their health status.
- **Volume Information**: View all volumes size, encryption status and more.
- **Optional sleep-aware polling**: Check disk power state over SSH before calling the WD API, retaining the last successful sensor values while disks sleep.

---

## Installation

### HACS (Recommended)
1. Add [bgunner1987/ha-mycloud](https://github.com/bgunner1987/ha-mycloud) as a custom integration repository in HACS. Install release `v1.3.4` (manifest version `1.3.4`) or select `main` when testing current development changes.
2. Search for "WD My Cloud" and install the integration.
3. Restart Home Assistant.

### Manual
1. Copy the `custom_components/mycloud` folder into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

---

## Configuration

> [!IMPORTANT]  
> The Admin account can only be active in one place at a time (either the NAS Web UI or this integration).

1.  Go to **Settings** > **Devices & Services**.
2.  Click **Add Integration** and search for "**WD My Cloud**".
3.  Enter your device's **IP address** or **hostname** (e.g., `192.168.1.10` or `wdmycloud`). Do **not** include `http://` or `https://`.
4.  Enter your username and password (Must be an **admin** account)
5. Select NAS software version (Currently 2 or 5 are supported)

### Optional sleep-aware polling

Sleep-aware polling is disabled by default, so existing installations continue to use the WD API normally. It can be enabled during a new integration setup or later in the integration's **Configure** dialog. Set:

- **Enable sleep-aware polling**: enabled
- **Disk power probe interval**: `10` seconds by default and minimum
- **SSH port**: `22` (default)
- **SSH username**: use the external login name which actually works in PuTTY
- **SSH password**: the corresponding SSH password
- **Drive devices**: comma-separated whole-disk paths, for example `/dev/sda,/dev/sdc`

The feature requires SSH to be enabled on the NAS, `/usr/bin/hdparm` to be present, and the SSH user to have permission to run `/usr/bin/hdparm -C` for every configured drive. Device paths are restricted to whole SATA/SCSI disk names such as `/dev/sda`; shell fragments and partition paths are rejected.

Both setup and options forms show drive devices as a serializable text field. On submission, paths are trimmed, validated, and stored in canonical comma-separated form. Invalid input displays an error at the drive-devices field; no settings are saved. Validation applies even when sleep-aware polling is disabled, and the SSH client independently validates device paths before constructing any commands.

On the WD My Cloud EX2 Ultra, the external SSH login name can be `sshd` even though the shell opened after login displays `root`. Enter the same username which successfully authenticates in PuTTY; do not infer it from the shell prompt.

In sleep-aware mode, one internal lightweight loop runs on the separate `power_probe_interval` (default and minimum 10 seconds). Existing entries which stored the former 60-second default are migrated once to 10 seconds; other explicitly configured values are preserved. The `update_interval` (default 600 seconds) remains the legacy polling interval and becomes the minimum spacing for periodic full API snapshots in sleep-aware mode. Each power check reuses the SSH connection when possible and runs only `/usr/bin/hdparm -C` sequentially for the configured drives. A lock prevents overlapping checks. A connection-level failure gets at most one controlled reconnect; authentication, host-key, algorithm, timeout, command, and parser failures are not retried as connection failures.

When no drive reports `standby` but at least one reports `unknown`, bounded follow-ups occur about 2 and 5 seconds after the initial check. They stop immediately on any `standby` result or once every drive is safely `active/idle`. The WD API is contacted only when **every** configured drive reports `active/idle`:

- At startup, one full poll is pending, whether or not a stored snapshot exists.
- The first all-active check performs that full poll (system info, system status, device info, and firmware version).
- Further all-active checks use the recent snapshot without contacting or logging in to the WD API until the API interval becomes due.
- Any observed `standby` blocks API access and arms one immediate poll for the next all-active state.
- `unknown`, a malformed response, timeout, SSH failure, or host-key mismatch always blocks API access but does **not** rearm the current wake phase. If a sleep phase produced only `unknown`, a later all-active check may refresh once the API interval is due.
- The wake-phase state is not persisted. After a Home Assistant restart, already-awake disks may be queried once again.

This avoids repeatedly resetting the NAS standby timer: continuous all-active operation can refresh only at the configured API interval, while a definitely observed standby-to-active wake phase may refresh immediately. An API failure consumes the attempt and the same interval gate prevents a rapid retry loop, including after another quick standby transition. Only legacy mode retains its single immediate HTTP-403 retry. Subsequent blocked checks retain cached values (or report no available cache). The integration never calls `smartctl` and does not use `/tmp/standby`.

The shorter probe interval and bounded follow-ups catch transitions the old 60/600-second cadence missed. A complete standby/wake cycle between two checks can still be missed; the interval fallback ensures a later confirmed all-active state can eventually refresh even if only `unknown` was observed.

After a successful full refresh, the four WD API results are saved in Home Assistant storage. Blocked states retain those values with `data_stale: true` and an unchanged `last_successful_update`. A confirmed all-active state with a recent successful snapshot remains `data_stale: false` even when no full poll is due. Sleeping entities show `standby` as on and `active/idle` as off; any `unknown` or probe error makes all Sleeping entities unavailable for that ambiguous result.

The 10-second loop does not publish a Home Assistant coordinator update for an unchanged result and does not rewrite the cache. It notifies entities only for a relevant power/diagnostic transition or a full API snapshot, so the changing internal `last_power_check` alone does not create recorder traffic. The owned loop and all SSH/HTTP resources are cancelled or closed on integration unload.

On the first setup there is no snapshot to retain. Enable sleep-aware mode only when the NAS disks are already awake and allow one successful refresh. If the disks are sleeping or their state is unknown, setup stops with a message asking you to wake them; it does not silently call the WD API. There is no automatic force refresh.

Physical disk entities in sleep-aware mode match API names exactly to the basename of validated device paths: `sda` to `/dev/sda`, `sdc` to `/dev/sdc`. API and configuration order do not matter. Unconfigured or orphan API rows such as `sdb` are skipped for all physical disk sensors. Existing registry entries for orphan disks may remain unavailable after upgrading; they are not automatically deleted. SSH commands use only the independently validated configured paths, never API names.

The first successful SSH connection uses trust on first use (TOFU): Home Assistant stores the server's SHA-256 host-key fingerprint and requires the same key on later connections. If the NAS host key legitimately changes, verify the new key independently and re-create the integration to establish a new trust record.

AsyncSSH receives an explicit empty known-hosts object, not empty bytes or `None`. This prevents fallback to ambient `~/.ssh/known_hosts` while keeping the integration's TOFU/pin-verification callback mandatory.

### Diagnosing unavailable Sleeping sensors

If Sleeping sensors stay unavailable, inspect a **CPU or Memory sensor** in **Developer Tools > States**. System sensors remain available from the stored snapshot during SSH failures; their attributes include:

- `last_power_check`: timestamp of the latest power probe, even on failure.
- `power_states`: validated configured paths and their active/idle, standby, or unknown states.
- `power_probe_status`: `pending`, `disabled`, `ok` (a readable probe, including standby/mixed), `unknown` (hdparm explicitly reports unknown), or `error`.
- `power_probe_error_type`: current failure category, or null after a successful probe.
- `last_power_probe_error`: safe description of the most recent error, retained after recovery until the integration restarts. Check status/error_type to determine whether it is still current.

Failure categories are `authentication_failed`, `host_key_mismatch`, `algorithm_negotiation_failed`, `connection_failed`, `command_failed`, `timeout`, and `parse_failed`. Nonzero command exits and malformed hdparm responses now produce explicit diagnostics; a valid hdparm response of `unknown` is not a parser error. Partial results on a failed probe are discarded and every configured drive is reported unknown.

The first failure and each changed safe cause emit a warning with `exc_info=True` and a sanitized copy of the complete exception chain. Identical consecutive failures are suppressed; after a successful probe, a recurring failure is logged again. Known exception types, the connect/command stage, symbolic OS errors, command exit status, and known negotiation-failure categories identify the cause. Raw exception messages, server output/algorithm lists, usernames, passwords, host-key contents, fingerprints, and original traceback frames/locals are **not** sent to logging handlers. Changing only sensitive free text does not cause another log entry.

These diagnostics are runtime-only and are not saved in the persistent NAS cache. They are also recorded and logged if no cache exists, although first setup still fails closed without creating sensors. No legacy SSH algorithms are enabled. An SSH/probe failure always blocks WD API access; unlike an observed standby state, a transient error or `unknown` does not immediately rearm the current wake phase.

> [!WARNING]
> SSH credentials grant powerful access, especially when using `root`. Home Assistant stores the configured password, and backups may contain it. Use a dedicated/restricted SSH account where the NAS supports one, protect Home Assistant and its backups, and never reuse this password elsewhere.

---

## Supported Devices & Contributing

### Tests

With Python 3.12, install `requirements-test.txt`, then run `python -m pytest -q` and `python -m ruff check .`. Config/options tests serialize their actual Voluptuous schemas with the real `voluptuous_serialize.convert` implementation and reproduce the former free-function-validator failure as a negative control. Only the surrounding Home Assistant lifecycle/password-selector interfaces are stubbed; schema serialization and drive-path parsing are not. GitHub Actions also runs these regression tests, HACS validation, and Hassfest.

Wake-phase regressions exercise the real coordinator on a deterministic virtual timeline using its configured interval; NAS API responses and the Home Assistant scheduler interface are isolated. Platform tests execute the actual setup and sensor classes with reordered/orphan disks. Local loopback AsyncSSH server tests perform real handshakes with ephemeral keys to verify TOFU, pinned reconnects, and rejection of mismatches even when an ambient known-hosts file trusts the presented key.

### Devices

This integration currently supports V2 and V5 firmware. You can see a list of tested models in the client library's documentation:

* **[View Known Supported Models](https://github.com/J-shw/wdnas_client/blob/dev/docs/SUPPORTED_MODELS.md)**

**Want to add your device?**

If your model isn't on the list, or if you have a different firmware version, I'd love to add support for it. Please **[open a GitHub Issue](https://github.com/bgunner1987/ha-mycloud/issues/new?template=new_device_request.md)** and we can work together to get it added.

---

## Example
<img alt="Screenshot of integration use in Home Assistant" src="https://github.com/user-attachments/assets/0b93e3d9-71ba-4386-93f2-75c210a65656" />

---

## Entities

This integration provides the following entities. `[disk_name]` and `[volume_name]` will be replaced by the actual names found on your device.

### Sensors
* **CPU Usage**: `sensor.wd_my_cloud_cpu_usage`
* **Memory Usage**: `sensor.wd_my_cloud_memory_usage`
* **Total Storage**: `sensor.wd_my_cloud_total_storage`
* **Used Storage**: `sensor.wd_my_cloud_used_storage`
* **Unused Storage**: `sensor.wd_my_cloud_unused_storage`
* **Disk Temperature**: `sensor.wd_my_cloud_disk_[disk_name]_temperature`
* **Disk Size**: `sensor.wd_my_cloud_disk_[disk_name]_size`
* **Volume Size**: `sensor.wd_my_cloud_volume_[volume_name]_size`

### Binary Sensors
* **Disk Healthy**: `binary_sensor.wd_my_cloud_disk_[disk_name]_healthy`
* **Disk Sleeping**: `binary_sensor.wd_my_cloud_disk_[disk_name]_sleeping`
* **Disk Failed**: `binary_sensor.wd_my_cloud_disk_[disk_name]_failed`
* **Disk Over Temperature**: `binary_sensor.wd_my_cloud_disk_[disk_name]_over_temperature`
* **Volume Mounted**: `binary_sensor.wd_my_cloud_volume_[volume_name]_mounted`
* **Volume Unlocked**: `binary_sensor.wd_my_cloud_volume_[volume_name]_unlocked`
* **Volume Encrypted**: `binary_sensor.wd_my_cloud_volume_[volume_name]_encrypted`
