#!/usr/bin/env python3
"""End-to-end test for the wifi addon: this script plays EmulationStation.

It spawns wifi.py exactly as ES does - pipes on stdin and stdout, one JSON
object per line each way - with ES_WIFI_NMCLI pointing at mock-nmcli.py, and
walks the whole addon:

  * the main list: the status row, the SSH row, saved WIFI profiles only with
    the connected one marked, and the two rows that lead to a network that is
    not saved yet
  * the status row's details screen: address, MAC and gateway, with the terse
    escaping in a MAC put back together
  * the status row following the ethernet cable when wlan0 is idle, and
    saying "Not connected" only when nothing is up
  * the SSH row: Enabled/Disabled, both questions, a declined one that changes
    nothing, and is-active's nonzero exit read as a state and not a failure
  * a scan: duplicate SSIDs collapsed to their strongest reading, the hidden
    (nameless) row dropped, an escaped colon in an SSID put back together,
    strongest first
  * picking the network we are already on
  * joining an open network, which never asks for a password
  * joining a secured one, which does
  * a wrong password: the message, the half-created profile deleted, the
    password box offered again - and, for a network whose profile was already
    there, that same profile left alone
  * the hidden-network flow, and the "hidden yes" it has to pass
  * a saved network's own menu: connect, disconnect, forget with its question
  * a refused rescan, which is not fatal, and cancelling one with B
  * "back" on the main list, which closes the addon with status 0
  * a system with no nmcli at all

Run it: python3 tests/test_wifi.py
"""

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(os.path.dirname(HERE), "wifi", "wifi.py")
MOCK = os.path.join(HERE, "mock-nmcli.py")
MOCK_SYSTEMCTL = os.path.join(HERE, "mock-systemctl.py")

READ_TIMEOUT = 30.0
WIFI_TYPE = "802-11-wireless"

HOME_NET = "PrettyFlyForAWifi"
NEIGHBOUR = "BillWiTheScienceFi"
CAFE = "Cafe: Guest"           # the escaped-colon fixture: nmcli says "Cafe\: Guest"
OPEN_NET = "OpenLibrary"
BAD_NET = "BadPassNet"         # refuses every secret, and has no profile yet
LEFTOVER = "preconfigured"     # the RetroPie image's leftover wifi profile
HIDDEN_NET = "SecretBase"

WIFI_IP = "192.168.68.75"
WIFI_MAC = "2C:CF:67:AA:BB:CC"          # dev show emits these colons RAW, unescaped
WIRED_NAME = "Wired connection 1"
WIRED_IP = "192.168.68.40"
WIRED_MAC = "DC:A6:32:11:22:33"
GATEWAY = "192.168.68.1"


class Fail(Exception):
	pass


def check(condition, message):
	if not condition:
		raise Fail(message)


# ------------------------------------------------------------------ state

SEED = {
	# the scan as nmcli reports it: one row per access point, so the same
	# name appears more than once - and the '*' rides on the WEAKER of the
	# two home rows, which is what the real device does
	"networks": [
		{"ssid": HOME_NET, "signal": 100, "security": "WPA2", "in_use": False},
		{"ssid": HOME_NET, "signal": 89, "security": "WPA2", "in_use": True},
		{"ssid": "", "signal": 95, "security": "WPA2", "in_use": False},
		{"ssid": NEIGHBOUR, "signal": 76, "security": "WPA2", "in_use": False},
		{"ssid": NEIGHBOUR, "signal": 81, "security": "WPA2", "in_use": False},
		{"ssid": CAFE, "signal": 64, "security": "", "in_use": False},
		{"ssid": BAD_NET, "signal": 70, "security": "WPA2", "in_use": False},
		{"ssid": OPEN_NET, "signal": 58, "security": "--", "in_use": False},
	],
	"connections": [
		{"name": WIRED_NAME, "type": "802-3-ethernet", "device": "eth0"},
		{"name": "lo", "type": "loopback", "device": "lo"},
		{"name": HOME_NET, "type": WIFI_TYPE, "device": "wlan0"},
		{"name": LEFTOVER, "type": WIFI_TYPE, "device": ""},
	],
	# BAD_NET has no profile yet (the addon must delete the one the failed
	# attempt leaves behind); LEFTOVER has one (the addon must not touch it)
	# wlan0 carries two addresses, so "use the first" has something to be
	# right about; every MAC is nothing but colons, which is what makes
	# "dev show" a second proof that terse escaping is undone
	"devices": {
		"wlan0": {
			"hwaddr": WIFI_MAC,
			"addresses": [WIFI_IP + "/24", "192.168.68.201/24"],
			"gateway": GATEWAY,
		},
		"eth0": {
			"hwaddr": WIRED_MAC,
			"addresses": [WIRED_IP + "/24"],
			"gateway": GATEWAY,
		},
		"lo": {"hwaddr": "00:00:00:00:00:00", "addresses": ["127.0.0.1/8"]},
	},
	"bad_password": [BAD_NET, LEFTOVER],
	"rescan_fails": False,
	"rescan_delay": 0.0,
	"log": [],
}


SYSTEMCTL_SEED = {
	"units": {"ssh": {"active": True, "enabled": True}},
	"fail": None,
	"silent": False,
	"log": [],
}


def read_state(path):
	with open(path) as handle:
		return json.load(handle)


def patch_state(path, **updates):
	state = read_state(path)
	state.update(updates)
	with open(path, "w") as handle:
		json.dump(state, handle, indent=2)


def connection_names(path):
	return [entry["name"] for entry in read_state(path)["connections"]]


def log_of(path):
	return read_state(path)["log"]


def count(path, entry):
	return log_of(path).count(entry)


def devices_of(path):
	"""Which interface each connection is up on, as the mock has it."""
	return {entry["name"]: entry.get("device", "")
		for entry in read_state(path)["connections"]}


def set_devices(path, mapping):
	"""Rewrite what is up, behind the addon's back.

	Unplugging an ethernet cable is not something the addon can be asked to
	do, so the state is edited directly and the main list re-drawn.
	"""
	state = read_state(path)
	for entry in state["connections"]:
		entry["device"] = mapping.get(entry["name"], "")
	with open(path, "w") as handle:
		json.dump(state, handle, indent=2)


# ------------------------------------------------------------- ES stand-in

class Addon(object):
	"""The addon, and the pipes ES would be holding."""

	def __init__(self, state_path, nmcli, systemctl_state, systemctl):
		environment = dict(os.environ)
		environment["MOCK_NM_STATE"] = state_path
		environment["ES_WIFI_NMCLI"] = nmcli
		environment["MOCK_SYSTEMCTL_STATE"] = systemctl_state
		environment["ES_WIFI_SYSTEMCTL"] = systemctl

		self.log_path = state_path + ".stderr"
		self.log_file = open(self.log_path, "w")

		self.proc = subprocess.Popen(
			[sys.executable, ADDON],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=self.log_file,
			env=environment,
			cwd=os.path.dirname(ADDON),
			bufsize=0,
		)

		self.buffer = b""
		self.selector = selectors.DefaultSelector()
		self.selector.register(self.proc.stdout.fileno(), selectors.EVENT_READ)

	def send(self, **event):
		line = (json.dumps(event) + "\n").encode("utf-8")
		self.proc.stdin.write(line)
		self.proc.stdin.flush()

	def next_command(self, timeout=READ_TIMEOUT):
		deadline = time.monotonic() + timeout

		while True:
			newline = self.buffer.find(b"\n")
			if newline >= 0:
				line = self.buffer[:newline].decode("utf-8", "replace").strip()
				self.buffer = self.buffer[newline + 1:]
				if not line:
					continue
				try:
					return json.loads(line)
				except ValueError:
					raise Fail("addon wrote a line that is not JSON: %r" % line)

			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise Fail("addon said nothing for %gs" % timeout)

			if not self.selector.select(remaining):
				raise Fail("addon said nothing for %gs" % timeout)

			chunk = os.read(self.proc.stdout.fileno(), 4096)
			if not chunk:
				raise Fail("addon closed its stdout (exit %r)" % self.proc.poll())
			self.buffer += chunk

	def expect(self, cmd, timeout=READ_TIMEOUT):
		message = self.next_command(timeout)
		check(message.get("cmd") == cmd,
			"expected a %r command, got %r" % (cmd, message))
		return message

	def expect_after_progress(self, cmd, timeout=READ_TIMEOUT):
		"""The next non-progress command, keeping the progress screens seen.

		Anything else in between - an input box the addon should not have
		asked for, say - fails here rather than being skipped past.
		"""
		seen = []
		deadline = time.monotonic() + timeout

		while True:
			message = self.next_command(max(0.1, deadline - time.monotonic()))
			if message.get("cmd") == "progress":
				seen.append(message)
				continue
			check(message.get("cmd") == cmd,
				"expected %r after the progress screens, got %r" % (cmd, message))
			return message, seen

	def stop(self):
		try:
			if self.proc.poll() is None:
				self.proc.kill()
			self.proc.wait(timeout=5)
		except Exception:
			pass
		self.log_file.close()

	def stderr_tail(self, lines=25):
		self.log_file.flush()
		try:
			with open(self.log_path) as handle:
				return "".join(handle.readlines()[-lines:])
		except OSError:
			return ""

	def stderr_all(self):
		self.log_file.flush()
		try:
			with open(self.log_path) as handle:
				return handle.read()
		except OSError:
			return ""


def labels(message):
	return [item.get("label") for item in message["items"]]


def ids(message):
	return [item.get("id") for item in message["items"]]


def detail(message, label):
	for item in message["items"]:
		if item.get("label") == label:
			return item.get("detail")
	raise Fail("no row labelled %r in %r" % (label, labels(message)))


def refresh(addon):
	"""Re-draw the main list without changing anything.

	The empty-SSID path is a no-op with a screen at the end of it, which makes
	it the cheapest way to ask the addon to look at the world again.
	"""
	addon.send(event="select", id="hidden")
	addon.expect("input")
	addon.send(event="text", value="")
	return addon.expect("list")


def row_id(message, label):
	for item in message["items"]:
		if item.get("label") == label:
			return item.get("id")
	raise Fail("no row labelled %r in %r" % (label, labels(message)))


# ------------------------------------------------------------------ steps

STEPS = []
SEEN = {}       # screens a later step needs to point at a row from an earlier one


def step(title):
	def decorate(function):
		STEPS.append((title, function))
		return function
	return decorate


@step("main list: saved wifi profiles only, the connected one marked")
def step_main_list(addon, state_path, systemctl_path):
	addon.send(event="start")
	main = addon.expect("list")

	check(main["title"] == "INTERNET", "title was %r" % main["title"])
	check(labels(main) == ["Connected: %s" % HOME_NET, "SSH", HOME_NET, LEFTOVER,
			"Search for networks...", "Join hidden network..."],
		"rows were %r" % labels(main))

	# wlan0 and eth0 are BOTH up in the fixture: the wifi one wins
	check(ids(main)[0] == "status", "the status row is not first: %r" % ids(main))
	check(detail(main, "Connected: %s" % HOME_NET) == WIFI_IP,
		"the status row shows %r" % detail(main, "Connected: %s" % HOME_NET))

	check(ids(main)[1] == "ssh", "the SSH row is not second: %r" % ids(main))
	check(detail(main, "SSH") == "Enabled", "SSH is %r" % detail(main, "SSH"))

	check(detail(main, HOME_NET) == "Connected",
		"the active network is %r" % detail(main, HOME_NET))
	check(detail(main, LEFTOVER) == "Saved",
		"the idle profile is %r" % detail(main, LEFTOVER))

	blob = json.dumps(main)
	check("Wired connection 1" not in blob, "the ethernet connection was listed")
	check('"lo"' not in blob, "the loopback connection was listed")
	check(ids(main)[-2:] == ["scan", "hidden"],
		"the action rows were %r" % ids(main))


@step("the status row opens the details: address, MAC and gateway")
def step_status_details(addon, state_path, systemctl_path):
	addon.send(event="select", id="status")
	message = addon.expect("message")

	check(message["title"] == "CONNECTION", "title was %r" % message["title"])

	text = message["text"]
	for wanted in (HOME_NET, "wlan0", WIFI_IP, WIFI_MAC, GATEWAY):
		check(wanted in text, "%r is missing from %r" % (wanted, text))

	# the prefix length is not for reading, and only the FIRST of the two
	# addresses on the interface is the machine's
	check("/24" not in text, "the prefix length survived into %r" % text)
	check("192.168.68.201" not in text,
		"the second address was shown too: %r" % text)
	check("\\" not in text, "an nmcli escape survived into %r" % text)

	check("-t -f GENERAL.CONNECTION,GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY "
			"dev show wlan0" in log_of(state_path),
		"the details were not asked for in one terse query: %r" % log_of(state_path))

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(main["title"] == "INTERNET", "OK went to %r" % main["title"])


@step("SSH: the question is asked, and no means no")
def step_ssh_declined(addon, state_path, systemctl_path):
	before = read_state(systemctl_path)["units"]["ssh"]["active"]
	check(before is True, "the fixture does not start with SSH running")

	addon.send(event="select", id="ssh")
	question = addon.expect("confirm")
	check(question["text"]
			== "Disable SSH? Remote access to this system will stop working.",
		"the question was %r" % question["text"])

	addon.send(event="confirm", value=False)
	main = addon.expect("list")
	check(detail(main, "SSH") == "Enabled",
		"declining changed the row to %r" % detail(main, "SSH"))
	check(not any(entry.startswith("disable") for entry in log_of(systemctl_path)),
		"declining ran systemctl anyway: %r" % log_of(systemctl_path))


@step("SSH: disabling it flips the unit and the row, and inactive is not an error")
def step_ssh_disable(addon, state_path, systemctl_path):
	addon.send(event="select", id="ssh")
	addon.expect("confirm")

	addon.send(event="confirm", value=True)
	main, progress = addon.expect_after_progress("list")

	check(progress and progress[0]["title"] == "SSH",
		"no SSH progress screen, got %r" % progress)
	check("disable --now ssh" in log_of(systemctl_path),
		"systemctl was not asked to disable the unit: %r" % log_of(systemctl_path))
	check(read_state(systemctl_path)["units"]["ssh"]["active"] is False,
		"the unit is still running")

	# is-active exits 3 for an inactive unit; the row has to read the WORD
	check(detail(main, "SSH") == "Disabled",
		"a stopped unit reads as %r - is-active's exit status was treated as a "
		"failure" % detail(main, "SSH"))
	check(main["title"] == "INTERNET", "we did not land on the main list: %r" % main)


@step("SSH: enabling it again")
def step_ssh_enable(addon, state_path, systemctl_path):
	addon.send(event="select", id="ssh")
	question = addon.expect("confirm")
	check(question["text"] == "Enable SSH? This allows remote logins to this system.",
		"the question was %r" % question["text"])

	addon.send(event="confirm", value=True)
	main, _ = addon.expect_after_progress("list")

	check("enable --now ssh" in log_of(systemctl_path),
		"systemctl was not asked to enable the unit: %r" % log_of(systemctl_path))
	check(read_state(systemctl_path)["units"]["ssh"]["active"] is True,
		"the unit is not running")
	check(detail(main, "SSH") == "Enabled", "SSH reads %r" % detail(main, "SSH"))


@step("SSH: a systemctl that will not answer is Unknown, not a crash")
def step_ssh_unknown(addon, state_path, systemctl_path):
	patch_state(systemctl_path, silent=True)

	main = refresh(addon)
	check(detail(main, "SSH") == "Unknown", "SSH reads %r" % detail(main, "SSH"))

	addon.send(event="select", id="ssh")
	message = addon.expect("message")
	check(message["title"] == "SSH", "title was %r" % message["title"])
	check("systemctl" in message["text"], "message was %r" % message["text"])

	addon.send(event="confirm", value=True)
	addon.expect("list")

	patch_state(systemctl_path, silent=False)
	main = refresh(addon)
	check(detail(main, "SSH") == "Enabled",
		"SSH did not come back: %r" % detail(main, "SSH"))


@step("scan: duplicates collapsed, hidden row dropped, escaped colon put back")
def step_scan(addon, state_path, systemctl_path):
	addon.send(event="select", id="scan")
	found, progress = addon.expect_after_progress("list")

	check(progress and progress[0]["title"] == "SCANNING",
		"no SCANNING progress screen, got %r" % progress)
	check("Scanning for networks" in progress[0]["text"],
		"scan text was %r" % progress[0]["text"])

	check(found["title"] == "NETWORKS", "title was %r" % found["title"])
	check(labels(found) == [HOME_NET, NEIGHBOUR, BAD_NET, CAFE, OPEN_NET],
		"the picker was %r" % labels(found))

	# the two home rows collapse to one, at the STRONGER signal, and it is
	# still the connected one even though the '*' was on the weaker row
	check(detail(found, HOME_NET) == "100%  WPA2  (connected)",
		"the home row is %r" % detail(found, HOME_NET))
	check(detail(found, NEIGHBOUR) == "81%  WPA2",
		"the neighbour row is %r" % detail(found, NEIGHBOUR))
	check(detail(found, CAFE) == "64%  open",
		"the open row is %r" % detail(found, CAFE))
	check(detail(found, OPEN_NET) == "58%  open",
		"a '--' security is not open: %r" % detail(found, OPEN_NET))

	check("" not in labels(found), "the nameless (hidden) network was listed")
	check(not any("\\" in label for label in labels(found)),
		"an nmcli escape survived into a label: %r" % labels(found))

	SEEN["picker"] = found

	log = log_of(state_path)
	check("dev wifi rescan" in log, "nmcli was never asked to rescan")
	check("-t -f SSID,SIGNAL,SECURITY,IN-USE dev wifi list" in log,
		"the scan was not the terse four-field list: %r" % log)


@step("picking the network we are already on says so")
def step_already_connected(addon, state_path, systemctl_path):
	addon.send(event="select", id=row_id(SEEN["picker"], HOME_NET))
	message = addon.expect("message")

	check(message["title"] == "ALREADY CONNECTED", "title was %r" % message["title"])
	check("Already connected" in message["text"] and HOME_NET in message["text"],
		"message was %r" % message["text"])

	addon.send(event="confirm", value=True)
	back = addon.expect("list")
	check(back["title"] == "NETWORKS",
		"OK on 'already connected' went to %r" % back["title"])


@step("an open network is joined without ever asking for a password")
def step_open_network(addon, state_path, systemctl_path):
	addon.send(event="select", id=row_id(SEEN["picker"], OPEN_NET))
	main, progress = addon.expect_after_progress("list")

	check(progress and progress[0]["title"] == "CONNECTING",
		"no CONNECTING progress screen, got %r" % progress)
	check(OPEN_NET in progress[0]["text"], "progress said %r" % progress[0]["text"])

	check("dev wifi connect %s" % OPEN_NET in log_of(state_path),
		"nmcli was not asked to join the open network plainly: %r"
		% log_of(state_path))
	check(not any(entry.startswith("dev wifi connect %s password" % OPEN_NET)
			for entry in log_of(state_path)),
		"an open network was joined with a password")

	check(main["title"] == "INTERNET", "we did not land on the main list: %r" % main)
	check(detail(main, OPEN_NET) == "Connected",
		"the open network is %r" % detail(main, OPEN_NET))


@step("a secured network asks for a password, and joins with it")
def step_secured_network(addon, state_path, systemctl_path):
	addon.send(event="select", id="scan")
	found, _ = addon.expect_after_progress("list")

	addon.send(event="select", id=row_id(found, NEIGHBOUR))
	box = addon.expect("input")
	check(box["title"] == "Password for %s" % NEIGHBOUR,
		"the password box is titled %r" % box["title"])
	check(box.get("value") == "", "the password box was pre-filled: %r" % box)

	addon.send(event="text", value="letmein")
	main, progress = addon.expect_after_progress("list")

	check(progress and progress[0]["title"] == "CONNECTING",
		"no CONNECTING progress screen, got %r" % progress)
	check("dev wifi connect %s password ***" % NEIGHBOUR in log_of(state_path),
		"the password was not passed to nmcli: %r" % log_of(state_path))

	check(main["title"] == "INTERNET", "we did not land on the main list: %r" % main)
	check(detail(main, NEIGHBOUR) == "Connected",
		"the new network is %r" % detail(main, NEIGHBOUR))


@step("a wrong password: the half-created profile is deleted, and it asks again")
def step_wrong_password(addon, state_path, systemctl_path):
	addon.send(event="select", id="scan")
	found, _ = addon.expect_after_progress("list")

	check(BAD_NET not in connection_names(state_path),
		"the fixture already has a profile for %s" % BAD_NET)

	addon.send(event="select", id=row_id(found, BAD_NET))
	addon.expect("input")

	addon.send(event="text", value="not-the-password")
	message, _ = addon.expect_after_progress("message")

	check(message["title"] == "NOT CONNECTED", "title was %r" % message["title"])
	check("Wrong password?" in message["text"], "message was %r" % message["text"])

	check("connection delete id %s" % BAD_NET in log_of(state_path),
		"the profile NetworkManager left behind was not deleted: %r"
		% log_of(state_path))
	check(BAD_NET not in connection_names(state_path),
		"the half-created profile survived: %r" % connection_names(state_path))

	attempts = count(state_path, "dev wifi connect %s password ***" % BAD_NET)

	addon.send(event="confirm", value=True)
	retry = addon.expect("input")
	check(retry["title"] == "Password for %s" % BAD_NET,
		"the retry box is titled %r" % retry["title"])

	# back out of the retry: nothing more is attempted, and the picker returns
	addon.send(event="back")
	back = addon.expect("list")
	check(back["title"] == "NETWORKS",
		"back from the password box went to %r" % back["title"])
	check(count(state_path, "dev wifi connect %s password ***" % BAD_NET) == attempts,
		"backing out of the password box tried to connect anyway")


@step("hidden network: a failure leaves a PRE-EXISTING profile alone")
def step_hidden_failure(addon, state_path, systemctl_path):
	addon.send(event="back")
	addon.expect("list")

	addon.send(event="select", id="hidden")
	box = addon.expect("input")
	check(box["title"] == "Network name (SSID)", "the SSID box is titled %r" % box["title"])

	addon.send(event="text", value=LEFTOVER)
	addon.expect("input")

	deletes = count(state_path, "connection delete id %s" % LEFTOVER)

	addon.send(event="text", value="still-wrong")
	message, _ = addon.expect_after_progress("message")

	check(message["title"] == "NOT CONNECTED", "title was %r" % message["title"])
	check("dev wifi connect %s password *** hidden yes" % LEFTOVER
			in log_of(state_path),
		"the hidden join did not carry 'hidden yes': %r" % log_of(state_path))

	check(count(state_path, "connection delete id %s" % LEFTOVER) == deletes,
		"a failed attempt deleted a profile that was already there")
	check(LEFTOVER in connection_names(state_path),
		"the pre-existing profile is gone: %r" % connection_names(state_path))

	addon.send(event="confirm", value=True)
	addon.expect("input")           # the retry box

	addon.send(event="back")
	main = addon.expect("list")
	check(main["title"] == "INTERNET",
		"back from a hidden network's password box went to %r" % main["title"])


@step("hidden network: name, password, 'hidden yes', connected")
def step_hidden_success(addon, state_path, systemctl_path):
	addon.send(event="select", id="hidden")
	addon.expect("input")

	addon.send(event="text", value=HIDDEN_NET)
	addon.expect("input")

	addon.send(event="text", value="hunter2")
	main, progress = addon.expect_after_progress("list")

	check(progress and progress[0]["title"] == "CONNECTING",
		"no CONNECTING progress screen, got %r" % progress)
	check("dev wifi connect %s password *** hidden yes" % HIDDEN_NET
			in log_of(state_path),
		"the hidden join argv was wrong: %r" % log_of(state_path))

	check(main["title"] == "INTERNET", "we did not land on the main list: %r" % main)
	check(detail(main, HIDDEN_NET) == "Connected",
		"the hidden network is %r" % detail(main, HIDDEN_NET))


@step("an empty SSID for a hidden network just goes back")
def step_hidden_empty(addon, state_path, systemctl_path):
	addon.send(event="select", id="hidden")
	addon.expect("input")

	addon.send(event="text", value="   ")
	main = addon.expect("list")
	check(main["title"] == "INTERNET", "an empty SSID went to %r" % main["title"])


@step("a saved network's menu: connect, disconnect, and forget with its question")
def step_saved_menu(addon, state_path, systemctl_path):
	addon.send(event="select", id="saved:" + LEFTOVER)
	menu = addon.expect("list")
	check(menu["title"] == LEFTOVER, "title was %r" % menu["title"])
	check(ids(menu) == ["up", "forget"],
		"an idle profile offered %r" % ids(menu))

	addon.send(event="select", id="up")
	main, progress = addon.expect_after_progress("list")
	check(progress and progress[0]["title"] == "CONNECTING",
		"no CONNECTING progress screen, got %r" % progress)
	check("connection up id %s" % LEFTOVER in log_of(state_path),
		"a saved profile was not brought up by name: %r" % log_of(state_path))
	check(detail(main, LEFTOVER) == "Connected",
		"the saved profile is %r" % detail(main, LEFTOVER))

	addon.send(event="select", id="saved:" + LEFTOVER)
	menu = addon.expect("list")
	check(ids(menu) == ["down", "forget"],
		"a connected profile offered %r" % ids(menu))

	addon.send(event="select", id="down")
	main, progress = addon.expect_after_progress("list")
	check(progress and progress[0]["title"] == "DISCONNECTING",
		"no DISCONNECTING progress screen, got %r" % progress)
	check("connection down id %s" % LEFTOVER in log_of(state_path),
		"nmcli was never asked to disconnect: %r" % log_of(state_path))
	check(detail(main, LEFTOVER) == "Saved",
		"the profile is still %r" % detail(main, LEFTOVER))

	addon.send(event="select", id="saved:" + LEFTOVER)
	addon.expect("list")
	addon.send(event="select", id="forget")
	question = addon.expect("confirm")
	check("Forget %s?" % LEFTOVER in question["text"]
			and "password will be deleted" in question["text"],
		"the question was %r" % question["text"])

	addon.send(event="confirm", value=False)
	back = addon.expect("list")
	check(back["title"] == LEFTOVER, "no went to %r" % back["title"])

	addon.send(event="select", id="forget")
	addon.expect("confirm")
	addon.send(event="confirm", value=True)
	main, progress = addon.expect_after_progress("list")
	check(progress and progress[0]["title"] == "FORGETTING",
		"no FORGETTING progress screen, got %r" % progress)
	check("connection delete id %s" % LEFTOVER in log_of(state_path),
		"nmcli was never asked to delete the profile")
	check(LEFTOVER not in labels(main),
		"the forgotten profile is still listed: %r" % labels(main))


@step("the status row follows the cable, and says so when nothing is up")
def step_status_fallbacks(addon, state_path, systemctl_path):
	was = devices_of(state_path)

	# loopback stays up, because on a real machine it always is: "nothing is
	# connected" has to mean nothing a person could have joined
	set_devices(state_path, {"lo": "lo"})
	main = refresh(addon)
	check(labels(main)[0] == "Not connected",
		"with only loopback up the status row reads %r" % labels(main)[0])

	addon.send(event="select", id="status")
	message = addon.expect("message")
	check(message["title"] == "NOT CONNECTED", "title was %r" % message["title"])
	check("not on a network" in message["text"], "message was %r" % message["text"])

	addon.send(event="confirm", value=True)
	addon.expect("list")

	# the cable, with loopback still up alongside it and still not an answer
	set_devices(state_path, {"lo": "lo", WIRED_NAME: "eth0"})
	main = refresh(addon)

	check(labels(main)[0] == "Connected: %s" % WIRED_NAME,
		"with only the cable up the status row reads %r" % labels(main)[0])
	check(detail(main, "Connected: %s" % WIRED_NAME) == WIRED_IP,
		"the wired row shows %r" % detail(main, "Connected: %s" % WIRED_NAME))

	addon.send(event="select", id="status")
	message = addon.expect("message")
	for wanted in (WIRED_NAME, "eth0", WIRED_IP, WIRED_MAC):
		check(wanted in message["text"],
			"%r is missing from %r" % (wanted, message["text"]))

	addon.send(event="confirm", value=True)
	addon.expect("list")

	set_devices(state_path, was)
	refresh(addon)


@step("a refused rescan is not fatal, but an empty air is a message")
def step_rescan_refused(addon, state_path, systemctl_path):
	patch_state(state_path, rescan_fails=True)

	addon.send(event="select", id="scan")
	found, _ = addon.expect_after_progress("list")
	check(found["title"] == "NETWORKS",
		"a refused rescan lost the cached list: %r" % found)

	addon.send(event="back")
	addon.expect("list")

	kept = read_state(state_path)["networks"]
	patch_state(state_path, networks=[])

	addon.send(event="select", id="scan")
	message, _ = addon.expect_after_progress("message")
	check("Scanning not allowed" in message["text"],
		"nmcli's own words are missing from %r" % message["text"])

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(main["title"] == "INTERNET", "OK went to %r" % main["title"])

	patch_state(state_path, networks=kept, rescan_fails=False)


@step("back during a rescan cancels it and returns to the main list")
def step_rescan_cancel(addon, state_path, systemctl_path):
	patch_state(state_path, rescan_delay=6.0)

	addon.send(event="select", id="scan")
	first = addon.expect("progress")
	check(first["title"] == "SCANNING", "first screen was %r" % first)

	time.sleep(0.4)
	started = time.monotonic()
	addon.send(event="back")

	main, _ = addon.expect_after_progress("list", timeout=20.0)
	took = time.monotonic() - started

	check(main["title"] == "INTERNET", "cancelling went to %r" % main["title"])
	check(took < 5.0, "cancelling took %.1fs, the rescan was not cut short" % took)

	patch_state(state_path, rescan_delay=0.0)


@step("no password ever reaches the log")
def step_no_password_logged(addon, state_path, systemctl_path):
	noise = addon.stderr_all() + json.dumps(log_of(state_path))
	for secret in ("letmein", "hunter2", "not-the-password", "still-wrong"):
		check(secret not in noise, "%r was written to a log" % secret)


@step("back on the main list closes the addon, with status 0")
def step_close(addon, state_path, systemctl_path):
	addon.send(event="back")
	closing = addon.expect("close")
	check(closing["cmd"] == "close", "got %r" % closing)

	addon.proc.stdin.close()
	code = addon.proc.wait(timeout=10)
	check(code == 0, "the addon exited with status %r" % code)


# ------------------------------------------------------------------ driver

def run_walk(state_path, nmcli, systemctl_path, systemctl):
	addon = Addon(state_path, nmcli, systemctl_path, systemctl)
	failures = 0

	try:
		for title, function in STEPS:
			try:
				function(addon, state_path, systemctl_path)
				print("PASS  %s" % title)
			except Fail as error:
				print("FAIL  %s\n        %s" % (title, error))
				print("      addon stderr:\n%s" % addon.stderr_tail())
				failures += 1
				break
			except Exception as error:
				print("FAIL  %s\n        unexpected %s: %s"
					% (title, type(error).__name__, error))
				print("      addon stderr:\n%s" % addon.stderr_tail())
				failures += 1
				break
	finally:
		addon.stop()

	return failures


def run_without_nmcli(state_path, work, systemctl_path, systemctl):
	missing = os.path.join(work, "there-is-no-nmcli-here")
	addon = Addon(state_path, missing, systemctl_path, systemctl)
	title = "no nmcli: one message, then close, with status 0"

	try:
		addon.send(event="start")
		message = addon.expect("message", timeout=15)
		check("nmcli" in message["text"], "message was %r" % message["text"])

		addon.send(event="confirm", value=True)
		addon.expect("close", timeout=15)

		addon.proc.stdin.close()
		code = addon.proc.wait(timeout=10)
		check(code == 0, "the addon exited with status %r" % code)

		print("PASS  %s" % title)
		return 0
	except Fail as error:
		print("FAIL  %s\n        %s" % (title, error))
		print("      addon stderr:\n%s" % addon.stderr_tail())
		return 1
	finally:
		addon.stop()


def main():
	work = tempfile.mkdtemp(prefix="es-wifi-test-")
	bin_dir = os.path.join(work, "bin")
	os.makedirs(bin_dir)

	nmcli = os.path.join(bin_dir, "nmcli")
	shutil.copy(MOCK, nmcli)
	os.chmod(nmcli, 0o755)

	systemctl = os.path.join(bin_dir, "systemctl")
	shutil.copy(MOCK_SYSTEMCTL, systemctl)
	os.chmod(systemctl, 0o755)

	state_path = os.path.join(work, "state.json")
	with open(state_path, "w") as handle:
		json.dump(SEED, handle, indent=2)

	systemctl_path = os.path.join(work, "systemctl-state.json")
	with open(systemctl_path, "w") as handle:
		json.dump(SYSTEMCTL_SEED, handle, indent=2)

	print("addon:  %s" % ADDON)
	print("mocks:  %s" % nmcli)
	print("        %s" % systemctl)
	print("state:  %s" % state_path)
	print("        %s\n" % systemctl_path)

	failures = run_walk(state_path, nmcli, systemctl_path, systemctl)
	failures += run_without_nmcli(state_path, work, systemctl_path, systemctl)

	print("")
	if failures:
		print("FAILED (%d step%s)" % (failures, "" if failures == 1 else "s"))
		print("working files kept in %s" % work)
		return 1

	print("OK - %d steps passed" % (len(STEPS) + 1))
	shutil.rmtree(work, ignore_errors=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
