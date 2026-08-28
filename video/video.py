#!/usr/bin/env python3
"""Display mode for EmulationStation, as an addon.

An addon is a headless program.  It never draws anything and never reads the
terminal: EmulationStation spawns it with pipes on its stdin and stdout, the
addon asks for screens by writing one JSON object per line to stdout, and ES
reports what the user did by writing one JSON object per line to the addon's
stdin.  The protocol is ADDONS.md in the ES source tree; the idioms here are
its reference implementation's, as are its siblings in this repository,
wifi/wifi.py and slots/slots.py.

**Why this exists.**  A Pi 5 under KMS picks its video mode from the EDID the
display hands back, and a capture path that presents no EDID - or a garbage
one, which is what a vMix input does - gets no picture at all.  The usual cure
is an EDID emulator dongle, a plug that lies about being a 1080p60 television.
The kernel parameter

    video=HDMI-A-1:1920x1080@60D

is that dongle in software: force the mode, and the trailing D means "drive
this port even with no readable EDID".  It is a boot parameter, which is the
other half of the fix - a mode set at runtime dies with the next power cycle,
and this one does not.

The addon is deliberately shallow.  It exposes exactly one setting: what the
kernel is told about the HDMI port, or nothing at all.  It does not offer
overscan, rotation, colour depth or a per-connector matrix, because none of
those were ever the problem; if this one turns out to misbehave against real
capture hardware, that is when it grows.

**cmdline.txt is the one file on a Pi that must not be got wrong.**  A mangled
kernel command line is a machine that does not boot, and there is no login
prompt to fix it from - only another computer and an SD card reader.  So:

  * the live file is /boot/firmware/cmdline.txt.  The old /boot/cmdline.txt on
    a current Raspberry Pi OS is a note saying the file has moved, and writing
    to it does nothing except destroy the note;
  * the file is VALIDATED before it is written - one line, no other newlines -
    and anything else is refused by name with nothing written.  A cmdline.txt
    this addon does not recognise is a cmdline.txt somebody else is looking
    after;
  * every token that is not a video= token comes back byte for byte, in order.
    The addon has an opinion about one token out of a dozen;
  * <cmdline>.orig is written once, ever, the first time this addon changes
    anything, and never touched again: it is what the machine shipped with.
    <cmdline>.bak is the state before THIS change, and is rewritten every time.
    A backup that will not write aborts the change, because the backup is the
    entire safety net;
  * a write that would not change a byte is not performed at all.

The screen reads the display through kmsprint, which runs as "pi" and needs no
privileges.  A kmsprint that is missing or unhappy is not fatal: 1920x1080@60
and 1280x720@60 are offered whatever it says, because "no display detected" is
precisely the EDID-less case this addon exists for, and a screen that offered
nothing then would be useless exactly when it is needed.

The write is root's business (the file belongs to root and this runs as "pi"
with no terminal to type a password into), so it goes through sudo, and
write_argv() is the one place that command line is built - tee rather than a
shell, so no part of a path is ever parsed as a command.  reboot_argv() is the
same, for the same reason.

Environment knobs, for testing:

  ES_VIDEO_CMDLINE   the cmdline.txt to read and rewrite
  ES_VIDEO_KMSPRINT  a program to run instead of "kmsprint"
  ES_VIDEO_WRITE     a program to run instead of "sudo -n tee", given the
                     destination path, with the new content on its stdin
  ES_VIDEO_REBOOT    a program to run instead of "sudo -n reboot"
"""

import json
import os
import re
import selectors
import shutil
import subprocess
import sys


# ---------------------------------------------------------------- settings

CMDLINE = os.environ.get("ES_VIDEO_CMDLINE", "/boot/firmware/cmdline.txt")
KMSPRINT_OVERRIDE = os.environ.get("ES_VIDEO_KMSPRINT", "")
WRITE_OVERRIDE = os.environ.get("ES_VIDEO_WRITE", "")
REBOOT_OVERRIDE = os.environ.get("ES_VIDEO_REBOOT", "")

TITLE = "VIDEO"

KMSPRINT_TIMEOUT = 10.0   # reading DRM state; anything slower is stuck
WRITE_TIMEOUT = 10.0      # one short file through tee
REBOOT_TIMEOUT = 10.0     # "reboot" returns at once; the machine takes longer

MAX_MODES = 12            # a picker nobody can scroll is not a picker

DEFAULT_CONNECTOR = "HDMI-A-1"    # the Pi 5's first HDMI port

# The two modes every capture device in the world accepts.  They are on the
# list whatever kmsprint says, including when it says nothing at all: a
# display that reports no modes is the case this addon was written for.
STATIC_MODES = ((1920, 1080, 60), (1280, 720, 60))

# "D" is "drive this connector even if there is no EDID to read", which is the
# whole point of forcing a mode on a capture card.
FORCE_SUFFIX = "D"


# ---------------------------------------------------------------- ES pipes

STDIN_FD = sys.stdin.fileno()

pending = b""   # stdin arrives as a byte stream: the tail waits for its newline

_stdin_sel = selectors.DefaultSelector()
_stdin_sel.register(STDIN_FD, selectors.EVENT_READ)


def send(**command):
	"""Ask ES for a screen.  One JSON object, one line, flushed immediately."""
	print(json.dumps(command), flush=True)


def log(message):
	sys.stderr.write("video addon: %s\n" % message)
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
	while True:
		event = pop_event()
		if event is not None:
			return event
		if not fill(timeout):
			return None


# ------------------------------------------------------------------- modes

def mode_name(mode):
	"""A mode as this addon writes it everywhere: WIDTHxHEIGHT@RATE."""
	return "%dx%d@%d" % mode


def mode_area(mode):
	return mode[0] * mode[1]


def order_modes(modes):
	"""Biggest picture first, and the fastest of each size before the rest."""
	return sorted(modes, key=lambda mode: (mode_area(mode), mode[2]), reverse=True)


def limit_modes(modes):
	"""At most MAX_MODES rows, with the two promised ones always among them.

	The cap exists because a 4K television offers forty modes and a list that
	long is not a list.  The promise wins over the cap: a set that would push
	1280x720@60 off the end loses its smallest unpromised mode instead, because
	the whole point of this screen is to reach a mode the display did not ask
	for.
	"""
	kept = list(modes)
	missing = [mode for mode in STATIC_MODES if mode not in kept]
	room = MAX_MODES - len(missing)

	while len(kept) > room:
		for index in range(len(kept) - 1, -1, -1):
			if kept[index] not in STATIC_MODES:
				del kept[index]
				break
		else:
			break   # nothing left to drop but the promises themselves

	return order_modes(kept + missing)


# --------------------------------------------------------------- kmsprint

CONNECTOR_LINE = re.compile(
	r"^\s*Connector\s+\d+\s+\(\d+\)\s+(\S+)\s+\((connected|disconnected)\)")

# "Crtc 2 (93) 1920x1080@60.00 148.500 ..." - what the port is doing now
CRTC_LINE = re.compile(
	r"^\s*Crtc\s+\d+\s+\(\d+\)\s+(\d+)x(\d+)(i?)@([0-9.]+)")

# "   0 1920x1080@60.00  148.500 ..." - one row of "kmsprint --modes"
MODE_LINE = re.compile(r"^\s*\d+\s+(\d+)x(\d+)(i?)@([0-9.]+)")


def kmsprint_argv(args):
	"""The one place a kmsprint command line is built.

	No sudo: reading DRM state is something the "pi" user may do, and a menu
	that needed root to tell you the resolution would be a menu that fails on
	a machine where sudo is locked down.  ES_VIDEO_KMSPRINT replaces the
	binary, which is how the tests put a mock in the way.
	"""
	return [KMSPRINT_OVERRIDE or "kmsprint"] + list(args)


def kmsprint_present():
	if KMSPRINT_OVERRIDE:
		return os.path.exists(KMSPRINT_OVERRIDE)
	return shutil.which("kmsprint") is not None


def rate_of(text):
	"""kmsprint's refresh rate, as the number a person would say.

	59.94 and 60.00 are the same row on a menu, and a television that offers
	both offers them for reasons nobody standing in front of it cares about.
	"""
	try:
		return int(round(float(text)))
	except ValueError:
		return 0


def parse_kmsprint(text):
	"""Both forms of kmsprint output, as a list of connectors.

	The plain form and --modes differ only in what they put inside a connector
	block - a Crtc line for the mode in use, or one numbered row per supported
	mode - so one parser reads both and each caller takes the part it wants.
	Interlaced modes (the 'i' on the resolution) are dropped here and never
	seen again: half a frame at a time is not something to offer a capture
	card, and it is not what "1080p60" means.
	"""
	connectors = []
	current = None

	for line in (text or "").splitlines():
		match = CONNECTOR_LINE.match(line)
		if match is not None:
			current = {
				"name": match.group(1),
				"connected": match.group(2) == "connected",
				"crtc": None,
				"modes": [],
			}
			connectors.append(current)
			continue

		if current is None:
			continue

		match = CRTC_LINE.match(line)
		if match is not None:
			if not match.group(3):
				current["crtc"] = (int(match.group(1)), int(match.group(2)),
					rate_of(match.group(4)))
			continue

		match = MODE_LINE.match(line)
		if match is not None and not match.group(3):
			current["modes"].append((int(match.group(1)), int(match.group(2)),
				rate_of(match.group(4))))

	return connectors


def run_kmsprint(args):
	"""One kmsprint, as a list of connectors, or None if it would not answer.

	None is a state, not a crash: everything downstream has a fallback for it,
	because a Pi with no display attached is the machine this addon is for.
	"""
	argv = kmsprint_argv(args)

	try:
		done = subprocess.run(
			argv,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=KMSPRINT_TIMEOUT,
			text=True,
			errors="replace",
		)
	except subprocess.TimeoutExpired:
		log("%s did not answer within %gs" % (" ".join(argv), KMSPRINT_TIMEOUT))
		return None
	except OSError as error:
		log("could not run %s: %s" % (" ".join(argv), error))
		return None

	if done.returncode != 0:
		log("%s -> exit %d, %r" % (" ".join(argv), done.returncode,
			(done.stderr or "").strip()))
		return None

	return parse_kmsprint(done.stdout)


def first_connected(connectors):
	"""The connector this addon is talking about.

	The first connected one, because a Pi 5 has two HDMI ports and the picture
	is on the one with a cable in it.  Nothing connected at all still names a
	connector - the one the kernel listed first - so the forced mode has a port
	to be forced on.
	"""
	if not connectors:
		return None
	for connector in connectors:
		if connector["connected"]:
			return connector
	return connectors[0]


def read_display():
	"""What the display is doing right now.

	{"read": did kmsprint answer, "connector": name or "",
	 "connected": bool, "mode": (w, h, rate) or None}
	"""
	connectors = run_kmsprint([])
	if connectors is None:
		return {"read": False, "connector": "", "connected": False, "mode": None}

	connector = first_connected(connectors)
	if connector is None:
		return {"read": True, "connector": "", "connected": False, "mode": None}

	return {
		"read": True,
		"connector": connector["name"],
		"connected": connector["connected"],
		"mode": connector["crtc"],
	}


def read_offered_modes():
	"""The modes the display says it can take, deduped and ordered.

	Same resolution at 59.94 and at 60.00 is one row, and the first reading
	wins - kmsprint lists a connector's preferred timing first, so "the first
	one" is the one the display would rather have.
	"""
	connectors = run_kmsprint(["--modes"])
	if connectors is None:
		return []

	connector = first_connected(connectors)
	if connector is None or not connector["connected"]:
		return []

	seen = []
	for mode in connector["modes"]:
		if mode not in seen:
			seen.append(mode)

	return order_modes(seen)


# --------------------------------------------------------------- cmdline

FORCED_TOKEN = re.compile(
	r"^video=([^:]+):(\d+)x(\d+)@(\d+(?:\.\d+)?)([A-Za-z]*)$")


def read_cmdline():
	"""The kernel command line, validated.  (text, error).

	error is a sentence for a message box and text is None when this file is
	not something to rewrite.  It is the ONE validator: the main list uses it
	to say what is forced, and the write uses it again immediately before
	touching anything, because the answer has to be about the file as it is at
	the moment of the write and not as it was when the screen was drawn.

	What counts as a kernel command line: exactly one non-empty line, with at
	most a single trailing newline after it.  Real cmdline.txt is one line -
	the bootloader reads the whole file as the command line, so a second line
	is a second lot of parameters silently glued onto the first - and a file
	with more in it is a file some other tool is managing, not this one.
	"""
	try:
		with open(CMDLINE, "r", encoding="utf-8", errors="surrogateescape",
				newline="") as handle:
			text = handle.read()
	except OSError as error:
		return None, "Could not read %s: %s." % (CMDLINE, error.strerror or error)

	body = text[:-1] if text.endswith("\n") else text

	if "\n" in body or "\r" in body:
		return None, ("%s is not a single line, so this addon will not rewrite "
			"it. Something else is managing it." % CMDLINE)

	if not body.strip():
		return None, ("%s is empty, so there is no kernel command line to "
			"change." % CMDLINE)

	return text, ""


def forced_mode(text):
	"""What the command line forces.  (mode or None, the raw token or "").

	A video= token this addon did not write - a different syntax, a mode with
	flags it does not know - comes back as raw text with no mode, so the screen
	shows it verbatim rather than pretending to understand it.
	"""
	for token in (text or "").split():
		if not token.startswith("video="):
			continue
		match = FORCED_TOKEN.match(token)
		if match is None:
			return None, token
		return (int(match.group(2)), int(match.group(3)),
			rate_of(match.group(4))), token
	return None, ""


def rewrite_cmdline(text, connector, mode):
	"""The command line with our video= token set, or removed for mode None.

	Every other token comes back exactly as it went in and in the order it was
	in: this is the whole of how the kernel is started and the addon has an
	opinion about one token of it.  Whitespace between tokens becomes a single
	space, which is what a kernel command line is.
	"""
	tokens = [token for token in text.split() if not token.startswith("video=")]

	if mode is not None:
		tokens.append("video=%s:%s%s" % (connector, mode_name(mode), FORCE_SUFFIX))

	return " ".join(tokens) + "\n"


def write_argv(path):
	"""The one place a privileged write is built: the argv, path included.

	/boot/firmware/cmdline.txt belongs to root and the addon runs as "pi" with
	no terminal, so the real write is "sudo -n tee <path>" with the content on
	stdin - tee rather than a shell, so no part of a path is ever parsed as a
	command.  -n means a sudo that wants a password fails immediately instead
	of waiting forever on a terminal this process does not have.

	ES_VIDEO_WRITE replaces the whole thing with one program taking the
	destination path, which is how the tests watch what would have been
	written.
	"""
	if WRITE_OVERRIDE:
		return [WRITE_OVERRIDE, path]
	return ["sudo", "-n", "tee", path]


def write_file(path, content):
	"""Put content in path, through root.  "" when it stuck, else why not."""
	argv = write_argv(path)

	try:
		done = subprocess.run(
			argv,
			input=content,
			stdout=subprocess.DEVNULL,     # tee echoes what it wrote; ES is not interested
			stderr=subprocess.PIPE,
			timeout=WRITE_TIMEOUT,
			text=True,
			errors="replace",
		)
	except subprocess.TimeoutExpired:
		log("%s did not answer within %gs" % (argv[0], WRITE_TIMEOUT))
		return "%s did not answer within %g seconds." % (argv[0], WRITE_TIMEOUT)
	except OSError as error:
		log("could not run %s: %s" % (argv[0], error))
		return "Could not run %s: %s" % (argv[0], error)

	if done.returncode != 0:
		detail = (done.stderr or "").strip().splitlines()
		reason = detail[-1] if detail else "exit status %d" % done.returncode
		log("%s -> exit %d, %r" % (" ".join(argv), done.returncode, reason))
		return "Could not write %s: %s" % (path, reason)

	log("wrote %s" % path)
	return ""


def reboot_argv():
	"""The one place the reboot command line is built.

	Same shape as write_argv and for the same reason: rebooting is root's
	business, the addon has no terminal, and -n turns a sudo that wants a
	password into an immediate failure instead of a hang.
	"""
	if REBOOT_OVERRIDE:
		return [REBOOT_OVERRIDE]
	return ["sudo", "-n", "reboot"]


def apply_mode(connector, mode):
	"""Set (or clear) the forced mode in cmdline.txt.  "" or the reason why not.

	The order is the whole of the paranoia: validate, decide, and only then
	write - backups first, and the file the machine boots from last.  A backup
	that will not write stops everything, because writing the command line with
	no way back is the one outcome worth refusing.
	"""
	global changed

	text, error = read_cmdline()
	if error:
		return error

	updated = rewrite_cmdline(text, connector, mode)
	if updated == text:
		log("%s already says %s" % (CMDLINE,
			mode_name(mode) if mode is not None else "nothing about video"))
		return ""

	if not os.path.exists(CMDLINE + ".orig"):
		# what the machine shipped with, written once and never again: after
		# three changes the .bak is three changes deep and this is still the
		# line that booted the Pi the day it arrived
		error = write_file(CMDLINE + ".orig", text)
		if error:
			return error

	error = write_file(CMDLINE + ".bak", text)
	if error:
		return error

	error = write_file(CMDLINE, updated)
	if error:
		return error

	changed = True
	return ""


def reboot():
	"""Restart the machine.  "" when the command was accepted, else why not."""
	argv = reboot_argv()

	try:
		done = subprocess.run(
			argv,
			stdin=subprocess.DEVNULL,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			timeout=REBOOT_TIMEOUT,
			text=True,
			errors="replace",
		)
	except subprocess.TimeoutExpired:
		log("%s did not answer within %gs" % (argv[0], REBOOT_TIMEOUT))
		return "%s did not answer within %g seconds." % (argv[0], REBOOT_TIMEOUT)
	except OSError as error:
		log("could not run %s: %s" % (argv[0], error))
		return "Could not run %s: %s" % (argv[0], error)

	if done.returncode != 0:
		detail = (done.stderr or "").strip().splitlines()
		reason = detail[-1] if detail else "exit status %d" % done.returncode
		log("%s -> exit %d, %r" % (" ".join(argv), done.returncode, reason))
		return "Could not reboot: %s" % reason

	log("reboot requested")
	return ""


# ---------------------------------------------------------------- screens

screen = "none"           # which screen we last asked ES for: it decides "back"
after_message = "main"    # where the OK of the current message goes

display = None            # what kmsprint said, as of the last main list
forced = None             # the mode cmdline.txt forces, as of the last main list
forced_raw = ""           # ...as the token itself, for a syntax we did not write
cmdline_error = ""        # ...or why the file could not be read at all
modes = []                # the rows the mode picker is showing
choice = None             # the mode a question is up about; None means automatic
changed = False           # did WE rewrite cmdline.txt during this session


def show_message(title, text, after="main"):
	global screen, after_message
	send(cmd="message", title=title, text=text)
	screen = "message"
	after_message = after


def target_connector():
	"""The connector a forced mode goes on.

	The one with the cable in it, or the one already named in the command line
	(a machine set up for a capture card that is unplugged right now still
	knows which port it was set up for), or the Pi's first HDMI socket.
	"""
	if display and display["connector"]:
		return display["connector"]

	match = FORCED_TOKEN.match(forced_raw) if forced_raw else None
	if match is not None:
		return match.group(1)

	return DEFAULT_CONNECTOR


def live_description():
	"""What the status row's detail says about the display."""
	if display is None or not display["read"]:
		return "could not be read"
	if not display["connected"]:
		return "no display detected"
	if display["mode"] is None:
		return "%s, no mode set" % display["connector"]
	return "%s on %s" % (mode_name(display["mode"]), display["connector"])


def boot_description():
	"""What the command line forces, in words."""
	if cmdline_error:
		return "unreadable"
	if forced is not None:
		return "Forced: %s" % mode_name(forced)
	if forced_raw:
		return "Forced: %s" % forced_raw[len("video="):]
	return "Automatic (EDID)"


def reboot_pending():
	"""Is the command line saying something the running kernel is not doing?

	Two ways to know, and both are needed.  A forced mode that is not the mode
	on screen is the plain one.  The other is a removal: "automatic" makes no
	claim about the current mode, so nothing can be compared against it - but
	the addon knows perfectly well that it wrote the file a minute ago, and
	that flag is the only honest answer for that case.
	"""
	if changed:
		return True
	if forced is None or display is None or display["mode"] is None:
		return False
	return forced != display["mode"]


def show_main_list():
	"""The display, the setting, and every mode the setting could be."""
	global screen, display, forced, forced_raw, cmdline_error, modes

	display = read_display()

	text, cmdline_error = read_cmdline()
	if cmdline_error:
		log(cmdline_error)
		forced, forced_raw = None, ""
	else:
		forced, forced_raw = forced_mode(text)

	modes = limit_modes(read_offered_modes())

	detail = boot_description()
	if reboot_pending():
		detail += " (reboot to apply)"

	items = [
		{"id": "status", "label": "Display", "detail": live_description()},
		{"id": "mode", "label": "Mode", "detail": detail},
	]

	if forced is not None or forced_raw:
		items.append({
			"id": "auto",
			"label": "Automatic (use the display)",
			"detail": "remove the forced mode",
		})

	for mode in modes:
		row = {"id": "mode:%s" % mode_name(mode), "label": mode_name(mode)}
		if mode == forced:
			row["detail"] = "forced"
		items.append(row)

	send(cmd="list", title=TITLE, items=items)
	screen = "main"


def show_status_message():
	"""The fuller story behind the status row: the port, the mode, the setting."""
	lines = []

	if display is None or not display["read"]:
		lines.append("The display could not be read: kmsprint would not answer.")
	elif not display["connected"]:
		lines.append("No display is connected.")
		if display["connector"]:
			lines.append("Connector: %s" % display["connector"])
	else:
		lines.append("Connector: %s" % display["connector"])
		lines.append("Current mode: %s" % (mode_name(display["mode"])
			if display["mode"] is not None else "none set"))

	if cmdline_error:
		lines.append("Boot setting: %s" % cmdline_error)
	elif forced is not None:
		lines.append("Boot setting: forced to %s on %s"
			% (mode_name(forced), target_connector()))
	elif forced_raw:
		lines.append("Boot setting: %s" % forced_raw)
	else:
		lines.append("Boot setting: automatic - the display's own EDID decides.")

	show_message("DISPLAY", "\n".join(lines))


def show_mode_message():
	"""What the Mode row is, for somebody who selected it to find out."""
	lines = [
		"%s is set by the kernel command line in %s, so it survives every "
		"reboot." % (boot_description(), CMDLINE),
		"",
		"Forcing a mode is what makes a capture card or a switch work when it "
		"reports no EDID of its own. Pick one below, or Automatic to let the "
		"display decide.",
	]
	show_message("MODE", "\n".join(lines))


def ask_apply(mode):
	"""The question every change goes through.  mode None is Automatic."""
	global screen, choice

	choice = mode

	if mode is None:
		text = ("Return to the display's own EDID? The change takes effect "
			"after a reboot. If the screen stays black afterward, put a "
			"video= token back into %s from ssh." % CMDLINE)
		title = "AUTOMATIC?"
	else:
		text = ("Force %s? The change takes effect after a reboot. If the "
			"screen stays black afterward, edit video= out of %s from ssh."
			% (mode_name(mode), CMDLINE))
		title = "FORCE %s?" % mode_name(mode)

	send(cmd="confirm", title=title, text=text)
	screen = "confirm_apply"


def ask_reboot():
	global screen
	send(cmd="confirm", title="REBOOT?",
		text="The new display setting takes effect on the next boot. Reboot now?")
	screen = "confirm_reboot"


# ---------------------------------------------------------------- actions

def do_apply():
	global screen

	error = apply_mode(target_connector(), choice)
	if error:
		show_message("NOT CHANGED", error)
		return

	ask_reboot()


def do_reboot():
	global screen

	error = reboot()
	if error:
		show_message("NOT REBOOTED", error)
		return

	send(cmd="progress", title="REBOOT", text="Rebooting...")
	screen = "rebooting"


# ---------------------------------------------------------------- events

def on_select(item_id):
	if item_id == "status":
		show_status_message()

	elif item_id == "mode":
		show_mode_message()

	elif item_id == "auto":
		ask_apply(None)

	elif item_id.startswith("mode:"):
		wanted = item_id[len("mode:"):]
		for mode in modes:
			if mode_name(mode) == wanted:
				ask_apply(mode)
				return
		log("no such mode %r" % wanted)
		show_main_list()

	else:
		log("unknown item id %r" % item_id)
		show_main_list()


def on_confirm(value):
	if screen == "confirm_apply":
		if value:
			do_apply()
		else:
			show_main_list()

	elif screen == "confirm_reboot":
		if value:
			do_reboot()
		else:
			show_main_list()

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
	# anywhere else means the screen before it.  ES draws the BACK button on
	# every list itself, so there is no row here that does this.
	if screen == "main":
		send(cmd="close")
		sys.exit(0)

	if screen == "message":
		on_confirm(True)
	elif screen == "rebooting":
		# the machine is going down and this screen goes with it - but if it
		# somehow is not, B is not a trap
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
				if not kmsprint_present():
					log("kmsprint is not installed; the static modes are all "
						"this screen can offer")
				show_main_list()
			elif name == "select":
				on_select(event.get("id", ""))
			elif name == "confirm":
				on_confirm(bool(event.get("value")))
			elif name == "back":
				on_back()
			elif name == "text":
				log("text on the %r screen; this addon asks for none" % screen)
				show_main_list()
			else:
				log("unknown event %r" % name)

		except EOFError:
			# ES has gone away, and every screen we could ask for went with it
			log("ES closed our stdin, stopping")
			return 0


if __name__ == "__main__":
	sys.exit(main())
