"""Diagnostics, with credentials redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_AUTH_HASH, DOMAIN
from .coordinator import UrmetCoordinator

# The auth hash is a replayable credential for opening the door - never include
# it in a diagnostics download.
TO_REDACT = {CONF_AUTH_HASH, "uid", "serial_no"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: UrmetCoordinator = hass.data[DOMAIN][entry.entry_id]
    session = coordinator.session
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "connection": {
            "available": coordinator.available,
            "host": coordinator.host,
            "port": coordinator.port,
            "last_error": coordinator.last_error,
        },
        "device": async_redact_data(coordinator.device_info_raw, TO_REDACT),
        "state": {
            "station": coordinator.station,
            "quality": coordinator.quality,
            "streaming": coordinator.streaming,
            "talking": coordinator.talking,
        },
        "media": {
            "pipeline_running": coordinator.pipeline.running,
            "consumers": coordinator.pipeline.client_count,
            "frames": coordinator.pipeline.frames,
            "keyframes": coordinator.pipeline.keyframes,
        },
        "session": {
            "video_frames": session.video_frames if session else 0,
            "audio_frames": session.audio_frames if session else 0,
        },
    }
