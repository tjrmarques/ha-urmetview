#!/usr/bin/env python3
"""Find the device's current session port, and work out which method works.

The session port changes every session and is not in the LAN broadcast, so it
has to be rediscovered. This tries each strategy independently and reports the
result of every one - the point is not just to get a port, it is to learn which
method we can rely on so the integration can stop depending on Urmet's cloud.

    # try everything except the sweep
    python3 tools/urmet_probe.py --uid URMABB-700171-SMCYN

    # include the brute-force sweep (needs --host, takes 30-90s)
    python3 tools/urmet_probe.py --host 10.0.50.6 --sweep

    # just listen for the device's own announcement
    python3 tools/urmet_probe.py --broadcast

The interesting result is whether LAN search works. If it does, the integration
never needs to talk to Urmet's servers at all.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from _common import DEFAULT_UID, discovery, setup_logging


def _describe(asked: int, answered: int | None) -> str:
    """Say not just whether a port answered, but which port did.

    The distinction matters: the device sometimes answers from a port other
    than the one probed, and that answering port is the one a session must go
    to. Reporting only "OK" hides the mismatch that breaks the login later.
    """
    if answered is None:
        return "no reply"
    if answered == asked:
        return "session OK"
    return f"session OK, but answered from port {answered} - USE THAT ONE"


async def _run(args: argparse.Namespace) -> int:
    if args.broadcast:
        print(f"Listening for the LAN announcement on UDP 6688 (up to {args.timeout:.0f}s)...")
        print("This is autonomous and infrequent - nothing we send triggers it.")
        device = await discovery.async_listen_broadcast(timeout=args.timeout)
        if device is None:
            print("No announcement seen. The device may broadcast rarely; try a longer --timeout.")
            return 1
        print(f"\n  UID:          {device.uid}")
        print(f"  IP:           {device.host}")
        print(f"  MAC:          {device.mac}")
        print(f"  firmware:     {device.firmware}")
        print(f"  verify code:  {device.verification_code}")
        print(f"  password:     {device.password}   <- broadcast in cleartext to the whole LAN")
        print("\nNote the broadcast carries no session port; that still needs one of the methods below.")
        return 0

    results: list[tuple[str, str]] = []

    # 1. cached / supplied port
    if args.host and args.port:
        answered = await discovery.async_probe_port(args.host, args.port, args.uid)
        results.append(("supplied port", f"{args.host}:{args.port} -> {_describe(args.port, answered)}"))

    # 2. LAN search - the one we actually want to work
    print("Trying PPPP LAN search (broadcast to 32108)...")
    lan = await discovery.async_lan_search(timeout=args.timeout)
    if lan:
        for candidate in lan:
            answered = await discovery.async_probe_port(candidate.host, candidate.port, args.uid)
            results.append(("LAN SEARCH", f"{candidate} -> {_describe(candidate.port, answered)}"))
    else:
        results.append(("LAN search", "no reply - device likely does not answer LAN search"))

    # 3. cloud
    if not args.no_cloud:
        print("Trying cloud rendezvous...")
        for candidate in await discovery.async_cloud_lookup(args.uid, timeout=args.timeout):
            answered = await discovery.async_probe_port(candidate.host, candidate.port, args.uid)
            results.append(("cloud", f"{candidate} -> {_describe(candidate.port, answered)}"))
        if not any(r[0] == "cloud" for r in results):
            results.append(("cloud", "no candidates returned"))

    # 4. sweep
    if args.sweep:
        if not args.host:
            print("--sweep needs --host", file=sys.stderr)
            return 2
        print(f"Sweeping ports {args.sweep_start}-{args.sweep_end} at ~{args.rate}/s...")
        found = await discovery.async_port_sweep(
            args.host, args.uid, start=args.sweep_start, end=args.sweep_end, rate=args.rate
        )
        if found:
            for candidate in found:
                results.append(("PORT SWEEP", str(candidate)))
        else:
            results.append(("port sweep", "no port answered"))

    print("\n" + "=" * 68)
    print("RESULTS")
    print("=" * 68)
    for method, outcome in results:
        print(f"  {method:<14} {outcome}")
    print()
    if any("answered from port" in o for _, o in results):
        print("NOTE: the device answered from a different port than it was asked on.")
        print("      The integration follows the answering port; earlier builds did not,")
        print("      which is what caused 'Connection refused' right after discovery.\n")
    if any(m == "LAN SEARCH" and "session OK" in o for m, o in results):
        print("LAN search works -> the integration can be fully cloud-free.")
    elif any(m == "PORT SWEEP" for m, o in results):
        print("Sweep works -> cloud-free, at the cost of a 30-90s scan per reconnect.")
    else:
        print("Falling back to cloud lookup. Re-run with --sweep to test the local alternative.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", help="device LAN IP")
    parser.add_argument("--port", type=int, help="a known/cached port to verify")
    parser.add_argument("--uid", default=DEFAULT_UID)
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--broadcast", action="store_true", help="listen for the UDP 6688 announcement instead")
    parser.add_argument("--no-cloud", action="store_true", help="skip the cloud lookup")
    parser.add_argument("--sweep", action="store_true", help="brute-force the port range (needs --host)")
    parser.add_argument("--sweep-start", type=int, default=1024)
    parser.add_argument("--sweep-end", type=int, default=65535)
    parser.add_argument("--rate", type=int, default=3000, help="sweep packets per second")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()
    setup_logging(args.verbose)
    if args.broadcast and args.timeout < 30:
        args.timeout = 35.0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
