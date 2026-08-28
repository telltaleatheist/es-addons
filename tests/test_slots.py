#!/usr/bin/env python3
"""End-to-end test for the slots addon: this script plays EmulationStation.

It spawns slots.py exactly as ES does - pipes on stdin and stdout, one JSON
object per line each way - and gives it a whole fake Pi to look at:

  * ES_SLOTS_PROC_DEVICES, a fixture from tests/mock-input/ standing in for
    /proc/bus/input/devices, complete with the keyboard, the mouse and the
    js-less sibling entry a Pro Controller's motion sensors register
  * ES_SLOTS_DEV_INPUT, a directory of FIFOs named eventN.  A FIFO is close
    enough to an event node for this: the addon opens it non-blocking, selects
    on it and reads 24-byte input_event structs out of it, and the test writes
    the bytes a button press would have made
  * ES_SLOTS_RETROARCH_CFG, a copy of a real-shaped retroarch.cfg
  * ES_SLOTS_LED_ROOT, a fake /sys/class/leds with player LEDs for two of the
    pads, and ES_SLOTS_LED_WRITE pointing at mock-led-write.py so the writes
    that would need sudo are logged instead

and walks the whole addon:

  * the list: one row per pad, the first waiting, the js-less sibling and the
    keyboard and the mouse nowhere on it
  * a press on BTN_START and a stick shoved to its stop, neither of which
    claims anything
  * the second pad claiming Player 1, which rewrites retroarch.cfg, lights its
    player-1 LED and darkens the other three, all before the screen is redrawn
  * that rewrite: every unrelated line byte for byte, the quoting of each line
    as it was found, and ten different indices across the ten player lines
  * a second press from a pad that already has a slot, which does nothing at all
  * a select, which the rows do not answer
  * the first pad claiming Player 2 - an assignment that already read that way,
    so the file is not rewritten, but the LEDs still say so
  * three pads, where the second claim really does reorder the file
  * back, which closes the addon with status 0
  * a machine with no controllers on it
  * a pad whose event node will not open, which is a row and not a crash

Run it: python3 tests/test_slots.py
"""

import errno
import json
import os
import re
import selectors
import shutil
import struct
import subprocess
import sys
import tempfile
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(os.path.dirname(HERE), "slots", "slots.py")
FIXTURES = os.path.join(HERE, "mock-input")
LED_MOCK = os.path.join(HERE, "mock-led-write.py")

READ_TIMEOUT = 30.0
QUIET_SECONDS = 0.6      # long enough for a redraw that should not come

EVENT_FORMAT = "<qqHHi"
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

BTN_SOUTH = 304          # 0x130, the A button: a claim
BTN_EAST = 305           # 0x131, the B button: also a claim, when a pad sends it
BTN_START = 315          # 0x13b: the way out, never a claim
BTN_MODE = 316           # 0x13c: the home button, never a claim
ABS_X = 0x00

PAD_A = "Nintendo Switch Pro Controller"      # event3, joypad index 0
PAD_B = "8BitDo Ultimate 2C Wireless"         # event5, joypad index 1
PAD_C = "Microsoft X-Box 360 pad"             # event7, joypad index 2

HID_A = "0005:057E:2009.0003"
HID_B = "0005:2DC8:3106.0004"

EXIT_ROW = "Start or B exits"

# the LED tree: pad A and pad B have player LEDs, the Xbox pad has none, and
# the rest are the LEDs a Pi really does have sitting next to them
LED_DIRS = (
	[HID_A + ":green:player-%d" % n for n in range(1, 5)]
	+ [HID_B + ":blue:player-%d" % n for n in range(1, 5)]
	+ [HID_A + ":green:home", HID_A + ":green:player-9", "input1::capslock",
		"ACT", "PWR", "mmc0"]
)

INDEX_LINE = re.compile(
	r'^\s*input_player([0-9]+)_joypad_index\s*=\s*"?([0-9]+)"?')


class Fail(Exception):
	pass


def check(condition, message):
	if not condition:
		raise Fail(message)


# ------------------------------------------------------------- the fake Pi

class Workspace(object):
	"""A directory holding everything the addon is allowed to look at."""

	def __init__(self, root, fixture, nodes):
		self.dir = root
		os.makedirs(root)

		self.devices = os.path.join(root, "devices")
		shutil.copy(os.path.join(FIXTURES, fixture), self.devices)

		self.cfg = os.path.join(root, "retroarch.cfg")
		shutil.copy(os.path.join(FIXTURES, "retroarch.cfg"), self.cfg)

		self.dev_input = os.path.join(root, "dev-input")
		os.makedirs(self.dev_input)
		for node in nodes:
			os.mkfifo(os.path.join(self.dev_input, node))

		self.leds = os.path.join(root, "leds")
		for name in LED_DIRS:
			os.makedirs(os.path.join(self.leds, name))
			with open(os.path.join(self.leds, name, "brightness"), "w") as handle:
				handle.write("0\n")

		self.led_log = os.path.join(root, "led-log.txt")

		bin_dir = os.path.join(root, "bin")
		os.makedirs(bin_dir)
		self.led_write = os.path.join(bin_dir, "led-write")
		shutil.copy(LED_MOCK, self.led_write)
		os.chmod(self.led_write, 0o755)

		self.writers = {}

	def environment(self):
		environment = dict(os.environ)
		environment["ES_SLOTS_PROC_DEVICES"] = self.devices
		environment["ES_SLOTS_DEV_INPUT"] = self.dev_input
		environment["ES_SLOTS_RETROARCH_CFG"] = self.cfg
		environment["ES_SLOTS_LED_ROOT"] = self.leds
		environment["ES_SLOTS_LED_WRITE"] = self.led_write
		environment["MOCK_LED_LOG"] = self.led_log
		return environment

	def writer(self, node):
		"""The write end of one pad's FIFO, opened once and kept open.

		Kept open on purpose: closing it would give the addon an end of file on
		a device that in real life never has one, and the addon would rightly
		drop the pad.
		"""
		if node not in self.writers:
			self.writers[node] = PadWriter(os.path.join(self.dev_input, node))
		return self.writers[node]

	def close(self):
		for writer in self.writers.values():
			writer.close()
		self.writers = {}

	def cfg_text(self):
		with open(self.cfg, "r", newline="") as handle:
			return handle.read()

	def cfg_stamp(self):
		info = os.stat(self.cfg)
		return (info.st_ino, info.st_mtime_ns, info.st_size)

	def led_writes(self):
		try:
			with open(self.led_log) as handle:
				lines = handle.read().splitlines()
		except OSError:
			return []
		return [tuple(line.split(" ", 1)) for line in lines if line.strip()]


class PadWriter(object):
	"""The kernel's side of one event node: it writes what a pad would."""

	def __init__(self, path, timeout=5.0):
		deadline = time.monotonic() + timeout
		while True:
			try:
				# a FIFO with no reader refuses a non-blocking open with ENXIO,
				# which is the addon not having got to it yet
				self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
				return
			except OSError as error:
				if error.errno != errno.ENXIO or time.monotonic() > deadline:
					raise Fail("could not open %s for writing: %s" % (path, error))
				time.sleep(0.02)

	def raw(self, kind, code, value):
		os.write(self.fd, struct.pack(EVENT_FORMAT, 0, 0, kind, code, value))

	def press(self, code):
		self.raw(EV_KEY, code, 1)
		self.raw(EV_SYN, 0, 0)

	def release(self, code):
		self.raw(EV_KEY, code, 0)
		self.raw(EV_SYN, 0, 0)

	def tap(self, code):
		self.press(code)
		self.release(code)

	def stick(self, value):
		self.raw(EV_ABS, ABS_X, value)
		self.raw(EV_SYN, 0, 0)

	def close(self):
		try:
			os.close(self.fd)
		except OSError:
			pass


# ------------------------------------------------------------- ES stand-in

class Addon(object):
	"""The addon, and the pipes ES would be holding."""

	def __init__(self, workspace):
		self.log_path = os.path.join(workspace.dir, "stderr.log")
		self.log_file = open(self.log_path, "w")

		self.proc = subprocess.Popen(
			[sys.executable, ADDON],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=self.log_file,
			env=workspace.environment(),
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

	def expect_silence(self, seconds=QUIET_SECONDS):
		"""Nothing at all for a while: the assertion for a thing that must not happen."""
		deadline = time.monotonic() + seconds

		while True:
			if self.buffer.find(b"\n") >= 0:
				raise Fail("addon drew %r when nothing should have changed"
					% self.next_command(1))

			remaining = deadline - time.monotonic()
			if remaining <= 0:
				return

			if not self.selector.select(remaining):
				return

			chunk = os.read(self.proc.stdout.fileno(), 4096)
			if not chunk:
				raise Fail("addon closed its stdout (exit %r)" % self.proc.poll())
			self.buffer += chunk

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


# ------------------------------------------------------------------ reading

def labels(message):
	return [item.get("label") for item in message["items"]]


def details(message):
	return [item.get("detail") for item in message["items"]]


def detail(message, label):
	for item in message["items"]:
		if item.get("label") == label:
			return item.get("detail")
	raise Fail("no row labelled %r in %r" % (label, labels(message)))


def has_row(message, label):
	return any(item.get("label") == label for item in message["items"])


def slot_labels(message):
	return [item["label"] for item in message["items"]
		if item.get("id", "").startswith("slot:")]


def cfg_values(text):
	"""{player: joypad index} for every player line in the file."""
	values = {}
	for line in text.split("\n"):
		match = INDEX_LINE.match(line)
		if match:
			values[int(match.group(1))] = int(match.group(2))
	return values


def other_lines(text):
	"""Every line the addon has no business touching."""
	kept = []
	for line in text.split("\n"):
		match = INDEX_LINE.match(line)
		if match and 1 <= int(match.group(1)) <= 10:
			continue
		kept.append(line)
	return kept


def player_line(text, player):
	for line in text.split("\n"):
		match = INDEX_LINE.match(line)
		if match and int(match.group(1)) == player:
			return line
	raise Fail("no input_player%d_joypad_index line at all" % player)


def led_state(writes, hidid):
	"""{player LED number: brightness} from the writes aimed at one pad."""
	state = {}
	for path, value in writes:
		if hidid not in path:
			continue
		number = int(path.rsplit("player-", 1)[1].split("/")[0])
		state[number] = int(value)
	return state


def led_count(writes, hidid):
	return len([path for path, _value in writes if hidid in path])


def read_brightness(workspace, hidid, colour, number):
	path = os.path.join(workspace.leds, "%s:%s:player-%d" % (hidid, colour, number),
		"brightness")
	with open(path) as handle:
		return handle.read().strip()


# ------------------------------------------------------------------ steps

STEPS = []


def step(title):
	def decorate(function):
		STEPS.append((title, function))
		return function
	return decorate


@step("the list: one row per pad, the js-less sibling is not one of them")
def step_list(addon, work):
	addon.send(event="start")
	main = addon.expect("list")

	check(main["title"] == "CONTROLLER ORDER", "title was %r" % main["title"])
	check(slot_labels(main) == ["Player 1", "Player 2"],
		"the slot rows were %r" % labels(main))
	check(detail(main, "Player 1") == "press any button",
		"Player 1 says %r" % detail(main, "Player 1"))
	check(detail(main, "Player 2") == "waiting",
		"Player 2 says %r" % detail(main, "Player 2"))
	check(labels(main)[-1] == EXIT_ROW, "the last row is %r" % labels(main)[-1])

	blob = json.dumps(main)
	check("IMU" not in blob, "the motion-sensor sibling was listed as a pad")
	check("Keyboard" not in blob, "the keyboard was listed as a pad")
	check("Mouse" not in blob, "the mouse was listed as a pad")
	check("cannot be read" not in blob, "a pad would not open: %r" % details(main))


@step("Start and a stick claim nothing; the second pad's A button claims Player 1")
def step_first_claim(addon, work):
	before = work.cfg_text()
	check(cfg_values(before)[1] == 0, "the fixture does not start at player1 = 0")

	# neither of these is a claim, and pad A is the one making them: if either
	# counted, pad A - not pad B - would come out of this holding Player 1
	work.writer("event3").tap(BTN_START)
	work.writer("event3").tap(BTN_MODE)
	work.writer("event3").stick(32767)
	work.writer("event3").stick(-32768)

	work.writer("event5").tap(BTN_SOUTH)

	main = addon.expect("list")
	check(detail(main, "Player 1") == PAD_B,
		"Player 1 went to %r" % detail(main, "Player 1"))
	check(detail(main, "Player 2") == "press any button",
		"Player 2 says %r" % detail(main, "Player 2"))

	values = cfg_values(work.cfg_text())
	check(values[1] == 1, "player1 is joypad %r, not pad B's index 1" % values[1])
	check(values[2] == 0, "player2 is joypad %r, not pad A's index 0" % values[2])

	writes = work.led_writes()
	check(led_state(writes, HID_B) == {1: 1, 2: 0, 3: 0, 4: 0},
		"pad B's LEDs read %r" % led_state(writes, HID_B))
	check(led_count(writes, HID_B) == 4,
		"pad B's LEDs were written %d times" % led_count(writes, HID_B))
	check(led_count(writes, HID_A) == 0,
		"a pad that has claimed nothing had its LEDs painted: %r" % writes)
	check(read_brightness(work, HID_B, "blue", 1) == "1",
		"the player-1 LED file was not left lit")
	check(read_brightness(work, HID_B, "blue", 2) == "0",
		"the player-2 LED file was not left dark")

	# the two events that claim nothing must not have drawn anything either
	addon.expect_silence()


@step("the rewrite: unrelated lines byte for byte, quoting kept, ten unique indices")
def step_cfg_shape(addon, work):
	with open(os.path.join(FIXTURES, "retroarch.cfg"), "r", newline="") as handle:
		fixture = handle.read()
	after = work.cfg_text()

	check(other_lines(after) == other_lines(fixture),
		"a line the addon has no business touching changed:\n%s"
		% "\n".join(sorted(set(other_lines(after)) ^ set(other_lines(fixture)))))

	values = cfg_values(after)
	ours = [values[player] for player in range(1, 11)]
	check(sorted(ours) == list(range(10)),
		"the ten player lines are not ten different indices: %r" % ours)
	check(ours == [1, 0, 2, 3, 4, 5, 6, 7, 8, 9],
		"the spare indices did not stay in ascending order: %r" % ours)

	check(player_line(after, 3) == "input_player3_joypad_index = 2",
		"the unquoted line came back as %r" % player_line(after, 3))
	check(player_line(after, 4)
			== 'input_player4_joypad_index = "3"   # spacing and a comment, both kept',
		"the spacing or the comment was lost: %r" % player_line(after, 4))
	check(player_line(after, 11) == 'input_player11_joypad_index = "10"',
		"a player line that is not ours was rewritten: %r" % player_line(after, 11))


@step("a second press from a pad that already has a slot does nothing")
def step_repeat_press(addon, work):
	before = work.cfg_text()
	stamp = work.cfg_stamp()
	writes = len(work.led_writes())

	work.writer("event5").tap(BTN_EAST)
	work.writer("event5").tap(BTN_SOUTH)

	addon.expect_silence()
	check(work.cfg_text() == before, "retroarch.cfg was rewritten by a repeat press")
	check(work.cfg_stamp() == stamp, "retroarch.cfg was replaced by a repeat press")
	check(len(work.led_writes()) == writes,
		"the LEDs were repainted by a repeat press")


@step("a select is not an answer: the rows are informational")
def step_select_ignored(addon, work):
	addon.send(event="select", id="slot:1")
	addon.send(event="select", id="exit")
	addon.expect_silence()


@step("the other pad claims Player 2, which the file already said, so it is not rewritten")
def step_second_claim(addon, work):
	stamp = work.cfg_stamp()
	before = work.cfg_text()

	work.writer("event3").tap(BTN_SOUTH)

	main = addon.expect("list")
	check(detail(main, "Player 1") == PAD_B,
		"Player 1 moved to %r" % detail(main, "Player 1"))
	check(detail(main, "Player 2") == PAD_A,
		"Player 2 went to %r" % detail(main, "Player 2"))
	check(has_row(main, "All controllers assigned"),
		"nothing on the screen says every pad has a slot: %r" % labels(main))
	check(labels(main)[-1] == EXIT_ROW, "the last row is %r" % labels(main)[-1])

	check(work.cfg_text() == before,
		"the file changed although the assignment did not")
	check(work.cfg_stamp() == stamp,
		"the file was rewritten although the assignment did not change")

	writes = work.led_writes()
	check(led_state(writes, HID_A) == {1: 0, 2: 1, 3: 0, 4: 0},
		"pad A's LEDs read %r" % led_state(writes, HID_A))
	check(led_state(writes, HID_B) == {1: 1, 2: 0, 3: 0, 4: 0},
		"pad B's LEDs were disturbed: %r" % led_state(writes, HID_B))


@step("back closes the addon, with status 0")
def step_back_closes(addon, work):
	addon.send(event="back")
	addon.expect("close")

	addon.proc.stdin.close()
	code = addon.proc.wait(timeout=10)
	check(code == 0, "the addon exited with status %r" % code)


# ------------------------------------------------------------------ scenarios

def run_walk(work):
	addon = Addon(work)
	failures = 0

	try:
		for title, function in STEPS:
			try:
				function(addon, work)
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
		work.close()

	return failures


def scenario(title):
	"""Run one function that owns its own addon, and report it like a step."""
	def decorate(function):
		def run(work):
			addon = Addon(work)
			try:
				function(addon, work)
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
				work.close()
		return run
	return decorate


@scenario("three pads: the second claim really does reorder the file")
def run_three_pads(addon, work):
	addon.send(event="start")
	main = addon.expect("list")

	check(slot_labels(main) == ["Player 1", "Player 2", "Player 3"],
		"the slot rows were %r" % labels(main))
	check("DragonRise" not in json.dumps(main),
		"a joystick with no event node was given a slot")

	# pad B (joypad index 1) takes Player 1
	work.writer("event5").tap(BTN_SOUTH)
	main = addon.expect("list")
	check(detail(main, "Player 1") == PAD_B,
		"Player 1 went to %r" % detail(main, "Player 1"))

	values = cfg_values(work.cfg_text())
	check([values[player] for player in range(1, 4)] == [1, 0, 2],
		"the first claim wrote %r" % [values[player] for player in range(1, 4)])

	# pad C (joypad index 2) takes Player 2, which pushes pad A down to Player 3
	work.writer("event7").tap(BTN_SOUTH)
	main = addon.expect("list")
	check(detail(main, "Player 2") == PAD_C,
		"Player 2 went to %r" % detail(main, "Player 2"))
	check(detail(main, "Player 3") == "press any button",
		"the last slot is not the one waiting: %r" % detail(main, "Player 3"))

	after = work.cfg_text()
	values = cfg_values(after)
	ours = [values[player] for player in range(1, 11)]
	check(ours == [1, 2, 0, 3, 4, 5, 6, 7, 8, 9],
		"the second claim wrote %r" % ours)
	check(sorted(ours) == list(range(10)),
		"the ten player lines are not ten different indices: %r" % ours)

	with open(os.path.join(FIXTURES, "retroarch.cfg"), "r", newline="") as handle:
		fixture = handle.read()
	check(other_lines(after) == other_lines(fixture),
		"a line the addon has no business touching changed")

	# the Xbox pad has no player LEDs, and that is not a failure
	check(led_count(work.led_writes(), "045E:028E") == 0,
		"a pad with no player LEDs had some written anyway")
	check(led_state(work.led_writes(), HID_B) == {1: 1, 2: 0, 3: 0, 4: 0},
		"pad B's LEDs read %r" % led_state(work.led_writes(), HID_B))

	# the pad nobody touched keeps the slot it was already in, so the last
	# claim changes the screen and leaves the file alone
	stamp = work.cfg_stamp()
	work.writer("event3").tap(BTN_SOUTH)
	main = addon.expect("list")
	check(detail(main, "Player 3") == PAD_A,
		"Player 3 went to %r" % detail(main, "Player 3"))
	check(has_row(main, "All controllers assigned"),
		"nothing on the screen says every pad has a slot: %r" % labels(main))
	check(work.cfg_stamp() == stamp,
		"the file was rewritten although the assignment did not change")
	check(led_state(work.led_writes(), HID_A) == {1: 0, 2: 0, 3: 1, 4: 0},
		"pad A's LEDs read %r" % led_state(work.led_writes(), HID_A))

	# ES's own START closes the addon GUI without telling the addon, but if a
	# start event does arrive twice it means the same thing and ends the same way
	addon.send(event="start")
	addon.expect("close")
	addon.proc.stdin.close()
	check(addon.proc.wait(timeout=10) == 0, "the addon exited badly")


@scenario("no controllers: one message, then close, with status 0")
def run_no_pads(addon, work):
	addon.send(event="start")
	message = addon.expect("message")

	check(message["text"] == "No controllers are connected.",
		"the message was %r" % message["text"])

	addon.send(event="confirm", value=True)
	addon.expect("close")

	addon.proc.stdin.close()
	code = addon.proc.wait(timeout=10)
	check(code == 0, "the addon exited with status %r" % code)

	check(work.cfg_text() == open(os.path.join(FIXTURES, "retroarch.cfg"),
			newline="").read(),
		"retroarch.cfg was touched on a machine with no controllers")


@scenario("a pad whose event node will not open is a row, not a crash")
def run_unreadable(addon, work):
	addon.send(event="start")
	main = addon.expect("list")

	check(slot_labels(main) == ["Player 1", "Player 2"],
		"the slot rows were %r" % labels(main))
	problem = detail(main, PAD_C)
	check(problem and problem.startswith("cannot be read"),
		"the unreadable pad's row says %r" % problem)

	# and the addon is still very much alive
	work.writer("event3").tap(BTN_SOUTH)
	main = addon.expect("list")
	check(detail(main, "Player 1") == PAD_A,
		"Player 1 went to %r" % detail(main, "Player 1"))
	check(detail(main, PAD_C).startswith("cannot be read"),
		"the unreadable pad's row was forgotten on the redraw")

	addon.send(event="back")
	addon.expect("close")
	addon.proc.stdin.close()
	check(addon.proc.wait(timeout=10) == 0, "the addon exited badly")


def run_pure_parts():
	"""The two things the mocks hide: the real sudo line, and a missing line.

	Every other test drives the addon through ES_SLOTS_LED_WRITE, so the
	command the addon would really run on the Pi is the one thing the walk
	cannot see - and it is the one that needs sudo.  Nothing here spawns the
	addon; it is imported and asked directly.
	"""
	title = "the privileged command line is sudo -n tee, with the value on stdin"

	try:
		import importlib.util

		spec = importlib.util.spec_from_file_location("slots_addon", ADDON)
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)

		check(not module.LED_WRITE,
			"ES_SLOTS_LED_WRITE is set in this shell, so the real command "
			"line cannot be checked")

		path = "/sys/class/leds/%s:green:player-1/brightness" % HID_A
		argv, stdin_text = module.led_command(path, 1)
		check(argv == ["sudo", "-n", "tee", path], "the command was %r" % argv)
		check(stdin_text == "1\n", "the value on stdin was %r" % stdin_text)

		# a retroarch.cfg with no player lines at all still has to come back
		# saying who is who, or RetroArch falls back to player N = pad N-1
		text = 'input_joypad_driver = "udev"\n'
		values = {player: value for player, value
			in zip(range(1, 11), [1, 0, 2, 3, 4, 5, 6, 7, 8, 9])}
		updated = module.rewrite_cfg_text(text, values, 2)
		check(updated == 'input_joypad_driver = "udev"\n'
				'input_player1_joypad_index = "1"\n'
				'input_player2_joypad_index = "0"\n',
			"a file with no player lines came back as %r" % updated)

		print("PASS  %s" % title)
		return 0
	except Fail as error:
		print("FAIL  %s\n        %s" % (title, error))
		return 1
	except Exception as error:
		print("FAIL  %s\n        unexpected %s: %s"
			% (title, type(error).__name__, error))
		return 1


def main():
	root = tempfile.mkdtemp(prefix="es-slots-test-")

	walk = Workspace(os.path.join(root, "walk"), "devices", ["event3", "event5"])
	three = Workspace(os.path.join(root, "three"), "devices-three",
		["event3", "event5", "event7"])
	none = Workspace(os.path.join(root, "none"), "devices-none", [])
	# event11 is in the fixture and deliberately not here: a pad the addon
	# cannot open is exactly a node that is not there
	unreadable = Workspace(os.path.join(root, "unreadable"), "devices-unreadable",
		["event3"])

	print("addon:  %s" % ADDON)
	print("mock:   %s" % LED_MOCK)
	print("work:   %s\n" % root)

	failures = run_walk(walk)
	failures += run_three_pads(three)
	failures += run_no_pads(none)
	failures += run_unreadable(unreadable)
	failures += run_pure_parts()

	print("")
	if failures:
		print("FAILED (%d step%s)" % (failures, "" if failures == 1 else "s"))
		print("working files kept in %s" % root)
		return 1

	print("OK - %d steps passed" % (len(STEPS) + 4))
	shutil.rmtree(root, ignore_errors=True)
	return 0


if __name__ == "__main__":
	sys.exit(main())
