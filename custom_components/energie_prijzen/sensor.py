"""Sensor platform for the Energie Prijzen integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import EnergyType, covers
from .const import (
    ATTR_ENERGY_TAX,
    ATTR_ENERGY_TAX_SOURCE,
    ATTR_MARKET_PRICE,
    ATTR_PRICE_EXCL_VAT,
    ATTR_PRICES,
    ATTR_SURCHARGE,
    ATTR_TOMORROW_AVAILABLE,
    ATTR_VAT,
    DOMAIN,
    MANUFACTURER,
    UNIT_EUR_KWH,
    UNIT_EUR_M3,
)
from .coordinator import EnergiePrijzenConfigEntry, EnergiePrijzenCoordinator
from .pricing import (
    PricePoint,
    day_statistics,
    local_day_bounds,
    next_price,
    points_for_day,
    price_at,
)

PARALLEL_UPDATES = 0
PRICE_DECIMALS = 5


@dataclass(frozen=True, slots=True)
class PriceContext:
    """Everything a sensor needs to compute its state at a given moment."""

    points: tuple[PricePoint, ...]
    now: datetime
    timezone: tzinfo

    @property
    def today(self) -> list[PricePoint]:
        """Intervals starting today (local time)."""
        return points_for_day(
            self.points, self.now.astimezone(self.timezone).date(), self.timezone
        )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, PRICE_DECIMALS)


def _current_total(ctx: PriceContext) -> float | None:
    point = price_at(ctx.points, ctx.now)
    return _round(point.total) if point else None


def _next_total(ctx: PriceContext) -> float | None:
    point = next_price(ctx.points, ctx.now)
    return _round(point.total) if point else None


def _current_market(ctx: PriceContext) -> float | None:
    point = price_at(ctx.points, ctx.now)
    return _round(point.market_incl_vat) if point else None


def _current_energy_tax(ctx: PriceContext) -> float | None:
    point = price_at(ctx.points, ctx.now)
    return _round(point.energy_tax_incl_vat) if point else None


def _energy_tax_attributes(ctx: PriceContext) -> dict[str, Any]:
    point = price_at(ctx.points, ctx.now)
    if point is None:
        return {}
    return {
        ATTR_PRICE_EXCL_VAT: _round(point.energy_tax),
        ATTR_ENERGY_TAX_SOURCE: point.energy_tax_source,
    }


def _breakdown_attributes(ctx: PriceContext) -> dict[str, Any]:
    """Composition of the current all-in price (amounts excl. VAT, plus VAT)."""
    point = price_at(ctx.points, ctx.now)
    if point is None:
        return {}
    return {
        ATTR_MARKET_PRICE: _round(point.market),
        ATTR_SURCHARGE: _round(point.surcharge),
        ATTR_ENERGY_TAX: _round(point.energy_tax),
        ATTR_ENERGY_TAX_SOURCE: point.energy_tax_source,
        ATTR_PRICE_EXCL_VAT: _round(point.excl_vat),
        ATTR_VAT: _round(point.vat),
    }


def _current_price_attributes(ctx: PriceContext) -> dict[str, Any]:
    return {**_breakdown_attributes(ctx), **_price_list_attributes(ctx)}


def _stat(attr: str) -> Callable[[PriceContext], float | None]:
    def _value(ctx: PriceContext) -> float | None:
        stats = day_statistics(ctx.today)
        return _round(getattr(stats, attr)) if stats else None

    return _value


def _stat_time(attr: str) -> Callable[[PriceContext], datetime | None]:
    def _value(ctx: PriceContext) -> datetime | None:
        stats = day_statistics(ctx.today)
        return getattr(stats, attr).start if stats else None

    return _value


def _price_list_attributes(ctx: PriceContext) -> dict[str, Any]:
    """Prices of today and tomorrow, for charts (ApexCharts/Plotly) and automations."""
    today = ctx.now.astimezone(ctx.timezone).date()
    today_start, _ = local_day_bounds(today, ctx.timezone)
    tomorrow_start, tomorrow_end = local_day_bounds(
        today + timedelta(days=1), ctx.timezone
    )
    return {
        ATTR_PRICES: [
            {
                "start": point.start.astimezone(ctx.timezone).isoformat(),
                "end": point.end.astimezone(ctx.timezone).isoformat(),
                "price": _round(point.total),
            }
            for point in ctx.points
            if point.end > today_start and point.start < tomorrow_end
        ],
        ATTR_TOMORROW_AVAILABLE: covers(ctx.points, tomorrow_start, tomorrow_end),
    }


@dataclass(frozen=True, kw_only=True)
class EnergiePrijzenSensorDescription(SensorEntityDescription):
    """Describes an Energie Prijzen sensor."""

    energy_type: EnergyType
    value_fn: Callable[[PriceContext], float | datetime | None]
    attributes_fn: Callable[[PriceContext], dict[str, Any]] | None = None


def _price_description(
    key: str,
    energy_type: EnergyType,
    value_fn: Callable[[PriceContext], float | None],
    *,
    state_class: SensorStateClass | None = None,
    attributes_fn: Callable[[PriceContext], dict[str, Any]] | None = None,
    enabled: bool = True,
) -> EnergiePrijzenSensorDescription:
    return EnergiePrijzenSensorDescription(
        key=key,
        translation_key=key,
        energy_type=energy_type,
        native_unit_of_measurement=(
            UNIT_EUR_KWH if energy_type is EnergyType.ELECTRICITY else UNIT_EUR_M3
        ),
        state_class=state_class,
        suggested_display_precision=4,
        value_fn=value_fn,
        attributes_fn=attributes_fn,
        entity_registry_enabled_default=enabled,
    )


SENSORS: tuple[EnergiePrijzenSensorDescription, ...] = (
    _price_description(
        "current_price",
        EnergyType.ELECTRICITY,
        _current_total,
        state_class=SensorStateClass.MEASUREMENT,
        attributes_fn=_current_price_attributes,
    ),
    _price_description("next_price", EnergyType.ELECTRICITY, _next_total),
    _price_description("average_today", EnergyType.ELECTRICITY, _stat("average")),
    _price_description("lowest_today", EnergyType.ELECTRICITY, _stat("minimum")),
    _price_description("highest_today", EnergyType.ELECTRICITY, _stat("maximum")),
    EnergiePrijzenSensorDescription(
        key="cheapest_time_today",
        translation_key="cheapest_time_today",
        energy_type=EnergyType.ELECTRICITY,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_stat_time("cheapest"),
    ),
    EnergiePrijzenSensorDescription(
        key="most_expensive_time_today",
        translation_key="most_expensive_time_today",
        energy_type=EnergyType.ELECTRICITY,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_stat_time("most_expensive"),
    ),
    _price_description(
        "market_price",
        EnergyType.ELECTRICITY,
        _current_market,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _price_description(
        "energy_tax",
        EnergyType.ELECTRICITY,
        _current_energy_tax,
        state_class=SensorStateClass.MEASUREMENT,
        attributes_fn=_energy_tax_attributes,
    ),
    _price_description(
        "gas_current_price",
        EnergyType.GAS,
        _current_total,
        state_class=SensorStateClass.MEASUREMENT,
        attributes_fn=_current_price_attributes,
    ),
    _price_description("gas_next_price", EnergyType.GAS, _next_total),
    _price_description(
        "gas_market_price",
        EnergyType.GAS,
        _current_market,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _price_description(
        "gas_energy_tax",
        EnergyType.GAS,
        _current_energy_tax,
        state_class=SensorStateClass.MEASUREMENT,
        attributes_fn=_energy_tax_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergiePrijzenConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        EnergiePrijzenSensor(coordinator, description) for description in SENSORS
    )


class EnergiePrijzenSensor(CoordinatorEntity[EnergiePrijzenCoordinator], SensorEntity):
    """A price sensor. Its state is recomputed at every quarter of an hour."""

    entity_description: EnergiePrijzenSensorDescription
    _attr_has_entity_name = True
    # The price list is large and changes every refresh; don't store it in history.
    _unrecorded_attributes = frozenset({ATTR_PRICES})

    def __init__(
        self,
        coordinator: EnergiePrijzenCoordinator,
        description: EnergiePrijzenSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        entry_id = coordinator.config_entry.entry_id
        energy_type = description.energy_type
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{energy_type}")},
            translation_key=str(energy_type),
            manufacturer=MANUFACTURER,
            model=f"Dynamisch contract ({coordinator.source.name})",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._update_from_data()

    @property
    def available(self) -> bool:
        """Available as long as cached data covers the value (also if a fetch failed)."""
        return self._attr_available

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle new data or a quarter-hour tick."""
        self._update_from_data()
        super()._handle_coordinator_update()

    @callback
    def _update_from_data(self) -> None:
        data = self.coordinator.data
        points = data.get(self.entity_description.energy_type) if data else ()
        ctx = PriceContext(
            points=points, now=dt_util.utcnow(), timezone=self.coordinator.timezone
        )
        value = self.entity_description.value_fn(ctx)
        self._attr_native_value = value
        self._attr_available = value is not None
        if self.entity_description.attributes_fn is not None:
            self._attr_extra_state_attributes = self.entity_description.attributes_fn(
                ctx
            )
