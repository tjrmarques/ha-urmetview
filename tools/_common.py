"""Shared plumbing for the tools/ scripts.

Puts the protocol core on sys.path so these run straight from a checkout with
no install step and no third-party packages:

    uv run tools/urmet_probe.py --help
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "urmetview"))

from urmet import discovery, protocol  # noqa: E402
from urmet.const import DEFAULT_UID, DEFAULT_USERNAME  # noqa: E402
from urmet.session import UrmetSession  # noqa: E402

__all__ = [
    "DEFAULT_UID",
    "DEFAULT_USERNAME",
    "UrmetSession",
    "add_common_args",
    "async_resolve",
    "discovery",
    "protocol",
    "setup_logging",
]


def add_common_args(
    parser: argparse.ArgumentParser, auth_required: bool = True
) -> None:
    parser.add_argument("--host", help="device LAN IP (skips discovery)")
    parser.add_argument(
        "--port", type=int, help="device session port (skips port discovery)"
    )
    parser.add_argument("--uid", default=DEFAULT_UID, help="device UID")
    parser.add_argument(
        "--auth",
        required=auth_required,
        help="the 32-hex-char auth hash captured from a real app login",
    )
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="-v info, -vv debug"
    )


def setup_logging(verbose: int) -> None:
    level = [logging.WARNING, logging.INFO, logging.DEBUG][min(verbose, 2)]
    logging.basicConfig(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )


async def async_resolve(args) -> tuple[str, int]:
    """Work out the device's current address, however we can."""
    if args.host and args.port:
        return args.host, args.port

    print("Discovering device (no --host/--port given)...")
    candidate = await discovery.async_find_device(
        args.uid, host=args.host, allow_cloud=True, allow_sweep=bool(args.host)
    )
    if candidate is None:
        print(
            "Could not find the device.\n"
            "  - pass --host and --port from a packet capture, or\n"
            "  - run tools/urmet_probe.py --host <ip> --sweep to find the port",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"Found device: {candidate}")
    return candidate.host, candidate.port
