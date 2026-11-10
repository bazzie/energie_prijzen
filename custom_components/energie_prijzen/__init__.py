"""The Energie Prijzen integration (dynamic Vattenfall prices via EnergyZero)."""

from __future__ import annotations

from datetime import tzinfo

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import create_price_source
from .const import MARKET_TIMEZONE
from .coordinator import EnergiePrijzenConfigEntry, EnergiePrijzenCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_get_market_timezone() -> tzinfo:
    """Return the Dutch market timezone (loaded without blocking the loop)."""
    return await dt_util.async_get_time_zone(MARKET_TIMEZONE) or dt_util.UTC


async def async_setup_entry(
    hass: HomeAssistant, entry: EnergiePrijzenConfigEntry
) -> bool:
    """Set up Energie Prijzen from a config entry."""
    timezone = await async_get_market_timezone()
    source = create_price_source(async_get_clientsession(hass), timezone)
    coordinator = EnergiePrijzenCoordinator(hass, entry, source, timezone)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_start_interval_ticks())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EnergiePrijzenConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
