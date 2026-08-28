#!/usr/bin/env python3
"""A fake bluetoothctl, enough of one to test the bluetooth addon.

The addon only ever learns about Bluetooth through bluetoothctl, so putting
this on PATH under the name "bluetoothctl" is enough to walk every flow with
no radio and no hardware.

State lives in the JSON file named by MOCK_BT_STATE and persists across
invocations, because the addon runs bluetoothctl once per question:

    {
      "devices":      [ {"mac","name","icon","class","connected"} ],
      "discoverable": { "MAC": {"name","icon","class"} },
      "scan_script":  [ {"at": 0.3, "kind": "NEW"|"CHG", "mac", "tail"} ],
      "fail_step":    null | "pair" | "trust" | "connect",
      "log":          [ "devices", "info AA:..", ... ]
    }

"at" is seconds after "scan on".  Every scripted line is emitted with the
colour codes and the redrawn prompt that real interactive bluetoothctl mixes
into its output, so an addon that does not strip escape sequences fails here.

Only the one-shot commands write state; the scan session reads it once and
then only prints, so the two never fight over the file.
"""

import json
import os
import sys
import threading
import time


STATE_PATH = os.environ.get("MOCK_BT_STATE", "")

# what interactive bluetoothctl actually sprays around its output
PROMPT = "\x1b[0;94m[bluetooth]\x1b[0m# "
GREEN = "\x1b[0;92m"
RESET = "\x1b[0m"
ERASE = "\r\x1b[K"


def load():
	with open(STATE_PATH, "r") as handle:
		return json.load(handle)


def save(state):
	temp = STATE_PATH + ".tmp"
	with open(temp, "w") as handle:
		json.dump(state, handle, indent=2)
	os.replace(temp, STATE_PATH)


def note(state, entry):
	state.setdefault("log", []).append(entry)


def out(*lines):
	for line in lines:
		sys.stdout.write(line + "\n")
	sys.stdout.flush()


def find(state, mac):
	for device in state.get("devices", []):
		if device["mac"].upper() == mac.upper():
			return device
	return None


def scripted_icon(state, mac):
	"""The icon a [CHG] line hands out during the scan.

	BlueZ remembers a property once the device has volunteered it, so a device
	paired after such a line has the icon in its info from then on.
	"""
	for step in state.get("scan_script", []):
		if step["mac"].upper() != mac.upper():
			continue
		tail = step.get("tail", "")
		if tail.lower().startswith("icon:"):
			return tail.split(":", 1)[1].strip()
	return None


# ------------------------------------------------------------- one-shots

def cmd_devices(state, args):
	paired_only = bool(args) and args[0].lower() == "paired"
	for device in state.get("devices", []):
		if paired_only and not device.get("paired", True):
			continue
		out("%sDevice%s %s %s" % (GREEN, RESET, device["mac"], device["name"]))
	return 0


def cmd_info(state, args):
	if not args:
		out("Missing device address argument")
		return 1

	mac = args[0].upper()
	device = find(state, mac)

	if device is not None:
		out("%sDevice%s %s (public)" % (GREEN, RESET, mac))
		out("\tName: %s" % device["name"])
		out("\tAlias: %s" % device["name"])
		if device.get("class"):
			out("\tClass: %s" % device["class"])
		if device.get("icon"):
			out("\tIcon: %s" % device["icon"])
		out("\tPaired: yes")
		out("\tTrusted: yes")
		out("\tBlocked: no")
		out("\tConnected: %s" % ("yes" if device.get("connected") else "no"))
		out("\tUUID: Human Interface Device    (00001124-0000-1000-8000-00805f9b34fb)")
		return 0

	seen = state.get("discoverable", {}).get(mac)
	if seen is not None:
		out("%sDevice%s %s (public)" % (GREEN, RESET, mac))
		if seen.get("name"):
			out("\tName: %s" % seen["name"])
			out("\tAlias: %s" % seen["name"])
		if seen.get("class"):
			out("\tClass: %s" % seen["class"])
		if seen.get("icon"):
			out("\tIcon: %s" % seen["icon"])
		out("\tPaired: no")
		out("\tTrusted: no")
		out("\tBlocked: no")
		out("\tConnected: no")
		return 0

	out("Device %s not available" % mac)
	return 1


def cmd_pair(state, args):
	mac = args[0].upper()
	out("Attempting to pair with %s" % mac)

	if state.get("fail_step") == "pair":
		out("Failed to pair: org.bluez.Error.AuthenticationCanceled")
		return 1

	device = find(state, mac)
	if device is None:
		seen = dict(state.get("discoverable", {}).get(mac, {}))
		icon = seen.get("icon") or scripted_icon(state, mac)
		device = {
			"mac": mac,
			"name": seen.get("name") or mac.replace(":", "-"),
			"icon": icon,
			"class": seen.get("class"),
			"connected": False,
		}
		state.setdefault("devices", []).append(device)

	out("[CHG] Device %s Paired: yes" % mac)
	out("Pairing successful")
	return 0


def cmd_trust(state, args):
	mac = args[0].upper()

	if state.get("fail_step") == "trust":
		out("Failed to trust: org.bluez.Error.Failed")
		return 1

	out("[CHG] Device %s Trusted: yes" % mac)
	out("Changing %s trust succeeded" % mac)
	return 0


def cmd_connect(state, args):
	mac = args[0].upper()
	out("Attempting to connect to %s" % mac)

	if state.get("fail_step") == "connect":
		out("Failed to connect: org.bluez.Error.Failed br-connection-canceled")
		return 1

	device = find(state, mac)
	if device is None:
		out("Device %s not available" % mac)
		return 1

	device["connected"] = True
	out("[CHG] Device %s Connected: yes" % mac)
	out("Connection successful")
	return 0


def cmd_disconnect(state, args):
	mac = args[0].upper()
	device = find(state, mac)

	if device is None:
		out("Device %s not available" % mac)
		return 1

	out("Attempting to disconnect from %s" % mac)
	device["connected"] = False
	out("[CHG] Device %s Connected: no" % mac)
	out("Successful disconnected")
	return 0


def cmd_remove(state, args):
	mac = args[0].upper()
	device = find(state, mac)

	if device is None:
		out("Device %s not available" % mac)
		return 1

	state["devices"] = [d for d in state["devices"] if d is not device]
	out("[DEL] Device %s %s" % (mac, device["name"]))
	out("Device has been removed")
	return 0


ONE_SHOTS = {
	"devices": cmd_devices,
	"info": cmd_info,
	"pair": cmd_pair,
	"trust": cmd_trust,
	"connect": cmd_connect,
	"disconnect": cmd_disconnect,
	"remove": cmd_remove,
}


# ------------------------------------------------------------- the session

def emit_script(script):
	"""Play the scripted discovery lines, wearing bluetoothctl's decoration."""
	started = time.monotonic()

	for step in sorted(script, key=lambda step: step.get("at", 0)):
		delay = started + float(step.get("at", 0)) - time.monotonic()
		if delay > 0:
			time.sleep(delay)

		line = "%s%s%s[%s%s%s]%s Device %s %s" % (
			ERASE, PROMPT, GREEN, RESET, step["kind"], GREEN, RESET,
			step["mac"], step.get("tail", ""))
		sys.stdout.write(line + "\n")
		sys.stdout.flush()


def interactive():
	"""bluetoothctl with no arguments: read commands, answer forever."""
	state = load()

	out(PROMPT + "Agent registered")

	for line in sys.stdin:
		command = line.strip()

		if command == "scan on":
			out(PROMPT + "Discovery started",
				PROMPT + "[" + GREEN + "CHG" + RESET + "] Controller "
				"B8:27:EB:00:00:01 Discovering: yes")
			thread = threading.Thread(
				target=emit_script, args=(state.get("scan_script", []),), daemon=True)
			thread.start()

		elif command == "scan off":
			out(PROMPT + "Discovery stopped")

		elif command in ("exit", "quit"):
			sys.stdout.flush()
			os._exit(0)

		elif command:
			out(PROMPT + "Invalid command in menu main: %s" % command)

	sys.stdout.flush()
	os._exit(0)


def main():
	if not STATE_PATH:
		sys.stderr.write("mock bluetoothctl: MOCK_BT_STATE is not set\n")
		return 2

	args = sys.argv[1:]

	if not args:
		interactive()
		return 0

	state = load()
	note(state, " ".join(args))

	handler = ONE_SHOTS.get(args[0])
	if handler is None:
		out("Invalid command: %s" % args[0])
		save(state)
		return 1

	if args[0] != "devices" and len(args) < 2:
		out("Missing device address argument")
		save(state)
		return 1

	code = handler(state, args[1:])
	save(state)
	return code


if __name__ == "__main__":
	sys.exit(main())
