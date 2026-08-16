"""Protocol-level tuning constants.

Home Assistant specific constants live in ``../const.py``; nothing here may
depend on Home Assistant.
"""

from __future__ import annotations

from typing import Final

#: The official app pings roughly every 1.2s for the whole session. This is a
#: hard requirement, not a nicety - a client that pings only at session-open
#: sees media delivery quietly degrade.
PING_INTERVAL: Final = 1.2

#: Every received ``d0`` packet must be acked or the device stops advancing its
#: send window. Acks are cheap; send each one twice.
ACK_REPEAT: Final = 2

#: Commands are UDP fire-and-forget, so retry a few times while waiting.
COMMAND_TIMEOUT: Final = 4.0
COMMAND_RETRIES: Final = 3

#: How long to wait for the device to answer the login exchange.
LOGIN_TIMEOUT: Final = 8.0

#: Reordering window for a channel before we give up on a gap.
MAX_PENDING_PACKETS: Final = 256

#: If no media arrives for this long while a stream should be running, the
#: session is considered dead.
MEDIA_STALL_TIMEOUT: Final = 15.0

# --- Media ------------------------------------------------------------------

AUDIO_SAMPLE_RATE: Final = 8000
AUDIO_FRAME_BYTES: Final = 320  # 40ms of 8kHz mono mu-law

QUALITY_LD: Final = "ld"
QUALITY_SD: Final = "sd"
QUALITY_HD: Final = "hd"
QUALITY_OPTIONS: Final = [QUALITY_LD, QUALITY_SD, QUALITY_HD]
DEFAULT_QUALITY: Final = QUALITY_SD

# --- Defaults ---------------------------------------------------------------

DEFAULT_USERNAME: Final = "admin"
DEFAULT_UID: Final = "URMABB-700171-SMCYN"

#: How many times a talk frame is retransmitted. The app sends ~12; that is
#: ~1.2 Mbps of duplicate traffic, so we default lower and make it tunable.
DEFAULT_TALK_REPEAT: Final = 6
