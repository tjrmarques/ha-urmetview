"""Diagnostic sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UrmetCoordinator
from .entity import UrmetEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: UrmetCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UrmetStreamStateSensor(coordinator)])


class UrmetStreamStateSensor(UrmetEntity, SensorEntity):
    """What the video channel is doing.

    Useful when diagnosing 'video busy': it makes visible whether we are the
    ones holding the channel.
    """

    _attr_name = "Video channel"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:video"
    _attr_device_class = None
    _attr_options = ["idle", "streaming"]

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "video_state")

    @property
    def native_value(self) -> str:
        return "streaming" if self.coordinator.streaming else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        pipeline = self.coordinator.pipeline
        return {
            "consumers": pipeline.client_count,
            "frames": pipeline.frames,
            "keyframes": pipeline.keyframes,
            "station": self.coordinator.station,
        }
