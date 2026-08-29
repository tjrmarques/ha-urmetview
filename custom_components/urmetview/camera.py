"""Camera entity for the active outdoor station."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STATION_NAME
from .coordinator import UrmetCoordinator
from .entity import UrmetEntity

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_TIMEOUT = 12.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: UrmetCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UrmetCamera(coordinator)])


class UrmetCamera(UrmetEntity, Camera):
    """One camera, showing whichever outdoor station is selected.

    There is deliberately one entity rather than one per station: the device
    itself switches between them and can only show one at a time, so two
    entities would imply a simultaneity that does not exist.
    """

    _attr_name = STATION_NAME
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: UrmetCoordinator) -> None:
        UrmetEntity.__init__(self, coordinator, "camera")
        Camera.__init__(self)

    @property
    def is_streaming(self) -> bool:
        return self.coordinator.streaming

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "station": self.coordinator.station,
            "quality": self.coordinator.quality,
            "viewers": self.coordinator.viewers,
            "frames": self.coordinator.pipeline.frames,
            "keyframes": self.coordinator.pipeline.keyframes,
        }

    async def stream_source(self) -> str | None:
        """Return the muxed MPEG-TS endpoint.

        Home Assistant hands this to go2rtc as an ``ffmpeg:`` source, which is
        what gets us WebRTC playback with audio.
        """
        return await self.coordinator.async_ensure_stream()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Grab a single JPEG by running ffmpeg against the live stream."""
        url = await self.coordinator.async_ensure_stream()
        return await self._async_snapshot(url, width, height)

    async def _async_snapshot(
        self, url: str, width: int | None, height: int | None
    ) -> bytes | None:
        args = [
            self.coordinator.pipeline.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            url,
            "-frames:v",
            "1",
        ]
        if width and height:
            args += ["-vf", f"scale={width}:{height}"]
        args += ["-f", "image2", "-vcodec", "mjpeg", "pipe:1"]

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=SNAPSHOT_TIMEOUT
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            _LOGGER.warning("Timed out grabbing a snapshot from the intercom")
            return None

        if process.returncode != 0 or not stdout:
            _LOGGER.debug(
                "Snapshot failed: %s", stderr.decode("utf-8", "replace").strip()
            )
            return None
        return stdout
