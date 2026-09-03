"""Tests for config-entry-scoped cleanup."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.mycloud import async_unload_entry
from custom_components.mycloud.const import DOMAIN


@pytest.mark.asyncio
async def test_config_entry_unload_closes_only_its_resources():
    closed = 0

    async def close():
        nonlocal closed
        closed += 1

    class ConfigEntries:
        async def async_unload_platforms(self, entry, platforms):
            return True

    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry-1": {"async_close": close},
                "entry-2": {"async_close": None},
            }
        },
        config_entries=ConfigEntries(),
    )
    entry = SimpleNamespace(entry_id="entry-1")

    assert await async_unload_entry(hass, entry) is True
    assert closed == 1
    assert "entry-1" not in hass.data[DOMAIN]
    assert "entry-2" in hass.data[DOMAIN]
