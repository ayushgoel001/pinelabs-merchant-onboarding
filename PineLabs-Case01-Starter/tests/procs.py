"""Helpers for tests that run the real web process and the real worker as separate OS processes."""
import os
import socket
import subprocess
import sys
import time
from contextlib import closing

import httpx

from app import db
from app.run import stop_tree


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, timeout=20.0, step=0.05, message="condition"):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        try:
            if check():
                return time.monotonic()
        except (httpx.TransportError, OSError):
            pass
        time.sleep(step)
    raise AssertionError(f"Timed out waiting for {message}.")


def rows(path, sql, *args):
    with closing(db.connect(path)) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def scalar(path, sql, *args):
    with closing(db.connect(path)) as conn:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None


class Live:
    """One run directory with a real web process and a separately controlled worker."""
    def __init__(self, folder, timing="fast"):
        self.folder = folder
        self.path = db.initialize(folder, timing)
        self.port = free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.web = self.worker = None
        self.web_log = open(folder / "web.log", "w+b")
        self.worker_logs = []
        self.web = subprocess.Popen([sys.executable, "-m", "app.web", "--run-dir", str(folder), "--port", str(self.port)],
                                    stdout=self.web_log, stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=self.origin, headers={"Origin": self.origin}, timeout=10, trust_env=False)
        wait_for(lambda: self.client.get("/health").status_code == 200, message="the web process")
        self.client.get("/")  # intake session cookie

    def start_worker(self, env=None):
        log = open(self.folder / f"worker-{len(self.worker_logs)}.log", "w+b")
        self.worker_logs.append(log)
        self.worker = subprocess.Popen([sys.executable, "-m", "app.worker", "--run-dir", str(self.folder)],
                                       stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})})
        return self.worker

    def worker_log(self, index=-1):
        log = self.worker_logs[index]
        log.flush()
        log.seek(0)
        return log.read().decode("utf-8", "replace")

    def web_text(self):
        self.web_log.flush()
        self.web_log.seek(0)
        return self.web_log.read().decode("utf-8", "replace")

    def submit(self, body, client=None):
        response = (client or self.client).post("/api/cases", json=body)
        assert response.status_code in (200, 201), response.text
        return response.json()["case_id"]

    def operator(self):
        client = httpx.Client(base_url=self.origin, headers={"Origin": self.origin}, timeout=10, trust_env=False)
        ticket = (self.folder / "operator-link.txt").read_text(encoding="utf-8").split("#")[1]
        assert client.post("/access", json={"ticket": ticket}).status_code == 303
        return client

    def counts(self, case):
        """approvals / simulated issuer MIDs / local MIDs for one case."""
        issuer = scalar(self.folder / "simulator.db", "SELECT count(*) FROM sim_issuances WHERE operation_key=?", "mid/" + case)
        return (scalar(self.path, "SELECT count(*) FROM approvals WHERE case_id=?", case), issuer,
                scalar(self.path, "SELECT count(*) FROM merchants WHERE case_id=?", case))

    def state(self, case):
        return scalar(self.path, "SELECT state FROM applications WHERE id=?", case)

    def close(self):
        for process in (self.worker, self.web):
            if process is not None:
                stop_tree(process)
        self.client.close()
        for log in [self.web_log, *self.worker_logs]:
            log.close()
