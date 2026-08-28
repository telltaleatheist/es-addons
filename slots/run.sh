#!/bin/sh
# The controller-order addon, as ES spawns it.
#
# ES gives the addon its own stdin and stdout and leaves stderr alone, so
# stderr is where the addon's log goes.  Appending it to a file rather than
# letting it land in ES's own stderr means the pad that claimed each slot, and
# any LED or retroarch.cfg that refused to be written, can still be read
# afterwards - the screen this addon draws is gone the instant START is pressed.
#
# exec, so the addon is the process ES signals: ES sends SIGTERM to the child
# it spawned, and a shell sitting in the middle would swallow it.
exec python3 "$(dirname "$0")/slots.py" 2>>/tmp/slots-addon.log
