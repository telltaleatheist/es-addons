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

cp -r wifi ~/.emulationstation/addons/wifi
chmod +x ~/.emulationstation/addons/wifi/run.sh ~/.emulationstation/addons/wifi/wifi.py
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

## wifi

"Internet" in the main menu (the addon's directory and manifest `name` are still
`wifi`). Joins and manages wireless networks through `nmcli`, answers "am I online",
and switches SSH — Python 3 standard library only, no pip packages, no daemon, no
config file.

**Main list**

```
Connected: PrettyFlyForAWifi                     192.168.68.75
SSH                                                    Enabled
PrettyFlyForAWifi                                    Connected
preconfigured                                            Saved
Search for networks...                      join a new network
Join hidden network...                           type the name
```

The **status row** is first because it is the question people came with. It follows
the ethernet cable as well as the radio — a box plugged into the router is online,
and a screen reading "Not connected" because `wlan0` is idle would be lying — so it
shows `wlan0`'s connection if there is one, else the wired one, and loopback never
counts. Picking it opens everything `nmcli -t -f
GENERAL.CONNECTION,GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY dev show <iface>` will
say: connection, interface, IP address, MAC and gateway. An address arrives as
`192.168.68.75/24` and can arrive more than once (`IP4.ADDRESS[1]`,
`IP4.ADDRESS[2]`); the prefix is dropped and the first one wins. A MAC is nothing
but colons, which makes it the second place terse escaping has to be undone.

The **SSH row** is on this screen because it is the same question — "can I reach
this box from my desk" — and it is the thing a person wants to switch on the moment
they have an IP address to type. Its state is `systemctl is-active ssh`, and the
answer is read off *stdout*, not the exit status: `is-active` exits 3 for a unit
that is not running, and that is an answer, not a failure. A systemctl that will not
run or says nothing leaves the row reading `Unknown`, and picking it then says so
rather than guessing. Switching it asks first — "Enable SSH? This allows remote
logins to this system." / "Disable SSH? Remote access to this system will stop
working." — and runs `systemctl enable --now ssh` or `disable --now ssh`.

Only `802-11-wireless` profiles are listed below that: "Wired connection 1" and `lo`
are connections too, and neither belongs in a list of networks to join. What is up
is asked exactly once, of `nmcli connection show --active`, so the status row, the
main list, the connect/disconnect choice and the scan picker's mark cannot disagree.

Picking a saved network offers Connect *or* Disconnect, whichever applies, and
"Forget this network", which asks first and says the saved password goes with it.

**Search for networks** rescans and shows what is in the air, strongest first:

```
PrettyFlyForAWifi                          100%  WPA2  (connected)
BillWiTheScienceFi                                    81%  WPA2
Cafe: Guest                                            64%  open
```

A scan sees every access point, so a house with a repeater reports the same name
three or four times; the list shows each name once, at its best signal, and marked
as connected if *any* of its rows is the one in use — the row wearing the `*` is
often not the strongest. A row with no name at all is a hidden network announcing
itself; there is nothing to pick, so it is dropped, and "Join hidden network..." is
how you reach one.

Picking a network either brings its saved profile up, joins it outright if it is
open, or asks for the password. A wrong password says so and offers the box again —
and deletes the profile `nmcli dev wifi connect` leaves behind on the way, because
the next attempt would otherwise find that dead profile and fail the same way. A
profile that was *already* there is never deleted by a failed attempt.

B cancels a rescan. A refused rescan is not fatal: NetworkManager says "Scanning not
allowed immediately following previous scan" whenever the last one was seconds ago,
and its cached list is still the right answer — only an empty result is worth a
message, and then the rescan's own words go in it.

Everything `nmcli` is asked has a timeout, and a timeout, a failure or a missing
`nmcli` is a message box, never a traceback. Passwords are handed to `nmcli` as
argument-list entries, never through a shell, and the log line for a command has the
password blanked out, which matters because `run.sh` appends stderr to
`/tmp/wifi-addon.log`.

**sudo is not optional.** On the target machine (RetroPie on a Pi 5, running as
`pi`) polkit refuses NetworkManager operations — even a scan — from a session it
does not think is local, so every call is `sudo -n nmcli ...` and passwordless sudo
is what makes the addon work at all. `-n` means a sudo that wants a password fails
immediately rather than waiting forever on a terminal the addon does not have.
`systemctl` goes the same way, through `systemctl_argv()`, for the same reason.

Tested against NetworkManager / nmcli 1.42.4 (RetroPie on Raspberry Pi OS Bookworm).

## Tests

```sh
python3 tests/test_bluetooth.py
python3 tests/test_wifi.py
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

`tests/mock-nmcli.py` is a fake `nmcli` — enough of one to answer `dev wifi rescan`,
`dev wifi list`, `dev wifi connect`, `connection show` (with and without
`--active`), `connection up`, `connection down` and `connection delete` from a JSON
state file, escaping its terse output the way real nmcli does (`:` as `\:`) and
leaving a half-created profile behind when a secret is refused, exactly as
NetworkManager does.

`tests/mock-systemctl.py` is a fake `systemctl` — enough of one to answer
`is-active`, `enable --now` and `disable --now` from a JSON state file, and to exit
3 with `inactive` on stdout the way the real one does for a stopped unit.

`tests/test_wifi.py` plays the EmulationStation side over pipes: it points
`ES_WIFI_NMCLI` and `ES_WIFI_SYSTEMCTL` at the mocks — the two environment
variables the addon reads, which replace `sudo -n nmcli` and `sudo -n systemctl`
with a single binary each — and walks the addon end to end: the main list with the
ethernet and loopback connections filtered out, the status row and its details
screen (and the same row following the cable when `wlan0` is idle, and reading "Not
connected" when only loopback is up), the SSH row in both directions with a declined
question that changes nothing and a stopped unit that is a state rather than an
error, a scan whose
duplicate SSIDs collapse to their strongest reading with the nameless row dropped
and an escaped colon put back together, the already-connected network, an open
network joined with no password box, a secured one joined with it, a wrong password
that deletes the leftover profile and asks again (and a pre-existing profile it must
not touch), the hidden-network flow and its `hidden yes`, connect/disconnect/forget
from a saved network's menu, a refused rescan, cancelling a rescan with B, and
closing. It also asserts no password ever reaches stderr. It prints PASS or FAIL per
step and exits non-zero on any failure. No hardware, no radio, no sudo, no service
manager.
