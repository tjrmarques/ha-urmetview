"""Home Assistant specific constants for the UrmetView integration.

Protocol tuning lives in ``urmet/const.py``, which must stay importable without
Home Assistant.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "urmetview"

# --- Config entry keys ------------------------------------------------------

CONF_UID: Final = "uid"
CONF_AUTH_HASH: Final = "auth_hash"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_USERNAME: Final = "username"

# Options
#: Sample (pixel) aspect ratio to stamp into the H.264 SPS. The device sends
#: 960x240 with no aspect information, so players assume square pixels and the
#: picture comes out twice as wide as it should be. "1/2" squeezes it back;
#: empty leaves the bitstream untouched. A colon is accepted and converted -
#: it is ffmpeg's own option separator inside a filter spec, so it cannot be
#: passed through as written.
CONF_PIXEL_ASPECT: Final = "pixel_aspect"
DEFAULT_PIXEL_ASPECT: Final = "1/2"
PIXEL_ASPECT_OPTIONS: Final = ["1/2", "1/1", "2/1", "3/4", "4/3", ""]

CONF_QUALITY: Final = "quality"
CONF_STREAM_IDLE_TIMEOUT: Final = "stream_idle_timeout"
CONF_TALK_REPEAT: Final = "talk_repeat"
CONF_STATION_COUNT: Final = "station_count"
CONF_DOORBELL_TZSP: Final = "doorbell_tzsp"
CONF_DOORBELL_TZSP_PORT: Final = "doorbell_tzsp_port"
CONF_RING_PREWARM: Final = "ring_prewarm"
CONF_ALLOW_CLOUD: Final = "allow_cloud"

DEFAULT_STATION_COUNT: Final = 2

#: How long the video channel stays open after the last viewer disconnects.
#: Video is started on demand and released promptly so the phone app can use it.
DEFAULT_STREAM_IDLE_TIMEOUT: Final = 30

DEFAULT_TZSP_PORT: Final = 37008

# --- Entities ---------------------------------------------------------------

#: Urmet's own terminology, from the Kit 1730 manual.
STATION_NAME: Final = "Outdoor station"

SERVICE_OPEN_LOCK: Final = "open_lock"
SERVICE_OPEN_GATE: Final = "open_gate"
SERVICE_SELECT_STATION: Final = "select_station"
SERVICE_SET_QUALITY: Final = "set_quality"
SERVICE_TALK: Final = "talk"
SERVICE_ANSWER: Final = "answer"
SERVICE_HANG_UP: Final = "hang_up"
SERVICE_RESTART_VIDEO: Final = "restart_video"

ATTR_STATION: Final = "station"
ATTR_QUALITY: Final = "quality"
ATTR_MEDIA: Final = "media"

EVENT_DOORBELL: Final = "doorbell"

SIGNAL_STATE_UPDATED: Final = f"{DOMAIN}_state_updated"
