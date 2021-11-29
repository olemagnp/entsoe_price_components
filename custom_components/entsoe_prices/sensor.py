import asyncio
from typing import Any, Callable, Mapping, Optional

import logging
import voluptuous as vol
from entsoe import EntsoeDayAhead
from entsoe.forex import *
import datetime

from aiohttp import ClientError

from homeassistant.components.sensor import PLATFORM_SCHEMA

from homeassistant.helpers.entity import Entity

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
    HomeAssistantType,
    StateType,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time

from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_CURRENCY,
    CONF_UNIT_OF_MEASUREMENT,
)

from .const import (
    ATTR_TODAY,
    ATTR_TOMORROW,
    ATTR_PRICES,
    CONF_AREA,
    CONF_API_URL,
    CONF_FOREX_KIND,
    CONF_FOREX_TOKEN,
    FOREX_NORGES_BANK,
    FOREX_EXCHANGE_RATE,
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
    }
)


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
    sensors = [EntsoeSensor(entsoe, hass)]
    async_add_entities(sensors, update_before_add=False)

    futures = []
    for sensor in sensors:
        futures.append(sensor.initial_setup())

    await asyncio.gather(*futures)


class EntsoeSensor(Entity):
    def __init__(self, entsoe: EntsoeDayAhead, hass: HomeAssistantType) -> None:
        super().__init__()
        self.entsoe = entsoe

        self.attrs = {ATTR_PRICES: {}}
        self._name = f"Entsoe Day-Ahead Prices: {entsoe.area}"
        self._state = None
        self._available = None
        self._hass = hass

    @property
    def name(self) -> str:
        "Return the name of the entity"
        return self._name

    @property
    def unique_id(self) -> str:
        return self.entsoe.area

    @property
    def available(self) -> bool:
        return self._available

    @property
    def state(self) -> StateType:
        return self._state

    @property
    def device_state_attributes(self) -> Mapping[str, Any]:
        return self.attrs

    @property
    def should_poll(self) -> bool:
        return False

    async def next_day(self):
        self.attrs[ATTR_TODAY] = self.attrs[ATTR_TOMORROW]

        self.attrs[ATTR_TOMORROW] = None

    async def tomorrow_from_entsoe(self):
        try:
            await self.entsoe.update()
        except ClientError:
            self._available = False
            _LOGGER.exception("Error retrieving data from Entsoe")
            self.attrs[ATTR_PRICES][ATTR_TOMORROW] = None


        self.attrs[ATTR_PRICES][ATTR_TOMORROW] = self.map_entsoe_prices()

    async def today_from_entsoe(self):
        yesterday = datetime.datetime.now().replace(
            hour=13, minute=0
        ) - datetime.timedelta(days=1)

        try:
            await self.entsoe.update(yesterday)
        except ClientError:
            self._available = False
            _LOGGER.exception("Error retrieving data from Entsoe")
            self.attrs[ATTR_PRICES][ATTR_TODAY] = None
            return

        self.attrs[ATTR_PRICES][ATTR_TODAY] = self.map_entsoe_prices()

    def map_entsoe_prices(self):
        if self.entsoe.prices is None:
            return

        prices = sorted(self.entsoe.prices, key=lambda p: p.begin)

        return [
            p.price_target if p.price_target is not None else p.price_orig
            for p in prices
        ]

    async def handle_hour_change(self):
        now = datetime.datetime.now()

        hour = now.hour

        if hour == 0:
            self.next_day()

        if hour == 15:
            await self.tomorrow_from_entsoe()

        todays_prices = self.attrs[ATTR_PRICES].get(ATTR_TODAY, None)
        if todays_prices is None:
            await self.today_from_entsoe()
            todays_prices = self.attrs[ATTR_PRICES][ATTR_TODAY]
        
        if todays_prices is None:
            self._available = False
            return

        cur_price = todays_prices[hour]

        self._state = cur_price

        next_trigger = (now + datetime.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        )
        async_track_point_in_time(
            self._hass, lambda: self.handle_hour_change, next_trigger
        )
        self.async_write_ha_state()

    async def initial_setup(self):
        "Update the state of the sensor"
        await asyncio.gather(self.today_from_entsoe(), self.tomorrow_from_entsoe())
        await self.handle_hour_change()
