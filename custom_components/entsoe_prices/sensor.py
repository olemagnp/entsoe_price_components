import asyncio
from typing import Any, Callable, Mapping, Optional

import logging
import voluptuous as vol
from entsoe import EntsoeDayAhead
from entsoe.forex import *
from entsoe.exceptions import DataNotReadyError
import datetime

from aiohttp import ClientError

from homeassistant.components.sensor import PLATFORM_SCHEMA, SCAN_INTERVAL, SensorEntity

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
    HomeAssistantType,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time

from homeassistant.util import dt

from homeassistant.const import (
    CONF_NAME,
    CONF_ACCESS_TOKEN,
    CONF_CURRENCY,
    CONF_UNIT_OF_MEASUREMENT,
    DEVICE_CLASS_MONETARY,
)

from .const import (
    ATTR_TODAY,
    ATTR_TOMORROW,
    ATTR_PRICES,
    ATTR_EXCHANGE_RATE,
    CONF_AREA,
    CONF_API_URL,
    CONF_FOREX_KIND,
    CONF_FOREX_TOKEN,
    FOREX_NORGES_BANK,
    FOREX_EXCHANGE_RATE,
    FORMAT_DATE,
    UPDATE_HOUR,
)

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ACCESS_TOKEN): cv.string,
        vol.Required(CONF_AREA): cv.string,
        vol.Optional(CONF_CURRENCY, default="NOK"): cv.string,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT, default="kWh"): cv.string,
        vol.Optional(CONF_FOREX_KIND): cv.string,
        vol.Optional(CONF_FOREX_TOKEN, default=""): cv.string,
        vol.Optional(CONF_API_URL): cv.string,
        vol.Optional(CONF_NAME): cv.string,
    }
)

SCAN_INTERVAL = datetime.timedelta(seconds=30)


async def async_setup_platform(
    hass: HomeAssistantType,
    config: ConfigType,
    async_add_entities: Callable,
    discovery_info: Optional[DiscoveryInfoType] = None,
):
    session = async_get_clientsession(hass)

    kwargs = {
        key: config[val]
        for key, val in (("currency", CONF_CURRENCY), ("url", CONF_API_URL))
        if val in config
    }

    if config[CONF_FOREX_KIND] is not None:
        forex_kind = config[CONF_FOREX_KIND]
        if forex_kind == FOREX_NORGES_BANK:
            forex = NorgesBankForex(session)
        elif forex_kind == FOREX_EXCHANGE_RATE:
            forex = ExchangeRateForex(config[CONF_FOREX_TOKEN], session=session)
        else:
            raise ValueError(f"Unknown forex kind: {forex_kind}")
    else:
        forex = None

    entsoe = EntsoeDayAhead(
        config[CONF_ACCESS_TOKEN],
        config[CONF_AREA],
        session=session,
        forex=forex,
        **kwargs,
    )
    sensors = [
        EntsoeSensor(
            entsoe,
            hass,
            config[CONF_CURRENCY],
            config[CONF_UNIT_OF_MEASUREMENT],
            config.get(CONF_NAME),
        )
    ]
    async_add_entities(sensors, update_before_add=True)


class EntsoeSensor(SensorEntity):
    def __init__(
        self,
        entsoe: EntsoeDayAhead,
        hass: HomeAssistantType,
        currency: str,
        uom: str,
        name: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.entsoe = entsoe

        self.entsoe_lock = asyncio.Lock()

        self.attrs = {
            ATTR_PRICES: {},
            ATTR_TODAY: None,
            ATTR_TOMORROW: None,
        }
        self._name = (
            name if name is not None else f"Entsoe Day-Ahead Prices: {entsoe.area}"
        )
        self._state = None
        self._available = None
        self._hass = hass
        self.unit = f"{currency}/{uom}"

    @property
    def name(self) -> str:
        "Return the name of the entity"
        return self._name

    @property
    def unique_id(self) -> str:
        return self.entsoe.area

    # @property
    # def device_class(self) -> str:
    #     return DEVICE_CLASS_MONETARY

    @property
    def state_class(self) -> str:
        return "measurement"

    @property
    def available(self) -> bool:
        return self._available

    @property
    def native_value(self) -> float:
        return self._state

    @property
    def native_unit_of_measurement(self) -> str:
        return self.unit

    @property
    def device_state_attributes(self) -> Mapping[str, Any]:
        return self.attrs

    @property
    def should_poll(self) -> bool:
        return True

    def next_day(self):
        self.attrs[ATTR_PRICES][ATTR_TODAY] = self.attrs[ATTR_PRICES].get(ATTR_TOMORROW)
        self.attrs[ATTR_PRICES][ATTR_TOMORROW] = None
        self.attrs[ATTR_TODAY] = self.attrs[ATTR_TOMORROW]
        self.attrs[ATTR_TOMORROW] = None

    async def tomorrow_from_entsoe(self):
        async with self.entsoe_lock:
            tomorrow = dt.as_local(dt.utcnow()) + datetime.timedelta(days=1)
            try:
                await self.entsoe.update(tomorrow)
            except ClientError:
                self._available = False
                self.attrs[ATTR_PRICES][ATTR_TOMORROW] = None
                self.attrs[ATTR_TOMORROW] = None
                _LOGGER.exception("Error getting data from Entsoe for tomorrow.")
                return
            except DataNotReadyError:
                self.attrs[ATTR_PRICES][ATTR_TOMORROW] = None
                self.attrs[ATTR_TOMORROW] = None
                _LOGGER.info("No data ready for tomorrow from Entsoe")
                return

            prices = await self.map_entsoe_prices()
            self.attrs[ATTR_PRICES][ATTR_TOMORROW] = prices
            self.attrs[ATTR_TOMORROW] = dt.as_local(self.entsoe.start).strftime(
                FORMAT_DATE
            )

            self.attrs[ATTR_PRICES][ATTR_TOMORROW].update(await self.get_stats(prices))
            self.attrs[ATTR_PRICES][ATTR_TOMORROW][
                ATTR_EXCHANGE_RATE
            ] = self.entsoe.exchange_rate

            self._available = True

    async def today_from_entsoe(self):
        async with self.entsoe_lock:
            today = dt.as_local(dt.utcnow())

            try:
                await self.entsoe.update(today)

            except ClientError:
                self._available = False
                _LOGGER.exception("Error retrieving data from Entsoe")
                self.attrs[ATTR_PRICES][ATTR_TODAY] = None
                return
            except DataNotReadyError:
                self._available = False
                _LOGGER.exception("Data for today not ready from Entsoe")
                self.attrs[ATTR_PRICES][ATTR_TODAY] = None
                return

            prices = await self.map_entsoe_prices()
            self.attrs[ATTR_PRICES][ATTR_TODAY] = prices
            self.attrs[ATTR_TODAY] = dt.as_local(self.entsoe.start).strftime(
                FORMAT_DATE
            )

            self.attrs[ATTR_PRICES][ATTR_TODAY].update(await self.get_stats(prices))
            self.attrs[ATTR_PRICES][ATTR_TODAY][
                ATTR_EXCHANGE_RATE
            ] = self.entsoe.exchange_rate

            self._available = True

    async def get_stats(self, prices):
        items = prices.items()

        mini = min(items, key=lambda i: i[1])
        maxi = max(items, key=lambda i: i[1])
        mean = sum([i[1] for i in items]) / len(items)

        return {
            "peak_hour": maxi[0],
            "peak": maxi[1],
            "min_hour": mini[0],
            "min": mini[1],
            "avg": mean,
        }

    async def map_entsoe_prices(self):
        if self.entsoe.points is None:
            return

        return {
            dt.as_local(p.begin).hour: p.price_target
            if p.price_target is not None
            else p.price_orig
            for p in self.entsoe.points
        }

    async def async_update(self):
        now = dt.utcnow()
        now_local = dt.as_local(now)

        if self.attrs[ATTR_TODAY] != now_local.strftime(FORMAT_DATE):
            await self.hass.async_add_executor_job(self.next_day)

            if self.attrs[ATTR_PRICES][ATTR_TODAY] is None:
                await self.today_from_entsoe()

        if now.hour >= UPDATE_HOUR and self.attrs[ATTR_PRICES][ATTR_TOMORROW] is None:
            await self.tomorrow_from_entsoe()

        if self._available:
            self._state = self.attrs[ATTR_PRICES][ATTR_TODAY][now_local.hour]
