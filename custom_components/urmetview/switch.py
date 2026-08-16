"""Switch: hold the talk (outbound audio) channel open."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([UrmetTalkSwitch(coordinator)])


class UrmetTalkSwitch(UrmetEntity, SwitchEntity):
    """Opens the outbound audio path to the door station's speaker.

    On its own this only opens the channel - it does not capture a microphone,
    because Home Assistant has no server-side mic to capture. Use it to hold
    the channel open around several `urmetview.talk` calls, which is cheaper
    than opening and closing it for each one.
    """

    _attr_name = "Talk"
    _attr_icon = "mdi:microphone"

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        super().__init__(coordinator, "talk")

    @property
    def is_on(self) -> bool:
        return self.coordinator.talking

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_talk_start()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_talk_stop()
