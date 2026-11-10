"""Config flow for the Energie Prijzen integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import async_get_market_timezone
from .api import EnergyType, PriceSourceError, create_price_source
from .const import (
    CONF_ELECTRICITY_ENERGY_TAX,
    CONF_ELECTRICITY_SURCHARGE,
    CONF_ENERGY_TAX_AUTO,
    CONF_GAS_ENERGY_TAX,
    CONF_GAS_SURCHARGE,
    CONF_VAT_PERCENTAGE,
    DEFAULT_VAT_PERCENTAGE,
    DOMAIN,
    LOGGER,
    NAME,
    UNIT_EUR_KWH,
    UNIT_EUR_M3,
)
from .pricing import local_day_bounds

MANUAL_TAX_KEYS = (CONF_ELECTRICITY_ENERGY_TAX, CONF_GAS_ENERGY_TAX)


def _number(unit: str, maximum: float = 10) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=maximum,
            step="any",
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ELECTRICITY_SURCHARGE): _number(UNIT_EUR_KWH),
        vol.Required(CONF_GAS_SURCHARGE): _number(UNIT_EUR_M3),
        vol.Required(CONF_ENERGY_TAX_AUTO, default=True): BooleanSelector(),
        vol.Optional(CONF_ELECTRICITY_ENERGY_TAX): _number(UNIT_EUR_KWH),
        vol.Optional(CONF_GAS_ENERGY_TAX): _number(UNIT_EUR_M3),
        vol.Required(CONF_VAT_PERCENTAGE, default=DEFAULT_VAT_PERCENTAGE): _number(
            "%", 100
        ),
    }
)


def _normalize(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Convert numbers to float and drop empty optional fields."""
    return {
        key: bool(value) if key == CONF_ENERGY_TAX_AUTO else float(value)
        for key, value in user_input.items()
        if value is not None
    }


async def _async_validate(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> str | None:
    """Return an error key, or None if the options can be used."""
    manual_complete = all(key in options for key in MANUAL_TAX_KEYS)
    if not options[CONF_ENERGY_TAX_AUTO] and not manual_complete:
        return "energy_tax_required"

    timezone = await async_get_market_timezone()
    source = create_price_source(async_get_clientsession(hass), timezone)
    today = dt_util.utcnow().astimezone(timezone).date()
    start, _ = local_day_bounds(today, timezone)
    try:
        prices = await source.async_get_prices(
            EnergyType.ELECTRICITY, start, start + timedelta(minutes=15)
        )
    except PriceSourceError as err:
        LOGGER.debug("Price source check failed: %s", err)
        return "cannot_connect"

    source_has_tax = (
        source.provides_energy_tax
        and bool(prices)
        and all(p.energy_tax is not None for p in prices)
    )
    if options[CONF_ENERGY_TAX_AUTO] and not manual_complete and not source_has_tax:
        return "energy_tax_unavailable"
    return None


class EnergiePrijzenConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the contract parameters and verify the price source works."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _normalize(user_input)
            if (error := await _async_validate(self.hass, options)) is None:
                return self.async_create_entry(title=NAME, data={}, options=options)
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EnergiePrijzenOptionsFlow:
        """Return the options flow."""
        return EnergiePrijzenOptionsFlow()


class EnergiePrijzenOptionsFlow(OptionsFlowWithReload):
    """Change the contract parameters; the entry reloads automatically."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _normalize(user_input)
            if (error := await _async_validate(self.hass, options)) is None:
                return self.async_create_entry(data=options)
            errors["base"] = error

        current = user_input or {
            # Entries from before automatic energy tax existed used manual values.
            CONF_ENERGY_TAX_AUTO: False,
            **self.config_entry.options,
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(DATA_SCHEMA, current),
            errors=errors,
        )
