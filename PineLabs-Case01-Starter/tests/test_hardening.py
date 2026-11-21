"""M3 security hardening: regressions for the independent review's B1, B2, I1, I2 and I3."""
import json
import os
import signal
import socket
import sqlite3
import threading
import time
from contextlib import closing
from uuid import uuid4

import pytest

from app import db, fixtures, worker
from app.access import issue_ticket
from app.run import RestartBudget, supervise
from app.web import create_app, health
from conftest import Browser, Stack, correction
from test_verification import active, attempts, entered, query, scenario, started, state


def login(browser, path):
    assert browser.request("POST", "/access", json={"ticket": issue_ticket(path, "operator")}).status_code == 303


def decide(browser, case, issue, action, reason=None, version=None):
    from app.review import REASONS
    return browser.request("POST", f"/api/cases/{case}/issues/{issue['id']}/decisions",
                           json={"action": action, "version": version or issue["version"],
                                 "reason": reason or REASONS[action][0]})


def counts(stack, case):
    """approvals / simulated issuer MIDs / local MIDs"""
    return (len(query(stack.path, "SELECT 1 FROM approvals WHERE case_id=?", case)),
            stack.app.state.simulator.issuance_count(case),
            len(query(stack.path, "SELECT 1 FROM merchants WHERE case_id=?", case)))


def issue(stack, case, cause):
    return next(i for i in active(stack.path, case) if i["cause"] == cause)


# ---------------------------------------------------------------- B1 · adverse RISK result, then self-declared change

def test_b1_exploit_prohibited_profile_cannot_self_clear_by_changing_category(browser, stack):
    """Opus's exact exploit: WHOLESALE/SYNZONE-03 FAIL, merchant edits to RETAIL/SYNZONE-01, new RISK PASS."""
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    case = started(browser, stack, payload)
    stack.run_to(9)
    assert attempts(stack.path, case, "RISK")[0]["reason_code"] == "PROHIBITED_PROFILE"
    payload["merchant"]["category"], payload["merchant"]["zone"] = "RETAIL", "SYNZONE-01"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.sync()
    stack.run_to(20)
    risk = attempts(stack.path, case, "RISK")
    assert [(a["input_revision"], a["status"]) for a in risk] == [(1, "FAIL"), (2, "PASS")]
    # The new PASS alone never approves: a human must clear the change against the current assessment.
    assert state(stack.path, case) == "IN_REVIEW" and counts(stack, case) == (0, 0, 0)
    assert query(stack.path, "SELECT * FROM decisions WHERE case_id=?", case) == []
    hold = issue(stack, case, "RISK_INPUTS_CHANGED_AFTER_REFERRAL")
    assert "adverse RISK result" in hold["reason"] and hold["owner"] == "OPERATOR"
    login(browser, stack.path)
    assert "Clear changed risk inputs" in browser.request("GET", "/cases/" + case).text
    assert decide(browser, case, hold, "CLEAR_RISK_CHANGE").status_code == 200
    receipt = json.loads(query(stack.path, "SELECT receipt FROM approvals WHERE case_id=?", case)[0]["receipt"])
    assert receipt["risk_clearances"][0]["attempt_id"] == risk[1]["id"]
    stack.tick()
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


def test_b1_second_edit_rebinds_clearance_to_latest_assessment(browser, stack):
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    case = started(browser, stack, payload)
    stack.run_to(9)
    payload["merchant"]["category"], payload["merchant"]["zone"] = "RETAIL", "SYNZONE-01"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.sync()
    stack.run_to(10)
    login(browser, stack.path)
    stale = issue(stack, case, "RISK_INPUTS_CHANGED_AFTER_REFERRAL")
    payload["merchant"]["zone"] = "SYNZONE-02"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 2)).status_code == 200
    assert decide(browser, case, stale, "CLEAR_RISK_CHANGE").status_code == 409  # bound to revision 2
    stack.sync()
    stack.run_to(10)
    current = issue(stack, case, "RISK_INPUTS_CHANGED_AFTER_REFERRAL")
    assert current["id"] == stale["id"] and current["subject_revision"] == 3 and counts(stack, case) == (0, 0, 0)
    assert decide(browser, case, current, "CLEAR_RISK_CHANGE").status_code == 200
    receipt = json.loads(query(stack.path, "SELECT receipt FROM approvals WHERE case_id=?", case)[0]["receipt"])
    rev3 = attempts(stack.path, case, "RISK")[-1]
    assert rev3["input_revision"] == 3 and receipt["risk_clearances"][-1]["attempt_id"] == rev3["id"]


def test_b1_gate_refuses_resolved_hold_without_clearance(browser, stack):
    """The gate's own clearance layer (review O2), independent of the open-issue layer."""
    from app import approval
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    case = started(browser, stack, payload)
    stack.run_to(9)
    payload["merchant"]["category"], payload["merchant"]["zone"] = "RETAIL", "SYNZONE-01"
    browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1))
    stack.sync()
    stack.run_to(10)
    with db.write_tx(stack.path) as conn:
        conn.execute("SAVEPOINT bypass")
        conn.execute("UPDATE review_items SET status='RESOLVED' WHERE case_id=?", (case,))
        assert approval.build_receipt(conn, conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()) is None
        conn.execute("ROLLBACK TO bypass")
        conn.execute("RELEASE bypass")


# ---------------------------------------------------------------- B2 · identifiers bound to registered legal names

IMPOSTOR = dict(name="Aster Valley Stores", pan="SYNPAN-0001", gst="SYNGST-27-SYNPAN-0001-1",
                account="SYNACCT-0009", routing="SYNIFSC-0009", category="RETAIL", zone="SYNZONE-01")


def documents_for(merchant):
    printed = merchant["name"].upper()
    return {"identity": {"name": printed, "pan": merchant["pan"]},
            "tax": {"name": printed, "pan": merchant["pan"], "gst": merchant["gst"]},
            "bank": {"beneficiary": merchant["name"], "account": merchant["account"], "routing": merchant["routing"]}}


def test_b2_exploit_another_business_pan_and_gst_are_refused(browser, stack):
    """Opus's exact exploit: Acme's PAN and GST under a different legal name, with a bank account in that name."""
    payload = dict(submission_key=str(uuid4()), merchant=dict(IMPOSTOR), documents=documents_for(IMPOSTOR))
    case = started(browser, stack, payload)
    stack.run_to(12)
    result = {a["kind"]: (a["status"], a["reason_code"]) for a in attempts(stack.path, case)}
    assert result["PAN"] == ("FAIL", "NAME_MISMATCH") and result["GST"] == ("FAIL", "NAME_MISMATCH")
    assert state(stack.path, case) == "IN_REVIEW" and counts(stack, case) == (0, 0, 0)
    assert {(i["cause"], i["subject"]) for i in active(stack.path, case)} >= {
        ("CHECK_FAILED", "check.PAN"), ("CHECK_FAILED", "check.GST")}
    assert fixtures.label_for(IMPOSTOR) == "Custom synthetic input"
    login(browser, stack.path)
    failed = issue(stack, case, "CHECK_FAILED")
    for action in ("ACCEPT_REFERRAL", "ACCEPT_BENEFICIARY_MATCH"):
        assert decide(browser, case, failed, action).status_code == 409  # a definitive failure has no waiver
    # The genuine registered business with its own account then verifies normally on new revisions.
    genuine = dict(IMPOSTOR, name="Acme Traders Private Limited", account="SYNACCT-0001", routing="SYNIFSC-0001")
    body = correction(dict(payload, merchant=genuine, documents=documents_for(genuine)), 1)
    assert browser.request("PATCH", "/api/cases/" + case, json=body).status_code == 200
    stack.sync()
    stack.run_to(10)
    assert {a["kind"]: a["status"] for a in attempts(stack.path, case) if a["input_revision"] == 2} == {
        "PAN": "PASS", "GST": "PASS", "BANK": "PASS"}
    stack.tick()
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


@pytest.mark.parametrize(("registered", "submitted", "match"), [
    ("Acme Traders Private Limited", "ACME TRADERS PVT LTD", True),
    ("Acme Traders Private Limited", "acme traders private limited.", True),
    ("Acme Traders Private Limited", "Aster Valley Stores", False),
    ("Acme Traders Private Limited", "Acme Trading Private Limited", False),
])
def test_b2_documented_deterministic_name_rule(registered, submitted, match):
    assert fixtures.names_match(registered, submitted) is match


def test_b2_every_registry_identifier_has_a_registered_name_and_demo_fixtures_still_pass():
    for s in fixtures.SCENARIOS:
        m = s["merchant"]
        assert fixtures.names_match(fixtures.registered_name("PAN", m["pan"]), m["name"])
        assert fixtures.names_match(fixtures.registered_name("GST", m["gst"]), m["name"])
        assert fixtures.label_for(m) == s["label"]


# ---------------------------------------------------------------- I1 · weak beneficiary match, human acceptance

def test_i1_operator_accepts_review_band_beneficiary_with_reason(browser, stack):
    case = started(browser, stack, scenario("bank-name-review"))
    stack.run_to(8.5)
    review = issue(stack, case, "BENEFICIARY_REVIEW")
    login(browser, stack.path)
    page = browser.request("GET", "/cases/" + case).text
    assert "Accept beneficiary match" in page
    assert decide(browser, case, review, "ACCEPT_BENEFICIARY_MATCH", reason="not an offered reason").status_code == 422
    assert decide(browser, case, review, "ACCEPT_BENEFICIARY_MATCH", version=review["version"] + 1).status_code == 409
    assert decide(browser, case, review, "ACCEPT_BENEFICIARY_MATCH").status_code == 200
    receipt = json.loads(query(stack.path, "SELECT receipt FROM approvals WHERE case_id=?", case)[0]["receipt"])
    bank = next(w for w in receipt["checks"] if w["kind"] == "BANK")
    assert bank["name_score"] == 70.4 and bank["acceptance"]["actor"] == "operator"
    assert bank["acceptance"]["reason"] == "Registered name is an abbreviation of the legal name"
    stack.tick()
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


def test_i1_conflict_band_and_definitive_failures_cannot_be_accepted(browser, stack):
    m = dict(fixtures.BY_PAN["SYNPAN-0009"]["merchant"], account="SYNACCT-0007")  # registered to another business
    case = started(browser, stack, dict(submission_key=str(uuid4()), merchant=m, documents=fixtures.documents(m)))
    stack.run_to(8.5)
    bank = attempts(stack.path, case, "BANK")[0]
    assert bank["status"] == "PASS" and bank["name_score"] < 65
    conflict = issue(stack, case, "BENEFICIARY_REVIEW")
    login(browser, stack.path)
    assert "Accept beneficiary match" not in browser.request("GET", "/cases/" + case).text
    assert decide(browser, case, conflict, "ACCEPT_BENEFICIARY_MATCH").status_code == 409
    assert counts(stack, case) == (0, 0, 0) and state(stack.path, case) == "IN_REVIEW"


# ---------------------------------------------------------------- I2 · worker resilience and supervision

def test_i2_failing_phase_is_isolated_recorded_and_recovers(browser, stack, payload, monkeypatch):
    case = browser.create(payload)
    stack.sync()
    real = worker.dispatch_due
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args)

    monkeypatch.setattr(worker, "dispatch_due", flaky)
    clock = lambda: stack.now
    assert worker.run_cycle(stack.path, clock, stack.post) == ["dispatch"]
    with closing(db.connect(stack.path)) as conn:
        status = health(conn)
    assert status["worker_error"] == "dispatch · OperationalError"  # phase and class only, visible on the desk
    login(browser, stack.path)
    assert "recent error: dispatch · OperationalError" in browser.request("GET", "/desk").text
    assert {a["status"] for a in attempts(stack.path, case)} == {"SCHEDULED"}
    assert worker.run_cycle(stack.path, clock, stack.post) == []   # next tick recovers on its own
    assert {a["status"] for a in attempts(stack.path, case)} == {"SENT"}


def test_i2_persistent_failure_exits_unhealthy_but_transient_does_not(app, monkeypatch):
    class Poster:
        def ready(self):
            return True

        def __call__(self, *args):
            raise AssertionError("not reached")

    failures = {"left": 2}

    def heartbeat(path):
        if failures["left"]:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "heartbeat", heartbeat)
    path = app.state.database
    assert worker.serve(path, Poster(), 0, max_failed_ticks=3, sleep=lambda s: None, ticks=6) == 0
    failures["left"] = 10 ** 6
    assert worker.serve(path, Poster(), 0, max_failed_ticks=3, sleep=lambda s: None) == worker.EXIT_UNHEALTHY


def test_i2_restart_budget_is_bounded():
    now = [0.0]
    budget = RestartBudget(limit=2, window=10, clock=lambda: now[0])
    assert budget.allow() and budget.allow() and not budget.allow()
    now[0] = 11.0
    assert budget.allow()


def test_i2_launcher_restarts_a_killed_worker(tmp_path, capfd):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = db.initialize(tmp_path, "fast")
    stop = threading.Event()
    launcher = threading.Thread(target=supervise, args=(tmp_path, port, stop))
    launcher.start()

    def pid():
        with closing(db.connect(path)) as conn:
            value = db.setting(conn, "worker_pid")
            beat = db.setting(conn, "heartbeat")
        return value, float(beat) if beat else 0

    try:
        deadline = time.monotonic() + 20
        while not pid()[0] and time.monotonic() < deadline:
            time.sleep(0.1)
        first = pid()[0]
        assert first
        os.kill(int(first), signal.SIGTERM)
        killed_at = time.time()
        while time.monotonic() < deadline and not (pid()[0] != first and pid()[1] > killed_at):
            time.sleep(0.1)
        assert pid()[0] != first and pid()[1] > killed_at   # a new worker is heart-beating
    finally:
        stop.set()
        launcher.join(15)
    assert not launcher.is_alive()
    assert "Worker exited with code" in capfd.readouterr().out


# ---------------------------------------------------------------- I3 · timing margin

# "reference" only drives injected-clock tests, where wall-clock load cannot cause expiries.
@pytest.mark.parametrize("profile", ["demo", "fast"])
def test_i3_normal_replies_land_well_inside_the_timeout(profile):
    timing = db.TIMING[profile]
    normal = [b["delay"] for plans in fixtures.REGISTRY.values() for plan in plans.values() for b in plan
              if not set(b.get("faults", ())) & {"LATE", "SILENT", "HOLD"}]
    assert max(normal) * timing["delay_scale"] <= 0.5 * timing["attempt_timeout_s"]
    assert 1.5 * timing["attempt_timeout_s"] > timing["attempt_timeout_s"]  # LATE stays a real timeout scenario


def test_i3_demo_profile_happy_path_has_first_generation_replies(tmp_path, payload):
    app = create_app(tmp_path, "demo")
    client = Browser(app)
    stack = Stack(app, client.runner)
    try:
        case = client.create(payload)
        stack.sync()
        stack.tick()
        stack.run_to(4.25)
        assert [(a["generation"], a["status"]) for a in attempts(stack.path, case)] == [(1, "PASS")] * 4
        stack.tick()
        assert state(stack.path, case) == "ACTIVE" and entered(stack.path, case)[-1] == "ACTIVE"
    finally:
        stack.close()
        client.close()
