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

The trigger is a **TCP connection to port 32002** on two push servers, opened
the instant the button is pressed. It is confirmed in three independent ring
captures, and it is the only TCP the device ever makes:

| Capture | TCP SYN → `:32002` | `f1 f9` |
|---|---|---|
| urmet3 (13.1 s) | **+11.401 s** | absent |
| urmet4 (11.0 s) | **+7.590 s** | absent |
| urmet5 (108.9 s) | **+13.552 s** | +35.3 s |

The push servers are separate infrastructure from the P2P fleet — AWS
us-east-1 and DigitalOcean, versus `*.caycctv.com`'s AWS hosts — and the device
**never resolves them by name**. The only DNS it ever issues is for
`p2p2`/`p2p3.caycctv.com`, so the push addresses are hardcoded in firmware.
Match on the port, never on the address: a firmware update can move them with
no DNS to follow.

Two decoys worth naming, since both look convincing in a single short capture:

* `f1 f9` appears only when a call goes **unanswered**, ~22 s late. Absent from
  two of the three ring captures.
* `f1 12` is periodic registration, every ~33 s forever.

### Enabling it

The router mirrors that one connection to Home Assistant. On MikroTik this is a
**firewall mangle rule**, not `/tool sniffer` — a rule is part of the config so
it survives reboots, and `connection-state=new` narrows it to a single packet
per ring:

```
/ip firewall mangle
add chain=prerouting action=sniff-tzsp \
    protocol=tcp dst-port=32002 src-address=<device-ip> \
    connection-state=new \
    sniff-target=<ha-ip> sniff-target-port=37008 \
    comment="UrmetView doorbell"
```

Then turn on **Doorbell via router mirror** in the integration options and point
it at port 37008.

Verify before trusting it — `tools/urmet_tzsp.py --device-ip <device-ip>` prints
a banner on each ring.

**Optionally**, a second rule feeds the integration the device's current session
port for free, removing the cloud lookup on reconnect:

```
add chain=prerouting action=sniff-tzsp \
    protocol=udp dst-port=32100 src-address=<device-ip> \
    sniff-target=<ha-ip> sniff-target-port=37008 \
    comment="UrmetView port discovery"
```

`/tool sniffer` works too, but it is a diagnostic: it stops on reboot, and
without a tight filter it mirrors the video stream at ~300 KB/s.

**Other routers:** anything that can do TZSP or port-mirroring to the HA host
will work. If yours cannot, a dry contact on the indoor unit's chime line
driving an automation is simpler and more reliable than any of this.

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
