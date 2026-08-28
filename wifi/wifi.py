#!/usr/bin/env python3
"""WiFi for EmulationStation, as an addon.

An addon is a headless program.  It never draws anything and never reads the
terminal: EmulationStation spawns it with pipes on its stdin and stdout, the
addon asks for screens by writing one JSON object per line to stdout, and ES
reports what the user did by writing one JSON object per line to the addon's
stdin.  The protocol is ADDONS.md in the ES source tree; addons-examples/stub
is its reference implementation and the idioms here follow it, as does its
sibling in this repository, bluetooth/bluetooth.py.

The screen is deliberately plain.  A game console's network menu exists to get
the machine online, so:

  * the top row answers "am I online, and as what" - the connection's name and
    its IP address, and the whole of what nmcli knows about the interface if
    you pick it.  It follows the ethernet cable too: a box plugged into the
    router is online, and a screen that said "Not connected" because wlan0 is
    idle would be lying;
  * the saved networks are the main list - the one you are on says so, and
    picking one offers connect, disconnect, forget and nothing else;
  * "Search for networks..." scans and shows what is in the air, strongest
    first, and picking one either brings up its saved profile or asks for the
    password;
  * "Join hidden network..." is the same thing with the name typed in;
  * SSH is on the same screen because it is the same question - "can I reach
    this box from my desk" - and it is the one thing a person wants to switch
    on the moment they have an IP address to type.

Everything this addon knows about WiFi it learns from nmcli, and every call
goes through nmcli_argv(), for two reasons:

  * on the target machine (RetroPie on a Pi 5, running as "pi") polkit refuses
    NetworkManager operations - even a scan - from a session it does not think
    is local, so every call is "sudo -n nmcli ...".  Passwordless sudo is what
    makes the addon work at all; there is no interactive fallback, because
    there is no terminal to type a password into;
  * ES_WIFI_NMCLI replaces the whole prefix with one binary and no sudo, which
    is how the tests put a mock in the way.

nmcli's terse output (-t) escapes ':' as '\\:' and '\\' as '\\\\' inside every
field, so a network called "Cafe: Guest" arrives as "Cafe\\: Guest" and a naive
split(':') tears it in half.  split_terse() splits on unescaped colons only.

An nmcli that fails, times out or is missing is an outcome to report in a
message box, never a traceback.  A password is never written to stderr: the
log line for a command is redacted, which matters because run.sh appends
stderr to a file that outlives the session.

systemctl, which owns the SSH switch, is reached the same way and for the same
reason: systemctl_argv(), "sudo -n systemctl ...", one override.

Environment knobs, for testing:

  ES_WIFI_NMCLI      a program to run instead of "sudo -n nmcli"
  ES_WIFI_SYSTEMCTL  a program to run instead of "sudo -n systemctl"
"""

import json
import os
import selectors
import shutil
import subprocess
import sys
import time


# ---------------------------------------------------------------- settings

NMCLI_OVERRIDE = os.environ.get("ES_WIFI_NMCLI", "")
SYSTEMCTL_OVERRIDE = os.environ.get("ES_WIFI_SYSTEMCTL", "")

WIFI_TYPE = "802-11-wireless"
SSH_UNIT = "ssh"         # RetroPie's unit name; Fedora and friends call it sshd
MAX_NETWORKS = 30        # a picker nobody can scroll is not a picker

QUICK_TIMEOUT = 15.0     # connection show, wifi list, connection delete
RESCAN_TIMEOUT = 20.0    # dev wifi rescan
ACTION_TIMEOUT = 45.0    # connection up / down
CONNECT_TIMEOUT = 60.0   # dev wifi connect, which waits on a DHCP lease
CANCEL_POLL = 0.2        # how often a cancellable command looks at our stdin

# nmcli says "wrong password" in several voices; all of them mean the same
# thing to somebody standing in front of a television
SECRET_MARKERS = (
	"secrets were required",
	"no secrets provided",
	"802-1x supplicant",
)


# ---------------------------------------------------------------- parsing

def split_terse(line):
	"""One line of nmcli -t output, as fields.

	Splits on unescaped colons and unescapes the rest, which is the inverse of
	what nmcli does on the way out: ':' becomes '\\:' and '\\' becomes '\\\\'.
	"""
	fields = []
	field = []
	index = 0

	while index < len(line):
		char = line[index]
		if char == "\\" and index + 1 < len(line):
			field.append(line[index + 1])
			index += 2
			continue
		if char == ":":
			fields.append("".join(field))
			field = []
			index += 1
			continue
		field.append(char)
		index += 1

	fields.append("".join(field))
	return fields


def field(fields, position):
	return fields[position] if position < len(fields) else ""


def signal_of(text):
	try:
		return int(text.strip())
	except ValueError:
		return 0


def security_of(text):
	"""nmcli's SECURITY, or "" for an open network.

	Terse mode leaves it empty; the tabular mode writes "--", and a mixed nmcli
	build has been seen doing either, so both mean open.
	"""
	text = text.strip()
	return "" if text in ("", "--") else text


def redact(args):
	"""An nmcli argument list safe to write to the log.

	"password" is followed by the thing that must never reach a file.
	"""
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
	return " ".join(safe)


# ---------------------------------------------------------------- ES pipes

STDIN_FD = sys.stdin.fileno()

pending = b""   # stdin arrives as a byte stream: the tail waits for its newline

_stdin_sel = selectors.DefaultSelector()
_stdin_sel.register(STDIN_FD, selectors.EVENT_READ)


def send(**command):
	"""Ask ES for a screen.  One JSON object, one line, flushed immediately."""
	print(json.dumps(command), flush=True)


def log(message):
	sys.stderr.write("wifi addon: %s\n" % message)
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
	"""Throw away whatever arrived while we were blocked on nmcli.

	The only thing ES can send during a progress screen is "back", and a
	connection attempt that has already reached NetworkManager is not
	something to abandon halfway - but the events still have to be read, or
	the next screen would inherit them.
	"""
	while True:
		event = pop_event()
		if event is not None:
			log("ignoring %r received while busy" % event.get("event"))
			continue
		if not fill(0):
			return


# --------------------------------------------------------------- nmcli

class Result(object):
	"""What one nmcli command did, and what to tell the user if it failed."""

	def __init__(self, ok, lines, detail, blob="", cancelled=False):
		self.ok = ok
		self.lines = lines
		self.detail = detail
		self.blob = blob
		self.cancelled = cancelled

	def __repr__(self):
		return "Result(ok=%r, detail=%r)" % (self.ok, self.detail)


def nmcli_argv(args):
	"""The one place an nmcli command line is built.

	Without the override that is "sudo -n nmcli ...", because polkit will not
	let the "pi" user talk to NetworkManager from an ES session.  -n means a
	sudo that wants a password fails immediately instead of waiting forever on
	a terminal this process does not have.
	"""
	if NMCLI_OVERRIDE:
		return [NMCLI_OVERRIDE] + list(args)
	return ["sudo", "-n", "nmcli"] + list(args)


def nmcli_present():
	if NMCLI_OVERRIDE:
		return os.path.exists(NMCLI_OVERRIDE)
	return shutil.which("nmcli") is not None


def systemctl_argv(args):
	"""The one place a systemctl command line is built.

	Same shape as nmcli_argv, same reason: enabling a unit is root's business,
	the addon has no terminal, and -n turns a sudo that wants a password into
	an immediate failure instead of a hang.
	"""
	if SYSTEMCTL_OVERRIDE:
		return [SYSTEMCTL_OVERRIDE] + list(args)
	return ["sudo", "-n", "systemctl"] + list(args)


def finish(label, args, code, stdout, stderr, cancelled=False):
	"""Turn a finished command into a Result.

	Unlike bluetoothctl, nmcli means its exit status, so ok is the status and
	nothing else.  stderr is kept apart from stdout because stdout is parsed:
	an nmcli warning mixed into a terse listing would become a bogus row, and
	systemctl's answer to "is-active" is on stdout while its complaints are
	not.
	"""
	lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
	errors = [line.strip() for line in (stderr or "").splitlines() if line.strip()]

	ok = code == 0
	if errors:
		detail = errors[-1]
	elif lines:
		detail = lines[-1]
	else:
		detail = "nmcli exited with status %d." % code

	log("%s %s -> exit %d%s"
		% (label, redact(args), code, "" if ok else ", %r" % detail))

	return Result(ok, lines, detail, "\n".join(errors + lines).lower(), cancelled)


def run_tool(label, command, args, timeout):
	"""Run one command and wait for it.  ES's events wait their turn."""
	try:
		done = subprocess.run(
			command,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=timeout,
			text=True,
			errors="replace",
		)
	except subprocess.TimeoutExpired:
		log("%s %s timed out after %gs" % (label, redact(args), timeout))
		return Result(False, [], "%s did not answer within %g seconds." % (label, timeout))
	except OSError as error:
		log("could not run %s %s: %s" % (label, redact(args), error))
		return Result(False, [], "Could not run %s: %s" % (label, error))

	return finish(label, args, done.returncode, done.stdout, done.stderr)


def run_nmcli(args, timeout):
	args = list(args)
	return run_tool("nmcli", nmcli_argv(args), args, timeout)


def run_systemctl(args, timeout):
	args = list(args)
	return run_tool("systemctl", systemctl_argv(args), args, timeout)


def run_nmcli_cancellable(args, timeout):
	"""Run one nmcli, servicing our own stdin while it works.

	Only the rescan uses this.  A rescan is a wait with nothing at stake, so
	"back" during it means cancel and the Result comes back cancelled; a
	connection attempt, by contrast, is finished once it has started (see
	drain_events).  Output is small enough that reading it at the end cannot
	fill a pipe in the meantime.
	"""
	args = list(args)

	try:
		proc = subprocess.Popen(
			nmcli_argv(args),
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			errors="replace",
		)
	except OSError as error:
		log("could not run nmcli %s: %s" % (redact(args), error))
		return Result(False, [], "Could not run nmcli: %s" % error)

	deadline = time.monotonic() + timeout
	cancelled = False
	timed_out = False

	while proc.poll() is None:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			timed_out = True
			break

		# EOFError from fill() is ES closing our stdin, and it is meant to
		# escape: the child is killed on the way out by the finally below
		try:
			fill(min(remaining, CANCEL_POLL))
		except EOFError:
			stop(proc)
			raise

		while True:
			event = pop_event()
			if event is None:
				break
			if event.get("event") == "back":
				log("rescan cancelled by the user")
				cancelled = True
			else:
				log("ignoring %r during the rescan" % event.get("event"))

		if cancelled:
			break

	if cancelled or timed_out:
		stop(proc)
		if timed_out:
			log("nmcli %s timed out after %gs" % (redact(args), timeout))
			return Result(False, [], "nmcli did not answer within %g seconds." % timeout)
		return Result(False, [], "Cancelled.", cancelled=True)

	stdout, stderr = proc.communicate()
	return finish("nmcli", args, proc.returncode, stdout, stderr)


def stop(proc):
	"""End a child we are no longer waiting for, politely and then not."""
	if proc.poll() is not None:
		return
	proc.terminate()
	try:
		proc.wait(timeout=2)
		return
	except subprocess.TimeoutExpired:
		log("nmcli ignored SIGTERM, killing")
	proc.kill()
	try:
		proc.wait(timeout=2)
	except subprocess.TimeoutExpired:
		pass


def is_secret_failure(result):
	return any(marker in result.blob for marker in SECRET_MARKERS)


# ------------------------------------------------------------ what nmcli knows

def saved_wifi_names():
	"""Names of the saved wifi profiles, in nmcli's order.  None if it failed.

	Only 802-11-wireless: "Wired connection 1" and "lo" are connections too,
	and neither belongs on a WiFi screen.
	"""
	result = run_nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"], QUICK_TIMEOUT)
	if not result.ok:
		return None, result

	names = []
	for line in result.lines:
		fields = split_terse(line)
		if field(fields, 1) != WIFI_TYPE:
			continue
		name = field(fields, 0)
		if name and name not in names:
			names.append(name)

	return names, result


def active_connections():
	"""Every connection that is up, with the interface it is up on.

	This is the ONE answer to "what is this machine on": the status row, the
	main list's marks, the connect/disconnect choice and the scan picker all
	read the same query, so none of them can disagree with the others.
	"""
	result = run_nmcli(
		["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
		QUICK_TIMEOUT)
	if not result.ok:
		return None, result

	rows = []
	for line in result.lines:
		fields = split_terse(line)
		device = field(fields, 2)
		if not device:
			continue    # up, but on no interface: not something we are using
		rows.append({
			"name": field(fields, 0),
			"type": field(fields, 1),
			"device": device,
		})

	return rows, result


def wifi_row(rows):
	"""The wifi connection that is up, or None."""
	for row in rows:
		if row["type"] == WIFI_TYPE:
			return row
	return None


def pick_link(rows):
	"""The connection the status row talks about.  Wifi first, then the cable.

	A box can be on wifi, on ethernet, or on both, and the row has one line to
	say which.  wlan0 wins because this is the screen the wifi can be changed
	from; the wired answer is what stops the row reading "Not connected" while
	the machine is plainly online through a cable.  Loopback is not a network
	anybody joined.
	"""
	wireless = wifi_row(rows)
	if wireless is not None:
		return wireless

	for row in rows:
		if row["type"] != "loopback":
			return row

	return None


def device_details(device):
	"""What nmcli knows about one interface.  None if it would not say.

	Three things about this output are worth knowing:

	  * "dev show" is the one terse mode that does NOT escape its values
	    (measured on nmcli 1.42.4: GENERAL.HWADDR arrives as "2C:CF:67:...",
	    colons raw) - only the FIRST colon separates the key from the value,
	    so this parser partitions instead of split_terse.  The unescape stays,
	    defensively, for an nmcli that does escape here: it is a no-op on a
	    value with no backslashes;
	  * an address arrives with its prefix length, "192.168.68.75/24", and the
	    prefix is not what anybody standing at a television wants to read;
	  * there can be several, as IP4.ADDRESS[1], IP4.ADDRESS[2]...  The first
	    is the machine's address.
	"""
	result = run_nmcli(
		["-t", "-f", "GENERAL.CONNECTION,GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY",
			"dev", "show", device],
		QUICK_TIMEOUT)
	if not result.ok:
		return None

	details = {"connection": "", "hwaddr": "", "address": "", "gateway": ""}

	for line in result.lines:
		key, _, value = line.partition(":")
		value = value.replace("\\:", ":").replace("\\\\", "\\").strip()
		if value in ("", "--"):
			continue

		if key == "GENERAL.CONNECTION":
			details["connection"] = value
		elif key == "GENERAL.HWADDR":
			details["hwaddr"] = value
		elif key.startswith("IP4.ADDRESS") and not details["address"]:
			details["address"] = value.split("/")[0]
		elif key == "IP4.GATEWAY":
			details["gateway"] = value

	return details


def ssh_state():
	"""'enabled', 'disabled' or 'unknown'.

	"systemctl is-active" exits NONZERO when the unit is not running, and that
	is an answer, not a failure - so the state is read off stdout and the exit
	status is ignored.  Only a systemctl that could not be run, or one that
	said nothing at all, leaves the question open, and "Unknown" is what the
	row then says rather than a screen nobody asked for.
	"""
	result = run_systemctl(["is-active", SSH_UNIT], QUICK_TIMEOUT)

	answer = result.lines[0].strip().lower() if result.lines else ""
	if not answer:
		return "unknown"

	return "enabled" if answer == "active" else "disabled"


def scan_networks():
	"""Rescan, then list what is in the air.  (networks, error) - error is a string.

	The rescan is the part that can be refused for reasons that do not matter:
	NetworkManager says "Scanning not allowed immediately following previous
	scan" whenever the last one was seconds ago, and its cached list is still
	the right answer.  So a failed rescan is logged and the list is asked for
	anyway; only a list that comes back with nothing to show is worth a
	message, and then the rescan's own words go in it.
	"""
	rescan = run_nmcli_cancellable(["dev", "wifi", "rescan"], RESCAN_TIMEOUT)
	if rescan.cancelled:
		return None, None

	if not rescan.ok:
		log("rescan refused: %s" % rescan.detail)

	result = run_nmcli(
		["-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "dev", "wifi", "list"],
		QUICK_TIMEOUT)
	drain_events()

	if not result.ok:
		return None, "Could not ask nmcli what is in the air. %s" % result.detail

	networks = collapse(result.lines)

	if not networks:
		if not rescan.ok:
			return None, "Could not scan for networks. %s" % rescan.detail
		return None, ("No networks answered. Move closer to the router and "
			"search again.")

	return networks, None


def collapse(lines):
	"""Scan rows to one entry per network, strongest first.

	A scan sees every access point, so a house with a repeater and two bands
	reports the same SSID three or four times at different strengths.  The
	list the user wants has each name once, at its best signal - and marked as
	the connected one if ANY of the rows carrying that name is the one in use,
	because the row wearing the '*' is often not the strongest.

	An empty SSID is a hidden network announcing itself without a name.  There
	is nothing to pick, so it is dropped; "Join hidden network..." is how you
	reach one.
	"""
	networks = []
	by_name = {}

	for line in lines:
		fields = split_terse(line)
		ssid = field(fields, 0)
		if not ssid:
			continue

		signal = signal_of(field(fields, 1))
		security = security_of(field(fields, 2))
		in_use = field(fields, 3).strip() == "*"

		entry = by_name.get(ssid)
		if entry is None:
			entry = by_name[ssid] = {
				"ssid": ssid, "signal": signal,
				"security": security, "in_use": in_use,
			}
			networks.append(entry)
			continue

		entry["in_use"] = entry["in_use"] or in_use
		if signal > entry["signal"]:
			entry["signal"] = signal
			entry["security"] = security

	networks.sort(key=lambda entry: (-entry["signal"], entry["ssid"].lower()))
	return networks[:MAX_NETWORKS]


# ---------------------------------------------------------------- screens

screen = "none"           # which screen we last asked ES for: it decides "back"
after_message = "main"    # where the OK of the current message goes

saved = []                # saved wifi profile names, as of the last main list
active = ""               # the wifi profile that is up, as of the last main list
link = None               # the connection the status row is talking about
link_details = None       # ...and what nmcli said about its interface
ssh_status = "unknown"    # whether sshd is running, as of the last main list
networks = []             # what the last scan found
current = ""              # the saved profile whose menu / question is up
connect_ssid = ""         # the network a password is being typed for
connect_hidden = False    # ...and whether it has to be joined by name
asked_password = False    # ...and whether we asked, so a retry has somewhere to go


def show_message(title, text, after="main"):
	global screen, after_message
	send(cmd="message", title=title, text=text)
	screen = "message"
	after_message = after


def status_row():
	"""The top row: whether this machine is online, and as what.

	The address comes from the main list's own lookup rather than a second
	one, so the row and the details screen behind it cannot drift apart
	between two nmcli calls.  An interface nmcli would not describe still gets
	a row - it names the interface instead of the address, because "connected
	on eth0" is true and useful and "Not connected" would not be.
	"""
	if link is None:
		return {"id": "status", "label": "Not connected", "detail": "no network"}

	address = (link_details or {}).get("address")
	return {
		"id": "status",
		"label": "Connected: %s" % link["name"],
		"detail": address or link["device"],
	}


SSH_DETAIL = {"enabled": "Enabled", "disabled": "Disabled", "unknown": "Unknown"}


def ssh_row():
	return {"id": "ssh", "label": "SSH", "detail": SSH_DETAIL[ssh_status]}


def show_main_list():
	"""Where we stand, the saved networks, and the two ways to reach a new one."""
	global screen, saved, active, link, link_details, ssh_status

	names, result = saved_wifi_names()
	if names is None:
		show_message("WIFI ERROR",
			"Could not ask nmcli for the saved networks. %s" % result.detail,
			after="close")
		return

	rows, result = active_connections()
	if rows is None:
		show_message("WIFI ERROR",
			"Could not ask nmcli which network is connected. %s" % result.detail,
			after="close")
		return

	saved = names
	wireless = wifi_row(rows)
	active = wireless["name"] if wireless is not None else ""
	link = pick_link(rows)
	link_details = device_details(link["device"]) if link is not None else None

	ssh_status = ssh_state()

	items = [status_row(), ssh_row()]

	items += [
		{
			"id": "saved:" + name,
			"label": name,
			"detail": "Connected" if name == active else "Saved",
		}
		for name in saved
	]

	items.append({
		"id": "scan",
		"label": "Search for networks...",
		"detail": "join a new network",
	})
	items.append({
		"id": "hidden",
		"label": "Join hidden network...",
		"detail": "type the name",
	})

	send(cmd="list", title="INTERNET", items=items)
	screen = "main"


def show_saved_menu():
	global screen

	if not current:
		show_main_list()
		return

	items = []
	if current == active:
		items.append({"id": "down", "label": "Disconnect"})
	else:
		items.append({"id": "up", "label": "Connect"})
	items.append({"id": "forget", "label": "Forget this network"})

	send(cmd="list", title=current, items=items)
	screen = "saved"


def network_row(index, entry):
	detail = "%d%%  %s" % (entry["signal"], entry["security"] or "open")
	if entry["in_use"]:
		detail += "  (connected)"
	return {"id": "net:%d" % index, "label": entry["ssid"], "detail": detail}


def show_networks_list():
	global screen

	items = [network_row(index, entry) for index, entry in enumerate(networks)]
	send(cmd="list", title="NETWORKS", items=items)
	screen = "networks"


def show_password_input():
	global screen, asked_password

	asked_password = True
	send(cmd="input",
		title="Password for %s" % connect_ssid,
		text="Enter the wifi password",
		value="")
	screen = "password"


def show_status_message():
	"""Everything nmcli will say about the connection we are on.

	Asked again rather than reused: this is the screen somebody opens BECAUSE
	they want to know the address right now, and a lease can have changed
	since the list was drawn.
	"""
	if link is None:
		show_message("NOT CONNECTED",
			"This machine is not on a network. Join one below, or plug in a "
			"network cable.")
		return

	details = device_details(link["device"])
	if details is None:
		show_message("CONNECTION",
			"Connected to %s on %s. nmcli would not say any more than that."
			% (link["name"], link["device"]))
		return

	lines = [
		"Connection: %s" % (details["connection"] or link["name"]),
		"Interface: %s" % link["device"],
	]
	if details["address"]:
		lines.append("IP address: %s" % details["address"])
	if details["hwaddr"]:
		lines.append("MAC address: %s" % details["hwaddr"])
	if details["gateway"]:
		lines.append("Gateway: %s" % details["gateway"])

	show_message("CONNECTION", "\n".join(lines))


def show_hidden_input():
	global screen

	send(cmd="input", title="Network name (SSID)",
		text="Type the name of the hidden network", value="")
	screen = "hidden_ssid"


# ---------------------------------------------------------------- actions

def do_scan():
	global networks, screen

	screen = "progress"
	send(cmd="progress", title="SCANNING", text="Scanning for networks...")

	found, error = scan_networks()

	if found is None and error is None:
		show_main_list()    # cancelled
		return

	if found is None:
		show_message("NO NETWORKS", error)
		return

	networks = found
	show_networks_list()


def do_up(name):
	"""Bring a saved profile up.  Success is the refreshed main list."""
	global screen
	screen = "progress"

	send(cmd="progress", title="CONNECTING", text="Connecting to %s..." % name)
	result = run_nmcli(["connection", "up", "id", name], ACTION_TIMEOUT)
	drain_events()

	if result.ok:
		show_main_list()
		return

	show_message("NOT CONNECTED",
		"Could not connect to %s. %s" % (name, result.detail))


def do_down():
	global screen
	screen = "progress"

	send(cmd="progress", title="DISCONNECTING", text="Disconnecting %s..." % current)
	result = run_nmcli(["connection", "down", "id", current], ACTION_TIMEOUT)
	drain_events()

	if result.ok:
		show_main_list()
		return

	show_message("STILL CONNECTED",
		"Could not disconnect %s. %s" % (current, result.detail))


def do_forget():
	global screen
	screen = "progress"

	send(cmd="progress", title="FORGETTING", text="Removing %s..." % current)
	result = run_nmcli(["connection", "delete", "id", current], QUICK_TIMEOUT)
	drain_events()

	if result.ok:
		show_main_list()
		return

	show_message("NOT FORGOTTEN",
		"Could not forget %s. %s" % (current, result.detail))


def do_ssh():
	"""Flip SSH, and let the refreshed main list be the confirmation."""
	global screen
	screen = "progress"

	turning_on = ssh_status != "enabled"
	action = "enable" if turning_on else "disable"

	send(cmd="progress", title="SSH",
		text="%s SSH..." % ("Enabling" if turning_on else "Disabling"))
	result = run_systemctl([action, "--now", SSH_UNIT], ACTION_TIMEOUT)
	drain_events()

	if result.ok:
		show_main_list()
		return

	show_message("SSH", "Could not %s SSH. %s" % (action, result.detail))


def remove_half_profile(before):
	"""Delete the profile NetworkManager left behind by a failed connect.

	"nmcli dev wifi connect" writes the profile first and activates it second,
	so a wrong password leaves a saved network that has never worked - and the
	next attempt would find it, take the "already saved" path and fail again
	with the same bad secret.  Only a profile that was NOT there before is
	removed: a pre-existing one belongs to the user, and a failed attempt is
	no reason to throw their password away.
	"""
	if before is None:
		return    # we never got a clean "before", so we cannot tell whose it is

	after, _ = saved_wifi_names()
	if after is None:
		return

	if connect_ssid in after and connect_ssid not in before:
		log("removing the profile left behind by the failed connect")
		run_nmcli(["connection", "delete", "id", connect_ssid], QUICK_TIMEOUT)


def do_connect_new(password):
	"""Join a network by name, with a password if there is one.

	The argument list is handed to exec as a list.  Nothing here ever reaches
	a shell, because an SSID is whatever the neighbours felt like typing.
	"""
	global screen
	screen = "progress"

	send(cmd="progress", title="CONNECTING", text="Connecting to %s..." % connect_ssid)

	before, _ = saved_wifi_names()

	args = ["dev", "wifi", "connect", connect_ssid]
	if password:
		args += ["password", password]
	if connect_hidden:
		args += ["hidden", "yes"]

	result = run_nmcli(args, CONNECT_TIMEOUT)
	drain_events()

	if result.ok:
		show_main_list()
		return

	remove_half_profile(before)

	if is_secret_failure(result):
		text = "Could not connect to %s. Wrong password?" % connect_ssid
	else:
		text = "Could not connect to %s. %s" % (connect_ssid, result.detail)

	# a retry needs the password box back; a network we never asked about has
	# nowhere to retry to, so its failure ends on the main list
	show_message("NOT CONNECTED", text,
		after="password" if asked_password else "main")


def start_join(ssid, security, hidden=False):
	"""Decide how to join a network, and start doing it."""
	global connect_ssid, connect_hidden, asked_password

	connect_ssid = ssid
	connect_hidden = hidden
	asked_password = False

	if not hidden and ssid in saved:
		do_up(ssid)
		return

	if not hidden and not security:
		do_connect_new("")    # open network: nothing to ask
		return

	show_password_input()


# ---------------------------------------------------------------- events

def on_select(item_id):
	global current, screen

	if item_id == "status":
		show_status_message()

	elif item_id == "ssh":
		if ssh_status == "unknown":
			show_message("SSH",
				"Could not ask systemctl whether SSH is running on this "
				"system, so there is nothing safe to switch.")
		elif ssh_status == "enabled":
			send(cmd="confirm", title="DISABLE SSH?",
				text="Disable SSH? Remote access to this system will stop working.")
			screen = "confirm_ssh"
		else:
			send(cmd="confirm", title="ENABLE SSH?",
				text="Enable SSH? This allows remote logins to this system.")
			screen = "confirm_ssh"

	elif item_id == "scan":
		do_scan()

	elif item_id == "hidden":
		show_hidden_input()

	elif item_id.startswith("saved:"):
		name = item_id[6:]
		if name not in saved:
			log("no such saved network %r" % name)
			show_main_list()
			return
		current = name
		show_saved_menu()

	elif item_id.startswith("net:"):
		try:
			entry = networks[int(item_id[4:])]
		except (ValueError, IndexError):
			log("no such network %r" % item_id)
			show_networks_list()
			return

		if entry["in_use"]:
			show_message("ALREADY CONNECTED",
				"Already connected to %s." % entry["ssid"], after="networks")
			return

		start_join(entry["ssid"], entry["security"])

	elif item_id == "up":
		do_up(current)

	elif item_id == "down":
		do_down()

	elif item_id == "forget":
		send(cmd="confirm", title="FORGET?",
			text="Forget %s? The saved password will be deleted." % current)
		screen = "confirm_forget"

	else:
		log("unknown item id %r" % item_id)
		show_main_list()


def on_text(value):
	global connect_ssid, connect_hidden, asked_password

	if screen == "hidden_ssid":
		ssid = value.strip()
		if not ssid:
			show_main_list()
			return
		connect_ssid = ssid
		connect_hidden = True
		asked_password = False
		# a hidden network is always asked for a password, and an empty answer
		# is a real one: an open network joined by name
		show_password_input()

	elif screen == "password":
		do_connect_new(value)

	else:
		log("text on the %r screen" % screen)
		show_main_list()


def on_confirm(value):
	if screen == "confirm_forget":
		if value:
			do_forget()
		else:
			show_saved_menu()

	elif screen == "confirm_ssh":
		if value:
			do_ssh()
		else:
			show_main_list()

	elif screen == "message":
		# the OK of a message: it says nothing except where to go next
		if after_message == "close":
			send(cmd="close")
			sys.exit(0)
		if after_message == "password":
			show_password_input()
		elif after_message == "networks":
			show_networks_list()
		else:
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
		show_saved_menu()
	elif screen == "confirm_ssh":
		show_main_list()
	elif screen == "message":
		on_confirm(True)
	elif screen == "password":
		# the scan picker is where a password was asked for, unless the name
		# was typed in, and then there is nothing behind it but the main list
		if connect_hidden:
			show_main_list()
		else:
			show_networks_list()
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
				if not nmcli_present():
					log("nmcli is not installed")
					show_message("WIFI UNAVAILABLE",
						"nmcli was not found on this system, so this addon "
						"cannot manage WiFi. Install NetworkManager "
						"(sudo apt install network-manager) and try again.",
						after="close")
				else:
					log("nmcli command is %r" % " ".join(nmcli_argv([])))
					show_main_list()
			elif name == "select":
				on_select(event.get("id", ""))
			elif name == "text":
				on_text(event.get("value", ""))
			elif name == "confirm":
				on_confirm(bool(event.get("value")))
			elif name == "back":
				on_back()
			else:
				log("unknown event %r" % name)

		except EOFError:
			# ES has gone away, and every screen we could ask for went with it
			log("ES closed our stdin, stopping")
			return 0


if __name__ == "__main__":
	sys.exit(main())
