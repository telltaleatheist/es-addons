# es-addons

Addons for EmulationStation's addon host (the `feature/addon-host` branch).

An addon is a headless program. ES spawns it with pipes on its stdin and stdout and
draws its screens with ES's own components, so an addon gets the real menu, the real
fonts and real controller navigation without linking against anything. The addon
never draws and never reads the terminal: it writes one JSON object per line asking
for a screen, and reads one JSON object per line saying what the user did. The
protocol is `ADDONS.md` in the EmulationStation source tree.

## Installing

One directory per addon under `~/.emulationstation/addons/`:

```sh
mkdir -p ~/.emulationstation/addons
cp -r bluetooth ~/.emulationstation/addons/bluetooth
chmod +x ~/.emulationstation/addons/bluetooth/bluetooth.py
```

The directory is rescanned every time the menu is built, so a newly installed addon
costs a trip out of the main menu and back in — no restart. The executable bit is not
optional: an addon ES cannot run is skipped with a warning in `es_log.txt`.

Addons are a Linux/POSIX feature; on Windows nothing is spawned and no entry appears.

## bluetooth

"BLUETOOTH" in the main menu. Pairs and manages Bluetooth devices through
`bluetoothctl`, and nothing else — Python 3 standard library only, no pip packages,
no daemon, no config file.

It exists because the usual retro-handheld Bluetooth menu is a wall of MAC addresses,
and the person in front of it is holding a controller and wants it to work.

**Main list**

```
8BitDo Pro 2                                          Connected
Nintendo Switch Pro Controller                    Not connected
Other devices                                           2 paired
Search for new devices...                       pair a controller
```

Known **controllers** get a row each, with their connection state. Everything else —
speakers, keyboards, phones — collapses into one `Other devices` row that opens a
sub-list. A device is a controller when BlueZ's `Icon` is `input-gaming`, or, when
there is no icon yet, when its class of device is a peripheral whose minor class is
joystick or gamepad.

**Search for new devices** is the pairing flow. It scans for 20 seconds and the
*first* newly discovered controller is paired, trusted and connected immediately —
no list, no confirmation, the progress screen just narrates it:

```
Found 8BitDo Pro 2 - pairing...   →   ...trusting...   →   ...connecting...
```

A device that has not said what it is yet is asked directly (`bluetoothctl info`),
and an `Icon` that arrives late as a `[CHG]` line during the scan is honoured, so a
pad that announces itself in two stages still pairs on sight.

B cancels a scan. Once pairing has started it runs to its end — a half-paired device
is worse than a slow menu — and any failure names the step that failed and quotes
bluetoothctl's own error line.

If the scan finds no controller, the devices it did find are listed (nameless ones,
which BlueZ names after their own MAC, are dropped as noise, as are devices already
known), and anything picked from that list asks before pairing.

**A device's own menu** — reached from either list — offers Connect *or* Disconnect,
whichever applies, and "Forget this device", which asks first.

Everything `bluetoothctl` is asked has a timeout, and a timeout, a failure or a
missing `bluetoothctl` is a message box, never a traceback. Debugging noise goes to
stderr, which is where ES's own stderr goes.

Tested against BlueZ 5.66 (Raspberry Pi OS Bookworm).

## Tests

```sh
python3 tests/test_bluetooth.py
```

`tests/mock-bluetoothctl.py` is a fake `bluetoothctl` — enough of one to answer
`devices`, `info`, `pair`, `trust`, `connect`, `disconnect` and `remove` from a JSON
state file, and to play a scripted discovery over an interactive session, wearing the
colour codes and the redrawn prompt that real bluetoothctl mixes into its output.

`tests/test_bluetooth.py` plays the EmulationStation side over pipes: it puts the
mock on `PATH` as `bluetoothctl` and walks the addon end to end — the main list, the
Other devices sub-list, disconnect, forget, a scan that auto-pairs a controller
identified only by a late `[CHG] Icon` line, a pairing whose trust step fails, a scan
with no controller in it, pairing by hand, cancelling a scan, and closing. It prints
PASS or FAIL per step and exits non-zero on any failure. No hardware, no radio.

The addon reads one environment variable for the tests' benefit: `ES_BT_SCAN_SECONDS`
(default 20) sets how long a scan runs.
