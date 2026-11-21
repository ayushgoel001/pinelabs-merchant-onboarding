"""Run the web server (with its simulated providers) and the worker together on one run directory."""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from threading import Event
from pathlib import Path

from . import db
from .db import initialize

EXIT_WORKER_FAILED = 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path("private/demo"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timing", choices=sorted(db.TIMING))
    parser.add_argument("--demo-controls", action="store_true")
    args = parser.parse_args()
    initialize(args.run_dir, args.timing)
    stopping = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: stopping.set())
    web_extra = ["--demo-controls"] if args.demo_controls else []
    return supervise(args.run_dir, args.port, stopping, web_extra)


class RestartBudget:
    """Allow at most `limit` worker restarts within `window` seconds (M3 review I2)."""
    def __init__(self, limit=5, window=60.0, clock=time.monotonic):
        self.limit, self.window, self.clock, self.history = limit, window, clock, []

    def allow(self):
        now = self.clock()
        self.history = [t for t in self.history if now - t < self.window]
        if len(self.history) >= self.limit:
            return False
        self.history.append(now)
        return True


def stop_tree(child):
    """Stop a child and everything it started. On Windows the virtual-environment `python.exe` is a small launcher
    that starts the real interpreter as its own child; terminating only the launcher leaves the interpreter (and its
    listening socket) alive for a moment. taskkill /T ends the whole tree."""
    if child.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
        else:
            child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def port_open(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_port_closed(port, timeout=5.0):
    limit = time.monotonic() + timeout
    while port_open(port) and time.monotonic() < limit:
        time.sleep(0.02)
    return not port_open(port)


def supervise(run_dir, port, stopping, web_extra=(), budget=None, worker_command=None):
    """Keep the web process and the worker running. A worker that exits is restarted and logged; if restarts
    exceed the budget the launcher stops both processes and returns non-zero instead of serving a dead pipeline."""
    common = ["--run-dir", str(run_dir)]
    budget = budget or RestartBudget()
    children = []

    def start_worker():
        return subprocess.Popen(worker_command or [sys.executable, "-m", "app.worker", *common])

    try:
        children.append(subprocess.Popen([sys.executable, "-m", "app.web", *common, "--port", str(port), *web_extra]))
        children.append(start_worker())
        while children[0].poll() is None and not stopping.wait(0.25):
            code = children[1].poll()
            if code is None:
                continue
            if not budget.allow():
                print(f"Worker keeps exiting (last code {code}); restart budget exhausted, stopping.", flush=True)
                return EXIT_WORKER_FAILED
            print(f"Worker exited with code {code}; restarting.", flush=True)
            children[1] = start_worker()
        return children[0].returncode or 0
    finally:
        for child in children:
            stop_tree(child)
        # Return only when the listening port is really released, so callers can rely on a clean shutdown.
        wait_port_closed(port)


if __name__ == "__main__":
    sys.exit(main())
