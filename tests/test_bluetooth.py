#!/usr/bin/env python3
"""End-to-end test for the bluetooth addon: this script plays EmulationStation.

It spawns bluetooth.py exactly as ES does - pipes on stdin and stdout, one
JSON object per line each way - with mock-bluetoothctl.py installed on PATH
under the name "bluetoothctl", and walks the whole addon:

  * the main list: controllers one per row, everything else behind one row
  * the "Other devices" sub-list and a device menu opened from it
  * disconnect, and forget with its confirmation
  * a scan that discovers a controller only via a late [CHG] Icon line, and
    pairs it on the spot: pair, then trust, then connect, in that order
  * the same, with the trust step failing: the message must name the step
  * a scan that finds no controller: the found list, minus the nameless
    placeholder and minus devices we already know, and pairing from it by hand
  * cancelling a scan with "back"
  * "back" on the main list, which closes the addon with status 0
  * a system with no bluetoothctl at all

Run it: python3 tests/test_bluetooth.py
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
ADDON = os.path.join(os.path.dirname(HERE), "bluetooth", "bluetooth.py")
MOCK = os.path.join(HERE, "mock-bluetoothctl.py")

SCAN_SECONDS = 4.0
READ_TIMEOUT = 30.0

PAD_ONE = "11:22:33:44:55:66"
PAD_TWO = "AA:BB:CC:DD:EE:FF"
SPEAKER = "99:99:99:99:99:01"
KEYBOARD = "99:99:99:99:99:02"

EIGHTBITDO = "AA:AA:AA:AA:AA:01"
NAMELESS = "AA:AA:AA:AA:AA:02"
SWITCH_PAD = "AA:AA:AA:AA:AA:03"
LIVING_ROOM = "AA:AA:AA:AA:AA:04"
HEADPHONES = "AA:AA:AA:AA:AA:05"
LATE_PAD = "AA:AA:AA:AA:AA:06"


class Fail(Exception):
	pass


def check(condition, message):
	if not condition:
		raise Fail(message)


# ------------------------------------------------------------------ state

SEED = {
	"devices": [
		{"mac": PAD_ONE, "name": "Seeded Pad One", "icon": "input-gaming",
			"class": "0x002508", "connected": True},
		{"mac": PAD_TWO, "name": "Seeded Pad Two", "icon": "input-gaming",
			"class": "0x002508", "connected": False},
		{"mac": SPEAKER, "name": "Kitchen Speaker", "icon": "audio-card",
			"class": "0x240414", "connected": False},
		{"mac": KEYBOARD, "name": "Wireless Keyboard", "icon": "input-keyboard",
			"class": "0x002540", "connected": False},
	],
	"discoverable": {
		# no name and nothing else: BlueZ only has the placeholder for it
		NAMELESS: {},
		# a controller that says nothing about itself until a [CHG] Icon line
		EIGHTBITDO: {"name": "8BitDo Pro 2"},
		# a controller with no icon, identified by its class of device alone
		SWITCH_PAD: {"name": "Nintendo Switch Pro Controller", "class": "0x002508"},
		LIVING_ROOM: {"name": "Living Room Speaker", "icon": "audio-card"},
		HEADPHONES: {"name": "Bedroom Headphones", "icon": "audio-headset"},
		LATE_PAD: {"name": "Late Pad", "icon": "input-gaming"},
	},
	"scan_script": [],
	"fail_step": None,
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


# ------------------------------------------------------------- ES stand-in

class Addon(object):
	"""The addon, and the pipes ES would be holding."""

	def __init__(self, state_path, bin_dir, with_bluetoothctl=True):
		environment = dict(os.environ)
		environment["MOCK_BT_STATE"] = state_path
		environment["ES_BT_SCAN_SECONDS"] = str(SCAN_SECONDS)
		# the mock comes first, so it wins over a real bluetoothctl if there is
		# one; the rest of PATH stays because the mock has a #!/usr/bin/env line
		if with_bluetoothctl:
			environment["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
		else:
			environment["PATH"] = tempfile.mkdtemp()

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
		"""The next non-progress command, keeping the progress screens seen."""
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


def labels(message):
	return [item.get("label") for item in message["items"]]


def ids(message):
	return [item.get("id") for item in message["items"]]


def detail(message, label):
	for item in message["items"]:
		if item.get("label") == label:
			return item.get("detail")
	raise Fail("no row labelled %r in %r" % (label, labels(message)))


# ------------------------------------------------------------------ steps

STEPS = []


def step(title):
	def decorate(function):
		STEPS.append((title, function))
		return function
	return decorate


@step("main list: controllers first, everything else behind one row")
def step_main_list(addon, state_path):
	addon.send(event="start")
	main = addon.expect("list")

	check(main["title"] == "BLUETOOTH", "title was %r" % main["title"])
	check(labels(main) == ["Seeded Pad One", "Seeded Pad Two",
			"Other devices", "Search for new devices..."],
		"rows were %r" % labels(main))
	check(detail(main, "Seeded Pad One") == "Connected",
		"pad one detail was %r" % detail(main, "Seeded Pad One"))
	check(detail(main, "Seeded Pad Two") == "Not connected",
		"pad two detail was %r" % detail(main, "Seeded Pad Two"))
	check(detail(main, "Other devices") == "2 paired",
		"other devices detail was %r" % detail(main, "Other devices"))
	check(ids(main)[-1] == "scan", "the last row is not the scan row")


@step("Other devices: sub-list, a device menu, and back out of both")
def step_other_devices(addon, state_path):
	addon.send(event="select", id="others")
	sub = addon.expect("list")
	check(sub["title"] == "OTHER DEVICES", "title was %r" % sub["title"])
	check(labels(sub) == ["Kitchen Speaker", "Wireless Keyboard"],
		"rows were %r" % labels(sub))

	addon.send(event="select", id="dev:" + SPEAKER)
	menu = addon.expect("list")
	check(menu["title"] == "Kitchen Speaker", "title was %r" % menu["title"])
	check(ids(menu) == ["connect", "forget"], "rows were %r" % ids(menu))

	addon.send(event="back")
	back_to_sub = addon.expect("list")
	check(back_to_sub["title"] == "OTHER DEVICES",
		"back from a device menu opened from Other devices went to %r"
		% back_to_sub["title"])

	addon.send(event="back")
	back_to_main = addon.expect("list")
	check(back_to_main["title"] == "BLUETOOTH",
		"back from Other devices went to %r" % back_to_main["title"])


@step("device menu: disconnect a connected controller")
def step_disconnect(addon, state_path):
	addon.send(event="select", id="dev:" + PAD_ONE)
	menu = addon.expect("list")
	check(menu["title"] == "Seeded Pad One", "title was %r" % menu["title"])
	check(ids(menu) == ["disconnect", "forget"],
		"a connected device offered %r" % ids(menu))

	addon.send(event="select", id="disconnect")
	message, progress = addon.expect_after_progress("message")
	check(progress and progress[0]["title"] == "DISCONNECTING",
		"no DISCONNECTING progress screen, got %r" % progress)
	check("Seeded Pad One" in message["text"], "message was %r" % message["text"])

	check("disconnect " + PAD_ONE in read_state(state_path)["log"],
		"bluetoothctl was never asked to disconnect")

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(detail(main, "Seeded Pad One") == "Not connected",
		"pad one is still %r" % detail(main, "Seeded Pad One"))


@step("device menu: forget, with its confirmation")
def step_forget(addon, state_path):
	addon.send(event="select", id="dev:" + PAD_TWO)
	addon.expect("list")

	addon.send(event="select", id="forget")
	question = addon.expect("confirm")
	check("Seeded Pad Two" in question["text"] and "pair it again" in question["text"],
		"confirm text was %r" % question["text"])

	addon.send(event="confirm", value=True)
	message, progress = addon.expect_after_progress("message")
	check(progress and progress[0]["title"] == "FORGETTING",
		"no FORGETTING progress screen, got %r" % progress)
	check("removed" in message["text"].lower(), "message was %r" % message["text"])

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(labels(main) == ["Seeded Pad One", "Other devices",
			"Search for new devices..."],
		"rows after forgetting were %r" % labels(main))


@step("scan: a controller identified by a late [CHG] Icon is paired on sight")
def step_auto_pair(addon, state_path):
	patch_state(state_path, scan_script=[
		{"at": 0.3, "kind": "NEW", "mac": NAMELESS, "tail": "AA-AA-AA-AA-AA-02"},
		{"at": 0.5, "kind": "NEW", "mac": PAD_ONE, "tail": "Seeded Pad One"},
		{"at": 0.7, "kind": "NEW", "mac": LIVING_ROOM, "tail": "Living Room Speaker"},
		{"at": 1.0, "kind": "NEW", "mac": EIGHTBITDO, "tail": "8BitDo Pro 2"},
		{"at": 1.4, "kind": "CHG", "mac": EIGHTBITDO, "tail": "Icon: input-gaming"},
	])

	addon.send(event="select", id="scan")
	message, progress = addon.expect_after_progress("message")

	check(progress[0]["title"] == "SCANNING", "first screen was %r" % progress[0])
	check("pairing mode" in progress[0]["text"], "scan text was %r" % progress[0]["text"])

	pairing = [screen["text"] for screen in progress if screen["title"] == "PAIRING"]
	check(len(pairing) == 3, "expected three pairing steps, saw %r" % pairing)
	check("Found 8BitDo Pro 2 - pairing..." == pairing[0], "step 1 said %r" % pairing[0])
	check("trusting" in pairing[1], "step 2 said %r" % pairing[1])
	check("connecting" in pairing[2], "step 3 said %r" % pairing[2])

	log = read_state(state_path)["log"]
	order = [entry for entry in log if entry.endswith(EIGHTBITDO)
		and not entry.startswith("info")]
	check(order == ["pair " + EIGHTBITDO, "trust " + EIGHTBITDO, "connect " + EIGHTBITDO],
		"bluetoothctl was driven as %r" % order)

	check("8BitDo Pro 2" in message["text"] and "connected" in message["text"].lower(),
		"message was %r" % message["text"])

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(labels(main)[0] == "8BitDo Pro 2", "rows were %r" % labels(main))
	check(detail(main, "8BitDo Pro 2") == "Connected",
		"the new pad is %r" % detail(main, "8BitDo Pro 2"))


@step("scan: a controller known only by its class of device, whose trust fails")
def step_pair_failure(addon, state_path):
	patch_state(state_path, fail_step="trust", scan_script=[
		{"at": 0.3, "kind": "NEW", "mac": EIGHTBITDO, "tail": "8BitDo Pro 2"},
		{"at": 0.6, "kind": "NEW", "mac": SWITCH_PAD,
			"tail": "Nintendo Switch Pro Controller"},
	])

	addon.send(event="select", id="scan")
	message, progress = addon.expect_after_progress("message")

	pairing = [screen["text"] for screen in progress if screen["title"] == "PAIRING"]
	check(len(pairing) == 2, "expected to stop after trust, saw %r" % pairing)
	check("Nintendo Switch Pro Controller" in pairing[0],
		"the already-known 8BitDo should have been skipped, got %r" % pairing[0])

	check(message["title"] == "PAIRING FAILED", "title was %r" % message["title"])
	check("trust step failed" in message["text"],
		"the failing step is not named in %r" % message["text"])
	check("org.bluez.Error.Failed" in message["text"],
		"bluetoothctl's own error line is missing from %r" % message["text"])

	addon.send(event="confirm", value=True)
	addon.expect("list")


@step("scan: no controller, so the found list - placeholders and known devices out")
def step_found_list(addon, state_path):
	patch_state(state_path, fail_step=None, scan_script=[
		{"at": 0.3, "kind": "NEW", "mac": HEADPHONES, "tail": "Bedroom Headphones"},
		{"at": 0.6, "kind": "NEW", "mac": NAMELESS, "tail": "AA-AA-AA-AA-AA-02"},
		{"at": 0.9, "kind": "NEW", "mac": PAD_ONE, "tail": "Seeded Pad One"},
	])

	addon.send(event="select", id="scan")
	found, progress = addon.expect_after_progress("list")

	ticking = [screen["text"] for screen in progress
		if screen["title"] == "SCANNING" and "elapsed" in screen["text"]]
	check(ticking, "the scan never updated its progress text: %r" % progress)
	check("1 new device found" in ticking[-1],
		"the progress text does not count the finds: %r" % ticking[-1])

	check(found["title"] == "FOUND DEVICES", "title was %r" % found["title"])
	check(labels(found) == ["Bedroom Headphones", "Scan again"],
		"the found list was %r" % labels(found))
	check(detail(found, "Bedroom Headphones") == HEADPHONES,
		"detail was %r" % detail(found, "Bedroom Headphones"))
	check(NAMELESS not in json.dumps(found), "the nameless device was listed")
	check(PAD_ONE not in json.dumps(found), "an already-known device was listed")


@step("found list: pairing by hand asks first, and no means no")
def step_manual_pair(addon, state_path):
	addon.send(event="select", id="new:" + HEADPHONES)
	question = addon.expect("confirm")
	check("Bedroom Headphones" in question["text"], "asked %r" % question["text"])

	addon.send(event="confirm", value=False)
	back = addon.expect("list")
	check(back["title"] == "FOUND DEVICES", "no went to %r" % back["title"])

	addon.send(event="select", id="new:" + HEADPHONES)
	addon.expect("confirm")
	addon.send(event="confirm", value=True)

	message, progress = addon.expect_after_progress("message")
	pairing = [screen["text"] for screen in progress if screen["title"] == "PAIRING"]
	check(len(pairing) == 3 and pairing[0].startswith("Pairing with"),
		"manual pairing said %r" % pairing)
	check("Bedroom Headphones" in message["text"], "message was %r" % message["text"])

	addon.send(event="confirm", value=True)
	main = addon.expect("list")
	check(detail(main, "Other devices") == "3 paired",
		"the headphones did not land under Other devices: %r" % labels(main))


@step("scan: back cancels it, and nothing gets paired")
def step_cancel(addon, state_path):
	patch_state(state_path, scan_script=[
		{"at": 3.0, "kind": "NEW", "mac": LATE_PAD, "tail": "Late Pad"},
	])

	addon.send(event="select", id="scan")
	first = addon.expect("progress")
	check(first["title"] == "SCANNING", "first screen was %r" % first)

	time.sleep(0.5)
	started = time.monotonic()
	addon.send(event="back")

	main, _ = addon.expect_after_progress("list", timeout=SCAN_SECONDS + 10)
	took = time.monotonic() - started
	check(main["title"] == "BLUETOOTH", "cancelling went to %r" % main["title"])
	check(took < SCAN_SECONDS, "cancelling took %.1fs, the scan was not cut short" % took)
	check("pair " + LATE_PAD not in read_state(state_path)["log"],
		"a cancelled scan paired something anyway")


@step("back on the main list closes the addon, with status 0")
def step_close(addon, state_path):
	addon.send(event="back")
	closing = addon.expect("close")
	check(closing["cmd"] == "close", "got %r" % closing)

	addon.proc.stdin.close()
	code = addon.proc.wait(timeout=10)
	check(code == 0, "the addon exited with status %r" % code)


# ------------------------------------------------------------------ driver

def run_walk(state_path, bin_dir):
	addon = Addon(state_path, bin_dir)
	failures = 0

	try:
		for title, function in STEPS:
			try:
				function(addon, state_path)
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


def run_without_bluetoothctl(state_path, bin_dir):
	addon = Addon(state_path, bin_dir, with_bluetoothctl=False)
	title = "no bluetoothctl: one message, then close, with status 0"

	try:
		addon.send(event="start")
		message = addon.expect("message", timeout=15)
		check("bluetoothctl" in message["text"], "message was %r" % message["text"])

		addon.send(event="confirm", value=True)
		addon.expect("close", timeout=15)

		addon.proc.stdin.close()
		code = addon.proc.wait(timeout=10)
		check(code == 0, "the addon exited with status %r" % code)

		print("PASS  %s" % title)
		return 0
	except Fail as error:
		print("FAIL  %s\n        %s" % (title, error))
		return 1
	finally:
		addon.stop()


def main():
	work = tempfile.mkdtemp(prefix="es-bluetooth-test-")
	bin_dir = os.path.join(work, "bin")
	os.makedirs(bin_dir)

	shutil.copy(MOCK, os.path.join(bin_dir, "bluetoothctl"))
	os.chmod(os.path.join(bin_dir, "bluetoothctl"), 0o755)

	state_path = os.path.join(work, "state.json")
	with open(state_path, "w") as handle:
		json.dump(SEED, handle, indent=2)

	print("addon:  %s" % ADDON)
	print("mock:   %s" % os.path.join(bin_dir, "bluetoothctl"))
	print("state:  %s\n" % state_path)

	failures = run_walk(state_path, bin_dir)
	failures += run_without_bluetoothctl(state_path, bin_dir)

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
