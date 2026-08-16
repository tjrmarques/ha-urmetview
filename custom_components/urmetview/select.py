"""Select entities: active outdoor station, and stream quality."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_STATION_COUNT, DOMAIN, STATION_NAME
from .coordinator import UrmetCoordinator
from .entity import UrmetEntity
from .urmet.const import QUALITY_OPTIONS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: UrmetCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UrmetStationSelect(coordinator), UrmetQualitySelect(coordinator)])


class UrmetStationSelect(UrmetEntity, SelectEntity):
    """Which outdoor station the camera is showing.

    Doubles as the "which station is active" readout, since the device reports
    the station it landed on after every switch.
    """

    _attr_name = STATION_NAME
    _attr_icon = "mdi:door"

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "station")
        self._attr_options = [
            f"{STATION_NAME} {index}" for index in range(1, DEFAULT_STATION_COUNT + 1)
        ]

    @property
    def current_option(self) -> str | None:
        if self.coordinator.station is None:
            return None
        return f"{STATION_NAME} {self.coordinator.station}"

    async def async_select_option(self, option: str) -> None:
        station = int(option.rsplit(" ", 1)[1])
        await self.coordinator.async_select_station(station)


class UrmetQualitySelect(UrmetEntity, SelectEntity):
    """Stream quality.

    This changes bitrate and frame rate, not resolution - the stream is
    960x240 in every mode, and the official app behaves the same way.
    """

    _attr_name = "Stream quality"
    _attr_icon = "mdi:video-settings"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [quality.upper() for quality in QUALITY_OPTIONS]

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "quality")

    @property
    def current_option(self) -> str | None:
        return self.coordinator.quality.upper()

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_quality(option.lower())
