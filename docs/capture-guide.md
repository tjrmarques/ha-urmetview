# Capturing traffic for reverse engineering

Everything documented in [protocol.md](protocol.md) came from plain packet
capture — there is no encryption to defeat.

## MikroTik

Saving to a file on the router is simpler than streaming, and enough for most
work:

```
/tool sniffer set file-name=urmet.pcap file-limit=20000 \
    filter-ip-address=<device-ip>/32
/tool sniffer start
   ... do the thing, noting the wall-clock time of each action ...
/tool sniffer stop
```

Download `urmet.pcap` from **Files**, then:

```bash
python3 tools/urmet_pcap.py urmet.pcap
```

Filtering on the device IP catches both LAN and device→cloud traffic, which
matters when investigating the doorbell.

### Two things that have bitten us

**Size limits truncate media captures silently.** A session with video runs at
~300 KB/s, so a default file limit fills in seconds — the capture then looks like
it "missed" an event that actually happened after the buffer was full. Either
raise `file-limit` a lot, or capture with **no video streaming**, which drops the
rate to a few KB/s.

**Duplicate frames.** If every packet appears roughly twice, the sniffer is
seeing both a bridge and its member port. Capture on one interface only —
otherwise each megabyte buys you half as much time.

## Live decoding

To watch traffic in real time instead of post-processing a file, stream it as
TZSP:

```
/tool sniffer set filter-ip-address=<device-ip>/32 filter-port=32100 \
    filter-stream=yes streaming-enabled=yes streaming-server=<your-ip>:37008
/tool sniffer start
```

Then either point Wireshark at UDP 37008 (decode as TZSP), or:

```bash
python3 tools/urmet_tzsp.py --device-ip <device-ip>
```

## What to capture, and why

| Goal | Setup |
|---|---|
| Auth hash | Sniffer running, then open the UrmetView app. The login is sent once, at connect |
| Doorbell, idle session | `tools/urmet_listen.py` running, app closed, then ring |
| Doorbell, active session | App open **with video**, then ring. Needs a large file limit |
| Doorbell baseline | No app, no script, then ring |
| Control run | No app, no script, **no ring**, 90s. Distinguishes ring messages from periodic re-registration |

Always note the wall-clock second of each button press. Correlating that against
the capture is what turns "a message appeared" into "this message is the ring" —
without it, periodic device chatter is indistinguishable from an event.

## Handling captures safely

Captures contain the device UID, the verification code, the cleartext device
password and the replayable auth hash. Anyone holding them can open your door.
Scrub or encrypt before sharing, and rotate the device password afterwards.
