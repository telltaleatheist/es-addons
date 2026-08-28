#!/usr/bin/env python3
"""A fake privileged write, enough of one to test the video addon.

The real write is "sudo -n tee /boot/firmware/cmdline.txt" with the new command
line on stdin, because that file belongs to root and the addon runs as "pi"
with no terminal to type a password into.  A test has neither sudo nor a Pi, so
ES_VIDEO_WRITE points the addon here instead and this script is handed only the
destination path:

    mock-video-write.py <path>            new content on stdin

It appends "<path>" and the content to the file named by MOCK_VIDEO_WRITE_LOG,
one JSON object per line, which is what a test asserts on - the ORDER of the
writes is the point, because .orig and .bak have to be on disk before the file
the machine boots from is touched, and a test that only looked at the end state
would pass for an addon that wrote them afterwards.

MOCK_VIDEO_WRITE_FAIL names a file whose existence makes every write fail with
nothing written, which is how the "a backup that will not write aborts the
change" rule gets tested.  It is a file rather than an environment variable
because the addon is spawned once and the test needs to change its mind
halfway through the walk.
"""

import json
import os
import sys


LOG_PATH = os.environ.get("MOCK_VIDEO_WRITE_LOG", "")
FAIL_FLAG = os.environ.get("MOCK_VIDEO_WRITE_FAIL", "")


def main():
	if len(sys.argv) != 2:
		sys.stderr.write("usage: mock-video-write.py <path>\n")
		return 2

	path = sys.argv[1]
	content = sys.stdin.read()

	failed = bool(FAIL_FLAG) and os.path.exists(FAIL_FLAG)

	if LOG_PATH:
		with open(LOG_PATH, "a") as handle:
			handle.write(json.dumps(
				{"path": path, "content": content, "failed": failed}) + "\n")

	if failed:
		sys.stderr.write("tee: %s: Permission denied\n" % path)
		return 1

	try:
		with open(path, "w", newline="") as handle:
			handle.write(content)
	except OSError as error:
		sys.stderr.write("mock-video-write: %s\n" % error)
		return 1

	return 0


if __name__ == "__main__":
	sys.exit(main())
