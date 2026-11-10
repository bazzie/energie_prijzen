"""Shared fixtures for the Energie Prijzen tests."""

from __future__ import annotations

from collections.abc import Callable, Generator
from datetime import date, datetime, time, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energie_prijzen.api import (
    EnergyType,
    MarketPrice,
    PriceSource,
    PriceSourceError,
)
from custom_components.energie_prijzen.const import (
    CONF_ELECTRICITY_ENERGY_TAX,
    CONF_ELECTRICITY_SURCHARGE,
    CONF_ENERGY_TAX_AUTO,
    CONF_GAS_ENERGY_TAX,
    CONF_GAS_SURCHARGE,
    CONF_VAT_PERCENTAGE,
    DOMAIN,
)

AMS = ZoneInfo("Europe/Amsterdam")

# Manual energy tax.
OPTIONS: dict[str, float | bool] = {
    CONF_ELECTRICITY_SURCHARGE: 0.02,
    CONF_GAS_SURCHARGE: 0.08,
    CONF_ENERGY_TAX_AUTO: False,
    CONF_ELECTRICITY_ENERGY_TAX: 0.10,
    CONF_GAS_ENERGY_TAX: 0.60,
    CONF_VAT_PERCENTAGE: 21.0,
}

# Automatic energy tax, without manual fallback.
AUTO_OPTIONS: dict[str, float | bool] = {
    CONF_ELECTRICITY_SURCHARGE: 0.02,
    CONF_GAS_SURCHARGE: 0.08,
    CONF_ENERGY_TAX_AUTO: True,
    CONF_VAT_PERCENTAGE: 21.0,
}

# Energy tax supplied by the fake source.
SOURCE_ELECTRICITY_TAX = 0.09161
SOURCE_GAS_TAX = 0.60066


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in all tests."""


def electricity_price(start: datetime) -> float:
    """Deterministic test market price: depends on the local hour and quarter."""
    local = start.astimezone(AMS)
    return round(0.10 + 0.01 * local.hour + 0.001 * (local.minute // 15), 6)


def gas_price(start: datetime) -> float:
    """Deterministic test gas price per gas day."""
    return round(0.50 + 0.01 * start.astimezone(AMS).day, 6)


def quarter_prices(
    first_day: date,
    days: int = 1,
    price_fn: Callable[[datetime], float] = electricity_price,
    energy_tax: float | None = SOURCE_ELECTRICITY_TAX,
) -> list[MarketPrice]:
    """Quarter-hour prices for whole local days (handles 23/25-hour days)."""
    start = datetime.combine(first_day, time.min, AMS).astimezone(ZoneInfo("UTC"))
    end = datetime.combine(first_day + timedelta(days=days), time.min, AMS).astimezone(
        ZoneInfo("UTC")
    )
    result = []
    cursor = start
    while cursor < end:
        result.append(
            MarketPrice(
                cursor, cursor + timedelta(minutes=15), price_fn(cursor), energy_tax
            )
        )
        cursor += timedelta(minutes=15)
    return result


def gas_day_prices(
    first_day: date, days: int = 1, energy_tax: float | None = SOURCE_GAS_TAX
) -> list[MarketPrice]:
    """Gas day prices (06:00 - 06:00 local) starting on ``first_day``."""
    result = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        start = datetime.combine(day, time(6), AMS)
        end = datetime.combine(day + timedelta(days=1), time(6), AMS)
        result.append(MarketPrice(start, end, gas_price(start), energy_tax))
    return result


class FakePriceSource(PriceSource):
    """In-memory price source."""

    name = "Fake"
    provides_energy_tax = True

    def __init__(self) -> None:
        """Initialize."""
        self.prices: dict[EnergyType, list[MarketPrice]] = {
            EnergyType.ELECTRICITY: [],
            EnergyType.GAS: [],
        }
        self.errors: dict[EnergyType, PriceSourceError | None] = {
            EnergyType.ELECTRICITY: None,
            EnergyType.GAS: None,
        }
        self.calls = 0

    def set_error(self, error: PriceSourceError | None, *types: EnergyType) -> None:
        """Make the given (or all) energy types fail."""
        for energy_type in types or tuple(EnergyType):
            self.errors[energy_type] = error

    async def async_get_prices(
        self, energy_type: EnergyType, start: datetime, end: datetime
    ) -> list[MarketPrice]:
        """Return the stored prices overlapping the window."""
        self.calls += 1
        if (error := self.errors[energy_type]) is not None:
            raise error
        return [p for p in self.prices[energy_type] if p.end > start and p.start < end]


@pytest.fixture
def fake_source() -> FakePriceSource:
    """Return a fake price source with today's and yesterday's prices (2026-11-10)."""
    source = FakePriceSource()
    source.prices[EnergyType.ELECTRICITY] = quarter_prices(date(2026, 11, 10))
    source.prices[EnergyType.GAS] = gas_day_prices(date(2026, 11, 9), 2)
    return source


@pytest.fixture
def mock_source(fake_source: FakePriceSource) -> Generator[FakePriceSource]:
    """Patch the integration to use the fake price source."""

    def _create(*_args: Any) -> FakePriceSource:
        return fake_source

    with (
        patch(
            "custom_components.energie_prijzen.create_price_source", side_effect=_create
        ),
        patch(
            "custom_components.energie_prijzen.config_flow.create_price_source",
            side_effect=_create,
        ),
        patch(
            "custom_components.energie_prijzen.coordinator.random.randint",
            return_value=0,
        ),
    ):
        yield fake_source


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry."""
    return MockConfigEntry(
        domain=DOMAIN, title="Energie Prijzen", data={}, options=OPTIONS
    )
