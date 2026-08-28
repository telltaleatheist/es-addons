#!/usr/bin/env python3
"""Controller order for EmulationStation, as an addon.

An addon is a headless program.  It never draws anything and never reads the
terminal: EmulationStation spawns it with pipes on its stdin and stdout, the
addon asks for screens by writing one JSON object per line to stdout, and ES
reports what the user did by writing one JSON object per line to the addon's
stdin.  The protocol is ADDONS.md in the ES source tree; the idioms here are
its reference implementation's, as are its siblings in this repository,
wifi/wifi.py and bluetooth/bluetooth.py.

This is the Switch's gesture, on a Pi.  The screen says which pad is which
player, and you change it by pressing a button: the first pad to press one
becomes Player 1, the next pad to press one Player 2, and so on.  Pads nobody
touched keep the remaining slots in the order the kernel found them.  Pressing
Start is how you leave, because on the Switch it is - and because ES's own
START closes an addon's GUI whatever the addon thinks, so it is the one exit
this addon could not refuse even if it wanted to.

**That is why every claim is applied the moment it happens.**  There is no
"save" step and there can never be one: the button a person presses to say "I
am done" is the same button that takes the addon's screen away without asking.
So a claim rewrites retroarch.cfg, repaints the pad's player LEDs and redraws
the list, in that order, before the loop looks at anything else.  An addon that
buffered the assignment until an exit step would lose it every single time.

What counts as a controller is the `jsN` in its /proc/bus/input/devices entry.
The same physical pad often has more than one entry - a Switch Pro Controller's
motion sensors are a second entry with `Handlers=event9` and no js - and those
are not pads; a screen that offered "Player 3" for a gyroscope would be lying.
Enumeration order is ascending event-node number.

**The RetroArch index is an assumption, and it is written down here.**
RetroArch uses the udev joypad driver, whose pad indices are the order it
enumerates joystick devices in; we take that to be ascending event-node order,
which is what this addon sorts by.  A single-pad machine cannot be affected by
it either way (there is one pad and it is index 0); multi-pad ordering gets
checked on real hardware.  If it ever turns out to be false, this is the one
place that decides it: pad_index(), and nothing else.

A claim is an EV_KEY press, and only that.  EV_ABS is ignored entirely: sticks
drift, a d-pad is an axis on most pads, and a slot claimed by a controller
sitting on a shelf is worse than no feature at all.  BTN_START is the exit
gesture and BTN_MODE is the home button, so neither claims either.

Failures are rows, not tracebacks and not modal boxes.  A pad whose event node
will not open says so in its own row and the others still work; a retroarch.cfg
that cannot be written says so in a row and the LEDs still paint.  A message
box in the middle of this screen would be a screen the next button press
dismisses by accident.

Player LEDs are root's business (/sys/class/leds/<hidid>:green:player-N is not
writable by "pi"), so they go through sudo, and led_command() is the one place
that command line is built.  A pad with no player LEDs is skipped in silence -
most pads have none, and it is not a failure.

Environment knobs, for testing:

  ES_SLOTS_PROC_DEVICES  a file to read instead of /proc/bus/input/devices
  ES_SLOTS_DEV_INPUT     a directory of event nodes instead of /dev/input
  ES_SLOTS_RETROARCH_CFG the retroarch.cfg to rewrite
  ES_SLOTS_LED_ROOT      a directory of LED devices instead of /sys/class/leds
  ES_SLOTS_LED_WRITE     a program to run instead of "sudo -n tee"
"""

import json
import os
import re
import selectors
import struct
import subprocess
import sys


# ---------------------------------------------------------------- settings

PROC_DEVICES = os.environ.get("ES_SLOTS_PROC_DEVICES", "/proc/bus/input/devices")
DEV_INPUT = os.environ.get("ES_SLOTS_DEV_INPUT", "/dev/input")
RETROARCH_CFG = os.environ.get(
	"ES_SLOTS_RETROARCH_CFG", "/opt/retropie/configs/all/retroarch.cfg")
LED_ROOT = os.environ.get("ES_SLOTS_LED_ROOT", "/sys/class/leds")
LED_WRITE = os.environ.get("ES_SLOTS_LED_WRITE", "")

PLAYERS = 10          # RetroArch has input_player1..input_player10
PLAYER_LEDS = 4       # a Switch pad shows four of them and no more
LED_TIMEOUT = 5.0     # writing one byte to sysfs; anything slower is stuck

TITLE = "CONTROLLER ORDER"

# evdev's input_event, as this kernel lays it out: 64-bit time_t on a 64-bit
# ARM, then type, code and value.  24 bytes, and a short read is half an event
# that has to wait for the rest of itself.
EVENT_FORMAT = "<qqHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

EV_KEY = 0x01
KEY_PRESS = 1

BTN_START = 315       # 0x13b - the way out, never a claim
BTN_MODE = 316        # 0x13c - the home button, never a claim

# The button codes a pad can claim with.  BTN_MISC (0x100-0x10f) is what arcade
# sticks report, BTN_JOYSTICK..BTN_GEAR_UP (0x120-0x151) is every gamepad, and
# BTN_TRIGGER_HAPPY (0x2c0-0x2ff) is what a pad with more buttons than names
# uses.  The mouse range in between is deliberately not here.
CLAIM_RANGES = ((0x100, 0x10f), (0x120, 0x151), (0x2c0, 0x2ff))


# ---------------------------------------------------------------- ES pipes

STDIN_FD = sys.stdin.fileno()

pending = b""   # stdin arrives as a byte stream: the tail waits for its newline


def send(**command):
	"""Ask ES for a screen.  One JSON object, one line, flushed immediately."""
	print(json.dumps(command), flush=True)


def log(message):
	sys.stderr.write("slots addon: %s\n" % message)
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


# ------------------------------------------------------------- the devices

class Pad(object):
	"""One game controller, and the event node its buttons arrive on."""

	def __init__(self, name, node, sysfs, uniq):
		self.name = name or "Controller"
		self.node = node            # "event3"
		self.sysfs = sysfs or ""    # where the LEDs are found from
		self.uniq = uniq or ""      # MAC for a bluetooth pad, empty over USB
		self.index = 0              # RetroArch's joypad index: see pad_index()
		self.fd = None
		self.buffer = b""
		self.problem = ""           # why this pad cannot claim, in its own words

	def __repr__(self):
		return "Pad(%r, %r, index=%d)" % (self.name, self.node, self.index)


def parse_devices(text):
	"""Every game controller in /proc/bus/input/devices, in event-node order.

	The file is blocks separated by blank lines.  A block is a controller when
	its "H: Handlers=" line has a jsN in it - the joystick device is what makes
	a pad a pad, and the sibling entries the same hardware registers (a Pro
	Controller's motion sensors are event-only) have none.  A block with a js
	but no event node is skipped: its buttons cannot be read, and RetroArch's
	udev driver would not see it either.
	"""
	pads = []

	for block in re.split(r"\n\s*\n", text):
		name = ""
		sysfs = ""
		uniq = ""
		handlers = []

		for line in block.splitlines():
			line = line.strip()
			if line.startswith("N: Name="):
				name = line[len("N: Name="):].strip().strip('"')
			elif line.startswith("S: Sysfs="):
				sysfs = line[len("S: Sysfs="):].strip()
			elif line.startswith("U: Uniq="):
				uniq = line[len("U: Uniq="):].strip()
			elif line.startswith("H: Handlers="):
				handlers = line[len("H: Handlers="):].split()

		if not any(re.fullmatch(r"js[0-9]+", token) for token in handlers):
			continue

		nodes = [token for token in handlers if re.fullmatch(r"event[0-9]+", token)]
		if not nodes:
			log("%r has a joystick device but no event node, skipping" % name)
			continue

		pads.append(Pad(name, nodes[0], sysfs, uniq))

	pads.sort(key=lambda pad: int(pad.node[len("event"):]))
	return pads


def pad_index(pads):
	"""Give every pad the joypad index RetroArch will know it by.

	The ONE place the assumption in the module docstring lives: RetroArch's
	udev driver numbers joysticks in the order it enumerates them, and we take
	that to be ascending event-node order, which is the order parse_devices
	returns.
	"""
	for position, pad in enumerate(pads):
		pad.index = position


def read_pads():
	"""The controllers, or (None, reason) if the kernel would not say."""
	try:
		with open(PROC_DEVICES, "r", errors="replace") as handle:
			text = handle.read()
	except OSError as error:
		log("could not read %s: %s" % (PROC_DEVICES, error))
		return None, "Could not read %s: %s" % (PROC_DEVICES, error.strerror or error)

	pads = parse_devices(text)
	pad_index(pads)
	return pads, ""


def open_pad(pad):
	"""Open a pad's event node for reading.  A refusal is the pad's own row.

	Non-blocking, because the loop reads every pad and ES's pipe from one
	select() and must never sit in a read that has nothing behind it.  "pi" is
	in the "input" group on the target machine, so this needs no sudo; when it
	fails anyway the pad still counts as a controller (RetroArch will index it)
	and simply never claims a slot.
	"""
	path = os.path.join(DEV_INPUT, pad.node)
	try:
		pad.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
	except OSError as error:
		pad.fd = None
		pad.problem = "cannot be read: %s" % (error.strerror or error)
		log("could not open %s: %s" % (path, error))


def is_claim_code(code):
	"""Is this button code one a pad may claim a player slot with?"""
	if code in (BTN_START, BTN_MODE):
		return False
	return any(low <= code <= high for low, high in CLAIM_RANGES)


def drain_pad(pad):
	"""Every complete event now readable from one pad, and whether it lives.

	A partial event is kept: 24 bytes can arrive as two reads, and half a
	struct unpacked as a whole one is a button nobody pressed.
	"""
	events = []
	alive = True

	while True:
		try:
			chunk = os.read(pad.fd, EVENT_SIZE * 64)
		except BlockingIOError:
			break
		except OSError as error:
			pad.problem = "cannot be read: %s" % (error.strerror or error)
			log("%s stopped reading: %s" % (pad.node, error))
			alive = False
			break

		if not chunk:
			pad.problem = "disconnected"
			log("%s reached end of file" % pad.node)
			alive = False
			break

		pad.buffer += chunk

	while len(pad.buffer) >= EVENT_SIZE:
		frame = pad.buffer[:EVENT_SIZE]
		pad.buffer = pad.buffer[EVENT_SIZE:]
		_sec, _usec, kind, code, value = struct.unpack(EVENT_FORMAT, frame)
		events.append((kind, code, value))

	return events, alive


# ------------------------------------------------------------- retroarch.cfg

CFG_LINE = re.compile(
	r'^(\s*input_player([0-9]+)_joypad_index\s*=\s*)("[^"]*"|\S*)(.*)$')


def joypad_indices(pads, claimed):
	"""What every input_playerN_joypad_index line should say.

	Claimed pads first, in the order they claimed, then the pads nobody touched
	in enumeration order: "unclaimed pads fill the remaining slots".  The player
	lines past the last pad get the indices no pad is using, in ascending order,
	so that all ten lines still hold ten different values - two players sharing
	an index is how RetroArch ends up driving one pad with two sets of inputs.
	"""
	order = list(claimed) + [pad for pad in pads if pad not in claimed]

	used = [pad.index for pad in order][:PLAYERS]
	spare = [index for index in range(PLAYERS) if index not in used]
	values = (used + spare)[:PLAYERS]

	return {player + 1: values[player] for player in range(len(values))}


def rewrite_cfg_text(text, values, required):
	"""retroarch.cfg with the joypad index lines rewritten, and nothing else.

	Every other line comes back exactly as it went in - this file is the whole
	of RetroArch's configuration and the addon has an opinion about ten lines of
	it.  The quoting of a line is kept as it was found.  A player line that is
	missing but needed (a slot a pad is actually in) is appended, because
	RetroArch's default for an absent line is "player N uses pad N-1", which is
	the assignment we were asked to change.
	"""
	lines = text.split("\n")
	out = []
	seen = set()

	for line in lines:
		match = CFG_LINE.match(line)
		if match is None:
			out.append(line)
			continue

		player = int(match.group(2))
		if player not in values:
			out.append(line)
			continue

		quoted = match.group(3).startswith('"')
		value = '"%d"' % values[player] if quoted else "%d" % values[player]
		out.append(match.group(1) + value + match.group(4))
		seen.add(player)

	missing = [player for player in range(1, min(required, PLAYERS) + 1)
		if player not in seen]

	if missing:
		# a file that ends with a newline ends with an empty field here, and
		# the new lines belong before it rather than after
		tail = []
		while out and out[-1] == "":
			tail.append(out.pop())
		for player in missing:
			out.append('input_player%d_joypad_index = "%d"' % (player, values[player]))
		out.extend(tail)

	return "\n".join(out)


def save_order(pads, claimed):
	"""Write the assignment to retroarch.cfg.  "" when it stuck, else why not.

	Atomic: a temp file beside the real one and a rename, because a half
	written retroarch.cfg is a RetroArch that will not start, and this runs on
	a machine somebody is about to switch off at the wall.
	"""
	values = joypad_indices(pads, claimed)

	try:
		with open(RETROARCH_CFG, "r", encoding="utf-8",
				errors="surrogateescape", newline="") as handle:
			text = handle.read()
	except OSError as error:
		log("could not read %s: %s" % (RETROARCH_CFG, error))
		return "not saved: %s" % (error.strerror or error)

	updated = rewrite_cfg_text(text, values, len(pads))
	if updated == text:
		log("controller order in %s is already right" % RETROARCH_CFG)
		return ""

	temp = RETROARCH_CFG + ".slots-tmp"
	try:
		with open(temp, "w", encoding="utf-8",
				errors="surrogateescape", newline="") as handle:
			handle.write(updated)
		try:
			os.chmod(temp, os.stat(RETROARCH_CFG).st_mode & 0o7777)
		except OSError as error:
			log("could not copy the mode of %s: %s" % (RETROARCH_CFG, error))
		os.replace(temp, RETROARCH_CFG)
	except OSError as error:
		log("could not write %s: %s" % (RETROARCH_CFG, error))
		try:
			os.unlink(temp)
		except OSError:
			pass
		return "not saved: %s" % (error.strerror or error)

	log("wrote %s: %s" % (RETROARCH_CFG,
		" ".join("p%d=%d" % (player, values[player]) for player in sorted(values))))
	return ""


# ------------------------------------------------------------------ the LEDs

def led_paths(pad):
	"""This pad's player LEDs, as {1: path, ...}.  Empty for a pad with none.

	A LED is called "<hidid>:<colour>:player-<n>", and the hidid - something
	like 0005:057E:2009.0004 - has colons of its own, so the name is split from
	the right.  The hidid also appears in the pad's Sysfs path, which is the
	only thing tying a LED to the controller it is on.
	"""
	if not pad.sysfs:
		return {}

	try:
		names = os.listdir(LED_ROOT)
	except OSError as error:
		log("could not list %s: %s" % (LED_ROOT, error))
		return {}

	found = {}
	for name in names:
		parts = name.rsplit(":", 2)
		if len(parts) != 3:
			continue

		hidid, _colour, leaf = parts
		if not leaf.startswith("player-"):
			continue
		try:
			number = int(leaf[len("player-"):])
		except ValueError:
			continue
		if not 1 <= number <= PLAYER_LEDS:
			continue
		if hidid not in pad.sysfs:
			continue

		found[number] = os.path.join(LED_ROOT, name, "brightness")

	return found


def led_command(path, value):
	"""The one place a LED write is built: the argv, and what to feed its stdin.

	/sys/class/leds/.../brightness is root-owned, and the addon runs as "pi"
	with no terminal, so the real write is "sudo -n tee <path>" with the value
	on stdin - tee rather than a shell, so no part of a path is ever parsed as
	a command.  -n means a sudo that wants a password fails immediately instead
	of waiting forever on a terminal this process does not have.

	ES_SLOTS_LED_WRITE replaces the whole thing with one program taking the
	path and the value, which is how the tests watch the LEDs.
	"""
	if LED_WRITE:
		return [LED_WRITE, path, str(value)], None
	return ["sudo", "-n", "tee", path], "%d\n" % value


def write_led(path, value):
	"""Set one LED.  A LED that will not light is a log line and nothing more."""
	argv, feed_stdin = led_command(path, value)

	# subprocess.run refuses "input" and "stdin" together, and a command that
	# wants nothing on its stdin must still not inherit ours: ES is on it
	streams = {"input": feed_stdin} if feed_stdin is not None \
		else {"stdin": subprocess.DEVNULL}

	try:
		done = subprocess.run(
			argv,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=LED_TIMEOUT,
			text=True,
			errors="replace",
			**streams
		)
	except subprocess.TimeoutExpired:
		log("%s did not answer within %gs" % (argv[0], LED_TIMEOUT))
		return
	except OSError as error:
		log("could not run %s: %s" % (argv[0], error))
		return

	if done.returncode != 0:
		log("%s -> exit %d, %r" % (" ".join(argv), done.returncode,
			(done.stderr or "").strip()))


def paint_leds(pad, slot):
	"""Light the player LED for the slot this pad just claimed, and no other."""
	leds = led_paths(pad)
	if not leds:
		return

	if slot > PLAYER_LEDS:
		log("%s is player %d and has only %d LEDs" % (pad.name, slot, PLAYER_LEDS))
		return

	for number in sorted(leds):
		write_led(leds[number], 1 if number == slot else 0)


# ---------------------------------------------------------------- the screen

pads = []             # every controller, in enumeration order
claimed = []          # the pads that have claimed a slot, in claim order
started = False       # whether the "start" event has been seen
screen = "none"       # which screen we last asked ES for
cfg_problem = ""      # what went wrong the last time the order was saved


def show_message(title, text):
	global screen
	send(cmd="message", title=title, text=text)
	screen = "message"


def slot_rows():
	"""One row per player slot: who has it, or that it is waiting for somebody.

	The rows are informational.  There is nothing to select here - the input is
	the controller itself - so a select is ignored rather than being given a
	meaning the button that produced it was not asking for.
	"""
	rows = []

	for number in range(1, len(pads) + 1):
		if number <= len(claimed):
			detail = claimed[number - 1].name
		elif number == len(claimed) + 1:
			detail = "press any button"
		else:
			detail = "waiting"

		rows.append({
			"id": "slot:%d" % number,
			"label": "Player %d" % number,
			"detail": detail,
		})

	return rows


def show_list():
	"""Draw where things stand.  Called again after every claim."""
	global screen

	items = slot_rows()

	for pad in pads:
		if pad.problem:
			items.append({
				"id": "problem:" + pad.node,
				"label": pad.name,
				"detail": pad.problem,
			})

	if cfg_problem:
		items.append({
			"id": "cfg",
			"label": "Controller order",
			"detail": cfg_problem,
		})

	if pads and len(claimed) == len(pads):
		items.append({"id": "done", "label": "All controllers assigned"})

	items.append({"id": "exit", "label": "Start or B exits"})

	send(cmd="list", title=TITLE, items=items)
	screen = "list"


# ---------------------------------------------------------------- the claim

def claim(pad):
	"""This pad just pressed a button: give it the next free player slot.

	Everything is applied here and now - see the module docstring: START takes
	the screen away without warning, so an assignment that was not already on
	disk when the button went down is an assignment that never happened.
	"""
	global cfg_problem

	if pad in claimed:
		return

	claimed.append(pad)
	slot = len(claimed)
	log("player %d is %s (%s, joypad index %d)"
		% (slot, pad.name, pad.node, pad.index))

	cfg_problem = save_order(pads, claimed)
	paint_leds(pad, slot)
	show_list()


def service_pad(pad, selector):
	"""Read what one pad had to say, and claim a slot if it was a button."""
	events, alive = drain_pad(pad)

	for kind, code, value in events:
		if kind != EV_KEY or value != KEY_PRESS:
			continue
		if not is_claim_code(code):
			log("%s pressed %d, which does not claim a slot" % (pad.node, code))
			continue
		claim(pad)

	if not alive:
		selector.unregister(pad.fd)
		try:
			os.close(pad.fd)
		except OSError:
			pass
		pad.fd = None
		show_list()


# ---------------------------------------------------------------- the events

def begin(selector):
	"""The "start" event: find the pads, listen to them, draw the screen."""
	global pads

	found, problem = read_pads()
	if found is None:
		show_message(TITLE, problem)
		return

	pads = found
	log("%d controller(s): %s" % (len(pads),
		", ".join("%s=%s" % (pad.node, pad.name) for pad in pads) or "none"))

	if not pads:
		show_message(TITLE, "No controllers are connected.")
		return

	for pad in pads:
		open_pad(pad)
		if pad.fd is not None:
			selector.register(pad.fd, selectors.EVENT_READ, pad)

	show_list()


def handle_event(event, selector):
	"""One event from ES.  False means we are done and have said so."""
	global started

	name = event.get("event")

	if name == "start":
		if started:
			# ES's own START closes the addon GUI, so this is belt and braces
			log("start again, closing")
			send(cmd="close")
			return False
		started = True
		begin(selector)
		return True

	if name == "back":
		log("closing")
		send(cmd="close")
		return False

	if name == "confirm":
		# the only box this addon puts up is the one saying there is nothing
		# to assign, and its OK is the end of the addon
		if screen == "message":
			send(cmd="close")
			return False
		log("confirm on the %r screen, ignored" % screen)
		return True

	if name == "select":
		log("ignoring select on %r: the rows are informational"
			% event.get("id", ""))
		return True

	log("unknown event %r" % name)
	return True


def main():
	selector = selectors.DefaultSelector()
	selector.register(STDIN_FD, selectors.EVENT_READ, "es")

	try:
		while True:
			for key, _ in selector.select(None):
				if key.data == "es":
					feed(os.read(STDIN_FD, 4096))
				else:
					service_pad(key.data, selector)

			while True:
				event = pop_event()
				if event is None:
					break
				if not handle_event(event, selector):
					return 0

	except EOFError:
		# ES has gone away, and every screen we could ask for went with it
		log("ES closed our stdin, stopping")
		return 0


if __name__ == "__main__":
	sys.exit(main())
