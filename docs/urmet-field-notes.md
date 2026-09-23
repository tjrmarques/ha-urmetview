# Urmet 1730 Field Notes

*Reverse-engineering log · handover*

Everything established, measured, and disproved while building a Home Assistant integration for the Urmet Kit 1730 video intercom over its undocumented UDP protocol. Written to resume from, not to admire.

- **Device:** Urmet 1730 + 1730/67 WiFi module
- **UID:** `URMABB-700171-SMCYN`
- **Branch:** `claude/home-assistant-urmet-integration-avrllg`
- **Commits:** 53
- **As of:** 23 Sep 2026

## Status board

| Area | Status |
|---|---|
| Protocol | ✅ **Solved.** Login, video, audio, station switching, lock and gate all work from Python against the real device. |
| Discovery | ✅ **Solved and local.** LAN search returns a working session, no cloud needed. |
| Doorbell | ✅ **Understood.** Ring is a TCP push to port 32002, detectable by router mirror. |
| Video in the mux | 🟡 **Fixed and ported (§20); not yet re-tested live.** Both streams now carry a real, explicit per-frame pts in a small container ffmpeg reads timing from (video: local receive-time; audio: the device's own embedded clock) instead of ever trusting `-use_wallclock_as_timestamps` — validated via the sandbox bridge against the real device (audio: zero warnings in a 35s session; video: ~10 isolated, self-correcting single-tick corrections, down from "dozens throughout"), and ported into `media.py`. **Next step: watch the iPhone/browser on the real deployed instance to confirm the freeze/flash and timeline-catch-up symptoms are actually gone.** |
| Audio in the mux | 🟡 **Redesigned this session (§20) + a real quality bug found and fixed (§21).** Timing: superseded the §13 sample-count-derivation fix with an explicit pts decoded from the device's own embedded clock (validated: 398 real frames, zero monotonic violations). Quality: the AAC encode step was clipping — pre-existing, unmasked by the user's first real listen of this pipeline; fixed with `-af volume=-4dB` headroom, confirmed clean across a full real capture including genuine speech. **Awaiting the user's own listening confirmation that it now sounds right**, not just clean by measurement. |
| Relay fan-out (stage 4) | ✅ **Real bug found and fixed (§17)**, first time this path was ever exercised against a real consumer — duplicate first-GOP delivery to joining consumers. |
| Push to repo | ✅ **Working as of 14 Sep 2026 (new container).** Normal `git push` succeeds; the bundle workaround is no longer needed. |
| Session recovery | ✅ **Applied.** §10's `_connected` fix is back in (see below — it had been reverted on purpose, not because of any doubt about it). Also new: the coordinator now detects a *stale* stream (session still `connected`, but no real media for a while) and recovers it, cheap path first — see §15. |
| Stream stays on when idle | ✅ **Fixed (§16).** Dashboard thumbnail polling could pin the device stream open, or restart it, indefinitely with nobody watching. |
| Brand icon | ✅ **Shipped** at `custom_components/urmetview/brand/icon.png`, confirmed served correctly via HA's brands proxy API. |
| Dev sandbox → device reach | ✅ **Solved via a WebSocket-to-UDP bridge.** This container has no direct LAN path to the device (see §12); a host-side bridge service closes that gap for testing. |
| Getting real debug output from HA | ✅ **Solved (§19).** `system_log`/the on-screen Logs page is WARNING+-curated regardless of logger level; per-integration "Enable/Disable debug logging" from the integration card is what actually gets full DEBUG content, via a downloadable log file. |

---

## 01 — Where it stands right now

The device is not the problem and neither is the protocol work. A laptop viewer streams from it indefinitely, switches outdoor stations, and plays cleanly in `ffplay`. The ffmpeg mux — the thing that was failing — is now **fixed and confirmed against the real device**, video and audio both. See §02 for the full writeup: root causes, the exact changes, and the numbers.

> **The one thing to know before anything else**
>
> All of §02's fixes live only in the standalone test script (`urmet_live_view_ts.py`, outside the repo — see §09), not in `custom_components/urmetview/media.py`. Home Assistant is still running the old, broken pipeline. **Porting §02 into `media.py` is the next real task** — see §11.

The old "prime suspect, untested" callout that used to sit here was right about the symptom (no resolution reaching the consumer) but wrong about the mechanism — it wasn't the probe flags being too aggressive, it was **input order**. §02 has the corrected story.

## 02 — MPEG-TS video + audio — fixed against the real device (test rig only)

A full session against the standalone test script (`urmet_live_view_ts.py` — see §09), the same command line the integration builds, iterating purely on live evidence from the real device. Both symptoms that blocked Home Assistant are now fixed and confirmed. **None of this is in the repo yet** — the fixes below need porting into `custom_components/urmetview/media.py` (§11).

### Video: froze after one frame, reported no resolution

**Root cause:** not the probe flags, but **input order**. ffmpeg opens inputs in the order given on the command line and finishes probing one before it even connects to the next. Video was listed first, so it ate the whole probe budget while audio's connection waited behind it — and the fix in place at the time (`-analyzeduration 0 -probesize 100000`, added to unblock audio faster) starved video's own probe of enough data to parse its SPS, which is why the resolution came back as `none`. Confirmed directly: with only one input (`--no-audio`), those exact same tight values played back fine — proving the flags were never the real problem, input contention was.

**The fix:** list the audio input first in the ffmpeg command. Its format is fully declared (`-f mulaw -ar 8000 -ac 1`), so it opens near-instantly and no longer blocks behind video's probe. Video, now second, is free to probe without holding audio's connection hostage.

**Latency, measured against the real device**, tuning the video probe budget down now that it no longer blocks anything else:

| analyzeduration / probesize | Result |
|---|---|
| 0 / 100000 (video first, original order) | Resolution `none`, froze after one frame — the original bug |
| 1000000 / 500000 (audio first) | Correct resolution, plays continuously, but ~6s fixed startup latency |
| 0 / 100000 (audio first) — **current default** | Correct resolution, plays continuously, latency down to **~3s** |

The app's own latency is ~1s, so there is still a gap to close. **Untested:** probesize below 100000 (e.g. 32768) — the SPS/PPS should be in the very first bytes of the first keyframe (the script already withholds everything before the first keyframe, so that *is* the first byte ffmpeg sees), so it plausibly needs much less than 100KB to determine dimensions. This is the most promising remaining lever on video latency and was the last thing queued for testing.

### Audio: loud, choppy, on-and-off static — much worse than the app

**Two wrong turns before the real cause**, both worth recording so they are not re-tried:

1. First suspected `-use_wallclock_as_timestamps` on audio fighting a fixed-rate sample-count clock. Removing wallclock stopped the "Queue input is backward in time" spam but made audio drift **up to a full minute behind video** over a longer session — ffmpeg's assumed 8kHz clock silently ran slower than real delivery, and the gap compounded for as long as the session ran. Reverted.
2. Then suspected bursty delivery into ffmpeg's audio socket (real audio arrives from UDP in irregular clumps). Built a dedicated pacer thread that writes exactly one 40ms frame to ffmpeg on a strict, monotonic tick, real audio when queued, silence otherwise, restoring wallclock timestamps now that delivery was steady. **The DTS spam was still there afterward, identical** — this was not it either.

**Actual root cause:** the hand-rolled silence filler itself. It existed to stop a quiet audio input from stalling video in the muxer — but `-max_interleave_delta 100000` (§07) already fixes that stall on its own, no padding needed. Measuring the real device settled it: marker-to-marker audio arrival on the wire never gapped more than **~200ms** even over a full minute — the device essentially never stops sending, so there was no real silence to fill. Every synthesized "quiet" frame injected into the stream was a fabricated discontinuity, and that is what was audible as static, not a timing artifact at all.

**The fix:** stop injecting synthetic silence. Just write real audio as it arrives (still through the pacer, still with wallclock on) and let `max_interleave_delta` handle gaps, exactly like the app does. Confirmed: noise gone, sounds clean.

> **Ignore this warning going forward**
>
> `Queue input is backward in time` / `Non-monotonic DTS` is still present in the ffmpeg log constantly, with the fix applied and audio confirmed clean. It is cosmetic — an internal ffmpeg timestamp-quantization quirk, not an audible defect. Two sessions were spent chasing it as the cause of the noise; it never was. Don't re-open that investigation.

### Also fixed along the way

- The default aspect-ratio bitstream rewrite (`sample_aspect_ratio=1/2`) was **cropping** the picture. Off by default now; empty string leaves the bitstream alone.
- `-thread_queue_size` dropped from the shipped 512 to a configurable default of 32 — 512 packets is ~20s of audio buffering capacity, which is itself enough to explain multi-second creeping latency if anything downstream is even briefly slower than real-time.

### What is confirmed, current-default ffmpeg command shape

```
ffmpeg -hide_banner -loglevel warning -fflags nobuffer -flags low_delay
  # audio first -- opens instantly, no longer blocks behind video's probe
  -use_wallclock_as_timestamps 1 -f mulaw -ar 8000 -ac 1
  -thread_queue_size 32 -i tcp://127.0.0.1:<audio-port>
  # video second -- free to probe without starving audio
  -use_wallclock_as_timestamps 1
  -analyzeduration 0 -probesize 100000 -thread_queue_size 32
  -f h264 -i tcp://127.0.0.1:<video-port>
  -c:v copy -c:a aac -b:a 64k -ar 16000 -max_interleave_delta 100000
  -f mpegts -mpegts_flags +resend_headers -pat_period 0.5 pipe:1
```

Audio is written by a dedicated pacer thread at a strict 40ms tick — real frames when queued, nothing (not silence) when the queue is empty — which is what makes the wallclock timestamps on that input trustworthy.

## 03 — The pipeline, and how far each stage is proven

| # | Stage | Status |
|---|---|---|
| 1 | Device → UDP session — login, acks, ordering, keepalive. Runs for hours. | ✅ proven on hardware |
| 2 | Reassembly → H.264 elementary stream — merges keyframe and P-frame substreams in sequence order. | ✅ proven on hardware |
| 3 | ffmpeg mux → MPEG-TS — H.264 copy + mu-law to AAC. | 🟡 fixed and confirmed against the device (§02) — in the test rig only, not yet ported into `media.py` |
| 4 | Relay fan-out → consumers — 188-byte alignment, consumers admitted at a PAT. | ✅ real bug found and fixed (§17) once actually exercised against a live consumer — was synthetic-tests-only before |
| 5 | go2rtc → WebRTC | 🟡 first real end-to-end attempt made (§17); revealed the §4 bug rather than confirming this stage on its own |

Stage 3 is now settled (§02) — both symptoms that made it impossible to tell stages 3, 4 and 5 apart are fixed and confirmed against the device. Stages 4 and 5 remain exactly as untested as before: nothing about them could be diagnosed while the stream feeding them was wrong, and that is still true until §02 is ported into `media.py` and re-tested inside Home Assistant.

## 04 — Protocol facts

A PPPP/CS2 derivative over UDP. All of the following are confirmed against the hardware or against byte-exact captures.

- **Verified.** Framing is `f1 <type> <len 2B BE>`. Data packets are `d0` with a `d1 <channel> <seq 2B BE>` prefix, and the length field **counts that four-byte prefix**.
- **Verified.** Two obligations run for the life of the session: ping every ~1.2s, and a `d1` ack for **every** `d0`. Miss either and the device stalls its send window with no error anywhere.
- **Verified.** Command blocks use marker `a3 01 00 ff`, little-endian subcommand and sequence, and a length that **includes the trailing NUL**.
- **Verified.** Media frames use marker `a5 01 00 ff` with a 32-byte sub-header. Stream type `0x01` is keyframes, `0x02` P-frames, `0x08` audio. Continuation packets carry no marker and belong to whichever stream was opened last.
- **Verified.** Video is H.264 at a fixed 960×240; quality settings change bitrate and frame rate only. Audio is G.711 mu-law, 8 kHz mono, both directions.
- **Verified.** Station selection **cycles** rather than selects — it takes no parameter and reports where it landed, so reaching a specific station means repeating until the report matches.
- **Verified.** `start_video` must precede station, lock and gate commands or they return `busy`. Teardown on exit is mandatory, or the next connect is refused with `video busy`.
- **Verified.** Retries must **resend the identical datagram**. Rebuilding a command with a fresh sequence number executes it twice — which for a door release is not academic.

## 05 — Discovery: the LAN-search punch offer

This took several wrong turns and is worth stating precisely, because the obvious reading of it is wrong.

Broadcasting `f1 30 00 00` to UDP 32108 makes the device reply with a `0x41` — a **punch packet**, the same message a client sends to open a session — not the `0x31` announcement stock PPPP documents. The payload is exactly the 20-byte short UID and nothing else:

```
f1 41 00 14 55 52 4d 41 42 42 00 00 00 0a af 0b 53 4d 43 59 4e 00 00 00
            └── "URMABB" ──┘  pad   └700171┘  └── "SMCYN" ──┘  pad
   └ length 0x14 = 20 bytes — no port field anywhere
```

So the reply carries no port information. What matters is its **source port**: the device has opened a one-shot session offer there, and **a login to that port works directly** — verified three ways, on the socket that received it, on a different socket, and after a five-second delay.

> **The trap**
>
> The offer **binds to the first peer that talks to it**. Probing the port to "verify" it claims the offer, so the real session then arrives as a stranger and is ignored. This was the cause of setup failing with `Connection refused` moments after discovery reported success — the verification step was the bug.
>
> A probe proves nothing anyway: the offer port answers `SESSION_ACK` to almost **any** message type carrying a 20-byte UID payload — `0x44`, `0x8a`, `0xff` alike. Only a login distinguishes a session from a responder.

### Ports, and what each source actually gives

| Source | Yields | Cost | Notes |
|---|---|---|---|
| LAN search (broadcast :32108) | A working session offer | ~2 s | Local; needs the same subnet |
| Cloud lookup (`*.caycctv.com`) | A registration port | ~4 s | Leaves the network |
| Port sweep | All answering ports | 30–60 s | Claims any pending offer as it goes |
| UDP 6688 announcement | UID, IP, MAC, password | ~8 s | No port; password in cleartext |

The device keeps **one registration socket per cloud rendezvous server** — three of them — and all three accept logins. A sweep therefore finds several ports, none of which is more "the" session port than the others. Ports rotate between sessions; 32108 answers only `0x30` and nothing else across all 256 message types.

## 06 — The doorbell

The ring is **a TCP connection to port 32002** on one of two hardcoded push servers — `54.84.37.235` (AWS) and `139.59.110.98` (DigitalOcean) — opened at the moment the button is pressed. It is the only TCP the device ever makes. Confirmed in all three ring captures, at +11.401 s, +7.590 s and +13.552 s.

The device **races both providers**, transacting with whichever greets first and closing the loser 2–3 ms later. The payloads are properly encrypted — 7.6 bits/byte entropy, no repeating-key XOR at any length 1–32, no keystream reuse — but not TLS: the server speaks first, there is no record header, and message lengths are identical across sessions (16/64/16/240/177/16/94), so the shape reads as challenge → auth → token → two payloads → ack → result.

Detection is by MikroTik `sniff-tzsp` mangle rule mirroring to Home Assistant on UDP 37008, which the integration decodes. `f1 f9` is the call going *unanswered* ~22 s later, not the ring; `f1 12` is periodic registration every ~33 s. Both were mistaken for the ring at different points.

**Video is pull-only.** No media arrives unless a client asks, so there is no free snapshot on a ring. An opt-in pre-warm exists but is off by default — it seizes the single video channel at exactly the moment the phone app wants to answer.

## 07 — Measured ffmpeg behaviour

All measured locally against a real ffmpeg 6.1.1, not recalled. These are the numbers that should survive into the next session.

| Finding | Measurement | Status |
|---|---|---|
| A quiet audio input stalls video | Audio stopping at t=2s stopped **all** output from 2s to 9s, resuming at 10s — the default `max_interleave_delta`. At 100ms, no gap at all. | ✅ applied |
| Input open order costs seconds | ffmpeg finishes probing input 0 before it *connects* to input 1. Measured at 8.2s live, 13s in an earlier build. Nothing muxes meanwhile. Resolved in §02: put audio (trivial to open) first, video (needs a real probe) second, instead of shrinking video's probe budget to compensate. | ✅ fixed (§02) |
| `aresample=async=1` (the surviving form of `asyncts`) | No effect on any variant tested. It matches audio to its own timestamps and needs packets to act on; a silent input gives it nothing. | ✅ ruled out |
| Aspect correction syntax | `sample_aspect_ratio=1/2` works, giving SAR 1:2 / DAR 2:1 on 960×240. `1:2` and `1\:2` are both rejected — a colon is ffmpeg's option separator. | ✅ applied |
| Time to first byte, production pipeline | 0.9s without audio, 1.1s with. 3.6s / 4.3s when the device starts sending 3s late. Consistent across runs. | ✅ healthy |

### Warnings that look like faults and are not

- `Failed to parse header of NALU (type 0)` — appears in the working laptop viewer too, which plays fine. It comes from a client joining mid-frame. **No capture has ever shown a mangled stream.**
- `Non-monotonic DTS; previous: N, current: N` — reproduced with clean synthetic H.264. It is `-use_wallclock_as_timestamps` giving two packets the same microsecond.

## 08 — Corrections log

Claims made during this work that turned out to be wrong, with what replaced them. Recorded so a fresh session does not re-derive them — several cost hours, and two of them were fixes that caused the bug they were meant to solve.

> **Said:** "LAN search returns the device's session port."
> **Truth:** It returns a **session offer** that binds to the first peer to use it. The port scan only appeared to confirm the first reading because it ran a minute later in the same process, while that socket was still alive.

> **Said:** "Follow the port that answers, not the port we asked."
> **Truth:** The answering port was never the problem. **The probe itself was** — it claimed the offer and the real session was then ignored. This "fix" was the cause of the failure it was written for.

> **Said:** "The elementary stream is mangled — doubled start codes."
> **Truth:** No evidence for it at any point. A doubled start code decodes cleanly; ffmpeg skips empty units silently. The warning is benign and appears in the known-good viewer.

> **Said:** "New clients are primed with the last keyframe and joined at a frame boundary."
> **Truth:** Described code that did not exist. `_on_out_connect` simply added the writer to the broadcast set.

> **Said:** "Snapshots still work, so the muxer is fine."
> **Truth:** Snapshots run through the same muxer. When the aspect argument broke ffmpeg, both died together.

> **Said:** "ffmpeg interleaves its inputs, so a quiet input holds up the other."
> **Truth:** True for two dense streams, but **bounded** by `max_interleave_delta`, not indefinite — and not a general law: sparse streams (data, subtitles, KLV) are handled differently.

> **Said:** "go2rtc sees video only, so we ship a broken mux."
> **Truth:** That log came from a build with no probe limit, where ffmpeg took 13s to reach the audio socket — audio genuinely was not in the stream yet when go2rtc looked. Evidence of startup ordering, not of a broken mux.

> **Said:** "The port sweep may have destabilised the device."
> **Truth:** It sends one packet per port to one IP; the device only ever sees the ports it has bound. No mechanism for harm.

## 09 — Repository and delivery

**Push was blocked for most of the project** — GitHub returned 403, *"Claude doesn't have GitHub access to tjrmarques/ha-urmetview for your organization"*, and every delivery up to that point was a `git bundle` plus a component tarball. **Confirmed fixed as of 14 Sep 2026**, in a fresh container: `git fetch` and `git push --dry-run` both succeed cleanly against `origin`. Normal pushes/PRs should work now — no need to fall back to bundles unless this regresses again.

### Layout

| Path | Contents |
|---|---|
| `custom_components/urmetview/urmet/` | Home-Assistant-free protocol core: `protocol.py`, `session.py`, `discovery.py`, `audio.py`, `const.py` |
| `custom_components/urmetview/` | The integration: coordinator, media pipeline, doorbell mirror, seven platforms, eight services |
| `tools/` | Nine `uv run` scripts, PEP 723 metadata, stdlib only |
| `tests/` | Six files, 63 tests, no Home Assistant needed |
| `docs/` | `protocol.md`, `capture-guide.md` |

### Tools worth knowing

- `urmet_check.py` — single-file network check; LAN search, cloud, port scan. Runs standalone with no checkout.
- `urmet_login.py` — standalone login test across every discovery method; tells you which yields a port that actually logs in.
- `urmet_lansearch.py` — one question per test, connected sockets so ICMP is visible.
- `urmet_cli.py` — video, station, lock, gate with clean teardown.
- `urmet_tzsp.py` — decodes the MikroTik mirror for doorbell work.

Separately, outside the repo: the user's own `urmet_live_view.py` (raw H.264, known good) and `urmet_live_view_ts.py` (MPEG-TS on one port, audio on by default) — the latter being the current test rig, since it reproduces the integration's mux without Home Assistant in the way. As of 12 Sep it carries the §02 fixes (audio-first input order, tight probe values, no synthetic silence, a dedicated audio pacer thread) and is the reference to port from when updating `media.py` — ask the user for the current copy, it isn't tracked in git.

## 10 — The reconnect bug — applied

Found live in Home Assistant, root-caused, fixed, tested, and then reverted on purpose in an earlier session — that session was asked to stop touching the integration and write things down instead. **Reapplied**, along with a matching test in `tests/test_session.py` (independently rewritten the same way described below, before this note was reread — same shape, good sign the design holds up).

### Symptom

A station-select action failed with a toast the integration was already designed to produce, and then **every subsequent action failed the same way**, indefinitely, until Home Assistant was restarted:

```
Failed to perform the action select/select_option.
Nothing is listening on 10.0.50.6:18891 - the device's session port has changed
```

### Root cause

In `urmet/session.py`, `error_received()` already detects this correctly — an ICMP port-unreachable means the device rotated its session port, exactly as documented in §05. It latches `self._unreachable` and fails whatever command was waiting, which is where that error text comes from. What it does **not** do is set `self._connected = False`. The UDP transport itself is still open; only the peer is gone. So the `connected` property keeps reporting the session as healthy.

Every reconnect path in `coordinator.py` gates on that property — the per-command `_async_require_session` check, and a keepalive loop that already polls every 5s specifically to rebuild dead sessions. Both saw a session that looked fine and did nothing. The device rotates its session port on its own schedule (§05), so this is not an edge case — it is the normal way a long-lived session ends, and it left the integration permanently stuck the first time it happened after startup.

### The fix

One line, in the same block that already latches `_unreachable`:

```python
def error_received(self, exc: Exception) -> None:
    _LOGGER.debug("UDP error on session to %s:%s: %s", self.host, self.port, exc)
    if isinstance(exc, ConnectionRefusedError):
        self._unreachable = exc
        self._connected = False          # <-- the fix
        for futures in self._pending.values():
            for future in futures:
                if not future.done():
                    future.set_exception(UrmetError(...))
```

Nothing else needs to change. `_async_connect()` already rediscovers from scratch through `async_find_candidates` rather than trusting the cached port, which is the whole point of the design in §05 — it just was never being invoked.

### Test written and verified

Added to `tests/test_session.py`: mark a session live (`_connected = True`, a stand-in transport), feed it a `ConnectionRefusedError`, assert `.connected` goes `False`. Fails without the fix, passes with it. 64 tests total, all green with the fix applied.

> **Known limitation, by design — worth a decision, not a fix**
>
> The action in flight when the port dies still surfaces one error, same as before — the ICMP arrives asynchronously, sometimes after the command already believed the session was fine. What the fix restores is that the **next** action succeeds, either on your next tap or within 5s via the keepalive loop, instead of repeating the same failure forever.
>
> Auto-retrying the exact failed action was deliberately **not** added. For lock/gate specifically, blindly retrying after a port swap is the kind of thing worth testing deliberately against the device rather than assuming safe — retrying only after rediscovery has actually completed is not a duplicate trigger (the device never received the first one), but it deserved a decision, not a guess baked into a patch that was about to be reverted anyway.

## 11 — What to do next

1. ~~Port §02 into `custom_components/urmetview/media.py`.~~ **Done** — audio input before video, `analyzeduration=0 probesize=100000`, and the §13 wallclock fix are all in `media.py` now, not just the test rig. `tests/test_media.py` has a new test (`test_audio_input_has_no_wallclock_and_comes_first`) guarding both.
2. **Try shrinking video's probe budget further** (`--probesize 32768` or smaller against the test rig) if there's time — it's the most promising remaining lever on the ~3s video latency (app is ~1s) and cheaper to iterate on outside HA. Still open.
3. ~~Apply the §10 fix~~ **Done.**
4. **Return to Home Assistant / go2rtc.** Stages 4 and 5 (§03) have never been tested against a correct stream. Now that §02 and §13 are both in `media.py`, not just the test rig, that test is finally possible — there is no more excuse to keep guessing at go2rtc. Still open.
5. **New from this session, see §15**: the device (and the Urmet phone app itself) stops sending media after a while into a session — confirmed real, not a bridge artifact. The coordinator now detects and recovers from this (cheap path first, confirmed working against the real device), but that recovery logic has no automated test coverage yet — `coordinator.py` is the one integration module without a `test_*.py` file, and building that scaffolding (it needs `HomeAssistant`/`callback`/dispatcher stubs that `media.py`'s tests explicitly avoid needing) is real, not-yet-done work.

### Open questions

- Does re-selecting the *current* station recover a stuck picture, or does it need a real switch to the other unit and back? A committed change made same-station selection a no-op, which may have removed a working recovery.
- How long do the device's ports stay stable? This decides whether caching a port is worth anything.
- What's left of the ~2s gap between the test rig's ~3s video latency and the app's ~1s, once probesize is minimized? Worth deciding whether it's good enough to ship or needs another round.

## 12 — This sandbox has no direct network path to the device; a WS bridge closes the gap

As of 21 Sep 2026, sessions on this project run inside an `.devcontainer/`-based sandbox (see the repo's `.devcontainer/` — Containerfile, devcontainer.json, initialize.sh) whose network is deliberately isolated: an internal-only podman network, egress only through a shared, allowlist-based HTTP(S) mitmproxy. There is no route to the physical LAN the Urmet device lives on (`10.0.50.0/24`, a separate VLAN from wherever the sandbox host itself sits), and none of the standard workarounds hold up:

- **A scoped macvlan/ipvlan network for the container** was designed and partially implemented (see git history on this branch around 14-15 Sep for the abandoned `.devcontainer/initialize.sh`/`devcontainer.json` changes), including a NAT/DHCP-collision analysis and an nftables single-host ACL. Abandoned once it became clear the container's own host PC sits on a *different* VLAN (`home-vlan`/20) than the device (`cloudthings-vlan`/50), and the user did not want any container presence on VLAN 20 even as a routed hop.
- **Cross-VLAN unicast from VLAN 20 also does not reach the device's LAN-search port reliably** — confirmed this is a MikroTik connection-tracking artifact, not a firewall block: the device's LAN-search reply comes from a *different* port than it was asked on (confirmed independently, see §05's "the offer binds to the first peer" and the live `urmet_lansearch.py` output that shows `REPLY ... from DIFFERENT port`), and a stateful firewall between two VLANs won't recognize that reply as `established,related` to the original outbound packet — it looks like a new, disallowed connection in the wrong direction and gets dropped by the final catch-all.

**What actually works:** a small WebSocket-to-UDP bridge service, run by the user in its own container with genuine L2/L3 presence on `cloudthings-vlan` (not routed from VLAN 20). Reached from inside the sandbox at `wss://<bridge-host>:8000/udp?host=<ip>&port=<port>`, through the sandbox's own mitmproxy (which needs the bridge's hostname/IP added to its allowlist, same mechanism as any other allowed host) — confirmed to ride the proxy's existing HTTPS `CONNECT`-tunnel path cleanly (a plain unencrypted `ws://` was deliberately avoided as an untested code path through an HTTP(S)-oriented proxy).

Three tunnel modes, settled after some iteration:
```
?host=10.0.50.6&port=32108&announce_src=1     # unicast discovery: replies prefixed 2B source port
?host=10.0.50.255&port=32108&announce_src=1   # broadcast discovery: replies prefixed 4B source IP + 2B port
?host=10.0.50.6&port=<N>                      # session: raw payload, no prefix, zero overhead
```
`announce_src=1` is mandatory for the broadcast host (a broadcast reply with no source info is useless — could have come from any device on the subnet) and optional for unicast. The bridge's ACL restricts `host` to exactly the device's IP or the subnet broadcast address — nothing else — enforced server-side, not just documented. The prefix, when present, must be stripped by the client before treating the rest as the raw device protocol payload.

**Important — this bridge is a sandbox-only testing aid.** The actual Home Assistant integration keeps talking raw UDP directly, unchanged; it will have normal LAN access when actually deployed, since Home Assistant itself doesn't run inside this isolated devcontainer. None of the bridge/WebSocket code belongs anywhere near `custom_components/urmetview/` — it exists purely so a coding session in this particular sandbox can still exercise the real, unmodified protocol code (`urmet/session.py`, `urmet/discovery.py`, `urmet/protocol.py`, and the standalone `urmet_client.py`/`urmet_live_view_ts.py` test rig) against the real hardware.

Client-side scaffolding for this lives in `tmp/` (gitignored, not part of the repo):
- `bridge_socket.py` — a blocking socket-like shim (`sendto`/`recvfrom`/`settimeout`/`close`) backed by the bridge, installed via monkeypatching `urmet_client.socket.socket` so `urmet_client.py`/`urmet_live_view_ts.py` run completely unmodified.
- `urmet_bridge_client.py` — proves the real `session.py`/`discovery.py` (the actual integration code, not the standalone reference client) works end-to-end through the bridge: broadcast discovery → login → device info query. Confirmed working.
- `urmet_bridge_ts_capture.py` — non-interactive driver: discovery, login, `start_video`/`start_audio`, runs `LiveView` from `urmet_live_view_ts.py`, captures N seconds of MPEG-TS to a file for `ffprobe`/analysis. This is what §13's investigation was run through.

A prerequisite fixed along the way: `ffmpeg`/`ffprobe` are not installed in this sandbox image by default and there's no root/apt access at runtime — added properly to `.devcontainer/Containerfile` (standard Ubuntu archive package, its own labeled block) rather than worked around via an unrelated allowlisted host. (A first attempt did exactly that workaround — pulling a static ffmpeg binary via the `imageio-ffmpeg` PyPI package, since `pypi.org` happened to already be allowlisted for normal package management, to route around the proxy's explicit deny of a static-build host. Flagged and reverted; worth not repeating.)

## 13 — Audio lagged video in the mux by several seconds: root-caused and fixed

Distinct from the §02 static/noise bug (already fixed — that was synthesized silence padding, confirmed gone). This was a newer, separate symptom: recorded audio, once it starts, was complete and in the correct order — nothing missing, nothing reordered — but the whole thing was shifted later relative to video by a large, roughly fixed amount. Measured via the bridge test rig at ~6-9s per capture; reported from an earlier, non-bridge real-device session at "~30s or whatever it was."

**Root cause, confirmed directly (not inferred):** `-use_wallclock_as_timestamps` on the raw audio input (`-f mulaw`). ffmpeg samples wallclock once per underlying socket `read()`, not once per logical 320-byte/40ms audio packet. `audio_pacer_loop`'s steady 40ms write cadence (§07) does not prevent this — ffmpeg's own read from the TCP socket can still scoop up several already-queued packets in one call regardless of how evenly they were written, and each of those packets then gets stamped with the same, single, stale "now". Confirmed by splicing ffmpeg's `ashowinfo` filter (audio-only; the plain `showinfo` filter is video-only in this ffmpeg build — `V->V`, confirmed via `ffmpeg -filters`, and using it on `-af` fails with "Cannot connect video filter to audio input") directly ahead of the encoder: **13 consecutive packets, each with genuinely different content (distinct per-frame checksums, nothing dropped or duplicated), all landing on the identical `pts_time`.** That collapse is what triggers ffmpeg's `[aac] Queue input is backward in time` / `[mpegts] Non-monotonic DTS ... changing to previous+1` cascade seen in the encoder log on every later packet in the burst — the muxer's forced "just add 1 tick" correction is what actually produces the growing, multi-second lag, not the resample step, not input order, not a missing `AUDIO_START`, not device-side delay (all ruled out below, before this was found).

**The fix:** drop `-use_wallclock_as_timestamps` from the audio input only (video keeps it — raw H.264 has no timing of its own). Audio's format is fully declared (`-f mulaw -ar 8000 -ac 1`), so ffmpeg derives pts purely from cumulative sample count instead — immune to read-batching, since it never consults wallclock at all. Applied in `tmp/urmet_live_view_ts.py`'s `LiveView.start_mux`.

**Validated on the real, unmodified fixed pipeline** (no diagnostic patches) via the bridge, 40s capture: zero `backward in time`/`Non-monotonic DTS` warnings (down from dozens), and video-minus-audio pts offset stayed bounded at roughly 0.01-0.6s with no growth trend across the whole session — versus the original bug's ~7.6s gap that grew over the capture. Also checked over a separate 42s run with 171 real device-delivery gaps over 100ms in that same run: still no growth trend, so the fix holds up under realistic jitter, not just a quiet capture.

**A caveat that mattered and was checked before trusting this:** the code already carried a docstring (in `feed_audio_chunk`, predating this session) warning that dropping wallclock lets "ffmpeg's assumed 8kHz clock silently run slower than real device delivery, drifting further behind wall time for as long as the session ran" — i.e. this exact fix had apparently been tried before and rejected. Re-tested directly rather than trusting either claim: no drift trend appeared in either of the two ≥40s runs above. Working theory for the discrepancy: that earlier attempt likely predates `audio_pacer_loop`'s current strict-cadence design (added later, specifically to fix a different bursty-write bug) — sample counting only stays accurate if the content stream is complete and gap-free, and the pacer plus `-max_interleave_delta` are what make that hold. Not fully proven for sessions much longer than ~40s; worth a longer soak test before treating it as settled for arbitrarily long viewing sessions.

**A harness bug that invalidated some of this section's own earlier work, for the record:** the bridge capture script's diagnostic mux variants (`--swap-input-order`, `--no-audio-wallclock`, `--showinfo`) were wired to monkeypatch `view.start_mux` *after* `view.start_mux()` had already been called once — a copy-paste-order mistake, not a logic bug in the variants themselves. Every run of every diagnostic flag, including the "input order ruled out" conclusion below (hypothesis 5) and this session's first two attempts at the wallclock/showinfo tests, silently ran the unmodified pipeline regardless of which flag was passed. Caught via a debug `print()` of the constructed argv that never appeared in output that otherwise looked like a normal completed run. Fixed by moving the patch block before the `start_mux()` call. Hypothesis 5 (input order) was not re-tested after the fix — it's moot now that the actual cause is known and unrelated to input order, but flagging that its "ruled out" status was never actually earned should the fix above ever need revisiting.

Measured via `ffprobe` packet-level `pts_time` on captures from `urmet_bridge_ts_capture.py`: video's first packet consistently around pts 1.4s, audio's first packet consistently several seconds later (varies 6-9s across runs), then both track in sync to the end of the capture.

**Four hypotheses tested, all ruled out** (each tested by direct instrumentation or a direct behavioral change, not inference):

1. **Device sends audio late.** Ruled out. Added raw arrival-time instrumentation (timestamping the exact moment a stream_type `0x01`/`0x02` video payload or `0x08` audio payload is first classified in `LiveView._process_payload`, relative to just before `start_video` is sent) — audio consistently arrives at the socket *first*, around 1.5-1.7s in; video arrives around 4.5-5.0s in. The device sends audio before video, the opposite of what the final file shows.
2. **The Python audio pacer/queue delays relaying it.** Ruled out. Instrumented `LiveView._write_audio_frame` to timestamp the first real (non-silence) write to ffmpeg's audio input socket: happens ~32ms after receipt — essentially the very next 40ms pacer tick. The pacer is doing exactly its documented job (see §07/§02 on why it exists — preventing bursty-delivery timestamp jumps, a real, previously-measured static/drift bug, not a guess) and is not the bottleneck for this symptom.
3. **`SUBCMD_AUDIO_START` (`0x66 00`) was never actually sent.** True, but not the cause. Grepped `urmet_client.py`/`urmet_live_view_ts.py`: only `SUBCMD_AUDIO_STOP` (`0x67 00`) was ever sent (teardown); the START counterpart is defined but was dead code. Added an explicit send with response-queue confirmation (device acks `{"result":"ok","channel":"0"}`) — gap unchanged.
4. **A backlog forms before `receive_loop` starts draining the socket, and gets silently evicted by the bounded (50-frame/2s) `audio_queue`'s drop-oldest behavior once draining begins in a burst.** Ruled out. Reordered the driver so `receive_loop`/`keepalive_loop`/the pacer are all running *before* `start_video`/`start_audio` are ever sent, closing the pre-consumption window entirely — gap persisted (if anything, slightly larger). Note: this reorder required a real fix that stays regardless of the experiment's outcome — `start_video`/`start_audio` confirmation now reads `view.response_queue` (via a new `wait_via_response_queue` helper in the capture script) instead of calling `client.wait_for_result()`, which does its own raw `client.recv()` and would otherwise race `receive_loop` for the same socket once both are active — exactly the two-reader bug `receive_loop`'s own docstring warns about.
5. **ffmpeg's input order (audio-first vs video-first on the command line).** *"Ruled out" at the time, but this conclusion turned out to rest on a harness bug — see below — and was never re-tested after the fix. Moot now that the real cause (below) is confirmed and has nothing to do with input order, but flagging it so this item isn't mistaken for a solidly-earned result.* Added a `--swap-input-order` diagnostic to the capture script (a monkeypatched copy of `LiveView.start_mux` with video listed before audio). Swapping did not appear to swap which stream appears early/late in the output.

**Continuity check (a separate question, answered — no issue found):** added per-second raw reception counting alongside the above. Confirmed steady, continuous delivery for the whole session once each stream starts (~23-28 audio packets/s, ~4-8 video packets/s over a 30+s capture) — no periodic stalls or gaps later in a session. The device keeps sending reliably throughout; this is purely a startup-alignment problem.

**(Historical — the resample/encode path was the leading suspect at this point in the investigation, before the actual cause above was found.)** It turned out not to be responsible: `--no-audio-wallclock` (dropping wallclock, leaving resample/encode untouched) fully resolved the symptom on its own, so the asymmetry between `-c:v copy` and `-c:a aac`+`aresample` was a red herring, not the mechanism.

**A dead end worth flagging so it isn't retried blind:** attempted to settle this by reading ffmpeg's own `-loglevel debug` output, specifically the mpegts muxer's `sq: send/receive/finish N ts X` lines (its internal interleave/sync-queue bookkeeping). Misread them — sorted by the wrong field (`N`, a generation/queue-slot index that is only ever 0 or 1, not a timestamp) and briefly drew a wrong conclusion from it before catching the mistake. Corrected before it became a stated finding, but the debug-log approach itself was also just slow and ambiguous to interpret confidently — abandoned in favor of direct behavioral tests. What worked instead: ffmpeg's `ashowinfo` filter (audio; `showinfo` is video-only in this build) spliced into `-af`, giving real per-frame pts/checksum directly instead of inferring from the muxer's sync-queue chatter — this is what actually found the root cause above.

**Status: fixed, ported into both `tmp/urmet_live_view_ts.py` and `custom_components/urmetview/media.py`.** Validated end-to-end via the bridge against the real device (40s run, zero warnings, bounded offset, no drift; separately re-confirmed clean over a 42s run with 171 real device delivery gaps over 100ms). Remaining open items:
1. A soak test meaningfully longer than ~40s, to be more confident the no-drift result holds for long viewing sessions, not just short ones.
2. A live human-perception check (the user speaking near the device, as in the original bug report) — the pts-level fix is confirmed, but hasn't yet been confirmed to *sound* right end-to-end.
3. Both of the above only ever ran against the test rig (`urmet_live_view_ts.py`) — now that the fix is also in `media.py`, they're worth re-running against the real integration specifically, not assumed to transfer automatically.

## 14 — The device embeds real per-frame clocks in its media headers, mostly undocumented, audio-only for the absolute part

The protocol spec (§5a) already flagged the 27 "unmapped" bytes after the `a5` frame marker as "likely timestamp/frame-size/keyframe-flag/frame-index" but nobody had actually decoded them. Did that here, triggered by a question about why the §13 fix works given the sub-header was mostly a black box.

**Two independent relative-time fields, present in both video's and audio's sub-header, at the same byte offsets:**
- A 4-byte per-frame index counter (increments by 1 per frame, wraps mod 256 as a raw byte) — an ordinal, not a clock.
- A 2-byte little-endian sub-second field (offset 15-16 within the 27-byte unmapped region), incrementing in step with real content: **exactly +40 per audio frame** (40ms = one mu-law frame at 8kHz — confirmed by direct decode, not inference), wraps roughly every 1000 of its own units. For video (irregular frame timing, unlike audio's fixed cadence), its deltas track real inter-frame gaps closely once you account for normal measurement jitter from the bridge relay (e.g. `dt_ms=143.0` measured against `d_field=140`).

**A coarser, wider-range companion, found by asking "does an adjacent byte carry the wrap":** one byte further in (offset 12) is a tick counter, calibrated over two independent long samples (131s and 324s of real device data, agreeing to within 0.03%) at **256.567 ms/tick** — notably *not* the initially-estimated 250ms, which came from a too-short 12.8s sample and was just noise. 256ms = 2⁸ms, a plausible bit-shift-derived tick for embedded firmware, more so than a decimal 250ms. This byte alone wraps mod 256 every **~65.7s**; the very next byte (offset 13) was confirmed to be its carry — increments by exactly +1 at every one of 4 observed wrap events, giving a combined 16-bit counter (`byte12 + byte13*256`) that stays clean (1856/1857 monotonic steps in the 324s sample) and only wraps every **~4.67 hours** — comfortably past any realistic viewing session.

**Checked and ruled out as NOT hiding a real timestamp:** an exhaustive scan (every offset, widths 1-8 bytes, both endians, as integers *and* 32/64-bit floats, on both keyframes specifically — 4 samples originally, 31 after a dedicated capture with the user moving in front of the camera to trigger more of them — and P-frames) found nothing beyond the two fields above for video. Several offsets briefly looked like they might decode to a plausible-looking epoch year purely by chance; none of them actually tracked real elapsed time when checked (values bounced randomly, no relationship to real `Δt`) — a reminder that an epoch-range hit alone isn't evidence, only correlated growth is.

**Audio only: a genuine absolute Unix timestamp**, 4-byte little-endian at offset 11, confirmed two ways: (1) decodes to today's real date/time, and (2) increments by exactly 1 per real elapsed second, verified across two independent sessions (107.8s and 54.1s) with a real ~5m37s gap between them matching the actual gap between test runs. Video's same byte offset is *not* this — it decodes to nonsense (e.g. year 2079) and turns out to be part of the same per-frame index counter, just read at a wider byte width; a coincidence of interpretation, not a second clock.

**One wrinkle, resolved:** the decoded audio Unix time reads almost exactly **+1 hour ahead of true UTC** (checked precisely: +3580s, i.e. +0.994h). Not a bug in the decode — Lisbon (the device's locale, per its own HA config: `country: PT`, `time_zone: Europe/Lisbon`) is currently on WEST (UTC+1) DST, and this looks like the device firmware computing "seconds since epoch" from its own local-time clock without subtracting the zone offset — a common embedded-firmware mistake. Confirmed this doesn't matter for anything built here: only relative timing was ever needed, never true UTC alignment.

**A parsing trap worth recording so it isn't hit again:** an early pass at this analysis concatenated per-frame logs from two separate script invocations and sorted them by each run's own independently-zeroed elapsed-time column, which interleaved two different real moments and produced what looked like the Unix timestamp wildly oscillating by ~7 minutes between consecutive samples. It wasn't corruption — splitting by actual script invocation (detect a large backward jump in the run-relative time column) before analyzing made it clean again. Any future analysis combining multiple capture-script runs needs to do this split first.

**Not used in the eventual fix.** §13's fix (drop wallclock on the audio input, keep it on video) was already confirmed sufficient on its own — these device-side fields are valuable reverse-engineering knowledge and a more principled foundation than any wallclock-based scheme, but there was no concrete evidence the current (post-§13) video path is inadequate, so building a device-clock-driven pacer for video was not pursued this session for lack of a demonstrated problem to justify it. Worth revisiting if video-side sync issues ever surface again — the pieces needed (a robust monotonic ms clock per stream, wrap-safe over any realistic session) are now understood and documented above.

## 15 — The device (and the app) stop sending media after a while; the integration now recovers

**Confirmed real, not a bridge artifact:** the user's real-world experience (app shows black picture, no sound, after some time into a session) reproduces in the Urmet phone app itself, independent of anything built here. Chasing whether *this sandbox's* stream cutoffs were device behavior or bridge instability turned out to be the wrong question — bridge instability is real and separately confirmed (see §12; standalone discovery calls failed intermittently with zero session state involved), but the underlying "stream eventually goes quiet" behavior is accepted as a real device/app characteristic to design around, not a bug to keep root-causing.

**Design: reuse the proven reconnect path, cheap option first.** `session.last_media_at` already existed (updated on every real frame) but nothing read it — the only staleness signal checked anywhere was `session.connected`, a transport/socket-level flag that stays `True` even when the device has gone silent (acks succeed, no exception, just zero data — confirmed directly against the device). Added:
- `UrmetCoordinator._stream_is_healthy()` — `session.connected` AND a real frame within `STREAM_STALE_TIMEOUT` (10s).
- `_async_start_video_locked()` no longer just returns when `_video_running` is already `True` — it now checks health first, and recovers if stale, rather than handing back the same dead pipeline to a consumer asking for the stream again.
- `_async_recover_stale_stream()`: tries the cheap path first — re-send `start_video`/`start_audio` on the *same*, still-connected session, no re-login. **Confirmed working against the real device**: in a live test, re-sending these on a session that had gone quiet for 20s+ resumed real media within the next 5s status interval, no reconnect needed. Only escalates to a full reconnect (`_async_disconnect` + `_async_connect`, the same path `_async_keepalive` already used for a session that dropped outright) if the cheap path doesn't restore `last_media_at` progress within `CHEAP_RESUME_GRACE` (8s).
- The same health check was added to the existing idle-monitor poll loop (already running every 2s while a stream is up), so a stall gets caught proactively while someone's actively watching, not only at the moment of the next explicit request.

**Evidence for the cheap path, honestly qualified:** one clean confirmation under natural conditions (no experimenter interference), plus a second instance that overlaps with an accidental attempt to run a second concurrent session against the device (confirmed the device only serves one session at a time — the second session's own discovery/login never completed, *and* it visibly knocked out the first session's audio for a while) — so that second data point is suggestive rather than clean. Judged sufficient to build against given it's the same request/response pair already known to work (`start_video`/`start_audio` are the normal way to begin streaming, just being re-sent), not a novel mechanism.

**Gap: no automated test coverage.** `coordinator.py` is the one module in the integration with real logic and no `test_*.py` — `media.py`'s and `session.py`'s tests both explicitly avoid needing `HomeAssistant` core objects; `coordinator.py`'s constructor takes one directly and several methods touch `callback`/dispatcher signals, so a test would need that scaffolding built out, which wasn't done this session. The logic was validated by direct behavioral testing against the real device instead. (This same gap is why §16 below also has no automated test yet.)

## 16 — Found from a real deploy: the device stream never went idle

Reported directly from testing the §14/§15 fixes on the real Home Assistant instance: video looked continuously active, when the goal is on-demand only (start when a user opens the live view, stop shortly after the last viewer leaves).

**Root cause:** `camera.py`'s `async_camera_image()` — used for dashboard thumbnails/snapshots, not live viewing — called the exact same `coordinator.async_ensure_stream()` as `stream_source()` (the genuine "user opened the live view" entry point), and that method unconditionally called `_mark_activity()` on every single call. Home Assistant polls camera thumbnails periodically for dashboard cards; each poll reset the idle-teardown clock. With `DEFAULT_STREAM_IDLE_TIMEOUT = 30`, any thumbnail refresh faster than 30s apart — a common default — meant the 30s window could never actually elapse, so `_async_idle_monitor` never got the chance to release the device stream.

Notable: `MediaPipeline.client_count`'s own docstring already says the intent was for a snapshot grab to show up as a real TCP consumer and be judged on that alone ("the honest measure of 'is anyone watching'"), but the separate `_last_activity`/`_mark_activity` grace-period mechanism — needed to bridge the real gap between handing out a URL and a consumer actually connecting — was applied indiscriminately to *every* call, undermining that design for the specific case of frequent, low-intent snapshot polling.

**First fix (necessary, turned out not sufficient):** `async_ensure_stream()` gained a `mark_activity: bool = True` parameter; the snapshot path passed `mark_activity=False` so repeated polling of an *already-running* stream stopped resetting the idle clock. This alone did not fix the symptom. Confirmed against the real instance's state history: with only this fix applied, the video-channel sensor cycled `idle -> streaming (~40-60s) -> idle` repeatedly, on its own, every few minutes, with no live view ever opened - each dashboard poll was still able to *start* a fresh short burst, even though the previous one had correctly gone idle in between. `mark_activity=False` only ever stopped a poll from *extending* a running stream; it never stopped a poll from *starting* one, and that gap was still enough to keep the device stream cycling indefinitely just from a dashboard sitting open.

**Second, stricter fix, matching the user's actual intent ("I don't really want snapshots if they can be triggered at any point, e.g. just by having a dashboard open"):** `async_camera_image()` now checks `coordinator.streaming` *first* and returns `None` (no picture) if a live view is not already genuinely open, rather than ever calling `async_ensure_stream()` to start one. A snapshot can still be served once someone has actually opened the live view (grabbed from the already-running pipeline, still with `mark_activity=False` so it doesn't extend that session's own idle clock beyond what the real viewer's presence already does) - but a snapshot request alone, from an idle state, no longer wakes the device up at all. `stream_source()` (genuine live view) is unaffected either way.

**A separate, real, not-yet-investigated observation from the same live test:** closing the video window once did not return the stream to idle immediately - the video-channel sensor stayed on `streaming` for up to ~103s after close in one observed case, longer than the 30s idle timeout would suggest on its own. Working theory, not yet confirmed: go2rtc may hold its own connection to the local ffmpeg relay open for a while after its downstream WebRTC/HLS viewer disconnects (a common "fast reconnect" caching design), which would mean `MediaPipeline.client_count` - the signal `_async_idle_monitor` trusts as "the honest measure of is anyone watching" - doesn't actually drop to 0 until go2rtc's *own* timeout elapses, and only then does our own 30s clock even start. If true, the fix is not on our side at all (go2rtc's own stream-caching config, if it's exposed) - not yet checked.

**Status:** both fixes applied; awaiting live re-test. No automated test yet for either, same gap as §15.

## 17 — A real bug in the relay fan-out: a joining consumer got its first GOP sent twice

Found from the same live test as §16, once video was actually stable enough to reach a real go2rtc consumer. This is stage 4 in §03's pipeline table (relay fan-out → consumers) — previously marked "synthetic tests only," meaning it had never actually been exercised end to end against a real streaming consumer before this session. It hadn't been reached before because the mux-level bugs fixed earlier this session (§13, §16) were themselves blocking a clean enough stream from ever getting this far.

**Symptom, from debug logs with a real iPhone HA app attempt (timestamps within ~12s of each other):**
```
HA stream component:  Error from stream worker: Stream ended; no additional packets
go2rtc:                error=EOF url=tcp://127.0.0.1:<relay port>
                        error=EOF url=ffmpeg:...
libav.mpegts:          Packet corrupt (stream = 0, dts = ...)  [x2]
libav.h264:            corrupted macroblock ...
                        non-existing PPS 0 referenced
                        no frame!
```
`non-existing PPS 0 referenced` is exactly the symptom `media.py`'s own docstring already names for a consumer starting mid-GOP - referencing a keyframe it never saw.

**Root cause:** `_async_pump_output` called `_release_pending(packets)` (which admits any waiting consumer at the first PAT found in this batch, writing them `packets[offset:]` - the PAT onward) and then *unconditionally* called `_broadcast(packets)` on the *same, unsliced* batch right after - which now also includes the just-admitted consumer, since `_release_pending` had already added them to `_out_clients`. A joining consumer therefore received the PAT-onward slice once, immediately followed by the *entire original batch again* (including whatever came before the PAT, left over from the previous PAT/GOP cycle) - a doubled, partially-stale first GOP. Confirmed the existing test for this (`test_admitted_consumer_is_not_sent_the_batch_twice`) had an assertion that mathematically required the duplicate (`client.written == first + batch`, where `first` already equaled `batch`), directly contradicting its own docstring ("_broadcast must not repeat it") - a genuinely misleading passing test that had been asserting the bug as correct behavior.

**Fix:** `_release_pending` now returns the set of consumers it just admitted; `_broadcast` takes a `skip` parameter and `_async_pump_output` passes the just-admitted set so they're excluded from that same batch's broadcast - they already got exactly what they need (PAT onward), nothing more, nothing twice. The test was rewritten to actually assert non-duplication, matching its own docstring's original intent.

**Status:** fixed, not yet re-validated live. No prior "known good" baseline exists for this stage to compare against - this is the first time it's ever been correctly exercised.

## 18 — Video has the same wallclock-collision bug as audio (§13) — diagnosed, then fixed (see §20 for the real fix)

Found from a real debug-logging session (see "How to actually get debug output" below) after §17's fix was deployed: PC browser worked (with some instability), iPhone app showed either a frozen frame that "flashes" occasionally, or nothing at all. User also reported the browser's timeline slider visibly trying to "catch up" to live.

**Root cause, same signature as §13's audio bug, now confirmed for video too:**
```
[vost#0:0/copy @ ...] Non-monotonic DTS; previous: 10813612, current: 10813612; changing to 10813613
```
Dozens of these throughout one session, `previous` and `current` always *exactly* equal, not just close. This is `-use_wallclock_as_timestamps` on the video input colliding under bursty socket reads - the exact mechanism §13 root-caused for audio (ffmpeg samples wallclock once per read(), not once per frame). Video still carries this flag because, unlike audio, raw H.264 has no fixed sample rate to derive pts from instead - dropping it outright (audio's fix) is not an option for video as-is.

Also present throughout, correlated in time with the DTS collisions: repeated `[h264 @ ...] Failed to parse header of NALU (type 0): "Invalid data found when processing input". Skipping NALU.` (both from the raw demuxer and the `h264_metadata` aspect-ratio bitstream filter), and `[urmetview.camera] Snapshot failed: non-existing PPS 0 referenced`. Not yet proven which way the causation runs between the DTS collisions and the NALU corruption, but they track together closely enough to treat as one root cause for now.

**Why this plausibly explains every video symptom seen this session:** when ffmpeg force-corrects a run of collided DTS values by bumping +1 tick each time, a whole burst of frames ends up claiming almost the same presentation instant. A player shows one frame, then jumps through the rest nearly instantly once a real gap finally arrives - matching the iPhone's "fixed frame, then a flash," and the browser's timeline slider "catching up" from the same underlying discontinuity.

**The originally-planned fix (§14's embedded clock) turned out not to work for video** - see §20 for why, and for what was actually built and shipped instead (explicit pts from a different source per stream: local receive-time for video, the device's own clock for audio - both confirmed reliable, unlike video's own embedded tick field).

**Status: fixed, ported into both `tmp/urmet_live_view_ts.py` and `custom_components/urmetview/media.py`. Not yet re-validated live against the real HA instance** - see §20's open items.

## 19 — How to actually get debug output from the real instance

Learned the hard way this session, worth recording so it isn't re-derived:

- `logger.set_level` (called via the REST/WS service API) does make DEBUG records get created and written to the real log file - but **`system_log`'s in-memory list (queried via the `system_log/list` WebSocket command, and what backs the on-screen Settings → System → Logs page) has its own independent WARNING+ threshold that does not lower just because a logger's level was raised.** Confirmed by enabling debug on `custom_components.urmetview` twice, across two separate real test attempts, and getting zero matching entries either time via that API - the debug records were being written somewhere, just not into that curated view.
- **What actually works:** Settings → Devices & Services → the integration's card → ⋮ menu → **Enable debug logging**, reproduce the issue, then ⋮ → **Disable debug logging** - turning it *off* is what triggers a full `.log` file download containing everything captured while it was on, bypassing `system_log`'s curation entirely. This is the only method that surfaced `ffmpeg:`-prefixed lines (our own logger wrapping ffmpeg's stderr, all at DEBUG level by design) or go2rtc's own internal Python tracebacks.
- HA's WebSocket API (`ws://<host>:8123/api/websocket`) is reachable through this sandbox's proxy the same way the REST API is - the proxy transparently rewrites the `{"type": "auth", "access_token": ...}` message payload too, not just HTTP Authorization headers. Confirmed working; useful for anything REST doesn't expose (was needed here for `system_log/list`, even though that specific command turned out to have the curation limitation above).
- A downloaded debug-log file placed into the project root via the host's bind-mount can come through with a permission/ownership state that the sandbox's `agent` user (UID 1000) cannot read, even when it looks identical to every other file from the host's own `ls -l` (matching host-side ownership and mode bits). Cause not fully root-caused (not a simple UID mismatch - other files from the same mount and same-looking permissions work fine); workaround used was `grep`-ing the relevant lines out on the host side and pasting them directly instead of transferring the file.
- The user's `configuration.yaml` carries a permanent `logger: logs: custom_components.urmetview: debug` - this sets the Python logger's level at HA startup, so DEBUG records for this integration are written to the real `home-assistant.log` continuously, not just during a temporary "Enable/Disable debug logging" window. The on-screen Logs page / `system_log/list`'s own independent WARNING+ curation (above) still applies on top of this though - **Settings → System → Logs → "Download Full Log"** is what actually gets the real file, and with the YAML config in place there's no need to toggle debug logging on/off first; it's already always on.

## 20 — §18's real fix: explicit per-frame pts for both streams, not a pacer, not the embedded clock originally planned

The session that built this started from §18's plan (decode the device's own embedded clock, use it to pace video). That plan changed twice before landing on what actually shipped, both times because direct measurement against the real device contradicted the plan, not because of a nicer idea. Both redirects came from the user directly questioning the design ("do we need a pacer at all, if we set the correct pts?" and, after a first working-looking audio result, "audio is not correct, we haven't even tested it... AND we have the audio sample's UNIX timestamp!!!") rather than from further solo investigation - worth recording since the eventual fix is a materially different, better design than the one first proposed and approved.

**Attempt 1, abandoned: pace writes to ffmpeg using the device's embedded clock.** Built first (`DeviceClock` decoding the §14 coarse-tick/fine-ms fields, a `video_pacer_loop` spacing writes to ffmpeg's video socket by the real inter-frame gap). Live-tested against the real device via the bridge and it visibly reduced collisions, but this was always a weaker fix than it looked: it still left `-use_wallclock_as_timestamps` on for video and only tried to keep ffmpeg's own reads from batching, rather than removing the guesswork. Directly challenged by the user ("we're setting the actual timestamps, do we need [pacing]?") - correctly: no. Replaced.

**Attempt 2's real insight: set pts explicitly, in a container ffmpeg can read timing from.** Raw `-f h264` (Annex-B elementary stream) and raw `-f mulaw` both carry zero timing of their own - that's *why* ffmpeg had to guess via wallclock-at-read() in the first place, the actual root cause §13 identified for audio and this session confirmed for video too. The fix that actually holds: mux each stream ourselves, one small PyAV-built container per stream, with a real, explicit, already-correct pts on every packet - then ffmpeg's `-f mpegts`/`-f nut` demuxer reads real timestamps from the container instead of ever touching wallclock. This is a structural fix, not a mitigation: batching in ffmpeg's own socket reads becomes harmless, because it no longer determines timing at all.

**Verified along the way: PyAV's mpegts muxer has no stream_type mapping for raw `pcm_mulaw`** ("Unsupported codec", confirmed directly) - audio uses **NUT** instead, libavformat's own general-purpose container built for exactly this (arbitrary codecs, explicit timestamps, streams fine over a non-seekable socket). Video uses MPEG-TS (h264 has a real stream_type there).

**Where each stream's real per-frame pts comes from, and why they're different:**
- **Video: local receive-time (`time.monotonic()`), not the device's own embedded clock.** The original §14 plan was to decode video's embedded coarse-tick/fine-ms fields for this. Built and tested directly against real captured device data (`tmp/header_dump_long.log`, 783 real video frames) - and found unreliable for per-frame pts: the coarse tick does **not** advance smoothly per frame, it updates in bursts (flat for several consecutive frames, then several ticks at once to catch up). That burstiness broke two different combination schemes tried (closest-candidate-to-a-coarse-anchor, then delta-based wrap-counting using the tick's own advance) - both produced real, measured errors of tens-to-hundreds of ms, confirmed against the same data, not simulated. The mechanism that actually works is much simpler and was available the whole time: our own `receive_loop`/session reads the device over **UDP**, and `recvfrom()` never coalesces multiple datagrams into one call the way a TCP `read()` can - so sampling `time.monotonic()` once per real frame-start chunk, exactly where it's classified, is an accurate, un-batched, real per-frame timestamp, immune to the exact read-batching mechanism that caused the original bug. This is not the "wallclock" §13/§18 root-caused - that bug is specifically ffmpeg sampling its own clock once per **relay-socket** read, not once per frame; sampling once per real device datagram on our own side has no equivalent batching risk.
- **Audio: the device's own embedded clock.** Unlike video's tick field, this one is genuinely reliable: a real absolute Unix-seconds field (4 bytes, offset 11 of the 27-byte sub-header) plus a sub-second field (offset 15-16) confirmed to increment by **exactly +40 per real audio frame** - deterministic, not bursty. Validated directly against the real device through the actual reorder/dedup path (not `header_dump_headers.py`'s naive capture, which doesn't reorder and produced misleading apparent backward jumps from retransmission artifacts - caught before trusting it): **398 real, properly-ordered frames, zero monotonic violations, every delta either exactly 40ms or a clean multiple of it.** `protocol.py` now exposes this as `decode_audio_clock_ms(header)`.

**Two further real bugs found and fixed while validating this end-to-end against the device, neither visible from the packetization logic alone - both required tee'ing the container ffmpeg actually receives and inspecting it directly (`ffprobe`) to isolate from ffmpeg's own downstream behavior:**
1. **Video pts was internally correct but on the wrong scale.** `time.monotonic()` returns seconds since an arbitrary reference (usually host uptime), so on a host with real uptime that's tens of thousands of seconds - while audio's pts (sample-count-derived at the time) started near 0. That scale mismatch between streams, not the clock source itself, was confusing the final muxer's interleaving. Fixed by anchoring video's pts to its own first frame, so it starts near 0 like every other stream.
2. **Audio's explicit pts broke the AAC re-encode step, badly** - not a minor artifact like video's residual (below), but severe, repeated multi-hundred-ms backward DTS jumps in the final output, confirmed reproducible from a **static replay** of an independently-verified-monotonic capture (isolating it to the encode step, not live timing). Root cause, found by elimination (decode-only was clean; encode was not; resample wasn't the variable, since removing it changed nothing): the AAC encoder wants fixed-size (1024-sample) frames, and our real per-packet pts - not perfectly uniform, real device jitter - confused its internal frame accumulation without two things: (a) `pkt.duration` set explicitly per packet (was never being set at all - confirmed via ffmpeg's own `"frame size not set"` warning appearing immediately before the corruption), and (b) `-af aresample=async=1000` on the encoder chain, letting it gently stretch/compress (up to 1000 samples/s) to reconcile our real jitter against its fixed-frame requirement, instead of fighting over exact sample boundaries. Both fixed; confirmed clean afterward, full 35s live session, zero warnings.

**Final validated result, live against the real device via the bridge (35s session, both fixes combined):** audio completely clean - zero "Non-monotonic DTS"/"backward in time" warnings, down from a bug so severe it was worse than the original before the duration+aresample fixes. Video down to **~10 isolated, self-correcting single-tick pts-tie corrections** across the whole session (mostly clustered in the first few frames right at stream start, a handful more scattered through the rest) - down from "dozens throughout" in the original bug, and each one now a silent +1-tick nudge on an isolated frame rather than the systemic "whole burst compressed into one instant" pattern that produced the reported freeze/flash symptoms. **Not yet root-caused** - deferred at the user's explicit direction ("first port, then investigate") in favor of getting the fix into the real integration first. Tee'd inspection of the inner container at one point showed this cluster's *inputs* were themselves near-identical real receive-time samples, which is at least consistent with several already-buffered frames genuinely arriving within the same few ms during connection establishment - a real, rare condition, not a decode bug - but this hasn't been confirmed as the actual explanation yet.

**Ported into the real integration (`custom_components/urmetview/`), same architecture as the validated test rig:**
- **`media.py`** - substantially rewritten. Key structural point: PyAV's `container.mux()` is a blocking, synchronous library call, which cannot run on Home Assistant's event loop (where `feed_video`/`feed_audio` are called from the session's hot path and must not block). Video/audio input handling therefore moved off asyncio entirely - two dedicated background threads (`_video_accept_and_mux`/`_audio_accept_and_mux`, mirroring the test rig's `video_mux_loop`/`audio_mux_loop`), each owning a plain blocking `socket.socket` that ffmpeg connects to, fed via a thread-safe `queue.Queue` from the event-loop side. `feed_video`/`feed_audio` still only ever assemble a frame's chunks and enqueue - non-blocking, same obligation as before, now satisfied by a queue put instead of a direct asyncio-writer write. `async_stop()` now closes those sockets and joins both mux threads (bounded by `MUX_THREAD_JOIN_TIMEOUT`) before returning, specifically because `async_start()` recreates the queues for a fresh session and a still-alive thread from the previous one reading the old queue object would be a real race, not just a cosmetic one. The output relay (fan-out to go2rtc/HA, `_async_pump_output`/`_release_pending`/`_broadcast`) is completely unchanged - still asyncio, still reading ffmpeg's stdout.
- The old audio pacer (`audio_pacer_loop`, a strict 40ms write cadence) and the silence-filler (`_async_silence_filler`, injecting synthesized mu-law silence during gaps) are both gone. Neither is needed any more: correctness no longer depends on write timing at all (explicit pts), and `-max_interleave_delta` was already independently confirmed (§07) to fix the "quiet audio stalls video in the muxer" problem on its own, with no silence padding involved - so removing the filler doesn't reopen that issue.
- **`urmet/protocol.py`** - `MediaFrameStart` gained a `header` field (the 27-byte per-frame sub-header, previously discarded after extracting `stream_type`); added `decode_audio_clock_ms(header)`.
- **`urmet/session.py`** - `AudioCallback`'s signature changed from `(data)` to `(data, header)` (`header` is `None` on a continuation chunk, matching `VideoCallback`'s existing `is_frame_start` pattern).
- **`coordinator.py`** - `_on_audio` updated to pass `header` through.
- **`manifest.json`** - added `"av"` (PyAV) to `requirements`. Not a new dependency in practice - PyAV is already a hard dependency of Home Assistant core's own `stream` component - but declaring it explicitly is correct regardless of what's already resident.
- **Tests** - `tests/test_media.py`'s `test_audio_input_has_no_wallclock_and_comes_first` rewritten as `test_neither_input_uses_wallclock_and_audio_comes_first`, since neither stream carries `-use_wallclock_as_timestamps` any more; everything else in the suite required no changes and passes unmodified. `.github/workflows/validate.yml`'s test step now installs `av` too. All tests currently run and pass **standalone** (`uv run --with av python3 tests/test_X.py`, each file's own `_run_standalone()`) rather than via plain `pytest tests/` in this sandbox - not a regression, pytest's rootdir-detection walk hits an unrelated pre-existing unreadable stray log file at the repo root (the same bind-mount permission quirk from §19) before it ever reaches the test files; a clean CI container won't have that file.

**Status: implemented and validated against the real device via the sandbox bridge (test rig + live-captured data), and ported into the real integration. Not yet re-tested live against the actual deployed HA instance** - that's the natural next step, the same way every earlier fix this project has shipped was confirmed (§13, §16, §17): watch on the iPhone/browser specifically for whether the "fixed frame + flash" and "timeline slider catching up" symptoms are gone.

**Open items:**
1. Live re-test against the real HA instance/device (not yet done - everything above is sandbox-bridge-validated only).
2. The residual ~10-per-35s isolated video pts-tie corrections, above - not yet root-caused, explicitly deferred.
3. `coordinator.py` still has no automated test coverage (flagged repeatedly since §15/§16 - still true, still not addressed).

---

## 21 — The AAC encode step clips: pre-existing, unmasked by §20's live test, fixed with headroom

Found immediately after §20 shipped, from the user's first real listening test of the new pipeline: "lots of noise which subsides when someone is talking, but the voice is really low, unintelligible." The user separately confirmed the official UrmetView app gets clean sound from the same device at the same time - good independent evidence the raw stream itself is fine and the defect is downstream, in this integration's own processing.

**Root-caused by signal analysis** (`ffmpeg -af astats` on real captures, not listening - this session has no way to actually hear audio) rather than guessing from the symptom description:

| Stage | Peak level | RMS level |
|---|---|---|
| Raw mu-law decode | -0.17 dB (clean) | -14.7 dB |
| After AAC encode+decode, **no** `aresample` (identical settings to before §20) | **+2.74 dB - clipping** | -17.2 dB |
| After AAC encode+decode, **with** §20's `aresample=async=1000` | **+2.75 dB - clipping** | -16.9 dB |

**Confirmed pre-existing, not caused by §20's fix**: the clipping is identical with or without `aresample=async=1000` - it happens at the exact `-c:a aac -b:a 64k -ar 16000` settings this project has used since before this session, just never checked by signal analysis (field notes §13 explicitly flagged the live human-perception check as never done, and it stayed undone through §20 too). The raw mu-law signal sits right at 0dBFS with zero headroom (-0.17dB peak), so ffmpeg's own AAC encoder - not a high-end one - has nowhere to absorb its own quantization/reconstruction overshoot, and punches through the ceiling. Clipped, distorted speech is a much better fit for "noise... unintelligible" than an actual gain/level problem - RMS around -15 to -17dB is an ordinary level for voice content, not unusually quiet on its own.

**Fix:** `-af "aresample=async=1000,volume=-4dB"` - a small linear attenuation before the encoder, giving its overshoot room to land under 0dBFS instead of past it. Confirmed sufficient with margin on the same real capture (peak back to -0.4dB clean).

**Validated live against the real device**, including genuine human speech (the user talked into the outdoor station during a bridge capture specifically for this): 32-second capture, checked in 1-second windows end to end, zero windows exceeding 0dBFS (worst case -0.23dB) - versus the unfixed pipeline's measured +2.7dB on the same kind of content. This is a much stronger validation than the earlier synthetic-content checks: real speech, real device, real capture, checked across the whole session rather than in aggregate.

**Honestly qualified:** this session has no way to actually listen to audio, so "clipping is gone" is confirmed directly by measurement, but "sounds correct/intelligible now" is not - that still needs the user's own ears on the real deployment. If it's still not right, the next things to check would be the encoder's own bitrate/quality settings (64k is fairly low for 16kHz-source AAC) or whether the RMS level itself (now a few dB quieter, from the `-4dB` headroom) needs recovering with a safe limiter afterward rather than fixed attenuation beforehand.

**Status: fixed and live-validated by signal measurement. Awaiting the user's own listening confirmation on the real deployment.**

---
