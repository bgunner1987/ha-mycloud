import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from wdnas_client import client as nas_client

from .const import (
    CACHE_STORE_KEY,
    CACHE_STORE_VERSION,
    CONF_DRIVE_DEVICES,
    CONF_POWER_PROBE_INTERVAL,
    CONF_SLEEP_AWARE_ENABLED,
    CONF_SSH_PASSWORD,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_UPDATE_INTERVAL,
    DEFAULT_DRIVE_DEVICES,
    DEFAULT_POWER_PROBE_INTERVAL,
    DEFAULT_SSH_PORT,
    DEFAULT_SSH_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    HOST,
    PASSWORD,
    USERNAME,
    VERSION,
)
from .coordinator import MyCloudDataUpdateCoordinator
from .power_probe import (
    POWER_ACTIVE,
    POWER_STANDBY,
    POWER_UNKNOWN,
    SSHPowerStateClient,
    parse_drive_devices,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up the WD My Cloud sensor platform."""
    host = config_entry.data[HOST]
    username = config_entry.data[USERNAME]
    password = config_entry.data[PASSWORD]
    version = config_entry.data[VERSION]

    client = nas_client(username, password, host, version)
    update_interval_seconds = config_entry.options.get(
        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
    )
    scan_interval = timedelta(seconds=update_interval_seconds)
    _LOGGER.debug("Update interval set to %s seconds", update_interval_seconds)

    store = Store(
        hass,
        CACHE_STORE_VERSION,
        f"{CACHE_STORE_KEY}.{config_entry.entry_id}",
    )
    cached_envelope = await store.async_load()

    power_client = None
    if config_entry.options.get(CONF_SLEEP_AWARE_ENABLED, False):
        drive_devices = parse_drive_devices(
            config_entry.options.get(CONF_DRIVE_DEVICES, DEFAULT_DRIVE_DEVICES)
        )
        cached_fingerprint = (
            cached_envelope.get("ssh_host_key")
            if isinstance(cached_envelope, dict)
            else None
        )
        power_client = SSHPowerStateClient(
            host=host,
            port=config_entry.options.get(CONF_SSH_PORT, DEFAULT_SSH_PORT),
            username=config_entry.options.get(
                CONF_SSH_USERNAME, DEFAULT_SSH_USERNAME
            ),
            password=config_entry.options.get(CONF_SSH_PASSWORD, ""),
            drive_devices=drive_devices,
            expected_fingerprint=cached_fingerprint,
        )

    coordinator = MyCloudDataUpdateCoordinator(
        hass,
        _LOGGER,
        api_client=client,
        store=store,
        update_interval=scan_interval,
        config_entry=config_entry,
        cached_envelope=cached_envelope,
        power_client=power_client,
        power_probe_interval=timedelta(seconds=config_entry.options.get(
            CONF_POWER_PROBE_INTERVAL, DEFAULT_POWER_PROBE_INTERVAL
        )),
    )
    hass.data[DOMAIN][config_entry.entry_id].update(
        {"coordinator": coordinator, "async_close": coordinator.async_shutdown}
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_shutdown()
        hass.data[DOMAIN][config_entry.entry_id].clear()
        raise

    device_info_data = coordinator.data["device_info"]
    system_version_data = coordinator.data["system_version"]
    serial_number = device_info_data["serial_number"]
    device_name = device_info_data["name"]

    device = DeviceInfo(
        identifiers={(DOMAIN, serial_number)},
        name=device_name,
        manufacturer="Western Digital",
        model=device_info_data["description"],
        sw_version=system_version_data["firmware"]
    )

    sensors_to_add = [
        MyCloudCPUSensor(coordinator, device, serial_number, device_name),
        MyCloudMemorySensor(coordinator, device, serial_number, device_name),
        MyCloudTotalStorageSensor(coordinator, device, serial_number, device_name),
        MyCloudUsedStorageSensor(coordinator, device, serial_number, device_name),
        MyCloudUnusedStorageSensor(coordinator, device, serial_number, device_name)
    ]

    disks = coordinator.data["system_info"]["disks"]
    # Only validated configuration contributes command paths. API names are
    # untrusted identifiers and must match a configured basename exactly.
    configured_drives = {
        path.rsplit("/", 1)[-1]: path for path in coordinator.drive_devices
    }
    for disk in disks:
        api_name = disk.get("name")
        drive_device = (
            configured_drives.get(api_name) if isinstance(api_name, str) else None
        )
        if power_client is not None and drive_device is None:
            _LOGGER.debug("Ignoring API disk not present in configured drive devices")
            continue
        disk_serial = disk["sn"]
        disk_name = f"{device_name} Disk {disk['name']}"
        disk_model = disk["model"]

        disk_device = DeviceInfo(
            identifiers={(DOMAIN, disk_serial)},
            name=disk_name,
            manufacturer="Western Digital",
            model=disk_model,
            sw_version=system_version_data["firmware"],
            hw_version=disk["rev"],
            via_device=(DOMAIN, serial_number)
        )

        sensors_to_add.extend([
            MyCloudDiskTempSensor(coordinator, disk_device, disk_serial, disk_name, disk),
            MyCloudDiskHealthySensor(coordinator, disk_device, disk_serial, disk_name, disk),
            MyCloudDiskSleepSensor(
                coordinator,
                disk_device,
                disk_serial,
                disk_name,
                disk,
                drive_device,
            ),
            MyCloudDiskFailedSensor(coordinator, disk_device, disk_serial, disk_name, disk),
            MyCloudDiskOverTempSensor(coordinator, disk_device, disk_serial, disk_name, disk),
            MyCloudDiskSizeSensor(coordinator, disk_device, disk_serial, disk_name, disk)
        ])
    
    volumes = coordinator.data["system_info"]["volumes"]
    for volume in volumes:
        volume_id = volume["id"]
        volume_name = f"{device_name} {volume['label']}"

        volume_device = DeviceInfo(
            identifiers={(DOMAIN, volume_id)},
            name=volume_name,
            manufacturer="Western Digital",
            model="Storage Volume",
            via_device=(DOMAIN, serial_number)
        )

        sensors_to_add.extend([
            MyCloudVolumeSizeSensor(coordinator, volume_device, volume_name, volume),
            MyCloudVolumeMountedSensor(coordinator, volume_device, volume_name, volume),
            MyCloudVolumeUnlockedSensor(coordinator, volume_device, volume_name, volume),
            MyCloudVolumeEncryptedSensor(coordinator, volume_device, volume_name, volume)
        ])

    async_add_entities(sensors_to_add)

class MyCloudCachedEntity(CoordinatorEntity):
    """Expose cache freshness without changing entity identity or value."""

    @property
    def extra_state_attributes(self):
        return {
            "data_stale": bool(self.coordinator.data.get("data_stale", False)),
            "last_successful_update": self.coordinator.data.get("last_full_update"),
        }


class MyCloudSensorBase(MyCloudCachedEntity, SensorEntity):
    def __init__(self, coordinator, device_info, serial_number, device_name, key, name, unit=None, device_class=None):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_{key}"
        self._attr_name = f"{device_name} {name}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = SensorStateClass.MEASUREMENT

# -- System --

class MyCloudCPUSensor(MyCloudSensorBase):
    def __init__(self, coordinator, device_info, serial_number, device_name):
        super().__init__(
            coordinator,
            device_info,
            serial_number,
            device_name,
            "cpu_usage",
            "CPU Usage",
            unit="%"
        )
        self._attr_icon = "mdi:cpu-64-bit"

    @property
    def state(self):
        return self.coordinator.data["system_status"]["cpu"]

class MyCloudMemorySensor(MyCloudSensorBase):
    def __init__(self, coordinator, device_info, serial_number, device_name):
        super().__init__(
            coordinator,
            device_info,
            serial_number,
            device_name,
            "memory_usage",
            "Memory Usage",
            unit="%"
        )
        self._attr_icon = "mdi:memory"

    @property
    def state(self):
        mem_data = self.coordinator.data["system_status"]["memory"]
        total = mem_data["total"]
        used = total - mem_data["unused"]
        if total > 0:
            return round((used / total) * 100, 2)
        return None
    
class MyCloudTotalStorageSensor(MyCloudCachedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES

    _attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES
    _attr_icon = "mdi:database"
    

    def __init__(self, coordinator, device_info, serial_number, device_name):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_total_storage"
        self._attr_name = f"{device_name} Total Storage"

    @property
    def native_value(self):
        size_data = self.coordinator.data["system_info"]["size"]
        return int(size_data["total"])

class MyCloudUsedStorageSensor(MyCloudCachedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES

    _attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES
    _attr_icon = "mdi:database-minus"

    def __init__(self, coordinator, device_info, serial_number, device_name):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_used_storage"
        self._attr_name = f"{device_name} Used Storage"

    @property
    def native_value(self):
        size_data = self.coordinator.data["system_info"]["size"]
        return int(size_data["used"])

class MyCloudUnusedStorageSensor(MyCloudCachedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES

    _attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES
    _attr_icon = "mdi:database-plus"

    def __init__(self, coordinator, device_info, serial_number, device_name):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_unused_storage"
        self._attr_name = f"{device_name} Unused Storage"

    @property
    def native_value(self):
        size_data = self.coordinator.data["system_info"]["size"]
        return int(size_data["unused"])

# -- Disks --

class MyCloudDiskTempSensor(MyCloudCachedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator, device_info, serial_number, disk_name, disk):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_disk_temp"
        self._attr_name = f"{disk_name} Temperature"
        self._disk_name = disk['name']

    @property
    def native_value(self):
        disks = self.coordinator.data.get("system_info", {}).get("disks", [])
        for disk in disks:
            if disk["name"] == self._disk_name:
                try:
                    return float(disk["temp"])
                except (TypeError, ValueError):
                    return None
        return None

class MyCloudDiskSizeSensor(MyCloudCachedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES

    _attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES
    _attr_icon = "mdi:harddisk"

    def __init__(self, coordinator, device_info, serial_number, disk_name, disk):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_disk_size"
        self._attr_name = f"{disk_name} Size"
        self._disk_name = disk['name']

    @property
    def native_value(self):
        disks = self.coordinator.data["system_info"]["disks"]
        for disk in disks:
            if disk["name"] == self._disk_name:
                return int(disk["size"])
        return None
    
class MyCloudDiskHealthySensor(MyCloudCachedEntity, BinarySensorEntity):
    _attr_icon = "mdi:shield-check"
    def __init__(self, coordinator, device_info, serial_number, disk_name, disk):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_disk_healthy"
        self._attr_name = f"{disk_name} Healthy"
        self._disk_name = disk['name']

    @property
    def is_on(self):
        disks = self.coordinator.data["system_info"]["disks"]
        for disk in disks:
            if disk["name"] == self._disk_name:
                return disk["healthy"]
        return False

class MyCloudDiskSleepSensor(MyCloudCachedEntity, BinarySensorEntity):
    _attr_icon = "mdi:sleep"
    def __init__(
        self,
        coordinator,
        device_info,
        serial_number,
        disk_name,
        disk,
        drive_device=None,
    ):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_disk_sleep"
        self._attr_name = f"{disk_name} Sleeping"
        self._disk_name = disk['name']
        self._drive_device = drive_device

    @property
    def available(self):
        power_states = self.coordinator.data.get("power_states", {})
        if self.coordinator.data.get("sleep_aware_enabled"):
            return (
                self._drive_device is not None
                and power_states.get(self._drive_device) != POWER_UNKNOWN
                and self._drive_device in power_states
                and super().available
            )
        return super().available

    @property
    def is_on(self):
        power_state = self.coordinator.data.get("power_states", {}).get(
            self._drive_device
        )
        if power_state == POWER_STANDBY:
            return True
        if power_state == POWER_ACTIVE:
            return False
        if self.coordinator.data.get("sleep_aware_enabled"):
            return None
        disks = self.coordinator.data["system_info"]["disks"]
        for disk in disks:
            if disk["name"] == self._disk_name:
                return disk["sleep"]
        return False

class MyCloudDiskFailedSensor(MyCloudCachedEntity, BinarySensorEntity):
    _attr_icon = "mdi:alert"
    def __init__(self, coordinator, device_info, serial_number, disk_name, disk):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_disk_failed"
        self._attr_name = f"{disk_name} Failed"
        self._disk_name = disk['name']

    @property
    def is_on(self):
        disks = self.coordinator.data["system_info"]["disks"]
        for disk in disks:
            if disk["name"] == self._disk_name:
                return disk["failed"]
        return False

class MyCloudDiskOverTempSensor(MyCloudCachedEntity, BinarySensorEntity):
    _attr_icon = "mdi:thermometer-alert"
    def __init__(self, coordinator, device_info, serial_number, disk_name, disk):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{serial_number}_disk_over_temp"
        self._attr_name = f"{disk_name} Over Temperature"
        self._disk_name = disk['name']

    @property
    def is_on(self):
        disks = self.coordinator.data["system_info"]["disks"]
        for disk in disks:
            if disk["name"] == self._disk_name:
                return disk["over_temp"]
        return False

# -- Volumes --

class MyCloudVolumeSizeSensor(MyCloudCachedEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES

    _attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES
    _attr_icon = "mdi:harddisk"

    def __init__(self, coordinator, device_info, volume_name, volume):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{volume['id']}_volume_size"
        self._attr_name = f"{volume_name} Size"
        self._volume_id = volume['id']

    @property
    def native_value(self):
        volumes = self.coordinator.data.get("system_info", {}).get("volumes", [])
        for volume in volumes:
            if volume["id"] == self._volume_id:
                try:
                    return int(volume["size"])
                except (TypeError, ValueError):
                    return None
        return None

class MyCloudVolumeMountedSensor(MyCloudCachedEntity, BinarySensorEntity):
    _attr_icon = "mdi:folder-pound"

    def __init__(self, coordinator, device_info, volume_name, volume):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{volume_name}_volume_mounted"
        self._attr_name = f"{volume_name} Mounted"
        self._volume_name = volume['name']

    @property
    def is_on(self):
        volumes = self.coordinator.data["system_info"]["volumes"]
        for volume in volumes:
            if volume["name"] == self._volume_name:
                return volume["mounted"]
        return False

class MyCloudVolumeUnlockedSensor(MyCloudCachedEntity, BinarySensorEntity):
    _attr_icon = "mdi:lock-open"

    def __init__(self, coordinator, device_info, volume_name, volume):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{volume_name}_volume_unlocked"
        self._attr_name = f"{volume_name} Unlocked"
        self._volume_name = volume['name']

    @property
    def is_on(self):
        volumes = self.coordinator.data["system_info"]["volumes"]
        for volume in volumes:
            if volume["name"] == self._volume_name:
                return volume["unlocked"]
        return False
        
class MyCloudVolumeEncryptedSensor(MyCloudCachedEntity, BinarySensorEntity):
    _attr_icon = "mdi:lock"

    def __init__(self, coordinator, device_info, volume_name, volume):
        super().__init__(coordinator)
        self._attr_device_info = device_info
        self._attr_unique_id = f"{volume_name}_volume_encrypted"
        self._attr_name = f"{volume_name} Encrypted"
        self._volume_name = volume['name']

    @property
    def is_on(self):
        volumes = self.coordinator.data["system_info"]["volumes"]
        for volume in volumes:
            if volume["name"] == self._volume_name:
                return volume["encrypted"]
        return False
