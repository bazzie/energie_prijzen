"""Tests for the pure price calculations, including DST days."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.energie_prijzen.api import MarketPrice, covers
from custom_components.energie_prijzen.pricing import (
    EnergyTaxSource,
    MissingEnergyTaxError,
    PriceSettings,
    build_price_points,
    calculate_total,
    day_statistics,
    local_day_bounds,
    next_price,
    points_for_day,
    price_at,
    resolve_energy_tax,
)

from .conftest import AMS, quarter_prices

SETTINGS = PriceSettings(surcharge=0.02, energy_tax=0.10, vat_percentage=21)


def test_calculate_total() -> None:
    """All-in price = (market + surcharge + tax) * (1 + VAT)."""
    assert calculate_total(0.10, SETTINGS) == pytest.approx((0.10 + 0.02 + 0.10) * 1.21)
    assert calculate_total(0.10, SETTINGS) == pytest.approx(0.2662)


def test_calculate_total_negative_market_price() -> None:
    """Negative market prices are passed through unchanged."""
    assert calculate_total(-0.30, SETTINGS) == pytest.approx((-0.30 + 0.12) * 1.21)


def test_calculate_total_without_vat() -> None:
    """VAT of 0% does not change the sum."""
    settings = PriceSettings(surcharge=0.01, energy_tax=0.05, vat_percentage=0)
    assert calculate_total(0.10, settings) == pytest.approx(0.16)


def test_build_price_points() -> None:
    """Points are sorted, deduplicated and contain market price incl. VAT."""
    start = datetime(2026, 11, 10, 9, tzinfo=UTC)
    raw = [
        MarketPrice(start + timedelta(minutes=15), start + timedelta(minutes=30), 0.2),
        MarketPrice(start, start + timedelta(minutes=15), 0.1),
        MarketPrice(start, start + timedelta(minutes=15), 0.1),
    ]
    points = build_price_points(raw, SETTINGS)
    assert [p.start for p in points] == [start, start + timedelta(minutes=15)]
    assert points[0].market == 0.1
    assert points[0].market_incl_vat == pytest.approx(0.121)
    assert points[0].total == pytest.approx(0.2662)


@pytest.mark.parametrize(
    ("day", "hours", "quarters"),
    [
        (date(2026, 11, 10), 24, 96),
        (date(2026, 3, 29), 23, 92),  # start of summer time
        (date(2026, 10, 25), 25, 100),  # end of summer time
    ],
)
def test_dst_days(day: date, hours: int, quarters: int) -> None:
    """Day bounds and per-day intervals respect 23 and 25 hour days."""
    start, end = local_day_bounds(day, AMS)
    assert end - start == timedelta(hours=hours)

    points = build_price_points(
        quarter_prices(day - timedelta(days=1), days=3), SETTINGS
    )
    today = points_for_day(points, day, AMS)
    assert len(today) == quarters
    assert today[0].start == start
    assert today[-1].end == end
    assert covers(today, start, end)


def test_price_at_on_dst_end_repeated_hour() -> None:
    """On 25 October the local hour 02:00-03:00 occurs twice; both are distinct."""
    points = build_price_points(quarter_prices(date(2026, 10, 25)), SETTINGS)
    first = datetime(2026, 10, 25, 2, 0, tzinfo=AMS, fold=0)  # CEST, 00:00 UTC
    second = datetime(2026, 10, 25, 2, 0, tzinfo=AMS, fold=1)  # CET, 01:00 UTC
    assert first.astimezone(UTC) != second.astimezone(UTC)
    p1 = price_at(points, first)
    p2 = price_at(points, second)
    assert p1 is not None
    assert p2 is not None
    assert p1.start != p2.start
    assert p2.start - p1.start == timedelta(hours=1)


def test_price_at_on_dst_start() -> None:
    """On 29 March 01:45 is followed directly by 03:00 local time."""
    points = build_price_points(quarter_prices(date(2026, 3, 29)), SETTINGS)
    moment = datetime(2026, 3, 29, 1, 50, tzinfo=AMS)
    current = price_at(points, moment)
    following = next_price(points, moment)
    assert current is not None
    assert following is not None
    assert current.start.astimezone(AMS).hour == 1
    assert following.start.astimezone(AMS).hour == 3
    assert following.start == current.end


def test_price_at_boundaries() -> None:
    """Start is inclusive, end is exclusive, outside the data returns None."""
    points = build_price_points(quarter_prices(date(2026, 11, 10)), SETTINGS)
    first = points[0]
    assert price_at(points, first.start) == first
    assert price_at(points, first.end) == points[1]
    assert price_at(points, first.start - timedelta(seconds=1)) is None
    assert price_at(points, points[-1].end) is None
    assert next_price(points, points[-1].start) is None


def test_next_price_over_midnight() -> None:
    """Next price after 23:45 is tomorrow 00:00 when tomorrow is known."""
    points = build_price_points(quarter_prices(date(2026, 11, 10), days=2), SETTINGS)
    moment = datetime(2026, 11, 10, 23, 50, tzinfo=AMS)
    following = next_price(points, moment)
    assert following is not None
    assert following.start == datetime(2026, 11, 11, 0, 0, tzinfo=AMS)


def test_day_statistics() -> None:
    """Average, min, max and their moments (first occurrence wins)."""
    start = datetime(2026, 11, 10, 0, tzinfo=AMS)
    raw = [
        MarketPrice(
            start + timedelta(minutes=15 * i),
            start + timedelta(minutes=15 * (i + 1)),
            value,
        )
        for i, value in enumerate([0.3, 0.1, 0.5, 0.1, 0.5])
    ]
    points = build_price_points(raw, SETTINGS)
    stats = day_statistics(points)
    assert stats is not None
    assert stats.minimum == pytest.approx(calculate_total(0.1, SETTINGS))
    assert stats.maximum == pytest.approx(calculate_total(0.5, SETTINGS))
    assert stats.average == pytest.approx(calculate_total(0.3, SETTINGS))
    assert stats.cheapest.start == start + timedelta(minutes=15)
    assert stats.most_expensive.start == start + timedelta(minutes=30)
    assert day_statistics([]) is None


def test_day_statistics_time_weighted() -> None:
    """Intervals of different length are weighted by duration."""
    start = datetime(2026, 11, 10, 0, tzinfo=UTC)
    raw = [
        MarketPrice(start, start + timedelta(hours=3), 0.0),
        MarketPrice(start + timedelta(hours=3), start + timedelta(hours=4), 0.4),
    ]
    settings = PriceSettings(0, 0, 0)
    stats = day_statistics(build_price_points(raw, settings))
    assert stats is not None
    assert stats.average == pytest.approx(0.1)


def test_covers_detects_gaps() -> None:
    """A missing interval means the window is not covered."""
    prices = quarter_prices(date(2026, 11, 10))
    start, end = local_day_bounds(date(2026, 11, 10), AMS)
    assert covers(prices, start, end)
    assert not covers(prices[:10] + prices[11:], start, end)
    assert not covers(prices[:-1], start, end)
    assert not covers([], start, end)


AUTO = PriceSettings(
    surcharge=0.02, energy_tax=None, vat_percentage=21, energy_tax_auto=True
)


@pytest.mark.parametrize(
    ("settings", "source_tax", "expected"),
    [
        (AUTO, 0.09161, (0.09161, EnergyTaxSource.AUTOMATIC)),
        # Automatic, but the source has no value: fall back to manual.
        (
            PriceSettings(0.02, 0.10, 21, energy_tax_auto=True),
            None,
            (0.10, EnergyTaxSource.MANUAL),
        ),
        # Manual mode ignores the source value.
        (SETTINGS, 0.09161, (0.10, EnergyTaxSource.MANUAL)),
    ],
)
def test_resolve_energy_tax(
    settings: PriceSettings,
    source_tax: float | None,
    expected: tuple[float, EnergyTaxSource],
) -> None:
    """Automatic energy tax with manual fallback."""
    assert resolve_energy_tax(source_tax, settings) == expected


def test_missing_energy_tax() -> None:
    """Without source value and without manual value the price cannot be computed."""
    start = datetime(2026, 11, 10, 9, tzinfo=UTC)
    raw = [MarketPrice(start, start + timedelta(minutes=15), 0.1)]
    with pytest.raises(MissingEnergyTaxError):
        build_price_points(raw, AUTO)


def test_price_breakdown() -> None:
    """A point knows its composition."""
    start = datetime(2026, 11, 10, 9, tzinfo=UTC)
    raw = [MarketPrice(start, start + timedelta(minutes=15), 0.10, energy_tax=0.09161)]
    (point,) = build_price_points(raw, AUTO)
    assert point.energy_tax == 0.09161
    assert point.energy_tax_source is EnergyTaxSource.AUTOMATIC
    assert point.surcharge == 0.02
    assert point.excl_vat == pytest.approx(0.21161)
    assert point.total == pytest.approx(0.21161 * 1.21)
    assert point.vat == pytest.approx(0.21161 * 0.21)
    assert point.energy_tax_incl_vat == pytest.approx(0.09161 * 1.21)
