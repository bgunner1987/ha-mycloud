"""Safe diagnostics, available without a successful WD API snapshot."""

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass, entry):
    """Export only runtime allowlists; never entry data/options or NAS output."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    return {
        **coordinator.runtime_diagnostics,
        **coordinator.power_probe_diagnostics,
        **coordinator.api_diagnostics,
    }
