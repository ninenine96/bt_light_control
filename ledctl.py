#!/usr/bin/env python3
"""Control an LEDDMX-* BLE light strip (LED LAMP app family).

Reverse-engineered 9-byte frames, written to GATT char 0xFFE1 (service 0xFFE0):

    Static colour  7B FF 07 C1 C2 C3 00 FF BF     (C1..C3 = RGB channel order)
    Power on       7B FF 04 03 FF FF FF FF BF
    Power off      7B FF 04 02 FF FF FF FF BF
    Brightness     7B FF 01 b1 pct 00 FF FF BF    (pct 0-100, b1 = pct*32/100)
    Pattern        7B FF 03 idx FF FF FF FF BF    (idx 0-210)

Usage:
    ./ledctl.py on
    ./ledctl.py off
    ./ledctl.py color red
    ./ledctl.py color 255 0 255
    ./ledctl.py brightness 50
    ./ledctl.py pattern 17
    ./ledctl.py scan
"""

import argparse
import asyncio
import colorsys
import signal
import sys

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError

from ledctl_lib import (
    CHAR_UUID,
    FRAME_ON,
    FRAME_OFF,
    NAMED_COLORS,
    NAME_PREFIXES,
    brightness_frame,
    color_frame,
    pattern_frame,
    scan_for_strip,
)


async def send_one(payload: bytes, address: str | None) -> None:
    addr = address or await scan_for_strip()
    if not addr:
        print("No LEDDMX strip found in range. Make sure no other app is connected.")
        sys.exit(1)
    print(f"Connecting to {addr} ...")
    async with BleakClient(addr, timeout=20) as client:
        print(f"Writing {payload.hex(' ')}")
        await client.write_gatt_char(CHAR_UUID, payload, response=False)
        await asyncio.sleep(0.6)
    print("Done.")


async def rainbow(address: str | None, duration_min: float, brightness: float,
                  step: float) -> None:
    addr = address or await scan_for_strip()
    if not addr:
        print("No LEDDMX strip found in range.")
        sys.exit(1)
    loop = asyncio.get_event_loop()
    stopped = asyncio.Event()
    t0 = loop.time()
    try:
        async with BleakClient(addr, timeout=20) as client:
            def _stop(sig, frame):
                stopped.set()
            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)
            print(f"Starting rainbow cycle ({duration_min} min) on {addr}. Ctrl+C or SIGTERM to stop.")
            await client.write_gatt_char(CHAR_UUID, FRAME_ON, response=False)
            await client.write_gatt_char(CHAR_UUID, brightness_frame(brightness), response=False)
            while not stopped.is_set():
                hue = ((loop.time() - t0) / (duration_min * 60.0)) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1.0, brightness / 100.0)
                frame = color_frame(round(r * 255), round(g * 255), round(b * 255))
                await client.write_gatt_char(CHAR_UUID, frame, response=False)
                await asyncio.sleep(step)
        print("\nStopped. Strip left on its last colour.")
    except (KeyboardInterrupt, SystemExit):
        print("\nStopped. Strip left on its last colour.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Control an LEDDMX-* BLE light strip.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--mac", help="strip address (default: auto-scan by name)")
    ap.add_argument("--scan", action="store_true", help="list LEDDMX strips and exit")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("on", help="turn the strip on")
    sub.add_parser("off", help="turn the strip off")

    p_color = sub.add_parser("color", help="set a solid colour")
    p_color.add_argument("value", nargs="+", help="named colour, or three ints R G B (0-255)")

    p_bright = sub.add_parser("brightness", help="set brightness 0-100")
    p_bright.add_argument("pct", type=int)

    p_pattern = sub.add_parser("pattern", help="set effect 0-210")
    p_pattern.add_argument("idx", type=int)

    p_rainbow = sub.add_parser("rainbow", help="continuous hue cycle")
    p_rainbow.add_argument("--minutes", type=float, default=30.0, help="cycle length (default 30)")
    p_rainbow.add_argument("--brightness", type=int, default=100, help="0-100 (default 100)")
    p_rainbow.add_argument("--step", type=float, default=0.2, help="seconds between updates")

    args = ap.parse_args()

    if not args.cmd and not args.scan:
        ap.error("need a command (on, off, color, brightness, pattern, rainbow) or --scan")
        return 2

    if args.scan:
        async def _scan():
            devices = await BleakScanner.discover(timeout=8.0)
            found = [d for d in devices if d.name and d.name.upper().startswith(
                tuple(p.upper() for p in NAME_PREFIXES))]
            if not found:
                print("No LEDDMX strips found.")
            for d in found:
                print(f"{d.address}  {d.name}")
        asyncio.run(_scan())
        return 0

    try:
        if args.cmd == "on":
            asyncio.run(send_one(FRAME_ON, args.mac))
        elif args.cmd == "off":
            asyncio.run(send_one(FRAME_OFF, args.mac))
        elif args.cmd == "color":
            if len(args.value) == 3:
                r, g, b = (int(x) for x in args.value)
            elif len(args.value) == 1 and args.value[0].lower() in NAMED_COLORS:
                r, g, b = NAMED_COLORS[args.value[0].lower()]
            else:
                ap.error("color takes a named colour or three ints 'R G B'")
                return 2
            if not all(0 <= c <= 255 for c in (r, g, b)):
                ap.error("RGB values must be 0-255")
                return 2
            asyncio.run(send_one(color_frame(r, g, b), args.mac))
        elif args.cmd == "brightness":
            asyncio.run(send_one(brightness_frame(args.pct), args.mac))
        elif args.cmd == "pattern":
            asyncio.run(send_one(pattern_frame(args.idx), args.mac))
        elif args.cmd == "rainbow":
            asyncio.run(rainbow(args.mac, args.minutes, args.brightness, args.step))
    except BleakDeviceNotFoundError:
        print(f"Could not find device {args.mac}. Is it powered on and in range?")
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())