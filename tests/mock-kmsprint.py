#!/usr/bin/env python3
"""A fake kmsprint, enough of one to test the video addon.

The addon learns everything it knows about the display from kmsprint, so
pointing ES_VIDEO_KMSPRINT at this script is enough to walk every flow with no
HDMI cable, no DRM and no Pi.

State lives in the JSON file named by MOCK_KMSPRINT_STATE and persists across
invocations, because the addon runs kmsprint twice per screen - once plain for
the mode in use, once with --modes for the list:

    {
      "connectors": [ {"name", "connected", "crtc", "modes": [ "1920x1080@60.00" ]} ],
      "fail":       false,      exit 1 with a complaint, the way a kmsprint
                                with no DRM device does
      "log":        [ "", "--modes" ]
    }

The two output forms are the real ones.  Plain kmsprint walks
connector -> encoder -> crtc -> plane and the mode in use is on the Crtc line;
--modes puts one numbered row per supported mode under each connector.  Both
carry the timing detail after the mode, and the fixture deliberately includes
interlaced rows (the 'i' on the resolution) and the same resolution at 59.94
and 60.00, because an addon that offered either of those to a capture card
would be wrong in a way nobody notices until the screen is black.
"""

import json
import os
import sys


STATE_PATH = os.environ.get("MOCK_KMSPRINT_STATE", "")

# The timing detail that follows a mode.  The addon does not read a byte of it;
# it is here because a parser that only ever saw a bare mode string would pass
# this test and fail on a Pi.
TIMING = "148.500 1920/88/44/148/+ 1080/4/5/36/+ 60 (60.00) P|D"


def load():
	with open(STATE_PATH, "r") as handle:
		return json.load(handle)


def save(state):
	temp = STATE_PATH + ".tmp"
	with open(temp, "w") as handle:
		json.dump(state, handle, indent=2)
	os.replace(temp, STATE_PATH)


def out(*lines):
	for line in lines:
		sys.stdout.write(line + "\n")
	sys.stdout.flush()


def connector_line(number, connector):
	return "Connector %d (%d) %s (%s)" % (
		number, 32 + number * 11, connector["name"],
		"connected" if connector.get("connected") else "disconnected")


def print_plain(state):
	"""kmsprint with no arguments: what each connector is doing right now."""
	for number, connector in enumerate(state.get("connectors", [])):
		out(connector_line(number, connector))

		if not connector.get("connected"):
			continue

		out("  Encoder %d (%d) TMDS" % (number, 30 + number * 13))

		crtc = connector.get("crtc")
		if not crtc:
			continue

		out("    Crtc %d (%d) %s %s" % (number + 2, 93 + number, crtc, TIMING))
		out("      Plane %d (%d) fb-id: 130 (crtcs: %d) 0,0 1920x1080 -> "
			"0,0 1920x1080 XR24" % (number, 31 + number, number))


def print_modes(state):
	"""kmsprint --modes: every mode each connector says it can take."""
	for number, connector in enumerate(state.get("connectors", [])):
		out(connector_line(number, connector))

		if not connector.get("connected"):
			continue

		for index, mode in enumerate(connector.get("modes", [])):
			out("  %2d %s  %s" % (index, mode, TIMING))


def main():
	args = sys.argv[1:]

	if not STATE_PATH:
		sys.stderr.write("mock-kmsprint: MOCK_KMSPRINT_STATE is not set\n")
		return 2

	state = load()
	state.setdefault("log", []).append(" ".join(args))
	save(state)

	if state.get("fail"):
		sys.stderr.write("kmsprint: could not open any DRM device\n")
		return 1

	if "--modes" in args:
		print_modes(state)
	else:
		print_plain(state)

	return 0


if __name__ == "__main__":
	sys.exit(main())
