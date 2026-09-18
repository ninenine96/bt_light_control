"""Raw frame tester for calibrating a physical LEDDMX strip (runs on import).

Deliberately minimal and self-contained apart from the UUID/frame constants:
``color_frame`` takes an explicit channel ``order`` so you can probe a new unit
for its true RGB channel order (see AGENTS.md "Colours / calibration notes").
This module executes BLE I/O at import, so it is NOT collected by run_tests.py.
"""

import asyncio
import sys

from bleak import BleakClient, BleakScanner

from led_protocol import CHAR_UUID, FRAME_OFF, FRAME_ON, SERVICE_UUID

ADDR = "41:42:9A:B1:2F:70"


def color_frame(r, g, b, order="rgb"):
    channels = {"r": r, "g": g, "b": b}
    c1, c2, c3 = (channels[ch] for ch in order)
    return bytes([0x7B, 0xFF, 0x07, c1, c2, c3, 0x00, 0xFF, 0xBF])


def brightness_frame(pct):
    pct = max(0, min(100, int(pct)))
    b1 = pct * 32 // 100
    return bytes([0x7B, 0xFF, 0x01, b1, pct, 0x00, 0xFF, 0xFF, 0xBF])


async def connect_check(addr):
    try:
        client = BleakClient(addr, timeout=20)
        await client.connect()
        print(f"Connected: {client.is_connected}")
        svc = client.services.resolve(SERVICE_UUID) if not client.services else client.services
        char = client.services.get_characteristic(CHAR_UUID)
        print(f"Char found: {char is not None}")
        if char:
            print(f"Char properties: {char.properties}")
        return client
    except Exception as e:
        print(f"FAILED to connect: {e}")
        sys.exit(1)


async def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "on"
    client = await connect_check(ADDR)
    if cmd == "on":
        payload = FRAME_ON
    elif cmd == "off":
        payload = FRAME_OFF
    elif cmd.startswith("rgb:"):
        _, rgb = cmd.split(":")
        r, g, b = (int(x) for x in rgb.split(","))
        order = sys.argv[2] if len(sys.argv) > 2 else "rgb"
        payload = color_frame(r, g, b, order)
    elif cmd.startswith("bright"):
        pct = int(cmd.split(":")[1])
        payload = brightness_frame(pct)
    else:
        print("Unknown command")
        await client.disconnect()
        sys.exit(1)

    print(f"Writing: {payload.hex(' ')}")
    await client.write_gatt_char(CHAR_UUID, payload, response=False)
    await asyncio.sleep(1.0)
    await client.disconnect()
    print("Done.")


asyncio.run(main())