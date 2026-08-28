#!/usr/bin/env python3
"""A fake systemctl, enough of one to test the wifi addon's SSH row.

The addon reaches systemctl only through systemctl_argv(), so pointing
ES_WIFI_SYSTEMCTL at this script is enough to walk the SSH flows without
touching a real service manager.  (The addon's real command line is
"sudo -n systemctl ..."; the override exists because a test cannot have sudo.)

State lives in the JSON file named by MOCK_SYSTEMCTL_STATE, in the same
temp directory as the nmcli mock's, and persists across invocations because
the addon runs systemctl once per question:

    {
      "units":  { "ssh": {"active": true, "enabled": true} },
      "fail":   null | "enable" | "disable",
      "silent": false,     is-active says nothing at all
      "log":    [ "is-active ssh", "enable --now ssh", ... ]
    }

The one thing this mock has to get right is that "is-active" exits 3 when the
unit is not running and still prints the state.  An addon that reads the exit
status instead of the word fails here, which is the point.
"""

import json
import os
import sys


STATE_PATH = os.environ.get("MOCK_SYSTEMCTL_STATE", "")


def load():
	with open(STATE_PATH, "r") as handle:
		return json.load(handle)


def save(state):
	temp = STATE_PATH + ".tmp"
	with open(temp, "w") as handle:
		json.dump(state, handle, indent=2)
	os.replace(temp, STATE_PATH)


def out(text):
	sys.stdout.write(text + "\n")
	sys.stdout.flush()


def error(text):
	sys.stderr.write(text + "\n")
	sys.stderr.flush()


def unit_of(state, name):
	return state.setdefault("units", {}).setdefault(
		name, {"active": False, "enabled": False})


def cmd_is_active(state, names):
	"""Prints the state, exits 3 when it is not "active".  Both are the answer."""
	if state.get("silent"):
		# a systemctl that will not say: no output, and an exit nobody can read
		# a state out of
		return 4

	if not names:
		error("Too few arguments.")
		return 1

	unit = unit_of(state, names[0])
	if unit["active"]:
		out("active")
		return 0

	out("inactive")
	return 3


def cmd_set(state, action, args):
	names = [arg for arg in args if not arg.startswith("-")]
	now = "--now" in args

	if not names:
		error("Too few arguments.")
		return 1

	if state.get("fail") == action:
		error("Failed to %s unit %s.service: Interactive authentication required."
			% (action, names[0]))
		return 1

	unit = unit_of(state, names[0])
	unit["enabled"] = (action == "enable")
	if now:
		unit["active"] = (action == "enable")

	return 0


def main():
	if not STATE_PATH:
		sys.stderr.write("mock systemctl: MOCK_SYSTEMCTL_STATE is not set\n")
		return 2

	args = sys.argv[1:]
	state = load()
	state.setdefault("log", []).append(" ".join(args))

	if not args:
		error("Too few arguments.")
		code = 1
	elif args[0] == "is-active":
		code = cmd_is_active(state, args[1:])
	elif args[0] in ("enable", "disable"):
		code = cmd_set(state, args[0], args[1:])
	else:
		error("Unknown command verb %s." % args[0])
		code = 1

	save(state)
	return code


if __name__ == "__main__":
	sys.exit(main())
