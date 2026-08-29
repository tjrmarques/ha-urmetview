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
| `button.restart_video` | Ask for the picture again after it goes black |
| `event.doorbell` | Fires on ring (`device_class: doorbell`) |
| `switch.talk` | Holds the outbound audio channel open |
| `binary_sensor.session` | Whether we hold a session |
| `sensor.video_channel` | `idle` / `streaming`, with consumer and frame counts |

Services: `urmetview.open_lock`, `open_gate`, `select_station`, `set_quality`,
`talk` (plays any ffmpeg-readable audio or TTS at the door), and
`answer` / `hang_up`.

**Answering a call.** The device never pushes media on a ring — video is
pull-only — so nothing is grabbed until something asks. `answer` deliberately
takes the video channel and opens the mic path; `hang_up` releases it again.
That matters because the channel is single-occupancy: while Home Assistant
holds it, the phone app cannot answer, and vice versa.

## Installing

### Manual (for testing — no HACS needed)

Copy **only** the `custom_components/urmetview/` folder into your Home Assistant
config directory, so you end up with:

```
<config>/custom_components/urmetview/manifest.json
<config>/custom_components/urmetview/urmet/protocol.py
...
```

`<config>` is wherever `configuration.yaml` lives — `/config` on Home Assistant
OS or in Docker, `~/.homeassistant` for a Core venv install.

#### Home Assistant OS

There is no shell by default, so use one of these. Either is fine; pick the one
whose add-on you already have.

> **Two SSH surfaces, easily confused.** The HA OS *host* console (physical, or
> SSH on port **22222**) drops you at a `ha >` prompt — the Home Assistant CLI,
> which takes only `ha` subcommands and has no `ls`, no `tar` and no `/config`.
> The **Terminal & SSH add-on** (port 22, from the Add-on Store) is different: a
> real shell with `/config` mounted. Check which you have with
> `ls /config/configuration.yaml` — if that prints, you are in the right one.

**Terminal & SSH add-on** (fastest, if installed) — *Settings → Add-ons →
Add-on Store → Terminal & SSH*, start it, open the Terminal tab:

```bash
mkdir -p /config/custom_components
cd /config
tar -xzf /config/urmetview-custom-component.tar.gz   # after uploading it here
ls /config/custom_components/urmetview/manifest.json  # should exist
```

**Samba share add-on** (no command line) — install and start *Samba share*,
then browse to `\\homeassistant\config` (Windows) or
`smb://homeassistant.local/config` (macOS/Linux). Extract the archive **on your
own machine**, then drag the resulting `urmetview` folder into
`config/custom_components/`, creating that folder if it does not exist.

*Studio Code Server* is the best option if you expect to reinstall the folder a
few times: drag-and-drop upload, a file tree, an integrated terminal, and you
can edit `configuration.yaml` for the debug logging in the same window.

The plain **File editor** add-on is a poor fit here — it edits files one at a
time and cannot unpack an archive.

Do not copy `tools/`, `tests/` or `docs/` — the integration is just that one
folder, and the nested `urmet/` package must come with it.

Then **restart Home Assistant** — *Settings → System → ⋮ (top right) → Restart
Home Assistant*. Restarting Core is enough; you do not need to reboot the host.
A YAML reload will **not** pick up a newly added integration.

Now add it under *Settings → Devices & Services → Add Integration → Urmet*. If
"Urmet" does not appear in the list, the folder is in the wrong place or failed
to import — check the log.

Turn on debug logging before the first start — this has not yet run inside a
live Home Assistant, so the most likely failure is an import error, and it will
be in the log rather than the UI:

```yaml
# configuration.yaml
logger:
  default: info
  logs:
    custom_components.urmetview: debug
```

Check *Settings → System → Logs*, or grep `home-assistant.log` for `urmetview`.

### Via HACS

Once the repo is published: **HACS** → Custom repositories → add the repo as an
*Integration* → install → restart → add the integration as above.

Either way you will be asked for the device UID and the auth hash (see below).
Host and port are optional; leave them blank to auto-discover.

### Updating a manual install

Replace the folder and restart. Home Assistant caches compiled bytecode, so if
a change seems not to apply, delete `custom_components/urmetview/__pycache__/`
and restart again.

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
uv run tools/urmet_pcap.py capture.pcap
```

It prints the hash, plus a traffic summary that tells you whether the capture is
usable at all. See [docs/capture-guide.md](docs/capture-guide.md) for MikroTik
sniffer commands.

### 2. Find the device

The session port changes every session and is not announced on the LAN, so it
has to be rediscovered each time.

```bash
uv run tools/urmet_probe.py --broadcast              # find the device's IP and UID
uv run tools/urmet_probe.py --host 10.0.50.6 --sweep # find the current port
```

`urmet_probe.py` tries each method independently and reports which ones work.
That matters: if PPPP LAN search answers, the integration can drop the cloud
round-trip entirely.

The integration itself tries them in this order, cheapest first:

| Step | When | Cost |
|---|---|---|
| The `host:port` you configured | only if you set **both** | ~1.5 s |
| LAN search (broadcast to :32108) | always | 2 s |
| Cloud lookup via `*.caycctv.com` | unless disabled in options | ~4 s |
| Local port scan | only if a Host is set | 30-90 s, 64k packets |

**LAN search is the whole connection**, not merely a lookup, so the integration
runs entirely cloud-free whenever Home Assistant shares a subnet with the
intercom. Broadcasting `f1 30 00 00` to UDP 32108 makes the device open a
**session offer** on a fresh port and punch back from it — a `0x41`, the same
message a client sends to start a session, not the `0x31` announcement stock
PPPP documents. Logging in to that port works directly. It went unnoticed for
a long time only because every early capture was taken from a different
subnet, and a broadcast cannot cross one.

The offer **binds to the first peer that talks to it**, which is the part that
matters. Anything probing the port before the session does — a checkCam to
"verify" it — claims the offer, and the real session then arrives as a
stranger and is ignored. That was the cause of setup failing with
`Connection refused` shortly after discovery reported success. Discovery here
therefore verifies nothing; the login is the test, and each candidate is tried
in turn until one accepts a session.

A probe would not tell you anything anyway. Measured against the device, the
offer port answers `SESSION_ACK` to almost any message type carrying a
20-byte UID payload — `0x44`, `0x8a`, `0xff`. An ack means only that something
is listening.

The scan is last because it is the most expensive, and because it sends
checkCam to every port, claiming any pending offer on the way. Turning off
**Use Urmet's servers to find the device** in the options skips the cloud step,
so discovery never leaves your network.

### Troubleshooting discovery

Turn on debug logging first:

```yaml
# configuration.yaml
logger:
  logs:
    custom_components.urmetview: debug
```

| What you see | What it means |
|---|---|
| `Candidates to try, in order: …` | Discovery's guesses. Each is tried with a real login until one works. |
| `No session at <ip>:<port>: …` | That candidate is not a session endpoint; the next one is being tried. |
| `Connection refused` right after discovery | Was the old probe-first bug. If you still see it, the offer is being claimed by something else on the network. |
| `LAN search got no reply` | Home Assistant is on a different subnet or VLAN from the intercom, or broadcast is filtered. Set the Host so the local port scan can act as the fallback. |
| `Cloud lookup returned no candidates` | Outbound UDP 32100 is blocked, or `*.caycctv.com` does not resolve. |
| `Could not locate the intercom by any method` | Set both Host and Port explicitly to bypass discovery. |

`uv run tools/urmet_probe.py --host <ip>` runs the same steps from a laptop
and prints the outcome of every one, including which port each answered from.
Run it from a machine on the intercom's subnet — from anywhere else the LAN
search result is meaningless rather than negative.

### 3. Watch the video

```bash
uv run tools/urmet_cli.py --auth <hash> --host <ip> --port <port> video --serve
# then, in another terminal:
ffplay -f h264 -i tcp://127.0.0.1:5599 -fflags nobuffer -flags low_delay
```

### 4. Open things

```bash
uv run tools/urmet_cli.py --auth <hash> station 2   # switch outdoor station
uv run tools/urmet_cli.py --auth <hash> lock        # release the door lock
uv run tools/urmet_cli.py --auth <hash> gate        # release the gate
```

Lock and gate act on whichever station is currently selected — that is a device
constraint, not a design choice.

## Scripts

| Script | Purpose |
|---|---|
| `urmet_check.py` | **Single-file** network check — no repo needed, answers which discovery methods work |
| `urmet_probe.py` | Find the device and its session port; compares discovery methods |
| `urmet_cli.py` | Video, station selection, lock, gate |
| `urmet_listen.py` | Sit on an idle logged-in session and log anything unsolicited |
| `urmet_talk.py` | Send audio out of the door station's speaker |
| `urmet_tzsp.py` | Decode router-mirrored traffic (doorbell research) |
| `urmet_pcap.py` | Extract the auth hash and summarise a capture |

## How the video works

Nothing streams until Home Assistant asks for it. The device has **one** video
channel, and while the integration holds it the phone app cannot answer a call,
so it is taken late and given back promptly.

1. You open the camera. HA calls `stream_source()`, which logs in if needed,
   starts the local pipeline, sends `start_video` (and `start_audio`) to the
   device, and returns `tcp://127.0.0.1:<port>`.
2. HA hands that URL to go2rtc, which connects to the relay. The device's
   H.264 and mu-law are muxed into MPEG-TS and fanned out to every viewer.
3. A monitor checks every 2 s for real TCP consumers. Once there are none for
   **Release video after** seconds (default 30), the stream is stopped and the
   channel handed back. It watches actual sockets rather than counting calls,
   because HA asks for a stream source without ever saying it has finished.
4. Lock, gate and station commands need video running — the device answers
   `busy` otherwise — so they bring it up briefly if nothing is watching.

**When the picture goes black**, which this device does on its own without
reporting anything, press **Restart video** (or call `urmetview.restart_video`).
There is no watchdog by design: an automatic restart would seize the video
channel at exactly the moment someone is trying to answer on their phone.

Re-picking the current station in the dropdown will *not* restart it. Home
Assistant does not call a select entity when the value has not changed, which
is why the button exists.

Selecting the *other* station keeps the stream up and switches the picture. The
device's command cycles rather than selects, so the integration repeats it
until the reported station matches what you asked for.

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

**The ring carries no video.** Captures taken with the app closed contain zero
media packets — the device raises the push and then waits. Video is pull-only,
so a picture of whoever is at the door does not exist until something asks for
it, which costs ~1 s to start plus up to ~5 s for a keyframe.

The **Start video when the bell rings** option pre-warms the stream so a
snapshot is ready sooner. It is off by default because it seizes the device's
single video channel exactly when your phone app wants it to answer the call —
only enable it if Home Assistant has replaced the app for you.

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
* The push notification the device sends on every ring is **not TLS** — the
  server speaks first, there is no TLS record header anywhere, and the message
  lengths are byte-identical across sessions. It is a proprietary protocol.
  The bodies do appear properly encrypted (7.6 bits/byte entropy, no repeating
  key at any length 1–32, no keystream reuse between messages), so this is not
  trivially readable — but it is also not TLS, so it carries none of TLS's
  certificate validation or forward secrecy, and every ring crosses the public
  internet under a scheme nobody outside the vendor has reviewed.

Put the intercom on an isolated VLAN. Treat the auth hash exactly like a
password. Scrub captures before sharing them.

## Licence

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Urmet.
