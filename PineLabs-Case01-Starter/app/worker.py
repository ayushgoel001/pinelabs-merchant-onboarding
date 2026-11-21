"""Due-job worker: heartbeat, deadline expiry, provider dispatch and MID issuance over loopback HTTP.

One tick = publish heartbeat, expire SENT attempts at or past their deadline, then lease and send due
dispatch and issuance jobs. HTTP happens between two short transactions, never inside one.

Resilience (M3 review I2): each phase is isolated, so a failure in one phase (for example SQLite busy past its
timeout) cannot stop the others or end the process. Every failure is logged by phase and exception class only,
and recorded in settings so the operator desk shows it. If failures persist for MAX_FAILED_TICKS consecutive
ticks the worker exits with EXIT_UNHEALTHY, and the launcher restarts it within a bounded budget.
"""
import argparse
import logging
import os
import sys
import time
from contextlib import closing
from pathlib import Path

import httpx

from . import db
from .db import connect, initialize, packed, write_tx
from .provisioning import provision_due
from .verification import dispatch_due, expire_due

LOG = logging.getLogger("worker")
MAX_FAILED_TICKS = 20
EXIT_UNHEALTHY = 75


def heartbeat(path):
    with write_tx(path) as conn:
        conn.execute("""INSERT INTO settings VALUES ('heartbeat',?)
                     ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (str(time.time()),))


def run_once(path, clock, post):
    """One deterministic worker step. Tests call it with an injected clock and in-process transport."""
    heartbeat(path)
    expired = expire_due(path, clock())
    sent = dispatch_due(path, clock, post)
    provision_due(path, clock, post)
    return expired, sent


def record_failure(path, phase, exc):
    """Visible, data-free failure record: phase, exception class, time and process."""
    detail = packed({"phase": phase, "error": type(exc).__name__, "at": time.time(), "pid": os.getpid()})
    LOG.error("worker phase %s failed: %s", phase, type(exc).__name__)
    try:
        with write_tx(path) as conn:
            conn.execute("""INSERT INTO settings VALUES ('worker_error',?)
                         ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (detail,))
    except Exception:  # the store itself may be the failing component; the log line above still exists
        pass


def run_cycle(path, clock, post, ready=lambda: True):
    """Run every phase even if an earlier one fails. Returns the names of phases that failed."""
    failed = []
    phases = [("heartbeat", lambda: heartbeat(path)), ("expiry", lambda: expire_due(path, clock()))]
    if ready():
        # If our own web process is not serving yet, wait instead of spending provider retry claims.
        phases += [("dispatch", lambda: dispatch_due(path, clock, post)),
                   ("issuance", lambda: provision_due(path, clock, post))]
    for name, phase in phases:
        try:
            phase()
        except Exception as exc:  # isolate the phase; the failure is logged and recorded, never swallowed silently
            record_failure(path, name, exc)
            failed.append(name)
    return failed


class LoopbackPoster:
    """Sends dispatch requests to the onboarding web process that hosts the simulated providers."""
    def __init__(self, path):
        self.path = path
        self.client = httpx.Client(trust_env=False)
        self.origin = None

    def ready(self):
        try:
            with closing(connect(self.path)) as conn:
                self.origin = db.setting(conn, "web_origin")
        except Exception:
            return False
        if not self.origin:
            return False
        try:
            return self.client.get(self.origin + "/health", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def __call__(self, route, body, headers, timeout):
        return self.client.post(self.origin + route, content=body, headers=headers, timeout=timeout)


def serve(path, poster, tick, max_failed_ticks=MAX_FAILED_TICKS, sleep=time.sleep, ticks=None):
    """Main loop. Returns EXIT_UNHEALTHY after max_failed_ticks consecutive ticks with a failing phase."""
    streak = 0
    while ticks is None or ticks > 0:
        failed = run_cycle(path, time.time, poster, poster.ready)
        streak = streak + 1 if failed else 0
        if streak >= max_failed_ticks:
            LOG.error("worker unhealthy after %d consecutive failing ticks; exiting for supervised restart", streak)
            return EXIT_UNHEALTHY
        if ticks is not None:
            ticks -= 1
        sleep(tick)
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path("private/demo"))
    parser.add_argument("--timing", choices=sorted(db.TIMING))
    args = parser.parse_args()
    path = initialize(args.run_dir, args.timing)
    with write_tx(path) as conn:
        conn.execute("""INSERT INTO settings VALUES ('worker_pid',?)
                     ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (str(os.getpid()),))
        tick = db.timing(conn)["tick_s"]
    poster = LoopbackPoster(path)
    try:
        return serve(path, poster, tick)
    except KeyboardInterrupt:
        return 0
    finally:
        poster.client.close()


if __name__ == "__main__":
    sys.exit(main())
