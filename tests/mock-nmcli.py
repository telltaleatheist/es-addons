#!/usr/bin/env python3
"""A fake nmcli, enough of one to test the wifi addon.

The addon only ever learns about WiFi through nmcli, so pointing
ES_WIFI_NMCLI at this script is enough to walk every flow with no radio and
no NetworkManager.  (The addon's real command line is "sudo -n nmcli ..."; the
override exists because a test cannot have sudo.)

State lives in the JSON file named by MOCK_NM_STATE and persists across
invocations, because the addon runs nmcli once per question:

    {
      "networks":     [ {"ssid","signal","security","in_use"} ],   scan rows,
                                                   duplicates and all
      "connections":  [ {"name","type","device"} ], device "" = not active
      "devices":      { "wlan0": {"hwaddr","addresses":[..],"gateway"} }
      "bad_password": [ "SSID" ],                   these refuse every secret
      "rescan_fails": false,
      "rescan_delay": 0.0,
      "log":          [ "dev wifi rescan", "connection up id X", ... ]
    }

The log records the argument list of every call with the password replaced,
so a test can assert what the addon asked for without the fixture ever
holding a secret it might print.

Terse output is escaped the way real nmcli escapes it - ':' as '\\:' and '\\'
as '\\\\' - so an addon that splits on a bare colon fails here, which is the
point of the "Cafe: Guest" fixture.
"""

import json
import os
import sys
import time


STATE_PATH = os.environ.get("MOCK_NM_STATE", "")

WIFI_TYPE = "802-11-wireless"
WIFI_DEVICE = "wlan0"


def load():
	with open(STATE_PATH, "r") as handle:
		return json.load(handle)


def save(state):
	temp = STATE_PATH + ".tmp"
	with open(temp, "w") as handle:
		json.dump(state, handle, indent=2)
	os.replace(temp, STATE_PATH)


def note(state, args):
	"""Record a call, with the password blanked out."""
	safe = []
	hide = False
	for arg in args:
		if hide:
			safe.append("***")
			hide = False
			continue
		safe.append(arg)
		if arg == "password":
			hide = True
	state.setdefault("log", []).append(" ".join(safe))


def out(*lines):
	for line in lines:
		sys.stdout.write(line + "\n")
	sys.stdout.flush()


def error(text):
	sys.stderr.write(text + "\n")
	sys.stderr.flush()


def escape(value):
	"""What nmcli -t does to a field before printing it."""
	return str(value).replace("\\", "\\\\").replace(":", "\\:")


def terse(*fields):
	out(":".join(escape(part) for part in fields))


def show_line(key, value):
	"""One line of "dev show" terse output.

	Real nmcli (measured on 1.42.4) does NOT escape values here - a MAC's
	colons arrive raw, and only the first colon on the line is a separator.
	Emitting the real shape is the whole point of the mock: an escaped MAC
	here once hid a parser that truncated "2C:CF:67:..." to "2C".
	"""
	out("%s:%s" % (key, value))


# ------------------------------------------------------------- connections

def find(state, name):
	for connection in state.get("connections", []):
		if connection["name"] == name:
			return connection
	return None


def activate(state, name):
	"""Bring a wifi profile up, which brings whichever one was up down."""
	for connection in state.get("connections", []):
		if connection["type"] == WIFI_TYPE:
			connection["device"] = ""

	connection = find(state, name)
	connection["device"] = WIFI_DEVICE

	# the scan list agrees with the connection list about what is in use
	for network in state.get("networks", []):
		network["in_use"] = network["ssid"] == name and network.get("signal", 0) > 0

	return connection


def deactivate(state, name):
	connection = find(state, name)
	if connection is None:
		return False
	connection["device"] = ""
	for network in state.get("networks", []):
		if network["ssid"] == name:
			network["in_use"] = False
	return True


# ---------------------------------------------------------------- commands

def cmd_connection_show(state, fields, args):
	active_only = "--active" in args

	for connection in state.get("connections", []):
		if active_only and not connection.get("device"):
			continue
		row = []
		for name in fields:
			if name == "NAME":
				row.append(connection["name"])
			elif name == "TYPE":
				row.append(connection["type"])
			elif name == "DEVICE":
				row.append(connection.get("device", ""))
			else:
				row.append("")
		terse(*row)

	return 0


def cmd_connection_up(state, args):
	if len(args) < 2 or args[0] != "id":
		error("Error: unknown connection.")
		return 10

	name = args[1]
	if find(state, name) is None:
		error("Error: unknown connection '%s'." % name)
		return 10

	activate(state, name)
	out("Connection successfully activated "
		"(D-Bus active path: /org/freedesktop/NetworkManager/ActiveConnection/1)")
	return 0


def cmd_connection_down(state, args):
	if len(args) < 2 or args[0] != "id":
		error("Error: unknown connection.")
		return 10

	name = args[1]
	if not deactivate(state, name):
		error("Error: '%s' is not an active connection." % name)
		return 10

	out("Connection '%s' successfully deactivated." % name)
	return 0


def cmd_connection_delete(state, args):
	if len(args) < 2 or args[0] != "id":
		error("Error: unknown connection.")
		return 10

	name = args[1]
	if find(state, name) is None:
		error("Error: unknown connection '%s'." % name)
		return 10

	deactivate(state, name)
	state["connections"] = [
		connection for connection in state["connections"]
		if connection["name"] != name
	]
	out("Connection '%s' successfully deleted." % name)
	return 0


def cmd_wifi_rescan(state, args):
	delay = float(state.get("rescan_delay", 0) or 0)
	if delay > 0:
		time.sleep(delay)

	if state.get("rescan_fails"):
		error("Error: Scanning not allowed immediately following previous scan.")
		return 1

	return 0


def cmd_wifi_list(state, fields, args):
	for network in state.get("networks", []):
		row = []
		for name in fields:
			if name == "SSID":
				row.append(network.get("ssid", ""))
			elif name == "SIGNAL":
				row.append(network.get("signal", 0))
			elif name == "SECURITY":
				row.append(network.get("security", ""))
			elif name == "IN-USE":
				row.append("*" if network.get("in_use") else "")
			else:
				row.append("")
		terse(*row)

	return 0


def cmd_dev_show(state, fields, args):
	"""nmcli -t -f GENERAL.CONNECTION,GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY dev show IFACE.

	Which connection is on the interface is read from the connection list, not
	from a second copy of the truth, so bringing a profile up here changes what
	"dev show" says about it - the way it does on a real machine.

	Addresses carry their prefix length and there can be more than one, as
	IP4.ADDRESS[1], IP4.ADDRESS[2].  A disconnected interface still has a MAC
	and nothing else, and nmcli writes its empty connection as "--".
	"""
	if not args:
		error("Error: argument is missing.")
		return 2

	name = args[0]
	device = state.get("devices", {}).get(name)
	if device is None:
		error("Error: Device '%s' not found." % name)
		return 10

	connection = ""
	for entry in state.get("connections", []):
		if entry.get("device") == name:
			connection = entry["name"]
			break

	show_line("GENERAL.CONNECTION", connection or "--")
	show_line("GENERAL.HWADDR", device.get("hwaddr", ""))

	if connection:
		for index, address in enumerate(device.get("addresses", []), start=1):
			show_line("IP4.ADDRESS[%d]" % index, address)
		if device.get("gateway"):
			show_line("IP4.GATEWAY", device["gateway"])

	return 0


def cmd_wifi_connect(state, args):
	"""nmcli dev wifi connect SSID [password PW] [hidden yes].

	The profile is written before it is activated, which is exactly why a
	wrong password leaves one behind - the addon's cleanup path exists for
	this, so the mock has to reproduce it.
	"""
	if not args:
		error("Error: SSID or BSSID are missing.")
		return 2

	ssid = args[0]
	rest = args[1:]

	password = None
	index = 0
	while index < len(rest):
		word = rest[index]
		if word == "password" and index + 1 < len(rest):
			password = rest[index + 1]
			index += 2
			continue
		if word == "hidden" and index + 1 < len(rest):
			index += 2    # the grammar; what the test asserts on is the log
			continue
		index += 1

	secured = any(
		network.get("ssid") == ssid and network.get("security") not in ("", "--")
		for network in state.get("networks", []))

	if find(state, ssid) is None:
		state.setdefault("connections", []).append(
			{"name": ssid, "type": WIFI_TYPE, "device": ""})

	if ssid in state.get("bad_password", []) or (secured and password is None):
		# NetworkManager keeps the profile it just wrote, working or not
		error("Error: Connection activation failed: (7) Secrets were required, "
			"but not provided.")
		return 4

	activate(state, ssid)
	out("Device '%s' successfully activated with "
		"'0e1f2a3b-4c5d-6e7f-8a9b-0c1d2e3f4a5b'." % WIFI_DEVICE)
	return 0


# ------------------------------------------------------------------ driver

def strip_globals(args):
	"""Peel nmcli's global options off the front.  Returns (fields, rest)."""
	fields = []
	index = 0

	while index < len(args):
		word = args[index]
		if word in ("-t", "--terse", "-p", "--pretty"):
			index += 1
			continue
		if word in ("-f", "--fields") and index + 1 < len(args):
			fields = [name.strip().upper() for name in args[index + 1].split(",")]
			index += 2
			continue
		break

	return fields, args[index:]


def dispatch(state, fields, words):
	if not words:
		error("Error: missing command.")
		return 2

	head = words[0]
	rest = words[1:]

	if head in ("connection", "con", "c"):
		if rest and rest[0] == "show":
			return cmd_connection_show(state, fields, rest[1:])
		if rest and rest[0] == "up":
			return cmd_connection_up(state, rest[1:])
		if rest and rest[0] == "down":
			return cmd_connection_down(state, rest[1:])
		if rest and rest[0] in ("delete", "del"):
			return cmd_connection_delete(state, rest[1:])

	elif head in ("device", "dev", "d"):
		if len(rest) >= 2 and rest[0] == "show":
			return cmd_dev_show(state, fields, rest[1:])
		if len(rest) >= 2 and rest[0] == "wifi":
			if rest[1] == "rescan":
				return cmd_wifi_rescan(state, rest[2:])
			if rest[1] == "list":
				return cmd_wifi_list(state, fields, rest[2:])
			if rest[1] == "connect":
				return cmd_wifi_connect(state, rest[2:])

	error("Error: unknown command '%s'." % " ".join(words))
	return 2


def main():
	if not STATE_PATH:
		sys.stderr.write("mock nmcli: MOCK_NM_STATE is not set\n")
		return 2

	args = sys.argv[1:]
	state = load()
	note(state, args)

	fields, words = strip_globals(args)
	code = dispatch(state, fields, words)

	save(state)
	return code


if __name__ == "__main__":
	sys.exit(main())
