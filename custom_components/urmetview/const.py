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

ATTR_STATION: Final = "station"
ATTR_QUALITY: Final = "quality"
ATTR_MEDIA: Final = "media"

EVENT_DOORBELL: Final = "doorbell"

SIGNAL_STATE_UPDATED: Final = f"{DOMAIN}_state_updated"
