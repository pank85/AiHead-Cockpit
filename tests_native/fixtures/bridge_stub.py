#!/usr/bin/python3
"""Offline replacement interpreter for the native bridge process.

The executable receives the native process's ``-m aiheaddev.bridge`` arguments.
It never opens sockets or uses Herdr. The descendant scenario deliberately keeps
inherited descriptors open until its process group is killed by the test.
"""
import os
import sys
import time

mode = os.environ["COCKPIT_BRIDGE_SCENARIO"]
if mode == "descendant":
    if os.fork() == 0:
        with open(os.environ["COCKPIT_DESCENDANT_MARKER"], "w", encoding="utf-8") as marker:
            marker.write(str(os.getpid()))
        time.sleep(30)
        os._exit(0)
    for _ in sys.stdin:
        pass
elif mode == "malformed":
    print("not json", flush=True)
    time.sleep(30)
elif mode == "crash":
    sys.exit(23)
else:
    for _ in sys.stdin:
        pass
