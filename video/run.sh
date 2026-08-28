#!/bin/sh
# The video addon, as ES spawns it.
#
# ES gives the addon its own stdin and stdout and leaves stderr alone, so
# stderr is where the addon's log goes.  Appending it to a file rather than
# letting it land in ES's own stderr means the exact command line that was
# written to cmdline.txt, and any sudo that refused to write it, can still be
# read afterwards - which matters more here than anywhere else, because the
# next thing this addon does is reboot the machine.
#
# exec, so the addon is the process ES signals: ES sends SIGTERM to the child
# it spawned, and a shell sitting in the middle would swallow it.
exec python3 "$(dirname "$0")/video.py" 2>>/tmp/video-addon.log
