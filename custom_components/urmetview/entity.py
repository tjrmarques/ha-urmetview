"""Shared base for UrmetView entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_STATE_UPDATED
from .coordinator import UrmetCoordinator


class UrmetEntity(Entity):
    """Base entity: device registry entry plus state-change subscription."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: UrmetCoordinator, key: str) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.uid}_{key}"

        info = coordinator.device_info_raw
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.uid)},
            name="Urmet intercom",
            manufacturer="Urmet",
            model=info.get("model") or "Kit 1730 / 1730-67",
            sw_version=info.get("version"),
            serial_number=info.get("serial_no"),
            configuration_url=None,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self.coordinator.entry_id}",
                self.async_write_ha_state,
            )
        )
