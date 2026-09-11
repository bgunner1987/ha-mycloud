"""Small Home Assistant stubs for unit-testing the integration in isolation."""

from __future__ import annotations

import sys
import types

homeassistant = types.ModuleType("homeassistant")
components = types.ModuleType("homeassistant.components")
sensor = types.ModuleType("homeassistant.components.sensor")
binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
config_entries = types.ModuleType("homeassistant.config_entries")
const = types.ModuleType("homeassistant.const")
core = types.ModuleType("homeassistant.core")
helpers = types.ModuleType("homeassistant.helpers")
entity = types.ModuleType("homeassistant.helpers.entity")
storage = types.ModuleType("homeassistant.helpers.storage")
selector = types.ModuleType("homeassistant.helpers.selector")
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")


class ConfigEntry:
    pass


class ConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        return super().__init_subclass__(**kwargs)

    def async_create_entry(self, **kwargs):
        return kwargs

    def async_show_form(self, **kwargs):
        return kwargs


class OptionsFlow(ConfigFlow):
    pass


class HomeAssistant:
    pass


class UpdateFailed(Exception):
    pass


class CoordinatorEntity:
    def __init__(self, coordinator, context=None):
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


class DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.data = None
        self.update_method = kwargs.get("update_method")
        self.update_interval = kwargs.get("update_interval")
        self.last_update_success = True
        self.updated_data_calls = 0
        self.update_error_calls = 0
        self._listeners = []

    async def async_config_entry_first_refresh(self):
        self.data = await self.update_method()

    async def async_shutdown(self):
        pass

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True
        self.updated_data_calls += 1
        for listener in tuple(self._listeners):
            listener()

    def async_set_update_error(self, error):
        self.last_update_success = False
        self.update_error_calls += 1

    def async_add_listener(self, update_callback, context=None):
        self._listeners.append(update_callback)

        def remove_listener():
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return remove_listener


config_entries.ConfigEntry = ConfigEntry
config_entries.ConfigFlow = ConfigFlow
config_entries.OptionsFlow = OptionsFlow
core.HomeAssistant = HomeAssistant
core.callback = lambda func: func
sensor.SensorEntity = type("SensorEntity", (), {})
sensor.SensorStateClass = types.SimpleNamespace(MEASUREMENT="measurement")
sensor.SensorDeviceClass = types.SimpleNamespace(
    DATA_SIZE="data_size", TEMPERATURE="temperature"
)
binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})
const.UnitOfTemperature = types.SimpleNamespace(CELSIUS="°C")
const.UnitOfInformation = types.SimpleNamespace(BYTES="B", TERABYTES="TB")
entity.DeviceInfo = lambda **kwargs: kwargs
storage.Store = type("Store", (), {})
selector.TextSelector = lambda config=None: str
selector.TextSelectorConfig = lambda **kwargs: kwargs
selector.TextSelectorType = types.SimpleNamespace(PASSWORD="password")
update_coordinator.CoordinatorEntity = CoordinatorEntity
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
update_coordinator.UpdateFailed = UpdateFailed

homeassistant.components = components
homeassistant.config_entries = config_entries
homeassistant.const = const
homeassistant.core = core
homeassistant.helpers = helpers
components.sensor = sensor
components.binary_sensor = binary_sensor
helpers.entity = entity
helpers.selector = selector
helpers.storage = storage
helpers.update_coordinator = update_coordinator

sys.modules.setdefault("homeassistant", homeassistant)
sys.modules.setdefault("homeassistant.components", components)
sys.modules.setdefault("homeassistant.components.sensor", sensor)
sys.modules.setdefault("homeassistant.components.binary_sensor", binary_sensor)
sys.modules.setdefault("homeassistant.config_entries", config_entries)
sys.modules.setdefault("homeassistant.const", const)
sys.modules.setdefault("homeassistant.core", core)
sys.modules.setdefault("homeassistant.helpers", helpers)
sys.modules.setdefault("homeassistant.helpers.entity", entity)
sys.modules.setdefault("homeassistant.helpers.selector", selector)
sys.modules.setdefault("homeassistant.helpers.storage", storage)
sys.modules.setdefault("homeassistant.helpers.update_coordinator", update_coordinator)
