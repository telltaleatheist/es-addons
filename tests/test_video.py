#!/usr/bin/env python3
"""End-to-end test for the video addon: this script plays EmulationStation.

It spawns video.py exactly as ES does - pipes on stdin and stdout, one JSON
object per line each way - with ES_VIDEO_KMSPRINT, ES_VIDEO_WRITE and
ES_VIDEO_REBOOT pointing at mocks and ES_VIDEO_CMDLINE at a copy of a real
Pi 5 cmdline.txt, and walks the whole addon:

  * the main list: the status row reading the connected display, the Mode row
    reading the command line, and a mode picker with 59.94 collapsed into 60,
    the interlaced rows gone, the two promised modes present and the cap kept
  * the status row's details screen, and the Mode row's
  * forcing a mode: the question, the rewrite with every other token byte for
    byte, the video= token with its D, the .orig written once and the .bak
    written before the file the machine boots from, the reboot question, and
    declining it for a list that says "(reboot to apply)"
  * forcing a second mode, which replaces the token rather than adding one,
    leaves .orig alone and refreshes .bak
  * choosing the same mode twice, where the second time writes nothing at all
  * a write helper that refuses, which aborts at the backup and leaves
    cmdline.txt untouched
  * Automatic, which takes the token out again
  * a reboot that refuses, and then one that does not
  * "back" on the main list, which closes the addon with status 0
  * a cmdline.txt that is not one line, which is refused by name and untouched
  * a Pi with no display at all, which still offers 1080p60 and 720p60 on
    HDMI-A-1 - the whole reason this addon exists
  * a display on the second HDMI port, which is the port the token names
  * a machine with no kmsprint

Run it: python3 tests/test_video.py
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
ADDON = os.path.join(os.path.dirname(HERE), "video", "video.py")
MOCK_KMSPRINT = os.path.join(HERE, "mock-kmsprint.py")
MOCK_WRITE = os.path.join(HERE, "mock-video-write.py")
MOCK_REBOOT = os.path.join(HERE, "mock-reboot.py")
CMDLINE_FIXTURE = os.path.join(HERE, "mock-cmdline", "cmdline.txt")
GARBAGE_FIXTURE = os.path.join(HERE, "mock-cmdline", "cmdline-garbage.txt")

READ_TIMEOUT = 30.0


class Fail(Exception):
	pass


def check(condition, message):
	if not condition:
		raise Fail(message)


# ----------------------------------------------------------------- fixtures

# A 4K television's mode list, with everything that makes this hard: the same
# resolution at 59.94 and at 60.00, interlaced rows wearing the 'i', and more
# modes than any menu should show.
TV_MODES = [
	"4096x2160@30.00",
	"3840x2160@30.00",
	"3840x2160@29.97",
	"1920x1080@60.00",
	"1920x1080@59.94",
	"1920x1080i@60.00",
	"1920x1080i@59.94",
	"1920x1080@50.00",
	"1680x1050@59.88",
	"1600x900@60.00",
	"1440x900@59.90",
	"1280x1024@75.02",
	"1280x1024@60.02",
	"1280x720@60.00",
	"1280x720@59.94",
	"1280x720@50.00",
	"1024x768@60.00",
	"800x600@60.32",
	"720x576@50.00",
	"720x480@60.00",
	"720x480@59.94",
	"640x480@60.00",
	"640x480@59.94",
]

# What the picker must make of that: deduped to one row per rounded rate,
# no interlaced row anywhere, biggest picture first and the fastest of each
# size before the rest, and twelve rows and no more.
EXPECTED_MODES = [
	"4096x2160@30",
	"3840x2160@30",
	"1920x1080@60",
	"1920x1080@50",
	"1680x1050@60",
	"1600x900@60",
	"1280x1024@75",
	"1280x1024@60",
	"1440x900@60",
	"1280x720@60",
	"1280x720@50",
	"1024x768@60",
]

TV_SEED = {
	"connectors": [
		{"name": "HDMI-A-1", "connected": True,
			"crtc": "1920x1080@60.00", "modes": TV_MODES},
		{"name": "HDMI-A-2", "connected": False, "crtc": None, "modes": []},
	],
	"fail": False,
	"log": [],
}

# Nothing plugged in: no connector reports a mode, and this is precisely the
# case the addon exists for - a capture input with no EDID looks like this.
NO_DISPLAY_SEED = {
	"connectors": [
		{"name": "HDMI-A-1", "connected": False, "crtc": None, "modes": []},
		{"name": "HDMI-A-2", "connected": False, "crtc": None, "modes": []},
	],
	"fail": False,
	"log": [],
}

# A computer monitor: eighteen modes and neither of the two this addon
# promises, so the cap and the promise are in direct conflict and the promise
# has to win - a list capped to its twelve biggest modes would not contain
# 720p60, which is the one mode a stubborn capture card always takes.
MONITOR_MODES = [
	"3840x2160@60.00", "3840x2160@30.00",
	"2560x1440@60.00", "2560x1440@50.00",
	"1920x1200@60.00",
	"1920x1080@50.00", "1920x1080@30.00",
	"1600x1200@60.00", "1680x1050@60.00", "1600x900@60.00",
	"1440x900@60.00", "1366x768@60.00", "1280x1024@60.00",
	"1280x800@60.00", "1152x864@75.00", "1024x768@60.00",
	"800x600@60.00", "640x480@60.00",
]

EXPECTED_MONITOR_MODES = [
	"3840x2160@60",
	"3840x2160@30",
	"2560x1440@60",
	"2560x1440@50",
	"1920x1200@60",
	"1920x1080@60",     # promised, and the monitor never offered it
	"1920x1080@50",
	"1920x1080@30",
	"1600x1200@60",
	"1680x1050@60",
	"1600x900@60",
	"1280x720@60",      # promised, and it cost the monitor its smallest modes
]

MONITOR_SEED = {
	"connectors": [
		{"name": "HDMI-A-1", "connected": True,
			"crtc": "3840x2160@60.00", "modes": MONITOR_MODES},
	],
	"fail": False,
	"log": [],
}

# The cable is in the other socket, which is the port the forced mode has to
# name: "video=HDMI-A-1:..." on a Pi whose picture is on HDMI-A-2 does nothing.
SECOND_PORT_SEED = {
	"connectors": [
		{"name": "HDMI-A-1", "connected": False, "crtc": None, "modes": []},
		{"name": "HDMI-A-2", "connected": True, "crtc": "1280x720@60.00",
			"modes": ["1920x1080@60.00", "1280x720@60.00", "720x480@59.94"]},
	],
	"fail": False,
	"log": [],
}


# --------------------------------------------------------------- the fake Pi

class World(object):
	"""One temp directory's worth of fake Pi.

	A cmdline.txt of its own (copied, because the test rewrites it), the
	kmsprint state, and the logs the two privileged helpers leave behind.  Each
	run of the addon gets a fresh one, so a run that mangles a file cannot
	quietly change what a later run is testing.
	"""

	def __init__(self, root, name, seed, cmdline_source=CMDLINE_FIXTURE):
		self.dir = os.path.join(root, name)
		os.makedirs(self.dir)

		self.cmdline = os.path.join(self.dir, "cmdline.txt")
		shutil.copy(cmdline_source, self.cmdline)

		self.state = os.path.join(self.dir, "kmsprint-state.json")
		with open(self.state, "w") as handle:
			json.dump(seed, handle, indent=2)

		self.write_log = os.path.join(self.dir, "writes.jsonl")
		self.write_fail = os.path.join(self.dir, "write-fails")
		self.reboot_log = os.path.join(self.dir, "reboots")
		self.reboot_fail = os.path.join(self.dir, "reboot-fails")

	# ------------------------------------------------------------ the files

	@property
	def orig(self):
		return self.cmdline + ".orig"

	@property
	def bak(self):
		return self.cmdline + ".bak"

	def contents(self, path):
		if not os.path.exists(path):
			return None
		with open(path, "r", newline="") as handle:
			return handle.read()

	def cmdline_text(self):
		return self.contents(self.cmdline)

	def tokens(self, path=None):
		text = self.contents(path or self.cmdline)
		return text.split() if text is not None else None

	def video_tokens(self, path=None):
		return [token for token in (self.tokens(path) or [])
			if token.startswith("video=")]

	# ------------------------------------------------------------- the logs

	def writes(self):
		if not os.path.exists(self.write_log):
			return []
		with open(self.write_log) as handle:
			return [json.loads(line) for line in handle if line.strip()]

	def written_paths(self):
		return [os.path.basename(entry["path"]) for entry in self.writes()]

	def reboots(self):
		if not os.path.exists(self.reboot_log):
			return []
		with open(self.reboot_log) as handle:
			return [line for line in handle.read().splitlines()]

	def kmsprint_calls(self):
		with open(self.state) as handle:
			return json.load(handle).get("log", [])

	# ------------------------------------------------------------ the switch

	def set_write_failing(self, failing):
		if failing:
			open(self.write_fail, "w").close()
		elif os.path.exists(self.write_fail):
			os.unlink(self.write_fail)

	def set_reboot_failing(self, failing):
		if failing:
			open(self.reboot_fail, "w").close()
		elif os.path.exists(self.reboot_fail):
			os.unlink(self.reboot_fail)


# ------------------------------------------------------------- ES stand-in

class Addon(object):
	"""The addon, and the pipes ES would be holding."""

	def __init__(self, world, programs, kmsprint=None):
		environment = dict(os.environ)
		environment["ES_VIDEO_CMDLINE"] = world.cmdline
		environment["ES_VIDEO_KMSPRINT"] = kmsprint or programs["kmsprint"]
		environment["ES_VIDEO_WRITE"] = programs["write"]
		environment["ES_VIDEO_REBOOT"] = programs["reboot"]
		environment["MOCK_KMSPRINT_STATE"] = world.state
		environment["MOCK_VIDEO_WRITE_LOG"] = world.write_log
		environment["MOCK_VIDEO_WRITE_FAIL"] = world.write_fail
		environment["MOCK_REBOOT_LOG"] = world.reboot_log
		environment["MOCK_REBOOT_FAIL"] = world.reboot_fail

		self.log_path = os.path.join(world.dir, "addon.stderr")
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


def labels(message):
	return [item.get("label") for item in message["items"]]


def ids(message):
	return [item.get("id") for item in message["items"]]


def detail(message, label):
	for item in message["items"]:
		if item.get("label") == label:
			return item.get("detail")
	raise Fail("no row labelled %r in %r" % (label, labels(message)))


def mode_labels(message):
	"""The picker's rows, without the two rows above them or Automatic."""
	return [item.get("label") for item in message["items"]
		if str(item.get("id", "")).startswith("mode:")]


def force(addon, mode_id, answer=True):
	"""Pick a mode row and answer its question.  Returns the question."""
	addon.send(event="select", id=mode_id)
	question = addon.expect("confirm")
	addon.send(event="confirm", value=answer)
	return question


# ------------------------------------------------------------------ steps

STEPS = []

BASE_TOKENS = open(CMDLINE_FIXTURE).read().split()
BASE_TEXT = open(CMDLINE_FIXTURE, newline="").read()


def step(title):
	def decorate(function):
		STEPS.append((title, function))
		return function
	return decorate


@step("main list: the display, the setting, and a deduped mode picker")
def step_main_list(addon, world):
	addon.send(event="start")
	main = addon.expect("list")

	check(main["title"] == "VIDEO", "title was %r" % main["title"])

	check(ids(main)[0] == "status", "the status row is not first: %r" % ids(main))
	check(labels(main)[0] == "Display", "the first row is %r" % labels(main)[0])
	check(detail(main, "Display") == "1920x1080@60 on HDMI-A-1",
		"the status row shows %r" % detail(main, "Display"))

	check(ids(main)[1] == "mode", "the Mode row is not second: %r" % ids(main))
	check(detail(main, "Mode") == "Automatic (EDID)",
		"the Mode row shows %r" % detail(main, "Mode"))

	check("auto" not in ids(main),
		"an Automatic row was offered with no mode forced: %r" % ids(main))

	rows = mode_labels(main)
	check(rows == EXPECTED_MODES, "the picker offered %r" % rows)
	check(len(rows) <= 12, "the picker offered %d rows" % len(rows))
	check("1920x1080@60" in rows, "1080p60 is missing from %r" % rows)
	check("1280x720@60" in rows, "720p60 is missing from %r" % rows)

	interlaced = [row for row in rows if "i@" in row]
	check(not interlaced, "interlaced modes were offered: %r" % interlaced)
	check(rows.count("1920x1080@60") == 1,
		"59.94 and 60.00 were not collapsed: %r" % rows)

	calls = world.kmsprint_calls()
	check(calls == ["", "--modes"],
		"kmsprint was called %r" % calls)

	# ES draws the BACK button itself, so an addon that adds one is adding a
	# second way out that does not match the others
	check(not [row for row in labels(main)
			if row and row.lower() in ("back", "exit")],
		"the addon drew its own back row: %r" % labels(main))


@step("the status row opens the details: the port, the mode, the setting")
def step_status_details(addon, world):
	addon.send(event="select", id="status")
	message = addon.expect("message")

	text = message["text"]
	for wanted in ("HDMI-A-1", "1920x1080@60", "automatic"):
		check(wanted in text, "%r is missing from %r" % (wanted, text))

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(main["title"] == "VIDEO", "OK went to %r" % main["title"])


@step("the Mode row explains itself, and names the file it lives in")
def step_mode_details(addon, world):
	addon.send(event="select", id="mode")
	message = addon.expect("message")

	check(world.cmdline in message["text"],
		"the file is not named in %r" % message["text"])
	check("EDID" in message["text"],
		"the reason is not given in %r" % message["text"])

	addon.send(event="confirm", value=True)
	addon.expect("list")


@step("forcing a mode: the rewrite, the backups, and the reboot question")
def step_force(addon, world):
	question = force(addon, "mode:1280x720@60", answer=False)

	check(question["text"].startswith(
			"Force 1280x720@60? The change takes effect after a reboot."),
		"the question was %r" % question["text"])
	check("video=" in question["text"] and world.cmdline in question["text"],
		"the way out of a black screen is not in %r" % question["text"])

	# declining the question changes nothing at all
	addon.expect("list")
	check(world.cmdline_text() == BASE_TEXT, "a declined question wrote the file")
	check(world.writes() == [], "a declined question ran the write helper")

	force(addon, "mode:1280x720@60", answer=True)
	reboot = addon.expect("confirm")
	check("Reboot now?" in reboot["text"], "the question was %r" % reboot["text"])

	tokens = world.tokens()
	check(tokens[:len(BASE_TOKENS)] == BASE_TOKENS,
		"a token that is not ours changed: %r" % tokens)
	check(tokens == BASE_TOKENS + ["video=HDMI-A-1:1280x720@60D"],
		"the command line is now %r" % tokens)

	text = world.cmdline_text()
	check(text.endswith("\n") and text.count("\n") == 1,
		"the file is not one line and a newline: %r" % text)

	check(world.contents(world.orig) == BASE_TEXT,
		"the .orig is %r" % world.contents(world.orig))
	check(world.contents(world.bak) == BASE_TEXT,
		"the .bak is %r" % world.contents(world.bak))

	# the backups go down BEFORE the file the machine boots from
	check(world.written_paths()
			== ["cmdline.txt.orig", "cmdline.txt.bak", "cmdline.txt"],
		"the writes were %r" % world.written_paths())

	addon.send(event="confirm", value=False)
	main = addon.expect("list")

	check(detail(main, "Mode") == "Forced: 1280x720@60 (reboot to apply)",
		"the Mode row shows %r" % detail(main, "Mode"))
	check("auto" in ids(main),
		"no Automatic row with a mode forced: %r" % ids(main))
	check(detail(main, "1280x720@60") == "forced",
		"the forced mode is marked %r" % detail(main, "1280x720@60"))


@step("a second mode replaces the token, keeps .orig and refreshes .bak")
def step_force_again(addon, world):
	first = world.cmdline_text()

	force(addon, "mode:1920x1080@50", answer=True)
	addon.expect("confirm")

	check(world.video_tokens() == ["video=HDMI-A-1:1920x1080@50D"],
		"the video tokens are %r" % world.video_tokens())
	check(world.tokens()[:len(BASE_TOKENS)] == BASE_TOKENS,
		"a token that is not ours changed: %r" % world.tokens())

	check(world.contents(world.orig) == BASE_TEXT,
		"the .orig was overwritten: %r" % world.contents(world.orig))
	check(world.contents(world.bak) == first,
		"the .bak is %r, not the state before this change" % world.contents(world.bak))

	# .orig exists now, so only the .bak and the file itself are written
	check(world.written_paths()[-2:] == ["cmdline.txt.bak", "cmdline.txt"],
		"the writes were %r" % world.written_paths())

	addon.send(event="confirm", value=False)
	main = addon.expect("list")
	check(detail(main, "Mode") == "Forced: 1920x1080@50 (reboot to apply)",
		"the Mode row shows %r" % detail(main, "Mode"))


@step("the same mode twice: the second time writes nothing at all")
def step_idempotent(addon, world):
	before_writes = len(world.writes())
	before_bak = world.contents(world.bak)
	before_text = world.cmdline_text()

	force(addon, "mode:1920x1080@50", answer=True)
	addon.expect("confirm")

	check(len(world.writes()) == before_writes,
		"the write helper ran %d more times" % (len(world.writes()) - before_writes))
	check(world.contents(world.bak) == before_bak, "the .bak was rewritten")
	check(world.cmdline_text() == before_text, "the command line was rewritten")

	addon.send(event="confirm", value=False)
	addon.expect("list")


@step("a write helper that refuses aborts at the backup, file untouched")
def step_write_fails(addon, world):
	before_text = world.cmdline_text()
	before_bak = world.contents(world.bak)

	world.set_write_failing(True)
	force(addon, "mode:1280x720@60", answer=True)
	message = addon.expect("message")
	world.set_write_failing(False)

	check(world.cmdline in message["text"],
		"the file is not named in %r" % message["text"])

	check(world.cmdline_text() == before_text,
		"cmdline.txt was written anyway: %r" % world.cmdline_text())
	check(world.contents(world.bak) == before_bak, "the .bak was written anyway")

	failed = [entry for entry in world.writes() if entry["failed"]]
	check(len(failed) == 1, "the helper was tried %d times" % len(failed))
	check(failed[0]["path"] == world.bak,
		"it gave up at %r rather than the backup" % failed[0]["path"])
	check(world.written_paths()[-1] == "cmdline.txt.bak",
		"it went on to write %r" % world.written_paths()[-1])

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(detail(main, "Mode") == "Forced: 1920x1080@50 (reboot to apply)",
		"the failed write changed the Mode row to %r" % detail(main, "Mode"))


@step("Automatic takes the token back out")
def step_automatic(addon, world):
	before = world.cmdline_text()

	addon.send(event="select", id="auto")
	question = addon.expect("confirm")
	check("EDID" in question["text"], "the question was %r" % question["text"])

	addon.send(event="confirm", value=True)
	addon.expect("confirm")

	check(world.cmdline_text() == BASE_TEXT,
		"the command line is %r" % world.cmdline_text())
	check(world.video_tokens() == [], "a video= token survived")
	check(world.contents(world.orig) == BASE_TEXT, "the .orig was overwritten")
	check(world.contents(world.bak) == before,
		"the .bak is %r" % world.contents(world.bak))

	addon.send(event="confirm", value=False)
	main = addon.expect("list")
	check(detail(main, "Mode") == "Automatic (EDID) (reboot to apply)",
		"the Mode row shows %r" % detail(main, "Mode"))
	check("auto" not in ids(main),
		"an Automatic row is still offered: %r" % ids(main))


@step("a reboot that refuses is a message, not a hang")
def step_reboot_fails(addon, world):
	force(addon, "mode:1280x720@60", answer=True)
	addon.expect("confirm")

	world.set_reboot_failing(True)
	addon.send(event="confirm", value=True)
	message = addon.expect("message")
	world.set_reboot_failing(False)

	check(message["title"] == "NOT REBOOTED", "the title was %r" % message["title"])
	check(len(world.reboots()) == 1, "the reboot ran %d times" % len(world.reboots()))

	addon.send(event="confirm", value=True)
	addon.expect("list")


@step("accepting the reboot runs the reboot command")
def step_reboot(addon, world):
	before = len(world.writes())

	# the mode is already the one in the file, so this asks the question and
	# writes nothing - and the reboot is still exactly what is wanted
	force(addon, "mode:1280x720@60", answer=True)
	addon.expect("confirm")
	check(len(world.writes()) == before, "the settled mode was written again")

	addon.send(event="confirm", value=True)
	progress = addon.expect("progress")
	check(progress["title"] == "REBOOT", "the screen was %r" % progress["title"])

	check(len(world.reboots()) == 2,
		"the reboot command ran %d times in all" % len(world.reboots()))


@step("back on the main list closes the addon, with status 0")
def step_close(addon, world):
	# B on the rebooting screen is not a trap: the machine is going down, but
	# if it somehow is not, there is still a way back
	addon.send(event="back")
	addon.expect("list")

	addon.send(event="back")
	addon.expect("close")

	addon.proc.stdin.close()
	code = addon.proc.wait(timeout=10)
	check(code == 0, "the addon exited with status %r" % code)


# ------------------------------------------------------------------ driver

def run_walk(world, programs):
	addon = Addon(world, programs)
	failures = 0

	try:
		for title, function in STEPS:
			try:
				function(addon, world)
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


def run_case(title, world, programs, body, kmsprint=None):
	"""One short walk of its own, on its own fake Pi."""
	addon = Addon(world, programs, kmsprint=kmsprint)

	try:
		body(addon, world)
		print("PASS  %s" % title)
		return 0
	except Fail as error:
		print("FAIL  %s\n        %s" % (title, error))
		print("      addon stderr:\n%s" % addon.stderr_tail())
		return 1
	except Exception as error:
		print("FAIL  %s\n        unexpected %s: %s"
			% (title, type(error).__name__, error))
		print("      addon stderr:\n%s" % addon.stderr_tail())
		return 1
	finally:
		addon.stop()


def case_garbage(addon, world):
	"""A cmdline.txt that is not one line is refused by name and untouched."""
	before = world.cmdline_text()

	addon.send(event="start")
	main = addon.expect("list")

	check(detail(main, "Mode") == "unreadable",
		"the Mode row shows %r" % detail(main, "Mode"))
	check("auto" not in ids(main),
		"an Automatic row was offered for a file we cannot read: %r" % ids(main))
	check("1920x1080@60" in mode_labels(main),
		"the picker went away too: %r" % mode_labels(main))

	force(addon, "mode:1280x720@60", answer=True)
	message = addon.expect("message")

	check(world.cmdline in message["text"],
		"the file is not named in %r" % message["text"])
	check(world.cmdline_text() == before, "the file was rewritten anyway")
	check(world.writes() == [], "the write helper was run at all")
	check(not os.path.exists(world.orig), "a .orig was made of a file we refused")
	check(not os.path.exists(world.bak), "a .bak was made of a file we refused")

	addon.send(event="confirm", value=True)
	addon.expect("list")

	addon.send(event="back")
	addon.expect("close")
	addon.proc.stdin.close()
	check(addon.proc.wait(timeout=10) == 0, "the addon exited nonzero")


def case_no_display(addon, world):
	"""No display at all: the two promised modes, on HDMI-A-1."""
	addon.send(event="start")
	main = addon.expect("list")

	check(detail(main, "Display") == "no display detected",
		"the status row shows %r" % detail(main, "Display"))

	rows = mode_labels(main)
	check(rows == ["1920x1080@60", "1280x720@60"],
		"the picker offered %r" % rows)

	addon.send(event="select", id="status")
	message = addon.expect("message")
	check("No display is connected." in message["text"],
		"the details said %r" % message["text"])
	addon.send(event="confirm", value=True)
	addon.expect("list")

	force(addon, "mode:1920x1080@60", answer=True)
	addon.expect("confirm")

	check(world.video_tokens() == ["video=HDMI-A-1:1920x1080@60D"],
		"the token is %r" % world.video_tokens())

	addon.send(event="confirm", value=False)
	addon.expect("list")


def case_second_port(addon, world):
	"""The picture is on HDMI-A-2, so the forced mode names HDMI-A-2."""
	addon.send(event="start")
	main = addon.expect("list")

	check(detail(main, "Display") == "1280x720@60 on HDMI-A-2",
		"the status row shows %r" % detail(main, "Display"))

	force(addon, "mode:1920x1080@60", answer=True)
	addon.expect("confirm")

	check(world.video_tokens() == ["video=HDMI-A-2:1920x1080@60D"],
		"the token is %r" % world.video_tokens())

	addon.send(event="confirm", value=False)
	addon.expect("list")


def case_promise_beats_cap(addon, world):
	"""A monitor offering neither promised mode still gets both, inside the cap."""
	addon.send(event="start")
	main = addon.expect("list")

	rows = mode_labels(main)
	check(len(rows) == 12, "the picker offered %d rows: %r" % (len(rows), rows))
	check(rows == EXPECTED_MONITOR_MODES, "the picker offered %r" % rows)


def case_no_kmsprint(addon, world):
	"""No kmsprint: the display cannot be read, and the modes still show."""
	addon.send(event="start")
	main = addon.expect("list")

	check(detail(main, "Display") == "could not be read",
		"the status row shows %r" % detail(main, "Display"))

	rows = mode_labels(main)
	check(rows == ["1920x1080@60", "1280x720@60"],
		"the picker offered %r" % rows)

	force(addon, "mode:1280x720@60", answer=True)
	addon.expect("confirm")

	check(world.video_tokens() == ["video=HDMI-A-1:1280x720@60D"],
		"the token is %r" % world.video_tokens())

	addon.send(event="confirm", value=False)
	addon.expect("list")


def main():
	work = tempfile.mkdtemp(prefix="es-video-test-")
	bin_dir = os.path.join(work, "bin")
	os.makedirs(bin_dir)

	programs = {}
	for name, source in (("kmsprint", MOCK_KMSPRINT),
			("video-write", MOCK_WRITE), ("reboot", MOCK_REBOOT)):
		path = os.path.join(bin_dir, name)
		shutil.copy(source, path)
		os.chmod(path, 0o755)
		programs[name.replace("video-", "")] = path

	print("addon:  %s" % ADDON)
	print("mocks:  %s" % programs["kmsprint"])
	print("        %s" % programs["write"])
	print("        %s" % programs["reboot"])
	print("work:   %s\n" % work)

	walk = World(work, "walk", TV_SEED)
	failures = run_walk(walk, programs)

	extras = [
		("a cmdline.txt that is not one line is refused, and untouched",
			World(work, "garbage", TV_SEED, GARBAGE_FIXTURE), case_garbage, None),
		("no display: 1080p60 and 720p60 offered, on HDMI-A-1",
			World(work, "no-display", NO_DISPLAY_SEED), case_no_display, None),
		("a display on the second port is the port the token names",
			World(work, "second-port", SECOND_PORT_SEED), case_second_port, None),
		("the two promised modes beat the cap, and the cap still holds",
			World(work, "monitor", MONITOR_SEED), case_promise_beats_cap, None),
		("no kmsprint: the display is unreadable, the modes are not",
			World(work, "no-kmsprint", TV_SEED), case_no_kmsprint,
			os.path.join(work, "there-is-no-kmsprint-here")),
	]

	for title, world, body, kmsprint in extras:
		failures += run_case(title, world, programs, body, kmsprint=kmsprint)

	print("")
	if failures:
		print("FAILED (%d step%s)" % (failures, "" if failures == 1 else "s"))
		print("working files kept in %s" % work)
		return 1

	print("OK - %d steps passed" % (len(STEPS) + len(extras)))
	shutil.rmtree(work, ignore_errors=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
