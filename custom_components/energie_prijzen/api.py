"""Market price sources.

The rest of the integration only depends on :class:`PriceSource`,
:class:`MarketPrice`, :class:`EnergyType` and the exceptions defined here. To switch
to another data source (for example ENTSO-E), add a new :class:`PriceSource`
implementation and return it from :func:`create_price_source`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final, Protocol

from aiohttp import ClientError, ClientSession

from .const import LOGGER


class EnergyType(StrEnum):
    """Supported energy types."""

    ELECTRICITY = "electricity"
    GAS = "gas"


@dataclass(frozen=True, slots=True)
class MarketPrice:
    """A market price for one interval, in EUR per unit (kWh or m³), excl. VAT."""

    start: datetime
    end: datetime
    price: float
    energy_tax: float | None = None
    """Energy tax for this interval (EUR/unit excl. VAT), if the source knows it."""


class PriceSourceError(Exception):
    """Generic error of a price source."""


class PriceSourceConnectionError(PriceSourceError):
    """The price source could not be reached or returned an HTTP error."""


class PriceSourceResponseError(PriceSourceError):
    """The price source returned data in an unexpected format."""


class PriceSource(ABC):
    """Abstract source of day-ahead market prices."""

    name: str
    provides_energy_tax: bool = False
    """True if returned prices include ``energy_tax``."""

    @abstractmethod
    async def async_get_prices(
        self, energy_type: EnergyType, start: datetime, end: datetime
    ) -> list[MarketPrice]:
        """Return all known intervals overlapping ``[start, end)``.

        The result is sorted by start time and contains market prices excluding
        VAT (plus the energy tax when ``provides_energy_tax``). Periods for which the source has no data yet (for example tomorrow
        before publication) are simply absent; that is not an error.

        Raises PriceSourceError on connection or format problems.
        """


class Interval(Protocol):
    """Anything with a start and an end."""

    @property
    def start(self) -> datetime:
        """Start of the interval (inclusive)."""

    @property
    def end(self) -> datetime:
        """End of the interval (exclusive)."""


def covers(intervals: Iterable[Interval], start: datetime, end: datetime) -> bool:
    """Return True if ``intervals`` cover ``[start, end)`` without gaps."""
    cursor = start
    for item in sorted(intervals, key=lambda p: p.start):
        if item.end <= cursor:
            continue
        if item.start > cursor:
            return False
        cursor = item.end
        if cursor >= end:
            return True
    return cursor >= end


class EnergyZeroClient(PriceSource):
    """Price source backed by the public EnergyZero REST API.

    Endpoint: ``GET https://public.api.energyzero.nl/public/v1/prices`` with query
    parameters ``energyType`` (``ENERGY_TYPE_ELECTRICITY``/``ENERGY_TYPE_GAS``),
    ``date`` (``DD-MM-YYYY``, local date) and ``interval``
    (``INTERVAL_QUARTER``/``INTERVAL_HOUR``/``INTERVAL_DAY``).

    The response contains the streams ``base`` (market price excl. VAT),
    ``base_with_vat``, ``all_in`` and ``all_in_with_vat``; each item has ``start``
    and ``end`` (UTC, ISO 8601) and ``price.value`` (decimal string). The
    difference ``all_in - base`` is constant per energy type and equals the energy
    tax excl. VAT; it is exposed as ``MarketPrice.energy_tax``. A request
    for a date also returns the neighbouring days when known, so a request for
    today usually returns tomorrow as well. A date without (complete) data
    returns HTTP 404.
    """

    name = "EnergyZero"
    provides_energy_tax = True

    BASE_URL: Final = "https://public.api.energyzero.nl/public/v1/prices"
    _ENERGY_TYPES: Final = {
        EnergyType.ELECTRICITY: ("ENERGY_TYPE_ELECTRICITY", "INTERVAL_QUARTER"),
        EnergyType.GAS: ("ENERGY_TYPE_GAS", "INTERVAL_DAY"),
    }
    _STREAM: Final = "base"
    _ALL_IN_STREAM: Final = "all_in"

    def __init__(
        self,
        session: ClientSession,
        timezone: tzinfo,
        request_timeout: float = 15.0,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._timezone = timezone
        self._request_timeout = request_timeout

    async def async_get_prices(
        self, energy_type: EnergyType, start: datetime, end: datetime
    ) -> list[MarketPrice]:
        """Return all known intervals overlapping ``[start, end)``."""
        collected: dict[datetime, MarketPrice] = {}
        day = start.astimezone(self._timezone).date()
        last_day = (end - timedelta(microseconds=1)).astimezone(self._timezone).date()

        while day <= last_day:
            day_start = max(start, self._start_of_day(day))
            day_end = min(end, self._start_of_day(day + timedelta(days=1)))
            if not covers(collected.values(), day_start, day_end):
                prices = await self._async_fetch_day(energy_type, day)
                if prices is None:
                    # No data for this date (yet); later dates won't have any.
                    break
                for item in prices:
                    collected[item.start] = item
            day += timedelta(days=1)

        return sorted(
            (p for p in collected.values() if p.end > start and p.start < end),
            key=lambda p: p.start,
        )

    def _start_of_day(self, day: date) -> datetime:
        return datetime.combine(day, time.min, self._timezone).astimezone(UTC)

    async def _async_fetch_day(
        self, energy_type: EnergyType, day: date
    ) -> list[MarketPrice] | None:
        """Fetch one date. Returns None if the API has no data for that date."""
        api_type, interval = self._ENERGY_TYPES[energy_type]
        params = {
            "energyType": api_type,
            "date": day.strftime("%d-%m-%Y"),
            "interval": interval,
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-energie_prijzen",
        }
        try:
            async with asyncio.timeout(self._request_timeout):
                response = await self._session.get(
                    self.BASE_URL, params=params, headers=headers
                )
                if response.status == HTTPStatus.NOT_FOUND:
                    LOGGER.debug("No %s prices available for %s", energy_type, day)
                    return None
                if response.status >= HTTPStatus.BAD_REQUEST:
                    raise PriceSourceConnectionError(
                        f"EnergyZero returned HTTP {response.status}"
                    )
                data = await response.json(content_type=None)
        except TimeoutError as err:
            raise PriceSourceConnectionError(
                "Timeout while contacting EnergyZero"
            ) from err
        except ClientError as err:
            raise PriceSourceConnectionError(
                f"Error while contacting EnergyZero: {err}"
            ) from err
        except ValueError as err:
            raise PriceSourceResponseError("EnergyZero returned invalid JSON") from err

        return self._parse(data)

    @classmethod
    def _parse(cls, data: Any) -> list[MarketPrice]:
        if not isinstance(data, dict) or not isinstance(data.get(cls._STREAM), list):
            raise PriceSourceResponseError(
                f"EnergyZero response has no '{cls._STREAM}' list"
            )
        try:
            all_in = {
                item["start"]: Decimal(item["price"]["value"])
                for item in data.get(cls._ALL_IN_STREAM) or []
            }
            result = []
            for item in data[cls._STREAM]:
                base = Decimal(item["price"]["value"])
                tax = all_in.get(item["start"])
                result.append(
                    MarketPrice(
                        start=datetime.fromisoformat(item["start"]).astimezone(UTC),
                        end=datetime.fromisoformat(item["end"]).astimezone(UTC),
                        price=float(base),
                        energy_tax=None if tax is None else float(tax - base),
                    )
                )
        except (KeyError, TypeError, ValueError, ArithmeticError) as err:
            raise PriceSourceResponseError(
                f"Unexpected item in EnergyZero response: {err}"
            ) from err
        return result


def create_price_source(session: ClientSession, timezone: tzinfo) -> PriceSource:
    """Return the configured price source."""
    return EnergyZeroClient(session, timezone)
