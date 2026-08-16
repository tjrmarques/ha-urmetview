# UrmetView for Home Assistant

Home Assistant integration for the **Urmet Kit 1730** video door entry system
with the **1730/67** WiFi call-forwarding module — the hardware behind the
*UrmetView* phone app.

The device has no local API. This talks its native UDP protocol directly, so
video, the door lock and the gate work locally without the vendor app.

> **Status: early development.** The protocol is reverse-engineered and verified
> against real hardware, and the standalone scripts in `tools/` work today. The
> Home Assistant integration itself is being built on top of them — see
> [Project status](#project-status).

## What works

| Capability | State |
|---|---|
| Live H.264 video from either outdoor station | Working (`tools/urmet_cli.py`) |
| Incoming audio (G.711 mu-law) | Working |
| Door lock release / gate release | Working |
| Switching between outdoor stations | Working |
| Stream quality LD / SD / HD | Working (bitrate & frame rate, not resolution) |
| Talk — audio *out* to the door station | Spike (`tools/urmet_talk.py`) |
| Doorbell ring event | **Unresolved** — see [Doorbell](#doorbell) |

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

Unresolved, and worth being straight about. Packet captures show that when the
bell rings, the device sends **nothing on the LAN** — not to the phone, not to a
client holding an open ping link. The Urmet manual describes the alert as a
"Linkage push … to all smartphones associated with the WiFi module", i.e. a
cloud push to registered phones. Home Assistant is not a registered phone and
cannot receive one.

Open question: whether a *logged-in* session receives an in-band ring message.
`tools/urmet_listen.py` exists to answer exactly that — run it, ring the bell,
and see whether anything unsolicited arrives.

If it turns out to be cloud-only, the practical options are:

1. **A dry contact on the indoor unit's call/chime line** (Shelly, ESPHome).
   Instant, fully local, and independent of all of this. Recommended.
2. **Router mirroring** — mirror the device's cloud-bound control traffic to HA
   and trigger on it (`tools/urmet_tzsp.py` validates this). Needs a router that
   can mirror, and needs a message that actually correlates with rings.

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
