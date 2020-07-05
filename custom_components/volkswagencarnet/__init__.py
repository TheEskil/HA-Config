# -*- coding: utf-8 -*-
import logging
import voluptuous as vol
from volkswagencarnet import Connection
import homeassistant.helpers.config_validation as cv

from datetime import timedelta
from homeassistant.const import (CONF_USERNAME, CONF_PASSWORD, CONF_RESOURCES, CONF_NAME, CONF_SCAN_INTERVAL)
from homeassistant.helpers import discovery
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util.dt import utcnow
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.icon import icon_for_battery_level

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'volkswagencarnet'
DATA_KEY = DOMAIN
CONF_REGION = 'region'
DEFAULT_REGION = 'SV'
CONF_MUTABLE = 'mutable'
CONF_SPIN = 'spin'
CONF_SCANDINAVIAN_MILES = "scandinavian_miles"

SIGNAL_STATE_UPDATED = f'{DOMAIN}.updated'

MIN_UPDATE_INTERVAL = timedelta(minutes=1)
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=5)

COMPONENTS = {
    'sensor': 'sensor',
    'binary_sensor': 'binary_sensor',
    'lock': 'lock',
    'device_tracker': 'device_tracker',
    'switch': 'switch',
    'climate': 'climate'
}

RESOURCES = [
    'position',
    'distance',
    'electric_climatisation',
    'combustion_climatisation',
    'window_heater',
    'combustion_engine_heating',
    'charging',
    'adblue_level',
    'battery_level',
    'fuel_level',
    'service_inspection',
    'oil_inspection',
    'last_connected',
    'charging_time_left',
    'electric_range',
    'combustion_range',
    'combined_range',
    'charge_max_ampere',
    'climatisation_target_temperature',
    'external_power',
    'parking_light',
    'climatisation_without_external_power',
    'door_locked',
    'trunk_locked',
    'request_in_progress',
    'windows_closed',
    'trip_last_average_speed',
    'trip_last_average_electric_consumption',
    'trip_last_average_fuel_consumption',
    'trip_last_duration',
    'trip_last_length'
]

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): cv.string,
        vol.Optional(CONF_MUTABLE, default=True): cv.boolean,
        vol.Optional(CONF_SPIN, default=''): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): (
            vol.All(cv.time_period, vol.Clamp(min=MIN_UPDATE_INTERVAL))),
        # vol.Optional(CONF_NAME, default={}): vol.Schema(
        #     {cv.slug: cv.string}),
        vol.Optional(CONF_NAME, default={}): cv.schema_with_slug_keys(
            cv.string
        ),
        vol.Optional(CONF_RESOURCES): vol.All(
            cv.ensure_list, [vol.In(RESOURCES)]
        ),
        vol.Optional(CONF_SCANDINAVIAN_MILES, default=False): cv.boolean
    }),
}, extra=vol.ALLOW_EXTRA)

async def async_setup(hass, config):
    """Setup Volkswagen Carnet component"""
    session = async_get_clientsession(hass)

    _LOGGER.debug("Creating connection to volkswagen carnet")
    connection = Connection(
        session=session,
        username=config[DOMAIN].get(CONF_USERNAME),
        password=config[DOMAIN].get(CONF_PASSWORD),
    )

    interval = config[DOMAIN].get(CONF_SCAN_INTERVAL)
    data = hass.data[DATA_KEY] = VolkswagenData(config)

    # login to carnet
    # _LOGGER.debug("Logging in to volkswagen carnet")
    # connection._login()
    # if not connection.logged_in:
    #     _LOGGER.warning('Could not login to volkswagen carnet, please check your credentials')

    def is_enabled(attr):
        """Return true if the user has enabled the resource."""
        return attr in config[DOMAIN].get(CONF_RESOURCES, [attr])

    def discover_vehicle(vehicle):
        """Load relevant platforms."""
        data.vehicles.add(vehicle.vin)


        dashboard = vehicle.dashboard(
            mutable=config[DOMAIN][CONF_MUTABLE],
            spin=config[DOMAIN][CONF_SPIN],
            scandinavian_miles=config[DOMAIN][CONF_SCANDINAVIAN_MILES],
        )

        for instrument in (
                instrument
                for instrument in dashboard.instruments
                if instrument.component in COMPONENTS and is_enabled(instrument.slug_attr)
        ):

            data.instruments.add(instrument)
            hass.async_create_task(
                discovery.async_load_platform(
                    hass,
                    COMPONENTS[instrument.component],
                    DOMAIN,
                    (vehicle.vin, instrument.component, instrument.attr),
                    config
                )
            )

    async def update(now):
        """Update status from Volkswagen Carnet"""
        try:
            # check if we can login
            if not connection.logged_in:
                await connection._login()
                if not connection.logged_in:
                    _LOGGER.warning('Could not login to volkswagen carnet, please check your credentials and verify that the service is working')
                    return False

            # update vehicles
            if not await connection.update():
                _LOGGER.warning('Could not query update from volkswagen carnet')
                return False

            _LOGGER.debug("Updating data from volkswagen carnet")
            for vehicle in connection.vehicles:
                if vehicle.vin not in data.vehicles:
                    _LOGGER.info(f'Adding data for VIN: {vehicle.vin} from carnet')
                    discover_vehicle(vehicle)

            async_dispatcher_send(hass, SIGNAL_STATE_UPDATED)
            return True

        finally:
            async_track_point_in_utc_time(hass, update, utcnow() + interval)

    _LOGGER.info("Starting volkswagencarnet component")
    return await update(utcnow())


class VolkswagenData:
    """Hold component state."""

    def __init__(self, config):
        """Initialize the component state."""
        self.vehicles = set()
        self.instruments = set()
        self.config = config[DOMAIN]
        self.names = self.config.get(CONF_NAME)

    def instrument(self, vin, component, attr):
        """Return corresponding instrument."""
        return next(
            (
                instrument
                for instrument in self.instruments
                if instrument.vehicle.vin == vin and
                instrument.component == component and
                instrument.attr == attr
            ),
            None
        )

    def vehicle_name(self, vehicle):
        """Provide a friendly name for a vehicle."""
        if (vehicle.vin and vehicle.vin.lower() in self.names):
            return self.names[vehicle.vin.lower()]
        elif vehicle.vin:
            return vehicle.vin
        else:
            return ''


class VolkswagenEntity(Entity):
    """Base class for all Volkswagen entities."""

    def __init__(self, data, vin, component, attribute):
        """Initialize the entity."""
        self.data = data
        self.vin = vin
        self.component = component
        self.attribute = attribute

    async def async_added_to_hass(self):
        """Register update dispatcher."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_STATE_UPDATED, self.async_write_ha_state
            )
        )

    @property
    def instrument(self):
        """Return corresponding instrument."""
        return self.data.instrument(self.vin, self.component, self.attribute)

    @property
    def icon(self):
        """Return the icon."""
        if self.instrument.attr in ['battery_level', 'charging']:
            return icon_for_battery_level(battery_level = self.instrument.state, charging = self.vehicle.charging)
        else:
            return self.instrument.icon

    @property
    def vehicle(self):
        """Return vehicle."""
        return self.instrument.vehicle

    @property
    def _entity_name(self):
        return self.instrument.name

    @property
    def _vehicle_name(self):
        return self.data.vehicle_name(self.vehicle)

    @property
    def name(self):
        """Return full name of the entity."""
        return f"{self._vehicle_name} {self._entity_name}"

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def assumed_state(self):
        """Return true if unable to access real state of entity."""
        return True

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return dict(
            self.instrument.attributes,
            model=f"{self.vehicle.model}/{self.vehicle.model_year}"
        )

    @property
    def device_info(self):
        """Return the device_info of the device."""
        return {
            "identifiers": {(DOMAIN, self.vin)},
            "name": self._vehicle_name,
            "manufacturer": "Volkswagen",
            "model": self.vehicle.model,
            "sw_version": self.vehicle.model_year,
        }

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self.vin}-{self.component}-{self.attribute}"
