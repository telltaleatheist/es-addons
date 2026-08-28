#!/bin/sh
# The wifi addon, as ES spawns it.
#
# ES gives the addon its own stdin and stdout and leaves stderr alone, so
# stderr is where the addon's log goes.  Appending it to a file rather than
# letting it land in ES's own stderr means a failed connection attempt can
# still be read afterwards, from a machine with no network.
#
# exec, so the addon is the process ES signals: ES sends SIGTERM to the child
# it spawned, and a shell sitting in the middle would swallow it.
exec python3 "$(dirname "$0")/wifi.py" 2>>/tmp/wifi-addon.log
