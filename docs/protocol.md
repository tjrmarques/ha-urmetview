# Urmet Kit 1730/67 (UrmetView) — Protocol Specification

Reverse-engineered from live packet captures and verified end-to-end against
real hardware (login, video start, and unit selection all confirmed working
byte-for-byte via a working Python reference client). Written for use as an
implementation reference — e.g. building a Home Assistant custom integration.

A working, minimal Python reference client (`urmet_client.py`, stdlib only)
already implements everything in this document except talk/audio-out and
full video decode-to-file.

---

## Implementation checklist

Read this before writing code. A client can complete cloud lookup, log in,
issue commands, receive correct `{"result":"ok"}` responses, and open the
gate — while media still fails completely. Every item below is required.

| # | Requirement | Section |
|---|---|---|
| 1 | Parse the outer header as `d1 <channel:1> <seq:2 BE>` — **not** a 1-byte seq | 1b |
| 2 | **Send `d1` acks for every received `d0` packet, including retransmits** | 1c, 6b |
| 3 | **Ping every ~1.2s for the whole session**, not just at connect | 4b-2 |
| 4 | Deduplicate on `(channel, seq)`, never on packet content | 6a |
| 5 | Merge video `stream_type` `0x01` **and** `0x02` into one H.264 stream | 5a |
| 6 | Call `start_video` before unit/key/gate commands, or they return `busy` | 4c |
| 7 | Length fields on `d0` count the 4-byte `d1` prefix (`payload + 4`) | 1b |
| 8 | **Send the 4-command session teardown on exit**, or the next connect is refused | 4e |

Items 2 and 3 are continuous background obligations with no
command/response visibility. They are the easiest to omit and the hardest
to diagnose.

### Symptom lookup

| Symptom | Most likely cause |
|---|---|
| Video delivers a short burst then freezes; huge packet counts but few distinct payloads | No `d1` acks (6b) |
| Session-wide `stream_type 0x01` frames absent; only P-frames | No `d1` acks (6b) — *not* keyframe withholding |
| `non-existing PPS 0 referenced` in ffmpeg/ffplay | Only `0x02` forwarded, or stream started mid-GOP (5a) |
| Sequence counter appears stuck at 0 | Reading only the low byte of a 16-bit seq (1b) |
| Live data dropped as "duplicate" | 8-bit dedup key wrapping (6a) |
| `{"result":"busy"}` from unit/key/gate | `start_video` not called first (4c) |
| `{"result":"video busy"}` from `start_video`; all counters flat at zero | Stale session holds the video channel (4c) |

---

## 0. Device identity

- Product: Urmet Kit 1730, WiFi module 1730/67, paired with two
  interconnected outdoor stations (Unit 1, Unit 2).
- App: UrmetView (iOS; Android package `com.cay.urmet`).
- OEM: the module is white-labeled from **CAY CCTV** (`caycctv.com`) —
  confirmed via the Android package name and the cloud server hostnames
  (Section 4a). The underlying transport is a TUTK/Kalay-style "PPPP" UDP
  scheme, but the JSON command set and media framing documented here are
  CAY/Urmet-specific — generic `lib32100`/`get_rtsp.cgi`-style commands
  seen documented for other vendors on the same transport SDK do **not**
  apply to this device.
- Reported device identity (from login handshake):
  ```
  model:     SKY98DC
  vendor:    URM
  dev_type:  DB (doorbell)
  serial_no: 000-000-000-001
  version:   0.7.0.20_EN
  ```
- MAC address (from the LAN discovery broadcast, Section 2c): `cc:64:1a:c2:35:41`.
- LAN discovery UID (broadcast on UDP 6688, periodic): `URMABB-700171-SMCYN`,
  paired with verification code `YALFJS#HLPDLZ` and an 8-character
  lowercase device password (format `PREFIX-NUMBER-SUFFIX` for the UID;
  the numeric segment is packed differently depending on context — see
  Section 2).

---

## 1. Outer wire framing

Two distinct framing shapes exist, depending on message type.

### 1a. Simple messages (checkCam, ping, session-ack, ping-ack)

```
f1 <type> <len_hi> <len_lo>  <payload>
```
4-byte header: magic (`f1`), 1-byte type, 2-byte big-endian length of
`payload` only. No further suffix.

Types used this way:

| Type | Meaning |
|---|---|
| `41` | checkCam - session-open request (phone to device) |
| `42` | session-open ack (device to phone), echoes the same UID payload |
| `e0` | ping |
| `e1` | ping ack |

(Type `d0` = data, Section 1b. Type `d1` = reliability ack, Section 1c;
both use different framing and are covered separately below.)

### 1b. Data messages (`d0`) - commands and media

```
f1 d0 <len_hi> <len_lo>  d1 <channel> <seq_hi> <seq_lo>  <payload>
```
- `<len_hi><len_lo>` (big-endian 16-bit) equals `len(payload) + 4` - the
  length field counts the 4-byte `d1 <channel> <seq>` prefix too, not just
  `payload`. This is a common off-by-4 mistake - verified against a real
  capture.
- `d1` - constant marker byte.
- `<channel>` - **`00` = command channel, `01` = media channel.** Each
  channel carries its own independent sequence counter.
- `<seq_hi><seq_lo>` - **2-byte big-endian sequence number**, incrementing
  per logical message on that channel. Each side maintains its own
  counters.
- `payload` is either a JSON command block (Section 3) or media frame data (Section 5).

> **Correction (important).** Earlier revisions of this document described
> the prefix as a fixed `d1 00 00` followed by a *1-byte* sequence, i.e.
> treating the channel byte as padding and reading only `seq_lo`. That is
> wrong. Because the low byte alone was being read, the sequence appeared
> to be pinned at `0` for an entire session, which led to a long chain of
> incorrect conclusions about device behaviour (see Section 5a). Any
> implementation must read the channel at offset 5 and the full 16-bit
> sequence at offsets 6-7.

### 1c. Reliability-layer acks (`d1`) - phone to device

**This is mandatory, not optional.** See Section 6.

```
f1 d1 <len_hi> <len_lo>  d1 <channel> 00 <count>  <seq 2B BE> * count
```
- `<len_hi><len_lo>` = length of everything after the 4-byte header.
- `<channel>` matches the channel of the packets being acked (`00`
  command, `01` media).
- `<count>` = number of 2-byte sequence numbers that follow.
- Then `count` big-endian 16-bit sequence numbers, referring to the `seq`
  field of received `d0` packets on that channel.

Real examples captured from the official app (all verified reproducible
byte-for-byte):
```
f1 d1 00 06  d1 01 00 01  00 01                                 (media, ack seq 1)
f1 d1 00 08  d1 01 00 02  00 02 00 02                           (media, 2 entries)
f1 d1 00 0c  d1 01 00 04  00 0b 00 0b 00 0b 00 0b               (media, 4 entries)
f1 d1 00 0c  d1 00 00 04  00 04 00 00 00 00 00 00               (command channel)
```
The app frequently repeats the same sequence number within one ack and
sends each ack packet multiple times - consistent with the brute-force
UDP redundancy used everywhere else in this protocol (Section 6).

---

## 2. UID wire packing

The UID `PREFIX-NUMBER-SUFFIX` (e.g. `URMABB-700171-SMCYN`) is packed
**differently in different contexts** — three distinct encodings observed.
The numeric segment is never sent as plain ASCII digits in the packed
forms (2a/2b) — always a big-endian integer.

### 2a. Short form (checkCam, session-ack)
20 bytes total:
```
offset 0-5:   PREFIX, ASCII (6 bytes, e.g. "URMABB")
offset 6-8:   00 00 00 (zero padding)
offset 9-11:  NUMBER packed as 3-byte big-endian integer
offset 12-16: SUFFIX, ASCII (5 bytes, e.g. "SMCYN")
offset 17-19: 00 00 00 (zero padding)
```
Verified byte-exact for UID `URMABB-700171-SMCYN`:
```
55 52 4d 41 42 42 00 00 00 0a af 0b 53 4d 43 59 4e 00 00 00
```
(`0a af 0b` = 700171 as 3-byte big-endian.)

### 2b. Long form (cloud lookup request, Section 4a)
36 bytes total - short form's first 20 bytes, then:
```
offset 20-21: our own local UDP port, 2 bytes little-endian
offset 22-35: 00 * 14 (zero padding, reserved)
```

> **Unresolved conflict — this document and the working prototype disagree.**
> `urmet_client.py`, the client observed working against the real servers,
> writes five zero bytes after the suffix and puts the port at **offset 22-23**,
> followed by twelve zero bytes:
> ```
> offset 17-21: 00 * 5
> offset 22-23: local UDP port, 2 bytes little-endian
> offset 24-35: 00 * 12
> ```
> Both forms are 36 bytes, so a length check cannot tell them apart, and a bad
> packing is answered with a status byte (`0xfd`) rather than an error a client
> would notice — it simply looks like the lookup silently returned nothing.
> The integration therefore sends **both** variants and uses whichever gets a
> candidate. Whichever the device accepts should be recorded here and the other
> removed.

### 2c. Broadcast form (LAN discovery, UDP 6688) — literal ASCII, different layout

The periodic LAN discovery broadcast uses a completely different, much
larger fixed-layout packet (544 bytes observed) with the UID stored as
**literal ASCII including the dashes**, not packed. Full field layout
(offsets confirmed from a real capture):

| Offset | Width | Field |
|---|---|---|
| 0-3 | 4 | header/magic (`22 11 01 08`) |
| 20-32 | 13 | subnet mask, ASCII (e.g. `255.255.255.0`), zero-padded |
| 36-44 | 9 | gateway IP, ASCII, zero-padded |
| 84-92 | 9 | device IP, ASCII, zero-padded |
| 100-105 | 6 | device MAC address, raw bytes (+2 zero-padding bytes = 8-byte slot) |
| 108-131 | 24 | UID, ASCII with dashes (e.g. `URMABB-700171-SMCYN`), zero-padded |
| 132-151 | 20 | verification code, ASCII (e.g. `YALFJS#HLPDLZ`), zero-padded |
| 152+ | 8 | device type, ASCII (`doorbell`), followed by a large gap (other unmapped fields) |
| 232-242 | 11 | firmware version, ASCII (`0.7.0.20_EN`) |
| 296-327 | 32 | **device password, ASCII, zero-padded** (e.g. `akivoueu` + 24 zero bytes) |
| 329 | 1 | flag byte (`01` observed — plausibly "password is set") |

This is the field layout for the broadcast specifically — it does **not**
match the packed forms in 2a/2b, and testing this raw padded password
buffer as direct hash input did not reproduce the login `auth` hash either
(see Section 4b).

---

## 3. JSON command sub-header (inside `d0` payloads)

```
offset 0-3:   a3 01 00 ff        command-channel marker (constant)
offset 4-5:   <subcmd, 2B LE>
offset 6-7:   <cmd seq, 2B LE>   independent counter, increments per command
offset 8-11:  00 00 00 00        reserved (always zero, observed)
offset 12-15: <len, 4B LE>       JSON payload length INCLUDING trailing null
offset 16..:  <JSON text> 00     UTF-8 JSON, null-terminated
```
Multiple command blocks can be concatenated in a single `d0` packet (login
does this - see Section 4b).

Trigger commands (unit-select, key, gate - Section 4d) don't carry JSON;
their "payload" past the sub-header is just a single `00` byte, with `len=1`.

---

## 4. Session lifecycle

### 4a. Cloud rendezvous (needed only to discover the LAN session port — see also Section 9)

Servers, resolved by hostname (confirmed via DNS, prefer hostname
resolution over hardcoding IPs — Urmet may rotate infrastructure):

| Hostname | IP (observed) |
|---|---|
| `p2p1.caycctv.com` | `3.121.150.135` |
| `p2p2.caycctv.com` | `15.161.180.1` |
| `p2p3.caycctv.com` | `35.181.124.200` |

All on UDP 32100. Registration/candidate-exchange only — **media never
flows through these when both endpoints are on the same LAN** (confirmed:
every captured session showed only a brief cloud touch at connection
start, then 100% device-to-phone traffic for video/audio/commands).

Request sequence:
1. Send `f1 00 00 00` ("hello") to all 3 servers.
2. Wait ~0.3s.
3. Send lookup request to all 3 servers:
   ```
   f1 20 <len 2B BE> <UID, long-form packed per Section 2b>
   ```
   (`len` = 36, matching the packed UID payload length.)

Responses (from whichever server answers - one was sufficient in testing):
```
f1 <type> 00 10  00 02  <port, 2B LE>  <IP, 4B reversed>  <8B reserved>
```
- `type=0x01`: server's reflection of your own public address (STUN-style;
  not the device's address - ignore for device discovery).
- `type=0x21`, 8 bytes total (`f1 21 00 04 <status> 00 00 00`): plain
  status ack. `status=0x00` = request accepted; nonzero = rejected (a bad
  UID packing produced `0xfd` in testing).
- `type=0x40`: a candidate address for the looked-up device - sent once
  per candidate (typically one LAN address, one public/relay address, both
  sharing the same port number). Prefer the RFC1918 (LAN) candidate; it's
  the one that actually works when both endpoints are local.

Decode: `port = int.from_bytes(data[6:8], 'little')`;
`ip = '.'.join(str(b) for b in reversed(data[8:12]))`.

The resulting `(ip, port)` is the device's current LAN session address -
this is what you connect to for everything in Section 4b onward. This port
is not fixed - it's chosen fresh by the device each session; re-run the
lookup rather than caching it long-term (unless you determine the device
doesn't actually need the cloud round-trip at all — see Section 9).

### 4b. Session open + login

Once you have `(device_ip, device_port)`:

1. Send checkCam (type `41`, Section 1a) 4 times - payload = short-form UID
   packed per Section 2a. (The 4x repetition matches observed real-client
   behavior; not confirmed strictly required, but safe to replicate.)
2. Send ping (type `e0`, empty payload).
3. Expect (not strictly required to wait for): session-ack (`42`, echoes
   UID) and ping-ack (`e1`).
4. Send login as one `d0` packet containing two concatenated command
   blocks:
   - Block 1: subcmd `c8 00`, JSON `{}` (a "hello" precursor, empty).
   - Block 2: subcmd `0b 00`, JSON
     `{"username":"admin","auth":"<32-hex-char MD5-format hash>"}`.

   Verified byte-exact real packet (outer seq=0, cmd seq 1 then 2):
   ```
   f1 d0 00 66 d1 00 00 00
   a3 01 00 ff c8 00 01 00 00 00 00 00 03 00 00 00 7b 7d 00
   a3 01 00 ff 0b 00 02 00 00 00 00 00 3f 00 00 00
   7b 22 75 73 65 72 6e 61 6d 65 22 3a 22 61 64 6d 69 6e 22 2c
   22 61 75 74 68 22 3a 22 41 38 39 33 35 43 38 44 41 34 41 42
   41 44 39 37 38 32 42 37 30 34 35 30 35 34 36 38 30 44 36 37 22 7d 00
   ```

5. Device replies (subcmd `c8 00`, sent repeatedly for the entire
   session as an apparent keepalive/announce - dedupe by content or by
   log level, don't rely on it appearing exactly once):
   ```json
   {"model":"SKY98DC","vendor":"URM","serial_no":"000-000-000-001",
    "channel":"1","version":"0.7.0.20_EN","limit":"Echosoft","dev_type":"DB"}
   ```
6. Device replies (subcmd `0b 00`) with the real login result:
   ```json
   {"auth":"ok"}
   ```

**Auth hash — derivation unknown, use the hash directly as your credential.**
Username is always literal `admin`. The `auth` field is a static
per-device MD5-format hash (32 hex chars) — confirmed identical across
sessions captured on different days.

Extensive testing was done to find the derivation formula and **none
succeeded**: plain `MD5(password)`, `MD5(username+password)` and reverse,
`MD5(password+verification_code)` and reverse, `MD5(uid+password)` and
reverse, `MD5(serial+password)` and reverse, all pairwise/triple
permutations of {UID, verification code, password, username, serial,
model} with `:`, `,`, `-`, `_`, `#`, `|`, `;` delimiters, classic
HTTP-Digest-style `MD5(username:realm:password)` against many candidate
realms, MD5 of UTF-16LE-encoded strings, double-MD5, truncated
SHA1/SHA256, and MD5 of the *raw wire-format zero-padded buffers*
(Section 2c) in various combinations including the device's MAC address.
None matched the observed hash `A8935C8DA4ABAD9782B7045054680D67` for the
known password `akivoueu`. Common trivial defaults (blank, `admin`,
`123456`, `password`, `urmet`) were also ruled out earlier.

**Practical recommendation:** don't try to derive this hash at runtime.
Capture it once from a real login (packet capture, filter for
`"username":"admin"`) and store the **hash itself** as the stored
credential/secret for the integration — functionally identical to storing
a password, and it's what's actually proven to work.

Follow-ups the official app sends after login, before `start_video`:
```
subcmd 86 03   GET /Network/P2PV2         -> XML P2P config blob
subcmd 86 03   GET /System/DeviceCap      -> XML capability blob,
                                              <StreamCount>2</StreamCount>
                                              (confirms 2 interconnected units)
subcmd ca 00   {}                          -> {"result":"ok","year":...} (date/time)
```
The two `86 03` requests are sent bundled with an empty `{}` hello in a
single packet. Their payload is **not** JSON - it is a literal
null-terminated `GET /path` string using the otherwise-normal command
block framing (Section 3). Responses are XML blobs. Replicating these is
recommended for fidelity with the app, though not proven to be strictly
required.

### 4b-2. Session keepalive - periodic ping REQUIRED

The official app sends a ping (bare `f1 e0 00 00`) roughly **every 1.2
seconds for the entire session**, not just once at session-open.
Observed timestamps from a real capture: 2.47s, 4.69s, 5.92s, 7.15s,
8.38s, 9.61s, 11.96s, ... continuing to session end. Like acks, each
ping is sent as a small burst of duplicates.

This is an ordinary background heartbeat, independent of any user action.
A client that pings only at session-open (as an early revision of this
document implied was sufficient) will see media delivery degrade.

Together with the `d1` acks (Section 6b), this is one of the two
continuous background obligations of a client. Neither is visible in the
command/response flow, and both are easy to omit while everything else
appears to work.

### 4c. Session busy state - important ordering constraint

Unit-select, key, and gate all require an active video session -
calling them before `start_video` (Section 4d) reliably returns
`{"result":"busy"}`. Always call `start_video` first, even if you don't
care about the video stream itself, before issuing gate/key/unit commands.
Confirmed via direct testing: `unit`/`key`/`gate` all succeed cleanly once
video is started first, and fail with `busy` otherwise.

**A second, distinct busy condition:** `start_video` itself can be
refused with `{"result":"video busy"}` (note the different string). This
means another session already holds the video channel — typically a
previous client that exited without closing its session, another copy of
the client running, or the phone app being open.

This failure is easy to miss because **everything else still works**:
cloud lookup succeeds, login returns `{"auth":"ok"}`, and unit/gate/key
commands are accepted. Only media never arrives, with video *and* audio
counters both flat at zero. A client that does not check the
`start_video` result will present this as a mysterious total media
failure.

Clearing it: close the phone app, ensure no other client is running,
wait ~30-60s for the device to time out the stale session, and power-cycle
the module if it persists.

**Prevention: send a proper session close (Section 4e).** A client that
exits without closing leaves the video channel held until device-side
timeout, which is the usual cause of this condition.

**Implementations must check the `start_video` result** and surface this
condition explicitly rather than proceeding.

---

### 4e. Session teardown

Captured directly from the official app closing a session. The app bundles
**four** command blocks into a single `d0` packet; the device acks all four:

```
subcmd 69 00   {"action":"stop"}     stop talk
subcmd 6a 00   {"channel":"1"}       audio sub-channel off
subcmd 67 00   {"channel":"0"}       stop audio stream
subcmd 65 00   {"channel":"0"}       stop video stream
```

`65 00` and `67 00` are the exact counterparts of `64 00` (start video)
and `66 00` (start audio).

Verified byte-exact (outer seq 7, cmd seqs 0x0b-0x0e):
```
f1 d0 00 86 d1 00 00 07
a3 01 00 ff 69 00 0b 00 00 00 00 00 12 00 00 00 {"action":"stop"}\0
a3 01 00 ff 6a 00 0c 00 00 00 00 00 10 00 00 00 {"channel":"1"}\0
a3 01 00 ff 67 00 0d 00 00 00 00 00 10 00 00 00 {"channel":"0"}\0
a3 01 00 ff 65 00 0e 00 00 00 00 00 10 00 00 00 {"channel":"0"}\0
```

Device responses (each on its own subcmd):
```
69 00 -> {"result":"ok"}
6a 00 -> {"result":"ok"}
67 00 -> {"result":"ok","channel":"0"}
65 00 -> {"result":"ok","channel":"0"}
```

After this the phone goes silent; the device continues pinging for a
while, unanswered.

**This is not optional in practice.** Without it the device holds the
video channel until its own timeout, and the next `start_video` is
refused with `{"result":"video busy"}` (Section 4c). Send it on every
exit path including Ctrl-C — there is no ack to wait for, so a few
repeats before the process exits is sufficient.

---

## 4d. Commands reference

All sent as `d0` JSON command blocks (Section 3) unless noted.

### Start video
```
subcmd 64 00   PHONE to DEV:  {"channel":"0","quality":"<Q>","type":"1"}
               DEV to PHONE:  {"result":"ok"}
```
`quality` values (confirmed by cycling through the app's LD/SD/HD selector):

| App label | `quality` |
|---|---|
| LD | `5` |
| SD | `1` (device default) |
| HD | `6` |

### Change stream quality mid-stream (same opcode as talk setup)
```
subcmd cd 00   PHONE to DEV:  {"channel":"0","video":{"codec":"unknown","quality":"<Q>"},
                                "audio":{"codec":"unknown","freq":"1",
                                         "sample":"<random nonce>","channel":"1"}}
               DEV to PHONE:  {"result":"ok","channel":"0"}
```
`"sample"` is a random per-request nonce - not meaningful, ignore it.
`"channel":"0"` throughout this and video-start is a fixed stream index,
NOT the unit selector (see next).

> **`quality` changes frame rate/bitrate, not resolution.** Verified two
> ways from a capture where the app switched SD (`1`) -> HD (`6`):
>
> - **Resolution is unchanged.** The SPS NAL was extracted from every
>   keyframe across the session: exactly one distinct SPS throughout. The
>   stream stays 960x240 in both modes.
> - **Bitrate roughly doubles, via frame rate.** Measured across the
>   switch:
>
>   | | frames | avg frame size | rate | bitrate |
>   |---|---|---|---|---|
>   | quality `1` (SD) | 14 in 2.7s | 4434 B | ~5 fps | 182 kbps |
>   | quality `6` (HD) | 42 in 4.5s | 4061 B | ~9 fps | 306 kbps |
>
>   Frame sizes are essentially the same; the device sends roughly twice
>   as many of them. So "HD" buys smoother motion, not a larger picture.
>
> A client seeing no resolution change after a quality command is
> therefore working correctly.

### Select unit (camera)
```
subcmd d2 07   PHONE to DEV:  (trigger, no JSON - single 0x00 byte payload)
               DEV to PHONE:  {"result":"ok","door":"1"}   -- now on unit 1
                       or     {"result":"ok","door":"2"}   -- now on unit 2
                       or     {"result":"busy"}             -- see Section 4c
```
The `"door"` field name is misleading - this is the camera/unit selector,
not a lock. (Initially misidentified as a door trigger before isolated
per-action captures clarified it.)

### Key / electric lock (acts on whichever unit is currently selected)
```
subcmd d0 07   PHONE to DEV:  (trigger)
               DEV to PHONE:  {"result":"ok"}
```

### Gate (acts on whichever unit is currently selected)
```
subcmd d1 07   PHONE to DEV:  (trigger)
               DEV to PHONE:  {"result":"ok"}
```

None of key/gate/select-unit carry a unit parameter themselves - unit
context is entirely set by the last `d2 07` call.

### Two-way talk (audio out to device - see Section 5b for wire audio format)
```
1. subcmd cd 00   (same stream-config command as resolution change, with
                    the audio sub-object populated)
2. subcmd 68 00   PHONE to DEV:  {"channel":"1"}       (audio sub-channel on)
                  DEV to PHONE:  {"result":"ok"}
3. subcmd 69 00   PHONE to DEV:  {"action":"start"}    (begin talk)
                  DEV to PHONE:  {"result":"ok"}
   ... outgoing mic audio frames flow, Section 5b ...
4. subcmd 69 00   PHONE to DEV:  {"action":"stop"}     (end talk)
5. subcmd 6a 00   PHONE to DEV:  {"channel":"0"}       (audio sub-channel off/idle)
                  DEV to PHONE:  {"result":"ok"}
```
**Confirmed unit-independent:** `"channel":"1"` in step 2 was tested while
actively on unit 2 (selected via `d2 07`, response `door:"2"` confirmed
immediately before) and remained `"1"` — this is a fixed audio-subsystem
index, not tied to which unit/camera is currently selected. **`6a 00` in
step 5 is not a camera/unit reset** — in the same test, `door` remained
`"2"` throughout the whole talk sequence (camera/unit selection is
exclusively controlled by `d2 07`, per Section 4d). `68 00`/`6a 00` with
`"channel":"1"`/`"0"` read as an audio-subsystem on/off toggle — the
counterpart pair to `action:start`/`action:stop`, not a video-related
setting. Steps 4/5 are typically sent bundled together in one packet,
matching how steps 2/3 are also bundled in practice.

---

## 5. Media framing

### 5a. Incoming video/audio (device to phone)

Frame-start packets carry a marker + 32-byte sub-header, at absolute packet
offset 8:
```
offset 8-11:   a5 01 00 ff        frame marker
offset 12:     <stream_type>      01 = video keyframes, 02 = video P-frames, 08 = audio
offset 13-39:  (27 bytes, largely unmapped - likely timestamp/frame-size/
                keyframe-flag/frame-index; not required for playback)
offset 40..:   raw payload        H.264 Annex-B NALs (video, both types) or mu-law bytes (audio)
```
Continuation packets (a frame spanning multiple UDP packets): no `a5`
marker - just the 8-byte outer header directly followed by more raw payload
bytes. Append to whichever stream buffer was most recently opened, until
the next `a5`-marked packet appears.

**There are two separate video sub-streams, not one — both must be merged
to get real continuous video.**

| | `stream_type 0x01` | `stream_type 0x02` |
|---|---|---|
| NAL type | 7 (SPS), followed by 8/6/5 (PPS/SEI/IDR) | 1 (non-IDR P-frame slice) |
| Frequency in a healthy 41.5s session | 5 frames | 113 frames, continuous, gaps as low as 9ms |
| Role | periodic keyframe / reference-frame refresh | the continuous live video |

This is a standard H.264 GOP structure: `0x01` supplies the I-frames
(SPS/PPS/IDR) that P-frames reference, `0x02` carries the continuous
P-frames built on top of them. **Both stream types must be merged into a
single H.264 elementary stream, in arrival order.** Forwarding only
`0x01` leaves a decoder with almost nothing; forwarding only `0x02`
produces `non-existing PPS 0 referenced` errors and no picture, because
P-frames have no reference.

> **Two retracted claims, recorded because they mislead.**
>
> 1. *"Video is event-driven - frames only arrive after interactive
>    commands."* This came from tracking only `0x01` and noticing its
>    frames clustered after commands. `0x02` was the real continuous
>    stream and was being discarded. Video is continuously streamed.
>
> 2. *"The device sometimes sends only P-frames and never a keyframe, so
>    the client must actively request one."* Sessions really were
>    observed with zero `0x01` frames - but the cause was the missing
>    `d1` ack layer (Section 6b), not a keyframe-withholding behaviour.
>    Without acks the device's send window never advances, so whatever
>    was in flight repeats forever and no new keyframe is ever produced.
>    Nudging with resolution-change commands does not fix this; sending
>    acks does.
>
> If video stalls after a short burst, or you see only P-frames, check
> that acks are being sent before investigating anything in this section.

**Video codec: H.264, Baseline profile.** Verified end-to-end - extracted
NAL units from a raw capture (both stream types merged), reassembled, and
successfully decoded/played with ffmpeg (`ffmpeg -f h264 -i clip.h264 -c:v
copy clip.mp4`).

**Incoming audio codec: mu-law (G.711u), 8kHz, mono.** Silence encodes as
the expected `0x7F`/`0xFF` byte pattern. Audio streams continuously once
started - a 41.5s session produced 795 distinct audio frames (deduped by
content) over ~30s of active audio, ~26 frames/sec, matching the
documented 40ms-per-frame format exactly. Raw (non-deduped) packet counts
will look much higher than this due to the 9-12x retransmission
redundancy (Section 6) - that inflation is expected protocol behavior,
not a bug.

### 5b. Outgoing audio (phone to device, talk feature)

Different marker (`a7`, not `a5`) and different, larger header - 84 bytes
total before real audio data starts:
```
offset 8-11:   a7 01 00 ff        outgoing-audio frame marker
offset 12-15:  89 00 00 00        constant (purpose not identified)
offset 16-19:  <len, 4B LE>       payload length; observed constant 0x0140 = 320
offset 20-83:  (64 bytes, observed as zero padding in samples - not
                further decoded; not required for correct playback/encode)
offset 84..:   raw payload        320 bytes mu-law, 40ms @ 8kHz
```
Total packet size: 404 bytes (8 outer + 76 sub-header/padding + 320 audio).

**Outgoing audio codec: mu-law (G.711u), 8kHz, mono - same codec as
incoming, different transport wrapper.** Verified by full round-trip
reconstruction from a live capture: extracted unique frames (deduplicated
via the outer `(channel, seq)` pair, Section 6a), decoded as mu-law/8kHz,
and confirmed clean/artifact-free audio once the header offset was
correctly identified (84, not a smaller value - an earlier off-by-16 bug
produced a loud periodic "hammering" artifact from one leftover zero byte
decoding as a huge-amplitude mu-law spike at every frame boundary; fixed
and confirmed via two independent checks: decoded duration matching real
elapsed capture time to within 0.06s, and frame-boundary sample deltas
matching in-frame deltas once corrected).

**To implement talk (send audio to the device):** encode microphone input
as mu-law/8kHz, split into 320-byte (40ms) chunks, wrap each in the
84-byte header above, assign an incrementing 16-bit sequence on the media
channel (Section 1b), and send each frame about 12 times (Section 6a) for
reliability. The device is expected to ack these with `d1` packets in the
same format the client uses (Section 1c); handling inbound acks for
outbound media has not been implemented or tested, so whether the device
similarly stalls without them is unverified.

---

## 6. Reliability layer

This protocol combines **two** mechanisms. Earlier revisions of this
document described only the first and explicitly (and wrongly) stated it
was "not a request/ack protocol". Implementing only the first will
produce a session that authenticates, accepts commands, and then stalls
after a handful of media frames.

### 6a. Brute-force redundancy

Every logical message is transmitted several times in quick succession -
observed 4-12x for most messages (commands about 4-6x, audio frames 12x),
and 30+ for the initial handshake burst. Receivers must deduplicate.

**Dedup key: the `(channel, seq)` pair** from the outer header (Section
1b) - `channel` at offset 5, 16-bit big-endian `seq` at offsets 6-7.
Process each unique pair once.

**Do not deduplicate by full packet content** - a legitimately repeated
identical result (e.g. re-querying the same state) must still be
surfaced, not silently dropped. Sequence-based dedup avoids this: true
retransmits share a sequence number, while two genuinely separate
requests get different ones even if their JSON is identical.

A generous dedup window is safe (512 entries works well) because the
sequence is 16-bit. Note that a naive 1-byte key wraps every 256
messages and will drop live data.

### 6b. Explicit acknowledgement (`d1`) - REQUIRED

The receiver must acknowledge received `d0` packets by sending `d1` ack
packets (Section 1c) carrying the channel and sequence numbers seen.

**The device holds its send window until acked.** Without acks it does
not advance: it retransmits the same frame indefinitely and never
encodes new ones. This is the single most important requirement for a
working media stream, and the easiest to miss because everything else -
cloud lookup, login, `start_video`, unit selection, command responses -
works perfectly without it.

How this was discovered, and what the failure looks like, since the
symptoms are badly misleading:

- Comparing phone-to-device traffic between an official app session and a
  custom client session:

  | msg type | official app | client with no acks |
  |---|---|---|
  | `d1` (ack) | **7434** | **0** |
  | `d0` (data) | 888 | 30 |
  | `e0` (ping) | 54 | 96 |

  Acks are the app's single most common outbound packet type by a wide
  margin.

- Symptoms without acks: video delivers a short initial burst then stops
  advancing. In one 25s capture the device sent 12433 video packets but
  only **13 distinct payloads**, all first seen within the first 4.7s;
  in one 2s window, 1128 packets were a single byte-identical payload.
  Audio survives noticeably longer because its lower bitrate fills the
  window more slowly.

- The behaviour is timing-dependent, so it presents as intermittent
  ("works sometimes"), which invites incorrect explanations.

**Ack retransmissions too.** If a duplicate arrives, the peer did not
receive (or has not yet processed) the previous ack - so ack it again
even though the payload itself is discarded as a duplicate. Ack *before*
the dedup check.

Send each ack packet a couple of times, consistent with 6a.

### 6c. Diagnostic note

Several incorrect theories were pursued before the ack layer was found -
"video is event-driven and needs a command nudge", "same-value quality
requests are no-ops so cycle values", "the device withholds keyframes",
"the device gets wedged by repeated testing". All were plausible fits to
the symptoms and all were wrong. What resolved it was a direct
side-by-side diff of app traffic versus client traffic, aggregated by
message type. When behaviour differs from the official app, **compare
the traffic profiles before theorising about device behaviour.**

---

## 7. Known gaps / not fully mapped

- Most bytes in the video/audio frame-info sub-header (Section 5a, offset
  13-39) beyond the stream-type byte.
- Most bytes in the outgoing-audio sub-header (Section 5b, offset 20-83)
  beyond the length field.
- **Exact derivation of the login `auth` hash** — extensively tested and
  unresolved; see Section 4b for the full list of ruled-out candidates.
  Store the hash itself as the credential rather than attempting to derive
  it from the password.
- Whether the 4x checkCam repetition and the exact port-candidate ordering
  in cloud lookup (LAN vs. relay) are strictly required or just observed
  real-client behavior.
- **Whether the device requires live internet/cloud access for anything
  beyond port discovery (Section 9).**
- Exact ack semantics beyond "ack what you receive": whether the device
  tracks cumulative vs. selective acknowledgement, what its send-window
  size is, and whether acks may be batched/delayed rather than sent
  per-packet. The app frequently repeats a sequence number within one ack
  and sends multi-entry acks, hinting at more structure than currently
  replicated. Acking every received packet works; it may not be optimal.
- Whether the device stalls symmetrically when the *client's* outbound
  media (talk audio) goes unacked (Section 5b).
- The meaning of the `00` byte at offset 6 of the `d1` ack body (between
  channel and count) - constant in all observed traffic.

---

## 8. Security considerations

Worth documenting plainly, since this affects real physical access control
(gate/lock triggers), not just video privacy:

- The LAN discovery broadcast (Section 2c) sends the UID, verification
  code, **and the device password in cleartext** to the entire local
  broadcast domain — not point-to-point; anything on that network segment
  can passively capture it with no active attack needed.
- The login exchange and the entire session thereafter (video, audio,
  every gate/key/unit command) are **completely unencrypted** — no TLS,
  no session-layer encryption of any kind. Everything in this document was
  derived from plain packet capture with no decryption step.
- The `auth` hash is **static** — the same value every session, with no
  challenge/nonce exchange. A single capture (from the broadcast, from a
  login exchange, or from network access at any point) gives permanent,
  replayable access to trigger gates/locks until the device password is
  changed. There is no session expiry protecting a historical capture.
- Cloud rendezvous (Section 4a) means the same static, unencrypted
  credential also potentially transits the public internet when the app
  is used remotely (relay fallback), not strictly confined to the LAN.
- **Practical mitigations:** network-isolate this device (VLAN/guest
  network with client isolation) so LAN-local passive capture requires
  compromising another device on the same segment first; rotate the
  device password periodically, since a captured hash has no built-in
  expiry; treat the hash exactly like a password when storing it in an HA
  config entry (secrets storage, not plain YAML).

---

## 9. Open question: does the device need live cloud access at all?

**Confirmed:** cloud rendezvous (Section 4a) is the only mechanism found
for *discovering* the current session port — the port is not fixed, isn't
announced on the LAN broadcast, and changes each session.

**Confirmed: the LAN discovery broadcast (Section 2c) does not carry the
session port either, and isn't even a stable port itself.** Checked
directly across multiple captures:

| Capture | Broadcast source port | Actual session port (from cloud lookup) |
|---|---|---|
| urmet-3 | 48992 | 11318 |
| urmet-5 | 48992 | 11304 |
| urmet-6 | 50192 | 20689 |
| urmet (first capture) | 56973 | — |

The broadcast's own source port is itself unstable across captures
(essentially ephemeral, unrelated to session state), and never matches the
session port used afterward. So the broadcast is useful for *discovering*
the device (UID, verification code, password, current LAN IP — see
Section 2c) but gives no path to the port needed to actually connect —
that still requires either cloud lookup (Section 4a) or local port-probing.

**Also confirmed: the broadcast is not triggered by / does not precede a
connection attempt** — in the one cold-start capture with full visibility,
the phone's cloud-derived connection to the device (6.224s) happened
*before* the next broadcast was observed (7.503s), and broadcast content
was identical across captures taken on separate days — consistent with an
autonomous, periodic announcement (most plausibly for first-time
pairing/setup discovery) rather than something reactive to a client's
connection attempt.

**Confirmed:** once the port is known (by any means), the session itself
is 100% local — verified directly by connecting with a manually-supplied
`--host`/`--port` from a prior capture and completing login, video start,
and unit selection with zero cloud traffic for that session. Every
captured real session showed the same pattern: a brief cloud touch at
connection start, then nothing but local device-phone traffic.

**Not yet confirmed:** whether the device would still accept/serve a local
session if it had no internet access at all during that session — i.e.
whether cloud registration is purely for external discoverability, or
whether the device's local session-handling is somehow gated on having a
live cloud check-in.

**Suggested test:** block the device's WAN access at the router, then
attempt a direct local connection using a port already known from a prior
capture (`--host`/`--port`, bypassing cloud lookup entirely). If it still
works, that confirms full local independence once the port is known.

**This does not block the HA implementation either way** — it only
determines which port-discovery strategy to build:
- If cloud access turns out to be required for anything beyond port
  discovery: implement Section 4a as designed (hostname-based cloud
  lookup).
- If the device works fully offline once the port is known: a **local
  port-range probing** strategy (trying a small range of candidate ports
  against the device's known LAN IP) becomes viable as a cloud-independent
  alternative, at the cost of being slightly less deterministic than a
  direct cloud lookup.

---

## 10. Suggested Home Assistant integration shape

- Gate/key control does not need this protocol. Physical relays
  (Shelly, ESPHome) on the outdoor stations' dry-contact terminals are
  simpler, faster, and fully decoupled from Urmet's cloud/app - already the
  recommended approach independent of this spec. Use this protocol's
  key/gate/unit commands only if physical relay access isn't available for
  a given output.
- Video: login, then `start_video`, then decode incoming H.264 (Section 5a),
  then feed into `go2rtc` (bundled with HA's camera integration) for a `camera`
  entity. LD/SD/HD selectable via `quality` in `start_video`.
- **Session maintenance must run as persistent background tasks**, not
  as part of request handling: a ping loop (~1.2s, Section 4b-2) and ack
  emission on every received packet (Section 6b). In an asyncio-based
  integration these belong in long-lived tasks tied to the config entry
  lifecycle, cancelled on unload. If these stop, media silently degrades
  while commands keep appearing to work — so surface task failures rather
  than swallowing them.
- Ack emission sits on the receive path and must not be starved by
  downstream backpressure (e.g. a slow consumer of decoded frames). Keep
  socket reads and acking independent of frame processing.
- Two-way audio: fully specified both directions (Section 5b), but HA has no
  native "intercom" entity - value is limited without a custom card/UI for
  push-to-talk. Lower priority than video/gate control.
- Session port discovery: implement Section 4a (or the local-probing
  alternative from Section 9, once resolved) as a lookup on each
  integration reload/reconnect rather than hardcoding a port, since it
  changes per session.
- Credential storage: store the login `auth` hash (Section 4b) directly as
  a config-entry secret — do not attempt to derive it from the device
  password at runtime.
- See Section 8 before deploying: this device has no meaningful
  transport-layer security. Network isolation is a strongly recommended
  companion to any integration built on this spec.
