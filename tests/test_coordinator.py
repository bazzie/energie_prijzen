"""Tests for the coordinator, refresh scheduling and the sensors."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.energie_prijzen.api import EnergyType, PriceSourceConnectionError
from custom_components.energie_prijzen.const import RETRY_BACKOFF
from custom_components.energie_prijzen.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.energie_prijzen.pricing import PriceSettings, calculate_total

from .conftest import (
    AMS,
    AUTO_OPTIONS,
    OPTIONS,
    SOURCE_ELECTRICITY_TAX,
    SOURCE_GAS_TAX,
    FakePriceSource,
    electricity_price,
    gas_day_prices,
    gas_price,
    quarter_prices,
)

ELECTRICITY = PriceSettings(
    OPTIONS["electricity_surcharge"], OPTIONS["electricity_energy_tax"], 21
)
GAS = PriceSettings(OPTIONS["gas_surcharge"], OPTIONS["gas_energy_tax"], 21)


def expected_total(moment: datetime) -> float:
    """All-in electricity price of the quarter containing ``moment``."""
    start = moment.replace(minute=moment.minute // 15 * 15, second=0, microsecond=0)
    return round(calculate_total(electricity_price(start), ELECTRICITY), 5)


def state_of(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """Return the state of the sensor with the given description key."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", "energie_prijzen", f"{entry.entry_id}_{key}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up the integration."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("mock_source")
async def test_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """All sensors get the correct values."""
    now = datetime(2026, 11, 10, 10, 5, tzinfo=AMS)
    freezer.move_to(now)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    current = state_of(hass, config_entry, "current_price")
    assert float(current.state) == expected_total(now)
    assert current.attributes["unit_of_measurement"] == "EUR/kWh"
    assert current.attributes["state_class"] == "measurement"
    assert len(current.attributes["prices"]) == 96
    assert current.attributes["prices"][0] == {
        "start": "2026-11-10T00:00:00+01:00",
        "end": "2026-11-10T00:15:00+01:00",
        "price": expected_total(datetime(2026, 11, 10, 0, 0, tzinfo=AMS)),
    }
    assert current.attributes["tomorrow_available"] is False
    # Price composition of the current quarter (manual energy tax).
    market = electricity_price(now.replace(minute=0))
    assert current.attributes["market_price"] == round(market, 5)
    assert current.attributes["surcharge"] == 0.02
    assert current.attributes["energy_tax"] == 0.1
    assert current.attributes["energy_tax_source"] == "manual"
    assert current.attributes["price_excl_vat"] == round(market + 0.12, 5)
    assert current.attributes["vat"] == round((market + 0.12) * 0.21, 5)
    tax = state_of(hass, config_entry, "energy_tax")
    assert float(tax.state) == round(0.1 * 1.21, 5)
    assert tax.attributes["price_excl_vat"] == 0.1

    assert float(state_of(hass, config_entry, "next_price").state) == expected_total(
        now + timedelta(minutes=15)
    )
    market = state_of(hass, config_entry, "market_price")
    assert float(market.state) == round(
        electricity_price(now.replace(minute=0)) * 1.21, 5
    )

    # Test prices rise during the day: cheapest 00:00, most expensive 23:45.
    assert float(state_of(hass, config_entry, "lowest_today").state) == expected_total(
        datetime(2026, 11, 10, 0, 0, tzinfo=AMS)
    )
    assert float(state_of(hass, config_entry, "highest_today").state) == expected_total(
        datetime(2026, 11, 10, 23, 45, tzinfo=AMS)
    )
    assert float(state_of(hass, config_entry, "average_today").state) > 0
    assert dt_util.parse_datetime(
        state_of(hass, config_entry, "cheapest_time_today").state
    ) == datetime(2026, 11, 10, 0, 0, tzinfo=AMS)
    assert dt_util.parse_datetime(
        state_of(hass, config_entry, "most_expensive_time_today").state
    ) == datetime(2026, 11, 10, 23, 45, tzinfo=AMS)

    gas_start = datetime(2026, 11, 10, 6, tzinfo=AMS)
    gas_current = state_of(hass, config_entry, "gas_current_price")
    assert float(gas_current.state) == round(
        calculate_total(gas_price(gas_start), GAS), 5
    )
    assert gas_current.attributes["unit_of_measurement"] == "EUR/m³"
    assert state_of(hass, config_entry, "gas_next_price").state == "unavailable"
    assert float(state_of(hass, config_entry, "gas_market_price").state) == round(
        gas_price(gas_start) * 1.21, 5
    )


async def test_interval_switch_without_api_calls(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The state changes at the quarter-hour boundary without fetching."""
    now = datetime(2026, 11, 10, 10, 14, 50, tzinfo=AMS)
    freezer.move_to(now)
    await setup(hass, config_entry)
    calls = mock_source.calls
    assert float(state_of(hass, config_entry, "current_price").state) == expected_total(
        now
    )

    boundary = datetime(2026, 11, 10, 10, 15, tzinfo=AMS)
    freezer.move_to(boundary)
    async_fire_time_changed(hass, boundary)
    await hass.async_block_till_done()

    assert float(state_of(hass, config_entry, "current_price").state) == expected_total(
        boundary
    )
    assert mock_source.calls == calls


async def test_refresh_waits_until_publication(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Before 14:00 without tomorrow the next refresh is at 14:00."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    await setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.next_refresh == datetime(2026, 11, 10, 14, 0, tzinfo=AMS)


async def test_backoff_until_tomorrow_available(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After 14:00 without tomorrow: increasing backoff; then normal schedule."""
    now = datetime(2026, 11, 10, 14, 0, 30, tzinfo=AMS)
    freezer.move_to(now)
    await setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=RETRY_BACKOFF[0])

    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=RETRY_BACKOFF[1])

    # Tomorrow gets published; refresh via the scheduled timer.
    mock_source.prices[EnergyType.ELECTRICITY] = quarter_prices(date(2026, 11, 10), 2)
    now += coordinator.update_interval + timedelta(seconds=1)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    await hass.async_block_till_done()

    assert coordinator.retry_count == 0
    current = state_of(hass, config_entry, "current_price")
    assert current.attributes["tomorrow_available"] is True
    assert len(current.attributes["prices"]) == 192
    # All known: next refresh is capped at 12 hours (before gas runs out at 06:00).
    assert coordinator.next_refresh == now + timedelta(hours=12)


async def test_refresh_when_gas_day_runs_out(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With all electricity known, refresh when the known gas day ends."""
    mock_source.prices[EnergyType.ELECTRICITY] = quarter_prices(date(2026, 11, 10), 2)
    freezer.move_to(datetime(2026, 11, 10, 20, 0, tzinfo=AMS))
    await setup(hass, config_entry)
    assert config_entry.runtime_data.next_refresh == datetime(
        2026, 11, 11, 6, 1, tzinfo=AMS
    )


async def test_complete_data_schedules_next_publication(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With all data known the next refresh is at the next publication or cap."""
    mock_source.prices[EnergyType.ELECTRICITY] = quarter_prices(date(2026, 11, 10), 2)
    mock_source.prices[EnergyType.GAS] = gas_day_prices(date(2026, 11, 9), 3)
    now = datetime(2026, 11, 10, 15, 0, tzinfo=AMS)
    freezer.move_to(now)
    await setup(hass, config_entry)
    # Capped at 12 hours (next publication is tomorrow 14:00).
    assert config_entry.runtime_data.next_refresh == now + timedelta(hours=12)


async def test_keep_data_on_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When all fetches fail the last valid data stays in use."""
    now = datetime(2026, 11, 10, 14, 30, tzinfo=AMS)
    freezer.move_to(now)
    await setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_source.set_error(PriceSourceConnectionError("boom"))

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.data is not None
    assert caplog.text.count("Error fetching electricity prices") == 1

    later = datetime(2026, 11, 10, 14, 45, tzinfo=AMS)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    assert float(state_of(hass, config_entry, "current_price").state) == expected_total(
        later
    )

    mock_source.set_error(None)
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    assert "Fetching electricity prices recovered" in caplog.text


async def test_partial_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failing gas fetch keeps old gas data while electricity updates."""
    freezer.move_to(datetime(2026, 11, 10, 14, 30, tzinfo=AMS))
    await setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    mock_source.set_error(PriceSourceConnectionError("gas down"), EnergyType.GAS)
    mock_source.prices[EnergyType.ELECTRICITY] = quarter_prices(date(2026, 11, 10), 2)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert len(coordinator.data.get(EnergyType.ELECTRICITY)) == 192
    assert len(coordinator.data.get(EnergyType.GAS)) == 2
    assert EnergyType.GAS in coordinator.failing_sources
    # Setup at 14:30 without tomorrow already used the first backoff step.
    assert coordinator.update_interval == timedelta(seconds=RETRY_BACKOFF[1])


async def test_setup_retry_on_first_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without any data the entry goes into setup retry."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    mock_source.set_error(PriceSourceConnectionError("down"))
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_day_rollover_uses_cached_tomorrow(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After midnight, statistics switch to the new day from cache."""
    mock_source.prices[EnergyType.ELECTRICITY] = quarter_prices(date(2026, 11, 10), 2)
    freezer.move_to(datetime(2026, 11, 10, 23, 50, tzinfo=AMS))
    await setup(hass, config_entry)
    mock_source.set_error(PriceSourceConnectionError("offline"))

    midnight = datetime(2026, 11, 11, 0, 0, tzinfo=AMS)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()

    assert (
        dt_util.parse_datetime(
            state_of(hass, config_entry, "cheapest_time_today").state
        )
        == midnight
    )
    assert float(state_of(hass, config_entry, "current_price").state) == expected_total(
        midnight
    )
    assert len(state_of(hass, config_entry, "current_price").attributes["prices"]) == 96


async def test_dst_day_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """On the 25-hour day the price list has 100 entries."""
    mock_source.prices[EnergyType.ELECTRICITY] = quarter_prices(date(2026, 10, 25))
    mock_source.prices[EnergyType.GAS] = gas_day_prices(date(2026, 10, 24), 2)
    freezer.move_to(datetime(2026, 10, 25, 12, 0, tzinfo=AMS))
    await setup(hass, config_entry)
    current = state_of(hass, config_entry, "current_price")
    assert len(current.attributes["prices"]) == 100


async def test_options_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Changing options recalculates the prices."""
    now = datetime(2026, 11, 10, 10, 5, tzinfo=AMS)
    freezer.move_to(now)
    await setup(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={**OPTIONS, "vat_percentage": 0}
    )
    await hass.async_block_till_done()

    settings = PriceSettings(ELECTRICITY.surcharge, ELECTRICITY.energy_tax, 0)
    assert float(state_of(hass, config_entry, "current_price").state) == round(
        calculate_total(electricity_price(now.replace(minute=0)), settings), 5
    )


async def test_diagnostics(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Diagnostics contain the coordinator state."""
    freezer.move_to(datetime(2026, 11, 10, 10, 5, tzinfo=AMS))
    await setup(hass, config_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["options"] == OPTIONS
    assert diagnostics["source"] == "Fake"
    assert diagnostics["prices"]["electricity"]["intervals"] == 96
    assert diagnostics["prices"]["gas"]["current"]["market"] == gas_price(
        datetime(2026, 11, 10, 6, tzinfo=AMS)
    )
    assert diagnostics["timezone"] == "Europe/Amsterdam"


async def test_automatic_energy_tax(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """Automatic mode uses the energy tax from the source."""
    entry = MockConfigEntry(domain="energie_prijzen", data={}, options=AUTO_OPTIONS)
    now = datetime(2026, 11, 10, 10, 5, tzinfo=AMS)
    freezer.move_to(now)
    await setup(hass, entry)

    market = electricity_price(now.replace(minute=0))
    current = state_of(hass, entry, "current_price")
    assert float(current.state) == round(
        (market + 0.02 + SOURCE_ELECTRICITY_TAX) * 1.21, 5
    )
    assert current.attributes["energy_tax"] == SOURCE_ELECTRICITY_TAX
    assert current.attributes["energy_tax_source"] == "automatic"

    tax = state_of(hass, entry, "energy_tax")
    assert float(tax.state) == round(SOURCE_ELECTRICITY_TAX * 1.21, 5)
    assert tax.attributes["energy_tax_source"] == "automatic"
    gas_tax = state_of(hass, entry, "gas_energy_tax")
    assert float(gas_tax.state) == round(SOURCE_GAS_TAX * 1.21, 5)
    assert gas_tax.attributes["unit_of_measurement"] == "EUR/m³"


async def test_automatic_energy_tax_fallback(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """Without source value the manual fallback is used."""
    mock_source.prices[EnergyType.ELECTRICITY] = quarter_prices(
        date(2026, 11, 10), energy_tax=None
    )
    entry = MockConfigEntry(
        domain="energie_prijzen",
        data={},
        options={**AUTO_OPTIONS, "electricity_energy_tax": 0.1, "gas_energy_tax": 0.6},
    )
    freezer.move_to(datetime(2026, 11, 10, 10, 5, tzinfo=AMS))
    await setup(hass, entry)

    current = state_of(hass, entry, "current_price")
    assert current.attributes["energy_tax"] == 0.1
    assert current.attributes["energy_tax_source"] == "manual"
    # Gas still has a source value.
    assert (
        state_of(hass, entry, "gas_current_price").attributes["energy_tax_source"]
        == "automatic"
    )


async def test_automatic_energy_tax_missing(
    hass: HomeAssistant,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No energy tax at all for one type: that type is treated as failing."""
    mock_source.prices[EnergyType.GAS] = gas_day_prices(
        date(2026, 11, 9), 2, energy_tax=None
    )
    entry = MockConfigEntry(domain="energie_prijzen", data={}, options=AUTO_OPTIONS)
    freezer.move_to(datetime(2026, 11, 10, 10, 5, tzinfo=AMS))
    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert EnergyType.GAS in entry.runtime_data.failing_sources
    assert state_of(hass, entry, "gas_current_price").state == "unavailable"
    assert state_of(hass, entry, "current_price").state != "unavailable"
    assert "Error fetching gas prices" in caplog.text
