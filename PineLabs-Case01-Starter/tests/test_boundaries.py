import ast
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from app import db, workflow
from app.access import issue_ticket
from app.extraction import SAFE_MESSAGE
from app.web import health
from app.worker import heartbeat
from conftest import Browser, correction


@pytest.mark.parametrize("field", ["pan","gst","routing","account"])
def test_identifier_rejection_is_private(browser, app, payload, caplog, field):
    pan = "Z" * 5 + "0" * 4 + "Z"
    value = {"pan": pan, "gst": "27" + pan + "1Z9",
             "routing": "Z" * 4 + "0" + "0" * 6, "account": "0" * 12}[field]
    payload["merchant"][field] = value
    with caplog.at_level(logging.INFO):
        result = browser.request("POST", "/api/cases", json=payload)
    assert result.status_code == 422 and result.json()["detail"] == SAFE_MESSAGE
    assert value not in result.text and value not in caplog.text
    assert "access POST /api/cases 422" in caplog.text
    for file in app.state.database.parent.glob("onboarding.db*"):
        assert value.encode() not in file.read_bytes()


def test_fragment_exchange_origin_and_case_authority(browser, app, payload, caplog):
    with caplog.at_level(logging.INFO):
        created = browser.request("POST", "/api/cases", json=payload).json()
        ticket = created["location"].split("#")[1]
        case = created["case_id"]
        response = browser.request("POST", "/access", json={"ticket": ticket})
        assert response.status_code == 303 and response.headers["location"] == "/cases/" + case
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert browser.request("POST", "/access", json={"ticket": ticket}).status_code == 403
        assert ticket not in caplog.text and ticket not in response.headers["location"]
    stranger = Browser(app)
    try:
        stranger.create({**payload, "submission_key": str(uuid4())})
        replay = stranger.request("POST", "/api/cases", json=payload).json()
        assert "#" not in replay["location"]
        assert stranger.request("GET", replay["location"]).status_code == 403
        assert stranger.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 403
        assert "Operator access required" in stranger.request("GET", "/desk").text
        stranger.request("POST", "/access", json={"ticket": issue_ticket(app.state.database, "operator")})
        assert stranger.request("GET", replay["location"]).status_code == 200
        assert stranger.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 403
    finally:
        stranger.close()
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1),
                           headers={"Origin": "https://example.invalid"}).status_code == 403


def test_oversize_and_unavailable_image(browser, app):
    r = browser.request("POST", "/api/cases", content=b" " * 32769, headers={"Content-Type":"application/json"})
    assert r.status_code == 413
    r = browser.request("POST", "/api/cases", content=b"", headers={"Content-Type":"image/png"})
    # Images are never accepted at intake: documents are uploaded to an existing case (see test_documents.py).
    assert r.status_code == 415 and "existing case" in r.json()["detail"]


def test_sqlite_discipline_and_rollback(app):
    path = app.state.database
    with closing(db.connect(path)) as conn:
        assert conn.isolation_level is None
        assert [conn.execute("PRAGMA " + key).fetchone()[0] for key in
                ("foreign_keys","busy_timeout","journal_mode","synchronous")] == [1,5000,"wal",2]
    with pytest.raises(RuntimeError), db.write_tx(path) as conn:
        conn.execute("INSERT INTO settings VALUES ('rollback_probe','temporary')")
        raise RuntimeError("intentional rollback")
    with closing(db.connect(path)) as conn:
        assert not conn.execute("SELECT 1 FROM settings WHERE name='rollback_probe'").fetchone()


def test_only_helper_owns_write_transactions_and_database_routes_are_sync():
    folder = Path(db.__file__).parent
    for file in folder.glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func.name in {"write_tx", "begin_immediate"} and file.name == "db.py":
                continue  # the helper and the lock acquisition it calls are the only place a write transaction begins
            for node in ast.walk(func):
                assert not (isinstance(node, ast.Attribute) and node.attr == "commit"), file.name
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert not node.value.lstrip().upper().startswith("BEGIN"), file.name
            if isinstance(func, ast.AsyncFunctionDef):
                names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
                assert not names.intersection({"write_tx","connect","amend","submit","case_view","exchange"})


def test_active_issue_unique_and_terminal_precondition(browser, app, payload, stack):
    del payload["documents"]["tax"]
    case = browser.create(payload)
    with pytest.raises(sqlite3.IntegrityError), db.write_tx(app.state.database) as conn:
        conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
            SELECT case_id,cause,subject,subject_revision+1,owner,status,reason FROM review_items""")
    from app.extraction import SAMPLE_DOCS
    payload["documents"]["tax"] = SAMPLE_DOCS["tax"]
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.sync()
    stack.tick()
    stack.now += 8
    stack.deliver()
    result = browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1))
    assert result.status_code == 409 and "servicing required" in result.text


def test_transition_guard_and_state_model(browser, app, payload):
    case = browser.create(payload)
    assert "issuer receipt" in browser.request("GET", "/state-model").text
    with db.write_tx(app.state.database) as conn:
        count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        for target, trigger in [("ACTIVE","issuer receipt"), ("IN_REVIEW","made up trigger")]:
            with pytest.raises(ValueError):
                workflow.transition(conn, case, target, trigger)
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == count


def test_heartbeat_boundary(app, monkeypatch):
    path = app.state.database
    with closing(db.connect(path)) as conn:
        assert not health(conn)["worker_online"]
        monkeypatch.setattr(time, "time", lambda: 100.0)
        heartbeat(path)
        monkeypatch.setattr(time, "time", lambda: 109.999)
        assert health(conn)["worker_online"]
        monkeypatch.setattr(time, "time", lambda: 110.0)
        assert not health(conn)["worker_online"]


def test_transition_relation_all_49_pairs(browser, app, payload):
    case = browser.create(payload)
    states = workflow.STATES
    needs, review, verify, approved, failed, active, rejected = states
    allowed = {(needs, review), (needs, verify), (needs, approved), (review, needs),
               (review, verify), (review, approved), (review, rejected), (verify, needs),
               (verify, review), (verify, approved), (approved, failed), (approved, active),
               (failed, active)}
    triggers = {needs: "merchant action", review: "operator hold", verify: "work queued",
                approved: "gate satisfied", failed: "issuance error/unknown",
                active: "issuer receipt", rejected: "operator rejection"}
    with db.write_tx(app.state.database) as conn:
        for source in states:
            for target in states:
                conn.execute("SAVEPOINT edge_probe")
                conn.execute("UPDATE applications SET state=? WHERE id=?", (source,case))
                count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
                if (source, target) in allowed and target in {approved, failed, active}:
                    workflow.validate_edge(source, target, triggers[target])
                    with pytest.raises(ValueError, match="receipt is required"):
                        workflow.transition(conn, case, target, triggers[target])
                    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == count
                elif (source, target) in allowed:
                    workflow.transition(conn, case, target, triggers[target])
                    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == count + 1
                else:
                    with pytest.raises(ValueError):
                        workflow.transition(conn, case, target, triggers[target])
                    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == count
                conn.execute("ROLLBACK TO edge_probe")
                conn.execute("RELEASE edge_probe")
