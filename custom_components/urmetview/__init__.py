"""The UrmetView integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import (
    ATTR_MEDIA,
    ATTR_QUALITY,
    ATTR_STATION,
    CONF_AUTH_HASH,
    CONF_DOORBELL_TZSP,
    CONF_DOORBELL_TZSP_PORT,
    CONF_QUALITY,
    CONF_RING_PREWARM,
    CONF_STREAM_IDLE_TIMEOUT,
    CONF_TALK_REPEAT,
    CONF_UID,
    DEFAULT_STREAM_IDLE_TIMEOUT,
    DEFAULT_TZSP_PORT,
    DOMAIN,
    SERVICE_ANSWER,
    SERVICE_HANG_UP,
    SERVICE_OPEN_GATE,
    SERVICE_OPEN_LOCK,
    SERVICE_SELECT_STATION,
    SERVICE_SET_QUALITY,
    SERVICE_TALK,
)
from .coordinator import UrmetCoordinator
from .urmet import UrmetAuthError, UrmetError
from .urmet.const import DEFAULT_QUALITY, DEFAULT_TALK_REPEAT, QUALITY_OPTIONS

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_ENTRY_SERVICE_SCHEMA = vol.Schema({vol.Required("entry_id"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up UrmetView from a config entry."""
    ffmpeg_binary = get_ffmpeg_manager(hass).binary

    coordinator = UrmetCoordinator(
        hass,
        entry_id=entry.entry_id,
        uid=entry.data[CONF_UID],
        auth_hash=entry.data[CONF_AUTH_HASH],
        username=entry.data.get(CONF_USERNAME, "admin"),
        ffmpeg_binary=ffmpeg_binary,
        host=entry.data.get(CONF_HOST),
        port=entry.data.get(CONF_PORT),
        quality=entry.options.get(CONF_QUALITY, DEFAULT_QUALITY),
        stream_idle_timeout=entry.options.get(
            CONF_STREAM_IDLE_TIMEOUT, DEFAULT_STREAM_IDLE_TIMEOUT
        ),
        talk_repeat=entry.options.get(CONF_TALK_REPEAT, DEFAULT_TALK_REPEAT),
        doorbell_mirror=entry.options.get(CONF_DOORBELL_TZSP, False),
        doorbell_port=entry.options.get(CONF_DOORBELL_TZSP_PORT, DEFAULT_TZSP_PORT),
        ring_prewarm=entry.options.get(CONF_RING_PREWARM, False),
    )

    try:
        await coordinator.async_setup()
    except UrmetAuthError as err:
        # A bad hash will never fix itself, so fail permanently rather than
        # retrying forever.
        raise ConfigEntryNotReady(str(err)) from err
    except UrmetError as err:
        raise ConfigEntryNotReady(str(err)) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, releasing the device session cleanly.

    The teardown matters: leaving without it holds the video channel until the
    device times out, and the next connect is refused as 'video busy'.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: UrmetCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_ANSWER,
                SERVICE_HANG_UP,
                SERVICE_OPEN_LOCK,
                SERVICE_OPEN_GATE,
                SERVICE_SELECT_STATION,
                SERVICE_SET_QUALITY,
                SERVICE_TALK,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[UrmetCoordinator]:
    """Resolve which coordinators a service call targets.

    Services accept a device or entity target; with none given and only one
    intercom configured, that one is used.
    """
    store: dict[str, UrmetCoordinator] = hass.data.get(DOMAIN, {})
    if not store:
        raise HomeAssistantError("No UrmetView intercom is configured")

    entry_ids: set[str] = set()
    registry = er.async_get(hass)
    for entity_id in call.data.get("entity_id", []) or []:
        entity = registry.async_get(entity_id)
        if entity is not None and entity.config_entry_id:
            entry_ids.add(entity.config_entry_id)
    for device_id in call.data.get("device_id", []) or []:
        for entity in er.async_entries_for_device(registry, device_id, True):
            if entity.config_entry_id:
                entry_ids.add(entity.config_entry_id)

    if not entry_ids:
        if len(store) == 1:
            return list(store.values())
        raise HomeAssistantError(
            "Multiple intercoms are configured - target one with device_id or entity_id"
        )
    resolved = [store[entry_id] for entry_id in entry_ids if entry_id in store]
    if not resolved:
        raise HomeAssistantError("The targeted device is not an UrmetView intercom")
    return resolved


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_OPEN_LOCK):
        return

    async def _open_lock(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_open_lock()

    async def _open_gate(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_open_gate()

    async def _select_station(call: ServiceCall) -> None:
        station = call.data[ATTR_STATION]
        for coordinator in _coordinators(hass, call):
            await coordinator.async_select_station(station)

    async def _set_quality(call: ServiceCall) -> None:
        quality = call.data[ATTR_QUALITY]
        for coordinator in _coordinators(hass, call):
            await coordinator.async_set_quality(quality)

    async def _answer(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_answer()

    async def _hang_up(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_hang_up()

    async def _talk(call: ServiceCall) -> None:
        media = call.data[ATTR_MEDIA]
        ffmpeg_binary = get_ffmpeg_manager(hass).binary
        for coordinator in _coordinators(hass, call):
            await coordinator.async_play_audio(media, ffmpeg_binary)

    base = {
        vol.Optional("entity_id"): cv.entity_ids,
        vol.Optional("device_id"): vol.All(cv.ensure_list, [cv.string]),
    }
    hass.services.async_register(DOMAIN, SERVICE_ANSWER, _answer, vol.Schema(base))
    hass.services.async_register(DOMAIN, SERVICE_HANG_UP, _hang_up, vol.Schema(base))
    hass.services.async_register(DOMAIN, SERVICE_OPEN_LOCK, _open_lock, vol.Schema(base))
    hass.services.async_register(DOMAIN, SERVICE_OPEN_GATE, _open_gate, vol.Schema(base))
    hass.services.async_register(
        DOMAIN,
        SERVICE_SELECT_STATION,
        _select_station,
        vol.Schema({**base, vol.Required(ATTR_STATION): vol.All(int, vol.Range(min=1, max=2))}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_QUALITY,
        _set_quality,
        vol.Schema({**base, vol.Required(ATTR_QUALITY): vol.In(QUALITY_OPTIONS)}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TALK,
        _talk,
        vol.Schema({**base, vol.Required(ATTR_MEDIA): cv.string}),
    )
