#!/usr/bin/env python3
"""Bluetooth pairing for EmulationStation, as an addon.

An addon is a headless program.  It never draws anything and never reads the
terminal: EmulationStation spawns it with pipes on its stdin and stdout, the
addon asks for screens by writing one JSON object per line to stdout, and ES
reports what the user did by writing one JSON object per line to the addon's
stdin.  The protocol is ADDONS.md in the ES source tree; addons-examples/stub
is its reference implementation and the idioms here follow it.

The screen is deliberately plain.  A game console's Bluetooth menu exists to
get a controller working, so:

  * known controllers are listed one per row, everything else collapses into a
    single "Other paired devices" row;
  * "Search for new devices..." scans, and the first controller it finds is
    paired, trusted and connected without asking anything.  A user holding the
    sync button on a pad wants exactly that and nothing else.

Everything this addon knows about Bluetooth it learns from bluetoothctl:

  * one-shot commands - devices, info, pair, trust, connect, disconnect,
    remove - are run non-interactively with a timeout;
  * scanning needs a session that stays alive, so a bluetoothctl is spawned
    with pipes, told "scan on", and its output read incrementally while the
    addon keeps servicing its OWN stdin, because ES delivers "back" during a
    progress screen and here "back" means cancel the scan.

A bluetoothctl that fails, times out or is missing is an outcome to report in
a message box, never a traceback.

Environment knobs, for testing:

  ES_BT_SCAN_SECONDS  how long a scan runs (default 45)
"""

import json
import os
import re
import pty
import selectors
import shutil
import subprocess
import sys
import time


# ---------------------------------------------------------------- settings

SCAN_SECONDS = float(os.environ.get("ES_BT_SCAN_SECONDS", "45"))
PROGRESS_INTERVAL = 2.0

QUICK_TIMEOUT = 10.0     # devices, info
ACTION_TIMEOUT = 30.0    # connect, disconnect, trust, remove
PAIR_TIMEOUT = 45.0      # pair, which waits on the other end of the radio
SESSION_STOP_TIMEOUT = 5.0

BTCTL = shutil.which("bluetoothctl")


# ---------------------------------------------------------------- parsing

# interactive bluetoothctl colours its output and redraws its prompt, so every
# line is stripped of escape sequences and of a leading "[bluetooth]# " before
# anything looks at it
ANSI = re.compile(
	r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]"      # CSI ... final byte
	r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC ... BEL / ST
	r"|[()][A-Za-z0-9]"                  # charset selection
	r"|[=>NOM78])"                       # the short ones
)
PROMPT = re.compile(r"^\[[^\]]*\]#\s*")

MAC = r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
DEVICE_LINE = re.compile(r"^Device\s+" + MAC + r"\s*(.*)$")
SCAN_LINE = re.compile(r"\[(NEW|CHG)\]\s+Device\s+" + MAC + r"\s*(.*)$")

# a nonzero exit is not the only way bluetoothctl says no, and a zero exit is
# not proof it said yes, so the text is read as well
FAILURE_MARKERS = (
	"failed to",
	"not available",
	"org.bluez.error",
	"invalid command",
	"no default controller",
	"missing device address",
)
SUCCESS_MARKERS = (
	"successful",
	"succeeded",
	"has been removed",
)

CONTROLLER_ICON = "input-gaming"

# Class of Device: the peripheral major class, and the two minor values that
# mean a thing with a stick on it
PERIPHERAL_MAJOR = 0x05
GAMEPAD_MINORS = (0x01, 0x02)   # joystick, gamepad


def clean(line):
	"""One line of bluetoothctl output, without the decoration.

	\x01 and \x02 are readline's invisible-text markers, wrapped around the
	colors when bluetoothctl talks to a terminal - measured on the Pi, they
	survive ANSI stripping and turn [NEW] into [\x01\x02NEW\x01\x02].
	"""
	line = line.replace("\x01", "").replace("\x02", "")
	return PROMPT.sub("", ANSI.sub("", line)).strip()


def is_placeholder_name(name, mac):
	"""True for BlueZ's stand-in name for a device that has not told us one.

	It is the MAC with dashes, and it is noise in a list of things to pair.
	"""
	if not name:
		return True
	return name.strip().replace("-", ":").upper() == mac.upper()


def parse_class(text):
	if not text:
		return None
	try:
		return int(text.strip().split()[0], 16 if "x" in text.lower() else 10)
	except (ValueError, IndexError):
		return None


def class_is_controller(value):
	if value is None:
		return False
	if (value >> 8) & 0x1F != PERIPHERAL_MAJOR:
		return False
	return ((value >> 2) & 0x0F) in GAMEPAD_MINORS


def classify(icon, class_text):
	"""'controller', 'other', or None when the device has not said yet.

	The icon is BlueZ's own answer and wins; the class of device is the
	fallback for a device whose icon has not turned up.
	"""
	if icon:
		return "controller" if icon.strip().lower() == CONTROLLER_ICON else "other"

	value = parse_class(class_text)
	if value is not None:
		return "controller" if class_is_controller(value) else "other"

	return None


# ---------------------------------------------------------------- ES pipes

STDIN_FD = sys.stdin.fileno()

pending = b""   # stdin arrives as a byte stream: the tail waits for its newline

_stdin_sel = selectors.DefaultSelector()
_stdin_sel.register(STDIN_FD, selectors.EVENT_READ)


def send(**command):
	"""Ask ES for a screen.  One JSON object, one line, flushed immediately."""
	print(json.dumps(command), flush=True)


def log(message):
	sys.stderr.write("bluetooth addon: %s\n" % message)
	sys.stderr.flush()


def feed(chunk):
	global pending
	if not chunk:
		raise EOFError("ES closed our stdin")
	pending += chunk


def pop_event():
	"""The next complete event already in the buffer, or None."""
	global pending

	while True:
		newline = pending.find(b"\n")
		if newline < 0:
			return None

		line = pending[:newline]
		pending = pending[newline + 1:]

		line = line.decode("utf-8", "replace").strip()
		if not line:
			continue

		try:
			return json.loads(line)
		except ValueError:
			log("could not parse %r" % line)


def fill(timeout):
	"""Wait up to timeout for stdin and read a chunk.  False if it ran out.

	timeout None blocks.  EOFError means ES has closed our stdin, which is how
	an addon is told to stop.
	"""
	if not _stdin_sel.select(timeout):
		return False
	feed(os.read(STDIN_FD, 4096))
	return True


def read_event(timeout=None):
	"""Next event from ES, or None if timeout (in seconds) ran out first."""
	deadline = None if timeout is None else time.monotonic() + timeout

	while True:
		event = pop_event()
		if event is not None:
			return event

		remaining = None
		if deadline is not None:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				return None

		if not fill(remaining):
			return None


def drain_events():
	"""Throw away whatever arrived while we were blocked on bluetoothctl.

	The only thing ES can send during a progress screen is "back", and a pair
	that has already started is not something to abandon halfway - but the
	events still have to be read, or the next screen would inherit them.
	"""
	while True:
		event = pop_event()
		if event is not None:
			log("ignoring %r received while busy" % event.get("event"))
			continue
		if not fill(0):
			return


# ---------------------------------------------------------- bluetoothctl

class Result(object):
	"""What one bluetoothctl command did, and what to tell the user if it failed."""

	def __init__(self, ok, lines, detail):
		self.ok = ok
		self.lines = lines
		self.detail = detail

	def __repr__(self):
		return "Result(ok=%r, detail=%r)" % (self.ok, self.detail)


def run_btctl(args, timeout):
	command = [BTCTL] + args

	try:
		done = subprocess.run(
			command,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			timeout=timeout,
			text=True,
			errors="replace",
		)
	except subprocess.TimeoutExpired:
		log("bluetoothctl %s timed out after %gs" % (" ".join(args), timeout))
		return Result(False, [], "bluetoothctl did not answer within %g seconds." % timeout)
	except OSError as error:
		log("could not run bluetoothctl %s: %s" % (" ".join(args), error))
		return Result(False, [], "Could not run bluetoothctl: %s" % error)

	lines = [line for line in (clean(l) for l in (done.stdout or "").splitlines()) if line]
	blob = "\n".join(lines).lower()

	failed = any(marker in blob for marker in FAILURE_MARKERS)
	succeeded = any(marker in blob for marker in SUCCESS_MARKERS)
	ok = (done.returncode == 0 or succeeded) and not failed

	detail = lines[-1] if lines else ("bluetoothctl exited with status %d." % done.returncode)

	log("bluetoothctl %s -> exit %d, ok=%s, %r"
		% (" ".join(args), done.returncode, ok, detail))

	return Result(ok, lines, detail)


def list_known_devices():
	"""Paired/known devices, newest bluetoothctl answer.  None if it failed."""
	# plain "devices" lists the whole cache - after a scan that is every gadget
	# in radio range, not just ours - so ask for the paired ones by name
	result = run_btctl(["devices", "Paired"], QUICK_TIMEOUT)
	if not result.ok:
		return None, result

	devices = []
	for line in result.lines:
		match = DEVICE_LINE.match(line)
		if not match:
			continue
		mac = match.group(1).upper()
		name = match.group(2).strip() or mac
		devices.append({"mac": mac, "name": name})

	return devices, result


def device_info(mac):
	"""bluetoothctl info, as a field dict.  Empty when it knows nothing."""
	result = run_btctl(["info", mac], QUICK_TIMEOUT)

	fields = {}
	for line in result.lines:
		if ":" not in line:
			continue
		key, value = line.split(":", 1)
		key = key.strip().lower()
		if key in ("device", "uuid"):
			continue    # the header line, and the UUID wall under it
		fields.setdefault(key, value.strip())

	return fields


def info_says_connected(fields):
	return fields.get("connected", "").strip().lower() == "yes"


# ------------------------------------------------------------ scan session

def start_scan_session():
	"""A bluetoothctl that stays alive so a scan can run inside it.

	It gets a pty, not pipes: bluetoothctl only flushes per line when it
	believes it is talking to a terminal - measured on the Pi, on a pipe its
	[NEW]/[CHG] lines sit in a buffer until exit, and stdbuf does not talk it
	out of that.  Commands go in and events come out through the master side;
	our own commands echo back too, and the parser just does not match them.
	"""
	master, slave = pty.openpty()
	try:
		proc = subprocess.Popen(
			[BTCTL],
			stdin=slave,
			stdout=slave,
			stderr=slave,
			env=dict(os.environ, TERM="dumb"),
			close_fds=True,
		)
	except Exception:
		os.close(master)
		os.close(slave)
		raise
	os.close(slave)
	proc.master_fd = master
	return proc


def session_write(proc, text):
	try:
		os.write(proc.master_fd, text.encode("utf-8"))
	except OSError as error:
		log("could not write %r to the scan session: %s" % (text.strip(), error))


def stop_scan_session(proc):
	"""scan off, exit, and then stop being polite about it."""
	session_write(proc, "scan off\n")
	session_write(proc, "exit\n")

	try:
		proc.wait(timeout=SESSION_STOP_TIMEOUT)
	except subprocess.TimeoutExpired:
		log("scan session ignored exit, terminating")
		proc.terminate()
		try:
			proc.wait(timeout=2)
		except subprocess.TimeoutExpired:
			log("scan session ignored SIGTERM, killing")
			proc.kill()
			try:
				proc.wait(timeout=2)
			except subprocess.TimeoutExpired:
				pass

	try:
		os.close(proc.master_fd)
	except OSError:
		pass


def note_scan_line(line, found, known_macs):
	"""Record what a [NEW]/[CHG] line says.  Returns the MAC it touched, or None."""
	match = SCAN_LINE.search(clean(line))
	if not match:
		return None

	kind, mac, tail = match.group(1), match.group(2).upper(), match.group(3).strip()

	if mac in known_macs:
		return None

	entry = found.get(mac)
	if entry is None:
		entry = found[mac] = {
			"mac": mac, "name": mac, "icon": None, "class": None,
			"kind": None, "queried": False, "last_info": 0.0,
		}

	if kind == "NEW":
		if tail and not is_placeholder_name(tail, mac):
			entry["name"] = tail
		elif entry["name"] == mac:
			entry["name"] = tail or mac
		return mac

	# [CHG] carries one property at a time; name, icon and class are recorded,
	# and any other property still counts as a sighting worth another look
	if ":" not in tail:
		return mac
	key, value = tail.split(":", 1)
	key, value = key.strip().lower(), value.strip()

	if key in ("name", "alias"):
		if value and not is_placeholder_name(value, mac):
			entry["name"] = value
		return mac
	if key == "icon":
		entry["icon"] = value
		return mac
	if key == "class":
		entry["class"] = value
		return mac

	return mac


def resolve_kind(entry):
	"""Decide whether a discovered device is a controller, asking if need be."""
	kind = classify(entry["icon"], entry["class"])

	now = time.monotonic()
	ask = False
	if kind is None and not entry["queried"]:
		# neither the icon nor the class has turned up in the scan output, so
		# ask BlueZ directly - it may already know both
		entry["queried"] = True
		ask = True
	elif kind == "controller" and is_placeholder_name(entry["name"], entry["mac"]) \
			and now - entry["last_info"] >= 2.0:
		# it is a controller and we want to put its real name on the pairing
		# screen - BlueZ often learns the name a moment after the class
		ask = True

	if ask:
		entry["last_info"] = now
		fields = device_info(entry["mac"])
		if fields:
			entry["icon"] = entry["icon"] or fields.get("icon")
			entry["class"] = entry["class"] or fields.get("class")
			name = fields.get("alias") or fields.get("name")
			if name and is_placeholder_name(entry["name"], entry["mac"]):
				entry["name"] = name
		kind = classify(entry["icon"], entry["class"])

	entry["kind"] = kind
	return kind


def named_found(found):
	"""Discovered devices worth showing: named, controllers first."""
	devices = [
		entry for entry in found.values()
		if not is_placeholder_name(entry["name"], entry["mac"])
	]
	devices.sort(key=lambda entry: (entry["kind"] != "controller", entry["name"].lower()))
	return devices


def run_scan(known_macs):
	"""Scan, servicing our own stdin throughout, until a controller turns up.

	Returns (devices, cancelled, error, controller).  controller is the first
	discovered device that is one, and the scan stops the moment it appears -
	pairing is what the user came here for.  error is a string when the
	session could not be started at all.
	"""
	try:
		proc = start_scan_session()
	except OSError as error:
		return [], False, "Could not start bluetoothctl: %s" % error, None

	found = {}
	controller = None
	cancelled = False
	eof = False
	buffer = b""

	selector = selectors.DefaultSelector()
	selector.register(STDIN_FD, selectors.EVENT_READ, "es")
	selector.register(proc.master_fd, selectors.EVENT_READ, "bt")
	bt_open = True

	started = time.monotonic()
	deadline = started + SCAN_SECONDS
	next_update = started + PROGRESS_INTERVAL

	send(cmd="progress", title="SCANNING",
		text="Put your controller in pairing mode...")

	session_write(proc, "scan on\n")

	try:
		while True:
			# whatever ES has already said
			while True:
				event = pop_event()
				if event is None:
					break
				if event.get("event") == "back":
					log("scan cancelled by the user")
					cancelled = True
				else:
					log("ignoring %r during the scan" % event.get("event"))

			now = time.monotonic()
			if cancelled or eof or now >= deadline:
				break

			touched = set()

			timeout = max(0.0, min(deadline, next_update) - now)
			for key, _ in selector.select(timeout):
				try:
					chunk = os.read(key.fd, 4096)
				except OSError:
					chunk = b""  # a pty master reads EIO, not EOF, when the child goes

				if key.data == "es":
					if not chunk:
						eof = True
						break
					feed(chunk)
					continue

				if not chunk:
					log("scan session closed its output")
					selector.unregister(key.fd)
					bt_open = False
					continue

				buffer += chunk
				# bluetoothctl ends lines with \n, and redraws with \r
				buffer = buffer.replace(b"\r", b"\n")
				while True:
					newline = buffer.find(b"\n")
					if newline < 0:
						break
					line = buffer[:newline].decode("utf-8", "replace")
					buffer = buffer[newline + 1:]
					already = set(found)
					mac = note_scan_line(line, found, known_macs)
					if mac:
						touched.add(mac)
						if mac not in already:
							log("discovered %r" % clean(line))

			for mac in touched:
				entry = found[mac]
				if resolve_kind(entry) != "controller":
					continue
				if is_placeholder_name(entry["name"], mac):
					continue    # a controller we cannot name yet: give it a moment
				controller = entry
				break

			if controller is not None:
				break

			if not bt_open and not eof:
				log("scan session is gone, ending the scan early")
				break

			now = time.monotonic()
			if now >= next_update and not cancelled:
				elapsed = int(now - started)
				count = len(named_found(found))
				send(cmd="progress", title="SCANNING",
					text="Put your controller in pairing mode.  %ds elapsed, %s."
						% (elapsed, "1 new device found" if count == 1
							else "%d new devices found" % count))
				while next_update <= now:
					next_update += PROGRESS_INTERVAL
	finally:
		selector.close()
		stop_scan_session(proc)

	if eof:
		raise EOFError("ES closed our stdin")

	if controller is None and not cancelled:
		# a controller whose name never arrived is still what the user is
		# holding - pairing it under its MAC beats saying nothing was found
		for entry in found.values():
			if entry.get("kind") == "controller":
				controller = entry
				break

	return named_found(found), cancelled, None, controller


# ---------------------------------------------------------------- screens

screen = "none"          # which screen we last asked ES for: it decides "back"
after_message = "main"   # where the OK of the current message goes
controllers = []         # known controllers, as of the last main list
others = []              # known everything-else, as of the last main list
found = []               # devices the last scan turned up
current = None           # the device whose menu / question is up
device_origin = "main"   # the list the current device menu was opened from


def find_known(mac):
	for device in controllers + others:
		if device["mac"] == mac:
			return device
	return None


def show_message(title, text, after="main"):
	global screen, after_message
	send(cmd="message", title=title, text=text)
	screen = "message"
	after_message = after


def row(device):
	return {
		"id": "dev:" + device["mac"],
		"label": device["name"],
		"detail": "Connected" if device["connected"] else "Not connected",
	}


def show_main_list():
	"""Controllers, one row for everything else, and the way to add a pad."""
	global screen, controllers, others

	devices, result = list_known_devices()
	if devices is None:
		show_message("BLUETOOTH ERROR",
			"Could not ask bluetoothctl for the known devices. %s" % result.detail,
			after="close")
		return

	for device in devices:
		fields = device_info(device["mac"])
		device["connected"] = info_says_connected(fields)
		device["kind"] = classify(fields.get("icon"), fields.get("class"))

	devices.sort(key=lambda device: device["name"].lower())
	controllers = [device for device in devices if device["kind"] == "controller"]
	others = [device for device in devices if device["kind"] != "controller"]

	items = [row(device) for device in controllers]

	if others:
		items.append({
			"id": "others",
			"label": "Other paired devices",
			"detail": "%d paired" % len(others),
		})

	items.append({
		"id": "scan",
		"label": "Search for new devices...",
		"detail": "pair a controller",
	})

	send(cmd="list", title="BLUETOOTH", items=items)
	screen = "main"


def show_other_list():
	global screen

	send(cmd="list", title="OTHER DEVICES", items=[row(device) for device in others])
	screen = "others"


def show_device_menu():
	global screen

	if current is None:
		show_main_list()
		return

	items = []
	if current.get("connected"):
		items.append({"id": "disconnect", "label": "Disconnect"})
	else:
		items.append({"id": "connect", "label": "Connect"})
	items.append({"id": "forget", "label": "Forget this device", "detail": current["mac"]})

	send(cmd="list", title=current["name"], items=items)
	screen = "device"


def show_found_list():
	global screen

	items = [
		{"id": "new:" + device["mac"], "label": device["name"], "detail": device["mac"]}
		for device in found
	]
	items.append({"id": "scan", "label": "Scan again", "detail": "%gs" % SCAN_SECONDS})

	send(cmd="list", title="FOUND DEVICES", items=items)
	screen = "found"


# ---------------------------------------------------------------- actions

def do_connect():
	global screen
	screen = "progress"

	send(cmd="progress", title="CONNECTING", text="Connecting to %s..." % current["name"])
	result = run_btctl(["connect", current["mac"]], ACTION_TIMEOUT)
	drain_events()

	if result.ok:
		show_message("CONNECTED", "%s is connected." % current["name"])
	else:
		show_message("NOT CONNECTED",
			"Could not connect to %s. %s" % (current["name"], result.detail))


def do_disconnect():
	global screen
	screen = "progress"

	send(cmd="progress", title="DISCONNECTING", text="Disconnecting %s..." % current["name"])
	result = run_btctl(["disconnect", current["mac"]], ACTION_TIMEOUT)
	drain_events()

	if result.ok:
		show_message("DISCONNECTED", "%s is disconnected." % current["name"])
	else:
		show_message("STILL CONNECTED",
			"Could not disconnect %s. %s" % (current["name"], result.detail))


def do_forget():
	global screen
	screen = "progress"

	send(cmd="progress", title="FORGETTING", text="Removing %s..." % current["name"])
	result = run_btctl(["remove", current["mac"]], ACTION_TIMEOUT)
	drain_events()

	if result.ok:
		show_message("FORGOTTEN",
			"%s has been removed. Pair it again to use it." % current["name"])
	else:
		show_message("NOT FORGOTTEN",
			"Could not remove %s. %s" % (current["name"], result.detail))


def do_pair(device, automatic):
	"""Pair, trust, then connect - and say which of the three said no.

	Once this has started it runs to its end: a half-paired device is worse
	than a slow menu, so "back" is read and dropped rather than obeyed.
	"""
	global screen
	screen = "progress"

	name = device["name"]
	mac = device["mac"]

	if automatic:
		steps = (
			("pair", "Found %s - pairing..." % name, ["pair", mac], PAIR_TIMEOUT),
			("trust", "Found %s - trusting..." % name, ["trust", mac], ACTION_TIMEOUT),
			("connect", "Found %s - connecting..." % name, ["connect", mac], ACTION_TIMEOUT),
		)
	else:
		steps = (
			("pair", "Pairing with %s..." % name, ["pair", mac], PAIR_TIMEOUT),
			("trust", "Trusting %s..." % name, ["trust", mac], ACTION_TIMEOUT),
			("connect", "Connecting to %s..." % name, ["connect", mac], ACTION_TIMEOUT),
		)

	for step, text, args, timeout in steps:
		send(cmd="progress", title="PAIRING", text=text)
		result = run_btctl(args, timeout)
		drain_events()

		if not result.ok:
			show_message("PAIRING FAILED",
				"Could not set up %s: the %s step failed. %s" % (name, step, result.detail))
			return

	show_message("CONNECTED", "%s is paired and connected." % name)


def do_scan():
	global found, screen, current

	screen = "progress"

	known_macs = {device["mac"] for device in controllers + others}
	devices, cancelled, error, controller = run_scan(known_macs)

	if error:
		show_message("SCAN FAILED", error)
		return

	if cancelled:
		show_main_list()
		return

	found = devices

	if controller is not None:
		# what the user came for: pair it, no questions
		current = dict(controller)
		do_pair(current, automatic=True)
		return

	if not found:
		show_message("NOTHING FOUND",
			"No new devices answered. Hold the pad's sync button until its "
			"lights are flashing, then search again.")
		return

	show_found_list()


# ---------------------------------------------------------------- events

def on_select(item_id):
	global current, device_origin, screen

	if item_id == "scan":
		do_scan()

	elif item_id == "others":
		show_other_list()

	elif item_id.startswith("dev:"):
		device = find_known(item_id[4:])
		if device is None:
			log("no such known device %r" % item_id)
			show_main_list()
			return
		current = device
		device_origin = "others" if screen == "others" else "main"
		show_device_menu()

	elif item_id.startswith("new:"):
		mac = item_id[4:]
		for device in found:
			if device["mac"] == mac:
				current = dict(device)
				send(cmd="confirm", title="PAIR?", text="Pair with %s?" % device["name"])
				screen = "confirm_pair"
				return
		log("no such discovered device %r" % item_id)
		show_found_list()

	elif item_id == "connect":
		do_connect()

	elif item_id == "disconnect":
		do_disconnect()

	elif item_id == "forget":
		send(cmd="confirm", title="FORGET?",
			text="Remove %s? You will need to pair it again." % current["name"])
		screen = "confirm_forget"

	else:
		log("unknown item id %r" % item_id)
		show_main_list()


def on_confirm(value):
	if screen == "confirm_forget":
		if value:
			do_forget()
		else:
			show_device_menu()

	elif screen == "confirm_pair":
		if value:
			do_pair(current, automatic=False)
		else:
			show_found_list()

	elif screen == "message":
		# the OK of a message: it says nothing except where to go next
		if after_message == "close":
			send(cmd="close")
			sys.exit(0)
		show_main_list()

	else:
		log("confirm on the %r screen" % screen)
		show_main_list()


def on_back():
	# The addon owns navigation.  B on the main list means we are done; B
	# anywhere else means the screen before it.
	if screen == "main":
		send(cmd="close")
		sys.exit(0)

	if screen == "confirm_forget":
		show_device_menu()
	elif screen == "confirm_pair":
		show_found_list()
	elif screen == "message":
		on_confirm(True)
	elif screen == "device":
		if device_origin == "others" and others:
			show_other_list()
		else:
			show_main_list()
	else:
		show_main_list()


def main():
	while True:
		try:
			event = read_event()
			if event is None:
				continue

			name = event.get("event")

			if name == "start":
				if BTCTL is None:
					log("bluetoothctl is not installed")
					show_message("BLUETOOTH UNAVAILABLE",
						"bluetoothctl was not found on this system, so this "
						"addon cannot manage Bluetooth devices. Install BlueZ "
						"(sudo apt install bluez) and try again.",
						after="close")
				else:
					log("using %s" % BTCTL)
					show_main_list()
			elif name == "select":
				on_select(event.get("id", ""))
			elif name == "confirm":
				on_confirm(bool(event.get("value")))
			elif name == "back":
				on_back()
			elif name == "text":
				log("unexpected text event: this addon never asks for input")
			else:
				log("unknown event %r" % name)

		except EOFError:
			# ES has gone away, and every screen we could ask for went with it
			log("ES closed our stdin, stopping")
			return 0


if __name__ == "__main__":
	sys.exit(main())
