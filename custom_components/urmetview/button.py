"""Buttons: door lock release, gate release, and restart video.

Both act on whichever outdoor station is currently selected - that is how the
device works, not a simplification. Switch stations with the station select
first if you need the other door.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import UrmetCoordinator
from .entity import UrmetEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: UrmetCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            UrmetLockButton(coordinator),
            UrmetGateButton(coordinator),
            UrmetRestartVideoButton(coordinator),
        ]
    )


class UrmetLockButton(UrmetEntity, ButtonEntity):
    """The key symbol on the handset: releases the door lock."""

    _attr_name = "Door lock release"
    _attr_icon = "mdi:key-variant"

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "lock")

    async def async_press(self) -> None:
        await self.coordinator.async_open_lock()


class UrmetGateButton(UrmetEntity, ButtonEntity):
    """The gate/driveway actuator."""

    _attr_name = "Gate release"
    _attr_icon = "mdi:gate"

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "gate")

    async def async_press(self) -> None:
        await self.coordinator.async_open_gate()


class UrmetRestartVideoButton(UrmetEntity, ButtonEntity):
    """Ask the device for the picture again.

    The stream going black after a while is normal for this device, and it
    reports no error when it happens. Re-picking the current station in the
    dropdown will not bring it back either, because Home Assistant does not
    call a select entity when the value has not changed - so without this
    there is nothing to press short of waiting out the idle timeout.
    """

    _attr_name = "Restart video"
    _attr_icon = "mdi:video-refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "restart_video")

    async def async_press(self) -> None:
        await self.coordinator.async_restart_video()
