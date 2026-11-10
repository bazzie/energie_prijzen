"""Tests for the config and options flow."""

from __future__ import annotations

from datetime import date, datetime

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energie_prijzen.api import PriceSourceConnectionError
from custom_components.energie_prijzen.const import (
    CONF_ENERGY_TAX_AUTO,
    CONF_GAS_ENERGY_TAX,
    CONF_VAT_PERCENTAGE,
    DOMAIN,
)

from .conftest import AMS, AUTO_OPTIONS, OPTIONS, FakePriceSource, quarter_prices


def suggested(result: dict) -> dict:
    """Return the pre-filled values (defaults and suggested values) of a form."""
    values = {}
    for key in result["data_schema"].schema:
        if (value := (key.description or {}).get("suggested_value")) is not None:
            values[str(key)] = value
        elif callable(key.default):
            values[str(key)] = key.default()
    return values


async def test_user_flow(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """The user flow creates an entry with the values as options."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=OPTIONS
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Energie Prijzen"
    assert result["data"] == {}
    assert result["options"] == OPTIONS


async def test_user_flow_default_vat(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """VAT defaults to 21%."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert suggested(result) == {CONF_VAT_PERCENTAGE: 21.0, CONF_ENERGY_TAX_AUTO: True}


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """A failing price source shows an error and allows a retry."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    mock_source.set_error(PriceSourceConnectionError("down"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=OPTIONS
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    mock_source.set_error(None)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=OPTIONS
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_single_instance(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_source: FakePriceSource
) -> None:
    """Only one entry is allowed."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The options flow shows current values and stores new ones."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert suggested(result) == OPTIONS

    new_options = {**OPTIONS, "electricity_surcharge": 0.03}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=new_options
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == new_options
    assert config_entry.runtime_data.settings["electricity"].surcharge == 0.03


async def test_user_flow_automatic_energy_tax(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """With automatic energy tax the manual fields may stay empty."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=AUTO_OPTIONS
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == AUTO_OPTIONS


async def test_user_flow_manual_requires_energy_tax(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """Manual mode needs both energy tax values."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    user_input = {**OPTIONS}
    del user_input[CONF_GAS_ENERGY_TAX]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "energy_tax_required"}


async def test_user_flow_source_without_energy_tax(
    hass: HomeAssistant, mock_source: FakePriceSource, freezer: FrozenDateTimeFactory
) -> None:
    """Automatic without fallback fails if the source has no energy tax."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    mock_source.prices["electricity"] = quarter_prices(
        date(2026, 11, 10), energy_tax=None
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=AUTO_OPTIONS
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "energy_tax_unavailable"}

    # With manual fallback values it is accepted.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            **AUTO_OPTIONS,
            "electricity_energy_tax": 0.1,
            "gas_energy_tax": 0.6,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_options_flow_switch_to_automatic(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_source: FakePriceSource,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Switching to automatic and clearing manual values removes them."""
    freezer.move_to(datetime(2026, 11, 10, 10, 0, tzinfo=AMS))
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=AUTO_OPTIONS
    )
    await hass.async_block_till_done()

    assert config_entry.options == AUTO_OPTIONS
    assert config_entry.runtime_data.settings["electricity"].energy_tax_auto is True
