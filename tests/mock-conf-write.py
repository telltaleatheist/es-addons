#!/usr/bin/env python3
"""A fake privileged write, enough of one to test the bluetooth addon.

The real write is "sudo -n tee /etc/bt-controller-autoconnect.conf" with the
whole new file on stdin, because that file belongs to root and the addon runs
as "pi" with no terminal to type a password into.  A test has neither sudo nor
a Pi, so ES_BT_CONF_WRITE points the addon here instead and this script is
handed only the destination path:

    mock-conf-write.py <path>             new content on stdin

It appends "<path>" and the content to the file named by MOCK_BT_CONF_LOG, one
JSON object per line, which is what a test asserts on when the question is how
MANY times the addon wrote - a conf line added twice is a bug the end state
cannot show.  Then it writes the content through to the path, so the fake conf
ends up as the addon believes it is.

MOCK_BT_CONF_FAIL names a file whose existence makes every write fail with
nothing written, which is how "a refused write never leaves the row claiming a
state it did not reach" gets tested.  It is a file rather than an environment
variable because the addon is spawned once and the test needs to change its
mind halfway through the walk.
"""

import json
import os
import sys


LOG_PATH = os.environ.get("MOCK_BT_CONF_LOG", "")
FAIL_FLAG = os.environ.get("MOCK_BT_CONF_FAIL", "")


def main():
	if len(sys.argv) != 2:
		sys.stderr.write("usage: mock-conf-write.py <path>\n")
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
		sys.stderr.write("mock-conf-write: %s\n" % error)
		return 1

	return 0


if __name__ == "__main__":
	sys.exit(main())
