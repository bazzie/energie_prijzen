"""Pure price calculation helpers (no Home Assistant dependencies)."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

from .api import MarketPrice


class EnergyTaxSource(StrEnum):
    """Where the energy tax of a price point comes from."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"


class MissingEnergyTaxError(ValueError):
    """No energy tax known: not from the source and not configured."""


@dataclass(frozen=True, slots=True)
class PriceSettings:
    """Contract parameters for one energy type. Amounts in EUR/unit excl. VAT."""

    surcharge: float
    energy_tax: float | None
    """Manually configured energy tax; fallback when automatic is unavailable."""
    vat_percentage: float
    energy_tax_auto: bool = False
    """Use the energy tax supplied by the price source when available."""

    @property
    def vat_factor(self) -> float:
        """Return the multiplication factor for VAT, e.g. 1.21."""
        return 1 + self.vat_percentage / 100


@dataclass(frozen=True, slots=True)
class PricePoint:
    """A price interval with market and all-in price."""

    start: datetime
    end: datetime
    market: float
    """Market price excl. VAT."""
    market_incl_vat: float
    """Market price incl. VAT (no surcharge, no energy tax)."""
    total: float
    """All-in price: (market + surcharge + energy tax) * (1 + VAT)."""
    surcharge: float
    """Supplier surcharge excl. VAT."""
    energy_tax: float
    """Energy tax excl. VAT."""
    energy_tax_source: EnergyTaxSource
    vat_factor: float
    """VAT multiplication factor, e.g. 1.21."""

    @property
    def excl_vat(self) -> float:
        """All-in price excluding VAT."""
        return self.market + self.surcharge + self.energy_tax

    @property
    def vat(self) -> float:
        """VAT amount of the all-in price."""
        return self.total - self.excl_vat

    @property
    def energy_tax_incl_vat(self) -> float:
        """Energy tax including VAT."""
        return self.energy_tax * self.vat_factor


@dataclass(frozen=True, slots=True)
class DayStatistics:
    """Statistics of the all-in price for one day."""

    average: float
    minimum: float
    maximum: float
    cheapest: PricePoint
    most_expensive: PricePoint


def resolve_energy_tax(
    source_tax: float | None, settings: PriceSettings
) -> tuple[float, EnergyTaxSource]:
    """Return the energy tax to use and where it comes from.

    Automatic mode prefers the value from the price source and falls back to the
    configured value. Raises MissingEnergyTaxError if neither is available.
    """
    if settings.energy_tax_auto and source_tax is not None:
        return source_tax, EnergyTaxSource.AUTOMATIC
    if settings.energy_tax is not None:
        return settings.energy_tax, EnergyTaxSource.MANUAL
    raise MissingEnergyTaxError("No energy tax from the source and none configured")


def calculate_total(
    market: float, settings: PriceSettings, energy_tax: float | None = None
) -> float:
    """Return the all-in price for a market price (excl. VAT).

    ``energy_tax`` overrides the configured value (e.g. an automatic value).
    """
    tax = settings.energy_tax if energy_tax is None else energy_tax
    if tax is None:
        raise MissingEnergyTaxError("No energy tax available")
    return (market + settings.surcharge + tax) * settings.vat_factor


def build_price_points(
    prices: Iterable[MarketPrice], settings: PriceSettings
) -> tuple[PricePoint, ...]:
    """Convert market prices to price points, sorted and de-duplicated by start.

    Raises MissingEnergyTaxError if the energy tax cannot be determined.
    """
    unique = {item.start.astimezone(UTC): item for item in prices}
    points = []
    for start in sorted(unique):
        item = unique[start]
        tax, tax_source = resolve_energy_tax(item.energy_tax, settings)
        total = calculate_total(item.price, settings, tax)
        points.append(
            PricePoint(
                start=start,
                end=item.end.astimezone(UTC),
                market=item.price,
                market_incl_vat=item.price * settings.vat_factor,
                total=total,
                surcharge=settings.surcharge,
                energy_tax=tax,
                energy_tax_source=tax_source,
                vat_factor=settings.vat_factor,
            )
        )
    return tuple(points)


def price_at(points: Sequence[PricePoint], moment: datetime) -> PricePoint | None:
    """Return the interval containing ``moment`` (start inclusive, end exclusive)."""
    index = bisect_right([p.start for p in points], moment) - 1
    if index >= 0 and points[index].start <= moment < points[index].end:
        return points[index]
    return None


def next_price(points: Sequence[PricePoint], moment: datetime) -> PricePoint | None:
    """Return the interval following the one that contains ``moment``."""
    current = price_at(points, moment)
    reference = current.end if current else moment
    for point in points:
        if point.start >= reference:
            return point
    return None


def local_day_bounds(day: date, timezone: tzinfo) -> tuple[datetime, datetime]:
    """Return start and end (in UTC) of a local calendar day (23, 24 or 25 hours).

    UTC is used on purpose: Python compares and subtracts datetimes that share
    the same tzinfo by wall-clock time, which is wrong across DST changes.
    """
    start = datetime.combine(day, time.min, timezone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, timezone).astimezone(UTC)
    return start, end


def points_for_day(
    points: Iterable[PricePoint], day: date, timezone: tzinfo
) -> list[PricePoint]:
    """Return the intervals starting on the given local day."""
    start, end = local_day_bounds(day, timezone)
    return [p for p in points if start <= p.start < end]


def day_statistics(points: Sequence[PricePoint]) -> DayStatistics | None:
    """Return statistics of the all-in price, time weighted by interval length."""
    if not points:
        return None
    total_seconds = sum((p.end - p.start).total_seconds() for p in points)
    average = (
        sum(p.total * (p.end - p.start).total_seconds() for p in points) / total_seconds
        if total_seconds
        else sum(p.total for p in points) / len(points)
    )
    cheapest = min(points, key=lambda p: p.total)
    most_expensive = max(points, key=lambda p: p.total)
    return DayStatistics(
        average=average,
        minimum=cheapest.total,
        maximum=most_expensive.total,
        cheapest=cheapest,
        most_expensive=most_expensive,
    )
