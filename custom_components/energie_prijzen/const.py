"""Constants for the Energie Prijzen integration."""

from __future__ import annotations

from datetime import time, timedelta
import logging
from typing import Final

DOMAIN: Final = "energie_prijzen"
LOGGER = logging.getLogger(__package__)

NAME: Final = "Energie Prijzen"
MANUFACTURER: Final = "Vattenfall (berekend via EnergyZero)"

# Config / options keys. All prices are in EUR per unit, excluding VAT.
CONF_ELECTRICITY_SURCHARGE: Final = "electricity_surcharge"
CONF_ELECTRICITY_ENERGY_TAX: Final = "electricity_energy_tax"
CONF_ENERGY_TAX_AUTO: Final = "energy_tax_auto"
CONF_GAS_SURCHARGE: Final = "gas_surcharge"
CONF_GAS_ENERGY_TAX: Final = "gas_energy_tax"
CONF_VAT_PERCENTAGE: Final = "vat_percentage"

DEFAULT_VAT_PERCENTAGE: Final = 21.0

# All day-based logic (today/tomorrow, statistics) uses the Dutch market timezone,
# regardless of the timezone configured in Home Assistant.
MARKET_TIMEZONE: Final = "Europe/Amsterdam"

# Day-ahead prices for tomorrow are usually published between 14:00 and 15:00.
TOMORROW_PUBLISH_TIME: Final = time(14, 0)

# Retry schedule (seconds) while waiting for data or after errors. The last value
# is repeated until data becomes available.
RETRY_BACKOFF: Final[tuple[int, ...]] = (300, 600, 1200, 1800, 3600)

# Upper bound for the time between two refreshes, as a safety net.
MAX_REFRESH_INTERVAL: Final = timedelta(hours=12)

# Unique (per config entry) device identifiers.
DEVICE_ELECTRICITY: Final = "electricity"
DEVICE_GAS: Final = "gas"

UNIT_EUR_KWH: Final = "EUR/kWh"
UNIT_EUR_M3: Final = "EUR/m³"

ATTR_PRICES: Final = "prices"
ATTR_TOMORROW_AVAILABLE: Final = "tomorrow_available"
ATTR_MARKET_PRICE: Final = "market_price"
ATTR_SURCHARGE: Final = "surcharge"
ATTR_ENERGY_TAX: Final = "energy_tax"
ATTR_ENERGY_TAX_SOURCE: Final = "energy_tax_source"
ATTR_VAT: Final = "vat"
ATTR_VAT_PERCENTAGE: Final = "vat_percentage"
ATTR_PRICE_EXCL_VAT: Final = "price_excl_vat"
