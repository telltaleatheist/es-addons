#!/usr/bin/env python3
"""A fake reboot, enough of one to test the video addon.

The real command is "sudo -n reboot" and there is exactly one thing a test
wants to know about it: whether it ran.  ES_VIDEO_REBOOT points the addon here
instead, this script takes no arguments, and it appends the argument list it
was given to the file named by MOCK_REBOOT_LOG.

MOCK_REBOOT_FAIL names a file whose existence makes it exit nonzero with a
complaint on stderr, the way a "sudo -n" that wants a password does - a machine
that refuses to reboot is a message box, not a hang.
"""

import os
import sys


LOG_PATH = os.environ.get("MOCK_REBOOT_LOG", "")
FAIL_FLAG = os.environ.get("MOCK_REBOOT_FAIL", "")


def main():
	if LOG_PATH:
		with open(LOG_PATH, "a") as handle:
			handle.write(" ".join(sys.argv[1:]) + "\n")

	if FAIL_FLAG and os.path.exists(FAIL_FLAG):
		sys.stderr.write("sudo: a password is required\n")
		return 1

	return 0


if __name__ == "__main__":
	sys.exit(main())
