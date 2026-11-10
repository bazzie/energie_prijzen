"""Tests for the EnergyZero client."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.energie_prijzen.api import (
    EnergyType,
    EnergyZeroClient,
    MarketPrice,
    PriceSourceConnectionError,
    PriceSourceResponseError,
)
from custom_components.energie_prijzen.pricing import local_day_bounds

from .conftest import AMS, gas_day_prices, quarter_prices

URL = EnergyZeroClient.BASE_URL
ENERGY_TAX = 0.09161


def api_response(prices: list[MarketPrice]) -> dict[str, Any]:
    """Build a response in the EnergyZero format."""

    def stream(factor: float, add: float = 0) -> list[dict[str, Any]]:
        return [
            {
                "start": p.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": p.end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "price": {"value": str(round((p.price + add) * factor, 7))},
            }
            for p in prices
        ]

    return {
        "interval": "RESPONSE_INTERVAL_QUARTER",
        "base": stream(1),
        "base_with_vat": stream(1.21),
        "all_in": stream(1, ENERGY_TAX),
        "all_in_with_vat": stream(1.21, ENERGY_TAX),
    }


def params(
    day: date,
    energy_type: str = "ENERGY_TYPE_ELECTRICITY",
    interval: str = "INTERVAL_QUARTER",
) -> dict[str, str]:
    """Return the expected query parameters."""
    return {
        "energyType": energy_type,
        "date": day.strftime("%d-%m-%Y"),
        "interval": interval,
    }


def window(day: date) -> tuple[datetime, datetime]:
    """Today + tomorrow window."""
    start, _ = local_day_bounds(day, AMS)
    _, end = local_day_bounds(day + timedelta(days=1), AMS)
    return start, end


async def test_today_and_tomorrow_in_one_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A response that includes the neighbouring days needs only one request."""
    day = date(2026, 11, 10)
    aioclient_mock.get(
        URL,
        params=params(day),
        json=api_response(quarter_prices(day - timedelta(days=1), 3)),
    )
    client = EnergyZeroClient(async_get_clientsession(hass), AMS)

    result = await client.async_get_prices(EnergyType.ELECTRICITY, *window(day))

    assert aioclient_mock.call_count == 1
    assert len(result) == 192
    assert result[0].start == datetime(2026, 11, 10, 0, 0, tzinfo=AMS)
    assert result[-1].end == datetime(2026, 11, 12, 0, 0, tzinfo=AMS)
    assert result[0].price == quarter_prices(day)[0].price
    # Energy tax = all_in - base, exact (no float noise).
    assert {p.energy_tax for p in result} == {ENERGY_TAX}


async def test_energy_tax_missing_all_in_stream(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without an all_in stream the energy tax is unknown."""
    day = date(2026, 11, 10)
    response = api_response(quarter_prices(day))
    del response["all_in"]
    aioclient_mock.get(URL, params=params(day), json=response)
    aioclient_mock.get(URL, params=params(day + timedelta(days=1)), status=404)
    client = EnergyZeroClient(async_get_clientsession(hass), AMS)

    result = await client.async_get_prices(EnergyType.ELECTRICITY, *window(day))
    assert len(result) == 96
    assert {p.energy_tax for p in result} == {None}


async def test_tomorrow_not_available(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 404 for tomorrow is not an error; only today is returned."""
    day = date(2026, 11, 10)
    aioclient_mock.get(URL, params=params(day), json=api_response(quarter_prices(day)))
    aioclient_mock.get(
        URL,
        params=params(day + timedelta(days=1)),
        status=404,
        json={
            "code": 5,
            "message": "no prices found for the given date",
            "details": [],
        },
    )
    client = EnergyZeroClient(async_get_clientsession(hass), AMS)

    result = await client.async_get_prices(EnergyType.ELECTRICITY, *window(day))

    assert aioclient_mock.call_count == 2
    assert len(result) == 96


async def test_today_not_available(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 404 for today returns an empty list and stops."""
    day = date(2026, 11, 10)
    aioclient_mock.get(URL, params=params(day), status=404, json={"code": 5})
    client = EnergyZeroClient(async_get_clientsession(hass), AMS)

    assert await client.async_get_prices(EnergyType.ELECTRICITY, *window(day)) == []
    assert aioclient_mock.call_count == 1


async def test_gas_prices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Gas uses day intervals from 06:00 to 06:00."""
    day = date(2026, 11, 10)
    aioclient_mock.get(
        URL,
        params=params(day, "ENERGY_TYPE_GAS", "INTERVAL_DAY"),
        json=api_response(gas_day_prices(day - timedelta(days=1), 2)),
    )
    aioclient_mock.get(
        URL,
        params=params(day + timedelta(days=1), "ENERGY_TYPE_GAS", "INTERVAL_DAY"),
        status=404,
    )
    client = EnergyZeroClient(async_get_clientsession(hass), AMS)

    result = await client.async_get_prices(EnergyType.GAS, *window(day))

    assert [p.start for p in result] == [
        datetime(2026, 11, 9, 6, tzinfo=AMS),
        datetime(2026, 11, 10, 6, tzinfo=AMS),
    ]


async def test_dst_day_request(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The 23-hour day of 29 March returns 92 quarters."""
    day = date(2026, 3, 29)
    aioclient_mock.get(URL, params=params(day), json=api_response(quarter_prices(day)))
    aioclient_mock.get(URL, params=params(day + timedelta(days=1)), status=404)
    client = EnergyZeroClient(async_get_clientsession(hass), AMS)

    result = await client.async_get_prices(EnergyType.ELECTRICITY, *window(day))
    assert len(result) == 92


@pytest.mark.parametrize(
    ("mock_kwargs", "error"),
    [
        ({"status": 500}, PriceSourceConnectionError),
        ({"exc": ClientError()}, PriceSourceConnectionError),
        ({"exc": TimeoutError()}, PriceSourceConnectionError),
        ({"text": "not json"}, PriceSourceResponseError),
        ({"json": {"unexpected": []}}, PriceSourceResponseError),
        ({"json": {"base": [{"start": "x"}]}}, PriceSourceResponseError),
    ],
)
async def test_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    """HTTP, connection and format errors raise PriceSource errors."""
    day = date(2026, 11, 10)
    aioclient_mock.get(URL, params=params(day), **mock_kwargs)
    client = EnergyZeroClient(async_get_clientsession(hass), AMS)

    with pytest.raises(error):
        await client.async_get_prices(EnergyType.ELECTRICITY, *window(day))
