import asyncio
import json
import time
from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app import db, worker
from app.extraction import SAMPLE_DOCS, SAMPLE_MERCHANT
from app.web import create_app
from app.workflow import STRANDED


class Browser:
    """HTTP client for the ASGI app. Cookies and redirects use normal HTTP semantics."""
    def __init__(self, app):
        self.runner = asyncio.Runner()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://127.0.0.1", headers={"Origin": "http://127.0.0.1"})
        self.request("GET", "/")

    def request(self, method, path, **kwargs):
        return self.runner.run(self.client.request(method, path, **kwargs))

    def close(self):
        self.runner.run(self.client.aclose())
        self.runner.close()

    def create(self, body):
        result = self.request("POST", "/api/cases", json=body)
        assert result.status_code == 201
        info = result.json()
        ticket = info["location"].split("#")[1]
        redirect = self.request("POST", "/access", json={"ticket": ticket})
        assert redirect.status_code == 303
        assert redirect.headers["location"] == "/cases/" + info["case_id"]
        return info["case_id"]


def check_invariants(path):
    """Cross-cutting facts that must hold after any test, crash or load run, whatever happened in between."""
    with closing(db.connect(path)) as conn:
        assert conn.execute(STRANDED).fetchall() == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not conn.execute("""SELECT 1 FROM applications a LEFT JOIN merchants m ON m.case_id=a.id
            WHERE (a.state='ACTIVE')<>(m.case_id IS NOT NULL)""").fetchall()
        # One approval, one claim, one issuance job and at most one local MID per case; every MID value unique.
        assert conn.execute("SELECT count(*) FROM approvals").fetchone()[0] == conn.execute(
            "SELECT count(*) FROM identity_claims").fetchone()[0]
        assert not conn.execute("""SELECT case_id FROM jobs WHERE kind='ISSUE_MID' GROUP BY case_id HAVING count(*)>1""").fetchall()
        assert not conn.execute("SELECT mid FROM merchants GROUP BY mid HAVING count(*)>1").fetchall()
        assert not conn.execute("""SELECT case_id FROM events WHERE event='STATE_ENTERED' AND detail='ACTIVE'
                                 GROUP BY case_id HAVING count(*)>1""").fetchall()
        for row in conn.execute("SELECT * FROM approvals"):
            receipt = json.loads(row["receipt"])
            assert row["receipt_digest"] == db.digest(row["receipt"])
            assert [w["kind"] for w in receipt["checks"]] == receipt["policy"]["required"]
            for w in receipt["checks"]:
                current = conn.execute("""SELECT a.* FROM attempts a JOIN checks c USING(case_id,kind,input_revision)
                    WHERE a.case_id=? AND a.kind=? ORDER BY generation DESC LIMIT 1""", (row["case_id"], w["kind"])).fetchone()
                assert current["id"] == w["attempt_id"] and current["input_revision"] == w["input_revision"]
                if current["status"] == "REFERRED":
                    assert w["kind"] == "RISK" and conn.execute("""SELECT 1 FROM decisions WHERE attempt_id=?
                        AND action='ACCEPT_REFERRAL' AND id=?""", (current["id"], w["acceptance"]["id"])).fetchone()
                else:
                    assert current["status"] == "PASS"
            assert conn.execute("SELECT count(*) FROM jobs WHERE case_id=?", (row["case_id"],)).fetchone()[0] == 1


@pytest.fixture(autouse=True)
def release_writer_connections():
    """write_tx keeps one long-lived connection per database. Close them so no test leaves an open file behind."""
    yield
    db.close_writers()


@pytest.fixture
def app(tmp_path):
    # Deterministic injected-clock tests use the 2/4/6/8 s reference timeline.
    instance = create_app(tmp_path, "reference")
    yield instance
    check_invariants(instance.state.database)


@pytest.fixture
def browser(app):
    client = Browser(app)
    yield client
    client.close()


@pytest.fixture
def payload():
    return dict(submission_key=str(uuid4()), merchant=deepcopy(SAMPLE_MERCHANT), documents=deepcopy(SAMPLE_DOCS))


def correction(payload, revision):
    return dict(merchant=payload["merchant"], documents=payload["documents"], data_revision=revision)


class Stack:
    """Drives the real worker step, the simulators' callback outbox and the HTTP routes in-process,
    with one injected clock. Every provider request and callback still crosses the ASGI HTTP boundary."""
    def __init__(self, app, runner):
        self.app, self.runner = app, runner
        self.path = app.state.database
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
        self.now = 0.0
        app.state.clock = lambda: self.now

    def sync(self):
        """Start the injected clock just after the real time used by intake writes."""
        self.now = max(self.now, time.time()) + 0.001
        self.origin = self.now
        return self.now

    def post(self, route, body, headers, timeout=None):
        return self.runner.run(self.client.post(route, content=body, headers=headers))

    def tick(self):
        return worker.run_once(self.path, lambda: self.now, self.post)

    def deliver(self):
        return self.app.state.simulator.deliver_due(self.now, self.post)

    def step(self):
        self.tick()
        delivered = self.deliver()
        if delivered:
            self.tick()  # settle-driven successors that are already due

    def run_to(self, offset, step=0.25):
        """Advance from the synced origin to origin + offset seconds, ticking and delivering each step."""
        target = self.origin + offset
        while self.now < target - 1e-9:
            self.now = min(target, self.now + step)
            self.step()

    def close(self):
        self.runner.run(self.client.aclose())


@pytest.fixture
def stack(app, browser):
    instance = Stack(app, browser.runner)
    yield instance
    instance.close()
