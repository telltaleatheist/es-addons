#!/usr/bin/env python3
"""A fake privileged LED write, enough of one to test the slots addon.

The real write is "sudo -n tee /sys/class/leds/<hidid>:green:player-N/brightness"
with the value on stdin, because that file belongs to root and the addon runs
as "pi" with no terminal to type a password into.  A test has neither sudo nor
a Switch controller, so ES_SLOTS_LED_WRITE points the addon here instead and
this script is handed the path and the value as arguments:

    mock-led-write.py <path> <value>

It does two things.  It appends "<path> <value>" to the file named by
MOCK_LED_LOG, which is what a test asserts on - the order of the writes is the
point, because "player 2" means one LED lit and three dark and a test that only
looked at the lit one would pass for an addon that never turned the others off.
And it writes the value into the file, so the fake sysfs tree ends up in the
state the pad's LEDs would be in.

Exit status is nonzero when the write fails, the way tee's is, so an addon that
ignores a failed LED write can be seen doing it in its own log.
"""

import os
import sys


LOG_PATH = os.environ.get("MOCK_LED_LOG", "")


def main():
	if len(sys.argv) != 3:
		sys.stderr.write("usage: mock-led-write.py <path> <value>\n")
		return 2

	path, value = sys.argv[1], sys.argv[2]

	if LOG_PATH:
		with open(LOG_PATH, "a") as handle:
			handle.write("%s %s\n" % (path, value))

	try:
		with open(path, "w") as handle:
			handle.write(value + "\n")
	except OSError as error:
		sys.stderr.write("mock-led-write: %s\n" % error)
		return 1

	return 0


if __name__ == "__main__":
	sys.exit(main())
