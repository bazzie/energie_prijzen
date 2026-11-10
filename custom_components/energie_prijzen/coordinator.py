"""DataUpdateCoordinator for the Energie Prijzen integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EnergyType, PriceSource, PriceSourceError, covers
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
    MAX_REFRESH_INTERVAL,
    RETRY_BACKOFF,
    TOMORROW_PUBLISH_TIME,
)
from .pricing import (
    MissingEnergyTaxError,
    PricePoint,
    PriceSettings,
    build_price_points,
    local_day_bounds,
    price_at,
)

type EnergiePrijzenConfigEntry = ConfigEntry[EnergiePrijzenCoordinator]

MIN_REFRESH_DELAY = timedelta(seconds=30)
# Small delay after a gas day ends before asking for the next one.
GAS_DAY_GRACE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class EnergyPriceData:
    """Cached prices for today and tomorrow, per energy type."""

    prices: Mapping[EnergyType, tuple[PricePoint, ...]] = field(default_factory=dict)

    def get(self, energy_type: EnergyType) -> tuple[PricePoint, ...]:
        """Return the price points for one energy type."""
        return self.prices.get(energy_type, ())


def settings_from_options(
    options: Mapping[str, Any],
) -> dict[EnergyType, PriceSettings]:
    """Build the price settings per energy type from config entry options."""
    vat = float(options.get(CONF_VAT_PERCENTAGE, DEFAULT_VAT_PERCENTAGE))
    # Entries created before automatic energy tax existed keep manual behaviour.
    auto = bool(options.get(CONF_ENERGY_TAX_AUTO, False))

    def _optional(key: str) -> float | None:
        value = options.get(key)
        return None if value is None else float(value)

    return {
        EnergyType.ELECTRICITY: PriceSettings(
            surcharge=float(options[CONF_ELECTRICITY_SURCHARGE]),
            energy_tax=_optional(CONF_ELECTRICITY_ENERGY_TAX),
            vat_percentage=vat,
            energy_tax_auto=auto,
        ),
        EnergyType.GAS: PriceSettings(
            surcharge=float(options[CONF_GAS_SURCHARGE]),
            energy_tax=_optional(CONF_GAS_ENERGY_TAX),
            vat_percentage=vat,
            energy_tax_auto=auto,
        ),
    }


class EnergiePrijzenCoordinator(DataUpdateCoordinator[EnergyPriceData]):
    """Fetch market prices, cache today + tomorrow and compute all-in prices.

    The refresh moment is computed after every update:
    - all data present: next refresh at the next publication moment (14:00);
    - tomorrow missing before 14:00: refresh at 14:00 (plus a small jitter);
    - tomorrow missing after 14:00, current price missing or an error: retry with
      an increasing backoff (see RETRY_BACKOFF).

    Sensor states switch at every quarter of an hour via a time listener, without
    any API calls.
    """

    config_entry: EnergiePrijzenConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: EnergiePrijzenConfigEntry,
        source: PriceSource,
        timezone: tzinfo,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=MIN_REFRESH_DELAY,
            always_update=False,
        )
        self.source = source
        self.timezone = timezone
        self.settings = settings_from_options(config_entry.options)
        self._points: dict[EnergyType, tuple[PricePoint, ...]] = {}
        self._failing: dict[EnergyType, str] = {}
        self.retry_count = 0
        self.next_refresh: datetime | None = None
        self.last_successful_fetch: datetime | None = None
        # Spread requests of different installations around the publish moment.
        self._publish_jitter = timedelta(seconds=random.randint(0, 300))

    @property
    def failing_sources(self) -> dict[EnergyType, str]:
        """Return the energy types whose last fetch failed, with the error."""
        return dict(self._failing)

    def local_today(self, now: datetime | None = None) -> date:
        """Return today's date in the market timezone."""
        return (now or dt_util.utcnow()).astimezone(self.timezone).date()

    @callback
    def async_start_interval_ticks(self) -> CALLBACK_TYPE:
        """Update entities at every quarter-hour boundary, without fetching."""

        @callback
        def _tick(_now: datetime) -> None:
            if self.data is not None:
                self.async_update_listeners()

        return async_track_utc_time_change(
            self.hass, _tick, minute=(0, 15, 30, 45), second=0
        )

    async def _async_update_data(self) -> EnergyPriceData:
        """Fetch prices for today and tomorrow."""
        now = dt_util.utcnow()
        today = self.local_today(now)
        window_start, _ = local_day_bounds(today, self.timezone)
        _, window_end = local_day_bounds(today + timedelta(days=1), self.timezone)

        errors: dict[EnergyType, Exception] = {}
        for energy_type in EnergyType:
            try:
                raw = await self.source.async_get_prices(
                    energy_type, window_start, window_end
                )
                # On failure the previous points for this type stay in use.
                self._points[energy_type] = build_price_points(
                    (p for p in raw if p.end > window_start),
                    self.settings[energy_type],
                )
            except (PriceSourceError, MissingEnergyTaxError) as err:
                errors[energy_type] = err
            self._log_source_state(energy_type, errors.get(energy_type))

        if len(errors) == len(EnergyType):
            # Keep the last valid data (coordinator keeps self.data) and retry.
            self._set_next_refresh(now, self._backoff_delay())
            raise UpdateFailed(
                f"Error fetching prices from {self.source.name}: "
                f"{next(iter(errors.values()))}"
            )

        self.last_successful_fetch = now
        data = EnergyPriceData(
            prices={
                energy_type: tuple(p for p in points if p.end > window_start)
                for energy_type, points in self._points.items()
            }
        )
        self._set_next_refresh(now, self._compute_delay(now, data, bool(errors)))
        return data

    def _log_source_state(
        self, energy_type: EnergyType, error: Exception | None
    ) -> None:
        """Log a failing source once, and log when it recovers."""
        if error is None:
            if self._failing.pop(energy_type, None) is not None:
                LOGGER.info("Fetching %s prices recovered", energy_type)
            return
        if energy_type not in self._failing:
            LOGGER.warning(
                "Error fetching %s prices from %s, keeping last known prices: %s",
                energy_type,
                self.source.name,
                error,
            )
        self._failing[energy_type] = str(error)

    def _compute_delay(
        self, now: datetime, data: EnergyPriceData, had_errors: bool
    ) -> timedelta:
        """Return the delay until the next refresh."""
        electricity = data.get(EnergyType.ELECTRICITY)
        gas = data.get(EnergyType.GAS)

        if (
            had_errors
            or price_at(electricity, now) is None
            or price_at(gas, now) is None
        ):
            return self._backoff_delay()

        today = self.local_today(now)
        tomorrow_start, tomorrow_end = local_day_bounds(
            today + timedelta(days=1), self.timezone
        )
        publish_today = self._publish_moment(today)

        if not covers(electricity, tomorrow_start, tomorrow_end):
            if now < publish_today:
                self.retry_count = 0
                return publish_today - now
            LOGGER.debug("Prices for tomorrow not yet available, retrying later")
            return self._backoff_delay()

        self.retry_count = 0
        publish_next = (
            publish_today
            if now < publish_today
            else self._publish_moment(today + timedelta(days=1))
        )
        # Make sure we fetch the next gas day once the known gas prices run out.
        gas_known_until = max(p.end for p in gas) + GAS_DAY_GRACE
        next_moment = min(publish_next, gas_known_until, now + MAX_REFRESH_INTERVAL)
        return next_moment - now

    def _publish_moment(self, day: date) -> datetime:
        """Return the (jittered) moment tomorrow's prices are expected, in UTC."""
        local = datetime.combine(day, TOMORROW_PUBLISH_TIME, self.timezone)
        return local.astimezone(UTC) + self._publish_jitter

    def _backoff_delay(self) -> timedelta:
        delay = RETRY_BACKOFF[min(self.retry_count, len(RETRY_BACKOFF) - 1)]
        self.retry_count += 1
        return timedelta(seconds=delay)

    def _set_next_refresh(self, now: datetime, delay: timedelta) -> None:
        delay = max(delay, MIN_REFRESH_DELAY)
        self.update_interval = delay
        self.next_refresh = now + delay
        LOGGER.debug("Next price refresh at %s", self.next_refresh)
