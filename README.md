# UrmetView for Home Assistant

Home Assistant integration for the **Urmet Kit 1730** video door entry system
with the **1730/67** WiFi call-forwarding module — the hardware behind the
*UrmetView* phone app.

The device has no local API. This talks its native UDP protocol directly, so
video, the door lock and the gate work locally without the vendor app.

> **Status: early development.** The protocol is reverse-engineered and verified
> against real hardware. The standalone scripts in `tools/` are proven; the
> integration is written but has not yet run inside a live Home Assistant.

## What works

| Capability | State |
|---|---|
| Live H.264 video from either outdoor station | Working (`tools/urmet_cli.py`) |
| Incoming audio (G.711 mu-law) | Working |
| Door lock release / gate release | Working |
| Switching between outdoor stations | Working |
| Stream quality LD / SD / HD | Working (bitrate & frame rate, not resolution) |
| Talk — audio *out* to the door station | Spike (`tools/urmet_talk.py`) |
| Doorbell ring event | Working, needs a router that can mirror — see [Doorbell](#doorbell) |

### Entities

| Entity | Notes |
|---|---|
| `camera.outdoor_station` | Live video + audio, via go2rtc/WebRTC |
| `select.outdoor_station` | Reports *and* switches the active station |
| `select.stream_quality` | LD / SD / HD |
| `button.door_lock_release` | The key symbol — acts on the active station |
| `button.gate_release` | Gate/driveway — acts on the active station |
| `event.doorbell` | Fires on ring (`device_class: doorbell`) |
| `switch.talk` | Holds the outbound audio channel open |
| `binary_sensor.session` | Whether we hold a session |
| `sensor.video_channel` | `idle` / `streaming`, with consumer and frame counts |

Services: `urmetview.open_lock`, `open_gate`, `select_station`, `set_quality`,
and `talk` (plays any ffmpeg-readable audio or TTS at the door).

## Installing

**HACS** → Custom repositories → add this repo as an *Integration* → install →
restart → *Settings → Devices & Services → Add Integration → Urmet*.

You will be asked for the device UID and the auth hash from step 1 below. Host
and port are optional; leave them blank to auto-discover.

## Getting started

Everything in `tools/` is stdlib-only and runs straight from a checkout — no
install, no dependencies (ffmpeg is needed only for talk audio). Run these on a
machine on the same LAN as the intercom.

### 1. Get your auth hash

The login credential is a static 32-hex-character hash. It **cannot** be derived
from the device password — that was tested exhaustively and never reproduced — so
it has to be lifted once from a real app login and then stored like a password.

Capture traffic while opening the UrmetView app, then:

```bash
python3 tools/urmet_pcap.py capture.pcap
```

It prints the hash, plus a traffic summary that tells you whether the capture is
usable at all. See [docs/capture-guide.md](docs/capture-guide.md) for MikroTik
sniffer commands.

### 2. Find the device

The session port changes every session and is not announced on the LAN, so it
has to be rediscovered each time.

```bash
python3 tools/urmet_probe.py --broadcast              # find the device's IP and UID
python3 tools/urmet_probe.py --host 10.0.50.6 --sweep # find the current port
```

`urmet_probe.py` tries each method independently and reports which ones work.
That matters: if PPPP LAN search answers, the integration can drop the cloud
round-trip entirely.

### 3. Watch the video

```bash
python3 tools/urmet_cli.py --auth <hash> --host <ip> --port <port> video --serve
# then, in another terminal:
ffplay -f h264 -i tcp://127.0.0.1:5599 -fflags nobuffer -flags low_delay
```

### 4. Open things

```bash
python3 tools/urmet_cli.py --auth <hash> station 2   # switch outdoor station
python3 tools/urmet_cli.py --auth <hash> lock        # release the door lock
python3 tools/urmet_cli.py --auth <hash> gate        # release the gate
```

Lock and gate act on whichever station is currently selected — that is a device
constraint, not a design choice.

## Scripts

| Script | Purpose |
|---|---|
| `urmet_probe.py` | Find the device and its session port; compares discovery methods |
| `urmet_cli.py` | Video, station selection, lock, gate |
| `urmet_listen.py` | Sit on an idle logged-in session and log anything unsolicited |
| `urmet_talk.py` | Send audio out of the door station's speaker |
| `urmet_tzsp.py` | Decode router-mirrored traffic (doorbell research) |
| `urmet_pcap.py` | Extract the auth hash and summarise a capture |

## Doorbell

When the bell rings the device sends **nothing on the LAN**. The Urmet manual
describes the alert as a "Linkage push … to all smartphones associated with the
WiFi module" — a cloud push to registered phones, which Home Assistant is not
and cannot become.

What the device *does* do is reach out to the internet the instant the button
is pressed. A 109-second capture containing exactly one ring, at a known time,
separated three candidate signals:

| Signal | When | Meaning |
|---|---|---|
| **TCP to port 32002** on two push servers | **at the button press** | **the ring** |
| `f1 f9` (UDP to the P2P servers) | 22 s later | call going unanswered |
| `f1 12` | every ~33 s, forever | periodic registration |

The last two are traps — both appear exactly once in a short capture and look
event-shaped. Triggering on `f1 f9` gives a doorbell that is 22 seconds late;
triggering on `f1 12` gives one that rings twice a minute forever.

### Enabling it

Mirror the device's traffic to Home Assistant. On MikroTik — note there is
deliberately **no port filter**, since the ring is TCP/32002:

```
/tool sniffer set filter-ip-address=<device-ip>/32 \
    filter-stream=yes streaming-enabled=yes streaming-server=<ha-ip>:37008
/tool sniffer start
```

Then turn on **Doorbell via router mirror** in the integration options.
`tools/urmet_tzsp.py` lets you verify the mirror first — it prints a banner on
each ring.

This is off by default because it needs a router that can mirror. Without it the
`event.doorbell` entity still exists and can be fired by an automation from any
other source — a dry contact on the indoor unit's chime line, for instance,
which is the most reliable option of all and needs none of this protocol.

## Design notes

* **One session, one station.** The device serves one client at a time and one
  outdoor station at a time. The integration holds a lightweight command session
  and starts video only on demand, so the phone app keeps working.
* **No PPPP library.** The transport is standard PPPP/CS2, but every available
  library pairs it with a *different vendor's* application layer. The framing we
  need is ~200 lines, so `urmet/protocol.py` implements it with zero
  dependencies.
* **Quality is bitrate, not resolution.** The stream is 960x240 in every mode.
  HD roughly doubles the frame rate. The official app behaves the same way.

Full protocol documentation: [docs/protocol.md](docs/protocol.md).

## Security

This device has no transport security, and that is worth understanding before
deploying it:

* The LAN discovery broadcast contains the UID, verification code **and the
  device password in cleartext**, sent to the whole broadcast domain.
* The session — video, audio, and every lock/gate command — is entirely
  unencrypted.
* The auth hash is **static**, with no challenge or nonce. Anyone who captures
  it once can open your door until the device password is changed.

Put the intercom on an isolated VLAN. Treat the auth hash exactly like a
password. Scrub captures before sharing them.

## Licence

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Urmet.
