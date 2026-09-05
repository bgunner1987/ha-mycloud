"""WD My Cloud integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_POWER_PROBE_INTERVAL,
    DEFAULT_POWER_PROBE_INTERVAL,
    DOMAIN,
    LEGACY_DEFAULT_POWER_PROBE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move the formerly stored 60-second default to the v2 default once."""
    if entry.version >= 2:
        return True
    options = dict(entry.options)
    if (
        options.get(CONF_POWER_PROBE_INTERVAL)
        == LEGACY_DEFAULT_POWER_PROBE_INTERVAL
    ):
        options[CONF_POWER_PROBE_INTERVAL] = DEFAULT_POWER_PROBE_INTERVAL
    hass.config_entries.async_update_entry(entry, options=options, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one My Cloud config entry."""
    _LOGGER.info("Setting up My Cloud integration")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and exactly its own resources."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    resources = domain_data.pop(entry.entry_id, {})
    async_close = resources.get("async_close")
    if async_close is not None:
        await async_close()
    if not domain_data:
        hass.data.pop(DOMAIN, None)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
