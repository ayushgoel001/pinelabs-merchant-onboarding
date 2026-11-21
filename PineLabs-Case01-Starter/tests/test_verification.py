"""Milestone 2: simulated providers over HTTP, signed asynchronous callbacks, deadlines and retries.

Every request and callback crosses the ASGI HTTP boundary. Time is an injected clock so deadline ties are
exact. Demo timing applies: 10 s attempt timeout, 2 s and 5 s backoff, provider delays of 2/4/6/8 s.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app import db, fixtures, seed, worker
from app.access import issue_ticket
from app.verification import claim_dispatch, provider_secrets, receive_callback, sign
from app.workflow import reconcile
from conftest import Browser, Stack, correction


def scenario(slug, complete=False):
    s = next(item for item in fixtures.SCENARIOS if item["slug"] == slug)
    documents = fixtures.documents(s["merchant"]) if complete else s["documents"]
    return dict(submission_key=str(uuid4()), merchant=deepcopy(s["merchant"]), documents=deepcopy(documents))


def query(path, sql, *args):
    with closing(db.connect(path)) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def attempts(path, case, kind=None):
    rows = query(path, "SELECT * FROM attempts WHERE case_id=? ORDER BY rowid", case)
    return [r for r in rows if kind in (None, r["kind"])]


def state(path, case):
    return query(path, "SELECT state FROM applications WHERE id=?", case)[0]["state"]


def active(path, case):
    return query(path, "SELECT * FROM review_items WHERE case_id=? AND status<>'RESOLVED' ORDER BY id", case)


def entered(path, case):
    return [e["detail"] for e in query(path, "SELECT detail FROM events WHERE case_id=? AND event='STATE_ENTERED' "
                                             "ORDER BY seq", case)]


def inbox(path, case):
    return query(path, "SELECT * FROM callback_inbox WHERE case_id=? ORDER BY received_at,rowid", case)


def deliveries(stack, provider=None):
    with closing(db.connect(stack.app.state.simulator.path)) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sim_deliveries ORDER BY id")]
    return [r for r in rows if provider in (None, r["provider"])]


def sim_requests(stack):
    with closing(db.connect(stack.app.state.simulator.path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM sim_requests ORDER BY rowid")]


def started(browser, stack, payload):
    case = browser.create(payload)
    stack.sync()
    stack.tick()
    return case


# ---------------------------------------------------------------- C03 / C05 · four providers, real round trip

def test_four_providers_round_trip_over_http(browser, stack, payload):
    path = stack.path
    case = started(browser, stack, payload)
    sent = attempts(path, case)
    assert [(a["kind"], a["status"]) for a in sent] == [(k, "SENT") for k in ("PAN", "GST", "BANK", "RISK")]
    assert all(a["deadline"] == pytest.approx(a["sent_at"] + 10) for a in sent)
    assert {a["reference"] for a in sent} == {r["reference"] for r in sim_requests(stack)}
    # Pending before any callback: the request is acknowledged, the result has not arrived.
    assert state(path, case) == "VERIFYING" and inbox(path, case) == []
    page = browser.request("GET", "/cases/" + case).text
    assert page.count(">SENT<") == 4 and "No provider callback has arrived yet." in page
    order = []
    for offset in (1.9, 2.0, 4.0, 6.0, 8.0):
        stack.run_to(offset)
        order.append([r["provider"] for r in inbox(path, case)])
    assert order == [[], ["PAN"], ["PAN", "GST"], ["PAN", "GST", "BANK"], ["PAN", "GST", "BANK", "RISK"]]
    settled = attempts(path, case)
    assert [a["status"] for a in settled] == ["PASS"] * 4
    assert {r["disposition"] for r in inbox(path, case)} == {"APPLIED"}
    assert next(a for a in settled if a["kind"] == "BANK")["name_score"] == 100.0
    assert state(path, case) == "ACTIVE" and entered(path, case) == ["VERIFYING", "APPROVED_PENDING_MID", "ACTIVE"]
    assert active(path, case) == []
    page = browser.request("GET", "/cases/" + case).text
    assert page.count(">APPLIED<") == 4 and "Approval receipt" in page and "SYNMID-" in page


# ---------------------------------------------------------------- F01 and the B1 resubmission rules

def gst_failed(browser, stack):
    payload = scenario("gst-fail")
    case = started(browser, stack, payload)
    stack.run_to(8.5)
    return payload, case


def test_pan_pass_gst_definitive_fail_opens_operator_review(browser, stack):
    _, case = gst_failed(browser, stack)
    path = stack.path
    assert {a["kind"]: a["status"] for a in attempts(path, case)} == {
        "PAN": "PASS", "GST": "FAIL", "BANK": "PASS", "RISK": "PASS"}
    [issue] = active(path, case)
    assert (issue["cause"], issue["subject"], issue["owner"]) == ("CHECK_FAILED", "check.GST", "OPERATOR")
    assert "GST_REGISTRATION_CANCELLED" in issue["reason"]
    assert state(path, case) == "IN_REVIEW" and entered(path, case) == ["VERIFYING", "IN_REVIEW"]
    # Callbacks for other checks still settle during review; a definitive fail gets no automatic retry.
    assert [r["disposition"] for r in inbox(path, case)] == ["APPLIED"] * 4
    stack.run_to(40)
    assert len(attempts(path, case, "GST")) == 1


def test_unchanged_resubmission_returns_to_operator_and_new_gst_rechecks(browser, stack):
    payload, case = gst_failed(browser, stack)
    path = stack.path
    [issue] = active(path, case)
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    [same] = active(path, case)
    assert same["id"] == issue["id"] and same["owner"] == "OPERATOR"
    assert same["reason"].endswith("resubmitted unchanged")
    assert state(path, case) == "IN_REVIEW" and entered(path, case) == ["VERIFYING", "IN_REVIEW"]
    assert len(attempts(path, case, "GST")) == 1

    payload["merchant"]["gst"] = payload["documents"]["tax"]["gst"] = "SYNGST-27-SYNPAN-0003-2"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 2)).status_code == 200
    [waiting] = active(path, case)
    assert (waiting["id"], waiting["owner"], waiting["subject_revision"]) == (issue["id"], "SYSTEM", 2)
    assert waiting["version"] == issue["version"] + 1
    assert state(path, case) == "VERIFYING"
    assert [(a["input_revision"], a["status"]) for a in attempts(path, case, "GST")] == [(1, "FAIL"), (2, "SCHEDULED")]
    assert len(attempts(path, case)) == 5  # PAN, BANK and RISK passes are kept
    stack.sync()
    stack.run_to(5)
    assert attempts(path, case, "GST")[-1]["status"] == "PASS"
    assert active(path, case) == [] and state(path, case) == "ACTIVE"
    assert query(path, "SELECT status FROM review_items WHERE id=?", issue["id"])[0]["status"] == "RESOLVED"


# ---------------------------------------------------------------- F02 · duplicate, second event, conflict

def test_duplicate_second_event_and_conflicting_reuse(browser, stack):
    path = stack.path
    case = started(browser, stack, scenario("duplicate-callback"))
    stack.run_to(2.0)
    assert [r["disposition"] for r in inbox(path, case)] == ["APPLIED"]
    before = entered(path, case)
    stack.run_to(3.75)
    pan = [d for d in deliveries(stack, "PAN")]
    assert [(d["kind"], d["last_status"], d["disposition"]) for d in pan] == [
        ("PRIMARY", 200, "APPLIED"), ("DUPLICATE", 200, "DUPLICATE"),
        ("SECOND_EVENT", 200, "IGNORED_SETTLED"), ("CONFLICT", 409, None)]
    rows = [r for r in inbox(path, case) if r["provider"] == "PAN"]
    assert [(r["disposition"], r["receipts"]) for r in rows] == [("APPLIED", 2), ("IGNORED_SETTLED", 1)]
    events = [e["event"] for e in query(path, "SELECT event FROM events WHERE case_id=?", case)]
    assert events.count("ATTEMPT_SETTLED") == 1 and events.count("CALLBACK_DUPLICATE") == 1
    assert events.count("CALLBACK_CONFLICT") == 1
    assert attempts(path, case, "PAN")[0]["status"] == "PASS"
    assert entered(path, case) == before


def test_concurrent_identical_deliveries_apply_once(browser, stack, payload):
    path = stack.path
    case = started(browser, stack, payload)
    primary = deliveries(stack, "PAN")[0]
    secrets = provider_secrets(path)
    stack.now = stack.origin + 2

    def deliver(_):
        return receive_callback(path, "PAN", primary["body"].encode(), primary["signature"], lambda: stack.now, secrets)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(body["disposition"] for status, body in pool.map(deliver, range(2)))
    assert results == ["APPLIED", "DUPLICATE"]
    assert [a["status"] for a in attempts(path, case, "PAN")] == ["PASS"]
    assert inbox(path, case)[0]["receipts"] == 2


# ---------------------------------------------------------------- F03 / I9 · deadlines

def test_late_callback_ignored_after_successor_decides(browser, stack):
    path = stack.path
    case = started(browser, stack, scenario("late-callback"))
    stack.run_to(9.75)
    assert attempts(path, case, "PAN")[0]["status"] == "SENT"
    stack.run_to(10)  # the timer expires generation 1 exactly at its deadline
    gen1, gen2 = attempts(path, case, "PAN")
    assert (gen1["status"], gen2["status"], gen2["generation"]) == ("EXPIRED", "SCHEDULED", 2)
    stack.run_to(11.75)
    assert attempts(path, case, "PAN")[1]["status"] == "SCHEDULED"  # 2 s backoff not yet elapsed
    stack.run_to(14)
    gen1, gen2 = attempts(path, case, "PAN")
    assert gen2["sent_at"] == pytest.approx(gen1["settled_at"] + 2) and gen2["status"] == "PASS"
    stack.run_to(15.5)  # generation 1's reply finally arrives
    pan = [r for r in inbox(path, case) if r["provider"] == "PAN"]
    assert [(r["attempt_id"], r["disposition"]) for r in pan] == [(gen2["id"], "APPLIED"), (gen1["id"], "IGNORED_DECIDED")]
    events = [e["event"] for e in query(path, "SELECT event FROM events WHERE case_id=?", case)]
    assert events.count("ATTEMPT_EXPIRED") == 1
    # F03 resolves in well under 30 s of demo time.
    assert gen2["settled_at"] - gen1["sent_at"] < 30


@pytest.mark.parametrize(("offset", "disposition", "status"), [(0.0, "LATE", "EXPIRED"), (-0.001, "APPLIED", "PASS")])
def test_reply_exactly_at_deadline_loses(browser, stack, offset, disposition, status):
    path = stack.path
    case = started(browser, stack, scenario("late-callback"))
    gen1 = attempts(path, case, "PAN")[0]
    primary = deliveries(stack, "PAN")[0]
    stack.now = gen1["deadline"] + offset  # the reply beats the timer to the database
    response = stack.post("/callbacks/PAN", primary["body"].encode(),
                          {"Content-Type": "application/json", "X-Signature": primary["signature"]})
    assert response.status_code == 200 and response.json()["disposition"] == disposition
    assert attempts(path, case, "PAN")[0]["status"] == status
    stack.now = gen1["deadline"]
    stack.tick()  # the timer runs afterwards and must not expire it again
    pan = attempts(path, case, "PAN")
    expiries = query(path, "SELECT detail FROM events WHERE case_id=? AND event='ATTEMPT_EXPIRED'", case)
    assert len([e for e in expiries if e["detail"].startswith("PAN")]) == (1 if status == "EXPIRED" else 0)
    assert len(pan) == (2 if status == "EXPIRED" else 1)


def test_worker_offline_sixty_seconds_consumes_no_attempts(browser, stack, payload):
    path = stack.path
    case = browser.create(payload)
    queued = query(path, "SELECT due_at FROM jobs ORDER BY id")[0]["due_at"]
    stack.now = queued + 60
    stack.tick()
    sent = attempts(path, case)
    assert [(a["generation"], a["status"]) for a in sent] == [(1, "SENT")] * 4
    assert all(a["sent_at"] == stack.now and a["deadline"] == stack.now + 10 for a in sent)
    details = [e["detail"] for e in query(path, "SELECT detail FROM events WHERE case_id=? AND event='REQUEST_SENT'", case)]
    assert all("dispatch delayed 60.0 s (no retry budget used)" in d for d in details)
    assert not query(path, "SELECT 1 FROM events WHERE event='ATTEMPT_EXPIRED'")


def test_callback_before_reference_write_is_not_regressed(browser, stack, payload):
    path = stack.path
    case = browser.create(payload)
    stack.sync()

    def racing(route, body, headers, timeout):
        response = stack.post(route, body, headers)
        if route == "/sim/PAN/requests":
            stack.now += 2.5   # the provider's reply arrives while the worker still holds the 202
            stack.deliver()
        return response

    worker.run_once(path, lambda: stack.now, racing)
    pan = attempts(path, case, "PAN")[0]
    assert pan["status"] == "PASS" and pan["reference"] == sim_requests(stack)[0]["reference"]
    assert query(path, "SELECT status FROM jobs WHERE attempt_id=?", pan["id"])[0]["status"] == "DONE"
    assert not query(path, "SELECT 1 FROM events WHERE event='PROVIDER_ANOMALY'")


# ---------------------------------------------------------------- retries, exhaustion and dead letters

def test_d01_missing_tax_then_transient_pan_recovers(browser, stack):
    path = stack.path
    payload = scenario("missing-tax")
    case = browser.create(payload)
    assert state(path, case) == "NEEDS_INFO" and attempts(path, case) == []
    payload["documents"]["tax"] = fixtures.documents(payload["merchant"])["tax"]
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    assert state(path, case) == "VERIFYING"
    stack.sync()
    stack.tick()
    stack.run_to(1)
    gen1, gen2 = attempts(path, case, "PAN")
    assert (gen1["status"], gen1["reason_code"], gen2["status"]) == ("TRANSIENT", "PROVIDER_TEMPORARILY_UNAVAILABLE", "SCHEDULED")
    stack.run_to(2.75)
    assert attempts(path, case, "PAN")[1]["status"] == "SCHEDULED"
    stack.run_to(8.5)
    gen1, gen2 = attempts(path, case, "PAN")
    assert gen2["sent_at"] == pytest.approx(gen1["settled_at"] + 2) and gen2["status"] == "PASS"
    assert [a["status"] for a in attempts(path, case) if a["kind"] != "PAN"] == ["PASS"] * 3
    assert entered(path, case) == ["NEEDS_INFO", "VERIFYING", "APPROVED_PENDING_MID", "ACTIVE"]
    assert active(path, case) == [] and state(path, case) == "ACTIVE"


def test_three_timeouts_exhaust_budget_with_backoff(browser, stack):
    path = stack.path
    case = started(browser, stack, scenario("exhausted"))
    stack.run_to(45)
    pan = attempts(path, case, "PAN")
    assert [(a["generation"], a["status"]) for a in pan] == [(1, "EXPIRED"), (2, "EXPIRED"), (3, "EXPIRED")]
    assert pan[1]["sent_at"] - pan[0]["settled_at"] == pytest.approx(2)
    assert pan[2]["sent_at"] - pan[1]["settled_at"] == pytest.approx(5)
    [issue] = active(path, case)
    assert (issue["cause"], issue["owner"], issue["subject"]) == ("CHECK_EXHAUSTED", "OPERATOR", "check.PAN")
    assert state(path, case) == "IN_REVIEW"
    stack.run_to(90)
    assert len(attempts(path, case, "PAN")) == 3


def test_transport_failures_dead_letter_then_deadline_retries(browser, stack, payload):
    path = stack.path
    case = browser.create(payload)
    stack.sync()

    def refused(route, body, headers, timeout):
        raise httpx.ConnectError("connection refused")

    for _ in range(3):
        worker.run_once(path, lambda: stack.now, refused)
        stack.now += 1
    jobs = query(path, "SELECT status,claims,last_reason FROM jobs ORDER BY id")
    assert [(j["status"], j["claims"]) for j in jobs] == [("DEAD", 3)] * 4
    assert all(a["status"] == "SENT" for a in attempts(path, case))
    browser.request("POST", "/access", json={"ticket": issue_ticket(path, "operator")})
    assert "4 dead-lettered dispatch job(s)" in browser.request("GET", "/desk").text
    stack.run_to(20.5)  # deadlines expire the never-delivered attempts; successors go through normally
    assert [(a["kind"], a["generation"], a["status"]) for a in attempts(path, case)][-4:] == [
        ("PAN", 2, "PASS"), ("GST", 2, "PASS"), ("BANK", 2, "PASS"), ("RISK", 2, "PASS")]


def test_crash_after_claim_expires_and_lease_is_reclaimed(browser, stack, payload):
    path = stack.path
    case = browser.create(payload)
    stack.sync()
    claim = claim_dispatch(path, stack.now)  # the worker dies before the HTTP request leaves
    assert claim["kind"] == "PAN" and attempts(path, case, "PAN")[0]["status"] == "SENT"
    stack.run_to(16)
    gen1, gen2 = attempts(path, case, "PAN")
    assert (gen1["status"], gen2["status"]) == ("EXPIRED", "PASS")
    job = query(path, "SELECT status,claims,last_reason FROM jobs WHERE attempt_id=?", gen1["id"])[0]
    assert (job["status"], job["claims"], job["last_reason"]) == ("CANCELLED", 1, "attempt no longer current")
    assert [r["attempt_id"] for r in sim_requests(stack) if r["provider"] == "PAN"] == [gen2["id"]]
    assert query(path, "SELECT count(*) AS n FROM events WHERE event='LEASE_RECLAIMED'")[0]["n"] == 1


# ---------------------------------------------------------------- B1 · holds pause successors

def add_hold(path, case, now):
    with db.write_tx(path) as conn:
        conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
            VALUES (?,'EVIDENCE_CONFLICT','bank.account',1,'OPERATOR','OPEN','Operator hold under test')""", (case,))
        reconcile(conn, case, now)


def release_hold(path, case, now):
    with db.write_tx(path) as conn:
        conn.execute("""UPDATE review_items SET status='RESOLVED',version=version+1
            WHERE case_id=? AND cause='EVIDENCE_CONFLICT'""", (case,))
        reconcile(conn, case, now)


def test_duplicate_hold_during_expiry_pauses_successor(browser, stack, payload):
    path = stack.path
    case = started(browser, stack, payload)
    add_hold(path, case, stack.now)
    assert state(path, case) == "IN_REVIEW"
    stack.now = stack.origin + 10
    stack.tick()  # no callbacks were delivered, so every SENT attempt expires under the hold
    assert {a["status"] for a in attempts(path, case)} == {"EXPIRED"}
    stack.now += 30
    stack.tick()
    assert len(attempts(path, case)) == 4 and state(path, case) == "IN_REVIEW"
    release_hold(path, case, stack.now)
    successors = attempts(path, case)[4:]
    assert [(a["generation"], a["status"]) for a in successors] == [(2, "SCHEDULED")] * 4
    assert state(path, case) == "VERIFYING"


def test_hold_parks_unsent_dispatch_without_budget(browser, stack, payload):
    path = stack.path
    case = browser.create(payload)
    stack.sync()
    add_hold(path, case, stack.now)
    assert {j["status"] for j in query(path, "SELECT status FROM jobs")} == {"HELD"}
    stack.tick()
    assert sim_requests(stack) == [] and {a["status"] for a in attempts(path, case)} == {"SCHEDULED"}
    release_hold(path, case, stack.now)
    stack.tick()
    assert [(a["generation"], a["status"]) for a in attempts(path, case)] == [(1, "SENT")] * 4


# ---------------------------------------------------------------- C05 · callback authentication

def test_rejected_callbacks_leave_business_state_intact(browser, stack, payload):
    path = stack.path
    case = started(browser, stack, payload)
    secrets = provider_secrets(path)
    primary = deliveries(stack, "PAN")[0]
    body = primary["body"].encode()
    gst_attempt = attempts(path, case, "GST")[0]
    snapshot = lambda: (state(path, case), len(query(path, "SELECT seq FROM events")), inbox(path, case),
                        [a["status"] for a in attempts(path, case)])
    before = snapshot()

    def send(provider, raw, signature):
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["X-Signature"] = signature
        return stack.post(f"/callbacks/{provider}", raw, headers).status_code

    message = json.loads(body)
    tampered = json.dumps({**message, "outcome": "FAIL"}).encode()
    unknown = json.dumps({**message, "attempt_id": uuid4().hex}).encode()
    cross = json.dumps({**message, "attempt_id": gst_attempt["id"]}).encode()
    wrong_endpoint = json.dumps({**message, "provider": "GST"}).encode()
    cases = [("PAN", body, None, 401), ("PAN", body, sign(secrets["GST"], body), 401),
             ("PAN", tampered, primary["signature"], 401), ("PAN", b"{}", sign(secrets["PAN"], b"{}"), 422),
             ("PAN", unknown, sign(secrets["PAN"], unknown), 404), ("PAN", cross, sign(secrets["PAN"], cross), 422),
             ("PAN", wrong_endpoint, sign(secrets["PAN"], wrong_endpoint), 422), ("ZZZ", body, primary["signature"], 404)]
    assert [send(p, raw, sig) for p, raw, sig, _ in cases] == [expected for *_, expected in cases]
    assert snapshot() == before
    counts = {(r["provider"], r["reason"]): r["count"] for r in query(path, "SELECT * FROM rejected_callbacks")}
    assert counts == {("PAN", "signature"): 3, ("PAN", "schema"): 1, ("PAN", "unknown attempt"): 1,
                      ("PAN", "provider mismatch"): 2}
    assert send("PAN", body, primary["signature"]) == 200
    assert attempts(path, case, "PAN")[0]["status"] == "PASS"


def test_provider_api_idempotency_and_client_authentication(stack):
    key = query(stack.path, "SELECT value FROM settings WHERE name='sim_api_key'")[0]["value"]
    auth = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    attempt = uuid4().hex
    body = json.dumps({"attempt_id": attempt, "inputs": {"name": "ACME", "pan": "SYNPAN-0001"}}).encode()
    first = stack.post("/sim/PAN/requests", body, auth)
    again = stack.post("/sim/PAN/requests", body, auth)
    assert (first.status_code, again.status_code) == (202, 200)
    assert first.json()["reference"] == again.json()["reference"] and again.json()["replayed"]
    changed = json.dumps({"attempt_id": attempt, "inputs": {"name": "ACME", "pan": "SYNPAN-0002"}}).encode()
    assert stack.post("/sim/PAN/requests", changed, auth).status_code == 409
    assert stack.post("/sim/PAN/requests", body, {"Content-Type": "application/json"}).status_code == 401
    assert stack.post("/sim/PAN/requests", body, {**auth, "Authorization": "Bearer wrong"}).status_code == 401
    assert stack.post("/sim/ABC/requests", body, auth).status_code == 404
    assert len(sim_requests(stack)) == 1 and len(deliveries(stack)) == 1


# ---------------------------------------------------------------- I7 · BANK registered beneficiary, RISK referral

def test_bank_registered_beneficiary_in_review_band(browser, stack):
    path = stack.path
    case = started(browser, stack, scenario("bank-name-review"))
    stack.run_to(8.5)
    bank = attempts(path, case, "BANK")[0]
    assert (bank["status"], bank["registered_name"], bank["name_score"]) == ("PASS", "ASTER VALLEY STORES", 70.4)
    [issue] = active(path, case)
    assert (issue["cause"], issue["owner"]) == ("BENEFICIARY_REVIEW", "OPERATOR")
    assert issue["reason"] == "check.BANK · registered beneficiary review · score 70.4"
    assert state(path, case) == "IN_REVIEW"


def test_risk_referral_holds_for_operator(browser, stack):
    path = stack.path
    case = started(browser, stack, scenario("risk-referral"))
    stack.run_to(8.5)
    assert attempts(path, case, "RISK")[0]["status"] == "REFERRED"
    [issue] = active(path, case)
    assert (issue["cause"], issue["owner"], issue["reason"]) == (
        "CHECK_REFERRED", "OPERATOR", "check.RISK · referred for review · HIGH_RISK_ZONE")


# ---------------------------------------------------------------- F04 subset · edits supersede authority

@pytest.mark.parametrize("edit_at", [5.0, 6.25])
def test_bank_edit_supersedes_old_authority(browser, stack, edit_at):
    path = stack.path
    payload = scenario("bank-edit")
    case = started(browser, stack, payload)
    stack.run_to(edit_at)
    old = attempts(path, case, "BANK")[0]
    assert old["status"] == ("SENT" if edit_at < 6 else "PASS")
    payload["merchant"]["account"] = payload["documents"]["bank"]["account"] = "SYNACCT-0016"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    bank = attempts(path, case, "BANK")
    assert [(a["input_revision"], a["status"]) for a in bank] == [
        (1, "SUPERSEDED" if edit_at < 6 else "PASS"), (2, "SCHEDULED")]
    simulator = stack.app.state.simulator
    if edit_at < 6:
        stack.run_to(6.25)  # the in-flight reply for the old account lands after the edit
        assert inbox(path, case)[-1]["disposition"] == "IGNORED_SUPERSEDED"
    else:
        event_id = next(r["event_id"] for r in inbox(path, case) if r["provider"] == "BANK")
        simulator.replay(event_id, "identical", stack.now)
        simulator.replay(event_id, "new-event", stack.now)
        stack.deliver()
        replays = [r for r in inbox(path, case) if r["attempt_id"] == old["id"]]
        assert [(r["disposition"], r["receipts"]) for r in replays] == [("APPLIED", 2), ("IGNORED_SUPERSEDED", 1)]
    gst = attempts(path, case, "GST")[0]
    assert gst["status"] == "SENT"
    assert simulator.release(gst["id"], stack.now) == 1  # operator releases the held GST reply
    stack.run_to(14)
    bank = attempts(path, case, "BANK")
    assert (bank[-1]["input_revision"], bank[-1]["status"], bank[-1]["name_score"]) == (2, "PASS", 100.0)
    assert attempts(path, case, "GST")[0]["status"] == "PASS"
    assert active(path, case) == [] and state(path, case) == "ACTIVE"
    receipt = json.loads(query(path, "SELECT receipt FROM approvals WHERE case_id=?", case)[0]["receipt"])
    winner = next(w for w in receipt["checks"] if w["kind"] == "BANK")
    assert winner["input_revision"] == 2 and winner["attempt_id"] == bank[-1]["id"]


def test_demo_controls_require_operator_flag_and_same_origin(tmp_path, payload):
    from app.web import create_app
    app = create_app(tmp_path / "controls", demo_controls=True)
    merchant, operator = Browser(app), Browser(app)
    try:
        merchant.create(payload)
        body = {"attempt_id": uuid4().hex}
        assert merchant.request("POST", "/sim/control/release", json=body).status_code == 403
        operator.request("POST", "/access", json={"ticket": issue_ticket(app.state.database, "operator")})
        assert operator.request("POST", "/sim/control/release", json=body).json() == {"released": 0}
        assert operator.request("POST", "/sim/control/release", json=body,
                                headers={"Origin": "https://example.invalid"}).status_code == 403
    finally:
        merchant.close()
        operator.close()
    plain = create_app(tmp_path / "plain")
    client = Browser(plain)
    try:
        client.request("POST", "/access", json={"ticket": issue_ticket(plain.state.database, "operator")})
        assert client.request("POST", "/sim/control/release", json={"attempt_id": uuid4().hex}).status_code == 404
    finally:
        client.close()


# ---------------------------------------------------------------- I8 · every scenario in one run

def test_seeded_scenarios_share_one_desk(app, browser, stack):
    path = stack.path
    seeded = {s["slug"]: case for s, case, _ in seed.seed(path.parent)}
    assert {s["slug"]: case for s, case, created in seed.seed(path.parent)} == seeded  # idempotent
    stack.sync()
    stack.run_to(9)
    midway = {slug: state(path, case) for slug, case in seeded.items()}
    assert {"NEEDS_INFO", "IN_REVIEW", "VERIFYING"} <= set(midway.values())
    assert midway["late-callback"] == "VERIFYING" and midway["gst-fail"] == "IN_REVIEW"
    stack.run_to(45)
    outcome = {slug: (state(path, case), sorted(i["cause"] for i in active(path, case))) for slug, case in seeded.items()}
    assert outcome == {
        "happy": ("ACTIVE", []),
        "missing-tax": ("NEEDS_INFO", ["EVIDENCE_MISSING"]),
        "gst-fail": ("IN_REVIEW", ["CHECK_FAILED"]),
        "duplicate-callback": ("ACTIVE", []),
        "late-callback": ("ACTIVE", []),
        "bank-edit": ("IN_REVIEW", ["CHECK_EXHAUSTED"]),
        "exhausted": ("IN_REVIEW", ["CHECK_EXHAUSTED"]),
        "risk-referral": ("IN_REVIEW", ["CHECK_REFERRED"]),
        "bank-name-review": ("IN_REVIEW", ["BENEFICIARY_REVIEW"]),
        "dup-pan": ("IN_REVIEW", ["DUPLICATE_CANDIDATE"]),
        "dup-name-zone": ("IN_REVIEW", ["DUPLICATE_CANDIDATE"]),
        "mid-before-commit": ("ACTIVE", []),
        "mid-lost-ack": ("ACTIVE", []),
        "mid-dead-letter": ("PROVISIONING_FAILED", []),
        "document-conflict": ("IN_REVIEW", ["EVIDENCE_CONFLICT"]),
    }
    browser.request("POST", "/access", json={"ticket": issue_ticket(path, "operator")})
    desk = browser.request("GET", "/desk").text
    assert all(s["label"] in desk for s in fixtures.SCENARIOS)
    assert attempts(path, seeded["document-conflict"]) == []
