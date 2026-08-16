"""Binary sensor: is the device session up."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    async_add_entities([UrmetConnectionSensor(coordinator)])


class UrmetConnectionSensor(UrmetEntity, BinarySensorEntity):
    """Whether we currently hold a logged-in session."""

    _attr_name = "Session"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "session")

    @property
    def available(self) -> bool:
        # This entity reports connectivity, so it must stay available even when
        # the session is down - otherwise it goes unknown exactly when it is
        # most useful.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.available

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "host": self.coordinator.host,
            "port": self.coordinator.port,
            "last_error": self.coordinator.last_error,
        }
