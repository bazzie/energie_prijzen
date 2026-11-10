"""Diagnostics support for the Energie Prijzen integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import EnergyType
from .coordinator import EnergiePrijzenConfigEntry
from .pricing import price_at


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnergiePrijzenConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (contains no personal data)."""
    coordinator = entry.runtime_data
    now = dt_util.utcnow()
    prices: dict[str, Any] = {}
    for energy_type in EnergyType:
        points = coordinator.data.get(energy_type) if coordinator.data else ()
        current = price_at(points, now)
        prices[energy_type] = {
            "intervals": len(points),
            "first_start": points[0].start.isoformat() if points else None,
            "last_end": points[-1].end.isoformat() if points else None,
            "current": (
                {
                    "start": current.start.isoformat(),
                    "market": current.market,
                    "market_incl_vat": current.market_incl_vat,
                    "surcharge": current.surcharge,
                    "energy_tax": current.energy_tax,
                    "energy_tax_source": current.energy_tax_source,
                    "total": current.total,
                }
                if current
                else None
            ),
        }

    return {
        "options": dict(entry.options),
        "source": coordinator.source.name,
        "source_provides_energy_tax": coordinator.source.provides_energy_tax,
        "timezone": str(coordinator.timezone),
        "last_update_success": coordinator.last_update_success,
        "last_exception": (
            repr(coordinator.last_exception) if coordinator.last_exception else None
        ),
        "last_successful_fetch": (
            coordinator.last_successful_fetch.isoformat()
            if coordinator.last_successful_fetch
            else None
        ),
        "next_refresh": (
            coordinator.next_refresh.isoformat() if coordinator.next_refresh else None
        ),
        "retry_count": coordinator.retry_count,
        "failing_sources": coordinator.failing_sources,
        "prices": prices,
    }
