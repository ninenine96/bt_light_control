# Protocol — LEDDMX-03 / "LED LAMP" BLE strip

Reverse-engineered from the LED LAMP Android app family, cross-checked against
public [led-hue-cycle](https://github.com/Rishi-Goyal/led-hue-cycle) projects,
and verified on this specific unit (`LEDDMX-03-2F70`).

## Controller

- Advertises name prefixes: `LEDDMX`, `LED DMX`, `LED-DMX`
- Accepts **ONE** BLE connection at a time (a phone app or leftover
  `bluetoothctl` connection blocks all scripts)
- Sleeps after ~20–60 s unattended and **stops advertising** — reconnect needs
  to wait for the next advertisement
- Whole strip shows a single solid colour; not individually addressable via
  this protocol

## GATT schema

| Item | UUID |
|---|---|
| Service | `0000ffe0-0000-1000-8000-00805f9b34fb` |
| Write characteristic | `0000ffe1-0000-1000-8000-00805f9b34fb` (read / write / write-without-response / notify) |
| CCCD descriptor | `0x2902` |

## Frames

All frames are exactly **9 bytes**, written to char `0xFFE1` with
`response=False` (fire-and-forget). Framing is `7B … BF`.

| Command | Frame | Notes |
|---|---|---|
| Power on | `7B FF 04 03 FF FF FF FF BF` | |
| Power off | `7B FF 04 02 FF FF FF FF BF` | |
| Solid colour | `7B FF 07 C1 C2 C3 00 FF BF` | `C1..C3` = channels in `COLOR_ORDER` |
| Brightness | `7B FF 01 b1 pct 00 FF FF BF` | `pct` 0–100; `b1 = pct*32/100` |
| Pattern/effect | `7B FF 03 idx FF FF FF FF BF` | `idx` 0–210 |

Details:

- **Colour:** `C1..C3` are written in the controller's expected channel order.
  Verified on this unit: plain **RGB**. If a future unit shows swapped channels
  (blue↔green, or orange→pink on a red frame), flip the `COLOR_ORDER` constant
  in `led_protocol.py` — options are `rgb, rbg, grb, gbr, brg, bgr`. Use
  `test_device.py rgb:255,0,0 <order>` to test each order against the hardware.
- **Brightness:** `pct` is 0–100; both bytes are scaled copies of the same value.
- **Pattern:** `idx` indexes the 200+ built-in "LED LAMP" animated effects.

Frames are written once per command; the controller holds state. `rainbow`
streams many colour frames per second (rate-limit to ~5 Hz — see the ambient
daemon's write cap).

## Connection behaviour (why scripts "hang" sometimes)

1. The device sleeps after inactivity and stops advertising. `BleakClient`
   connect then fails with "Device not found". **Fix:** scan/wait ~20 s for the
   next advertisement, or physically power-cycle the strip.
2. The device accepts only one link. **Fix:** close phone apps and run
   `bluetoothctl disconnect <mac>` before re-testing.
3. Harsh kills (SIGTERM/SIGKILL mid-connection) can leave the link wedged —
   the device shows "Connected" in BlueZ but ignores new connections. Recovery
   is `bluetoothctl disconnect <mac> && bluetoothctl remove <mac>`, then rescan.

`ble_link.Strip` automates most of this: it keeps a background scanner so it
notices a re-advertisement the moment it appears, stops the scan just before
each connect attempt (BlueZ refuses Connect while discovery runs), and retries
with exponential backoff (2→30 s).