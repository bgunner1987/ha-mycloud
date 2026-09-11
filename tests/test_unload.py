"""Tests for config-entry-scoped cleanup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.mycloud import async_migrate_entry, async_unload_entry
from custom_components.mycloud.const import (
    CONF_POWER_PROBE_INTERVAL,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_config_entry_unload_closes_only_its_resources():
    closed = 0
    listener_removed = 0

    async def close():
        nonlocal closed
        closed += 1

    def remove_startup_listener():
        nonlocal listener_removed
        listener_removed += 1

    class ConfigEntries:
        async def async_unload_platforms(self, entry, platforms):
            return True

    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry-1": {
                    "async_close": close,
                    "remove_startup_listener": remove_startup_listener,
                },
                "entry-2": {"async_close": None},
            }
        },
        config_entries=ConfigEntries(),
    )
    entry = SimpleNamespace(entry_id="entry-1")

    assert await async_unload_entry(hass, entry) is True
    assert closed == 1
    assert listener_removed == 1
    assert "entry-1" not in hass.data[DOMAIN]
    assert "entry-2" in hass.data[DOMAIN]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [(60, 10), (30, 30), (None, None)])
async def test_v1_entry_migrates_only_the_old_probe_default(stored, expected):
    updates = []

    class ConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            updates.append(kwargs)

    options = {"preserved": True}
    if stored is not None:
        options[CONF_POWER_PROBE_INTERVAL] = stored
    entry = SimpleNamespace(version=1, options=options)
    hass = SimpleNamespace(config_entries=ConfigEntries())
    assert await async_migrate_entry(hass, entry)
    assert updates == [{
        "options": {
            "preserved": True,
            **({CONF_POWER_PROBE_INTERVAL: expected} if expected is not None else {}),
        },
        "version": 2,
    }]
