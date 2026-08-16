"""Doorbell event entity."""

from __future__ import annotations

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UrmetCoordinator
from .entity import UrmetEntity

EVENT_RING = "ring"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: UrmetCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UrmetDoorbellEvent(coordinator)])


class UrmetDoorbellEvent(UrmetEntity, EventEntity):
    """Fires when someone rings the bell.

    An event entity rather than a binary sensor: a ring is instantaneous, so
    there is no honest answer to "how long does it stay on", and a missed
    off-transition would leave a binary sensor stuck. This is also what
    HomeKit Bridge's linked_doorbell_sensor and the mobile notification
    actions expect.

    The ring is detected by mirroring the device's cloud traffic (see
    doorbell.py). Where that is not set up, this entity still exists and can be
    fired by an automation from any other source - a dry contact on the chime
    line, for instance.
    """

    _attr_name = "Doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_RING]
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "doorbell")

    @property
    def available(self) -> bool:
        # Ring detection is independent of the device session, so this stays
        # available even when the session is down.
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.register_doorbell_callback(self._handle_ring)

    @callback
    def _handle_ring(self) -> None:
        self._trigger_event(EVENT_RING, {"station": self.coordinator.station})
        self.async_write_ha_state()
