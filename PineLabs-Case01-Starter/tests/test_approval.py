"""M3 policy, exact evidence, human judgments, upgrade and boundary interleavings."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest

from app import approval, db
from app.access import issue_ticket
from app.extraction import Amendment, Submission
from app.review import REASONS
from app.verification import claim_dispatch, complete_dispatch, provider_secrets, receive_callback, send_request
from app.web import create_app
from app.workflow import Problem, amend, reconcile, submit
from conftest import correction
from test_verification import active, add_hold, attempts, deliveries, entered, query, scenario, started, state


def login(browser, path):
    assert browser.request("POST", "/access", json={"ticket": issue_ticket(path, "operator")}).status_code == 303


def decision(browser, case, issue, action, **overrides):
    return browser.request("POST", f"/api/cases/{case}/issues/{issue['id']}/decisions",
                           json={"action": action, "version": issue["version"], "reason": REASONS[action][0], **overrides})


def advance_callbacks(stack, offset=8):
    stack.now = stack.origin + offset
    stack.deliver()


def sealed(path, case):
    rows = query(path, "SELECT * FROM approvals WHERE case_id=?", case)
    return json.loads(rows[0]["receipt"]) if rows else None


def test_gate_writes_exact_immutable_evidence_and_one_job(browser, stack, payload):
    case = started(browser, stack, payload)
    advance_callbacks(stack, 6)
    assert state(stack.path, case) == "VERIFYING" and sealed(stack.path, case) is None
    advance_callbacks(stack)
    assert state(stack.path, case) == "APPROVED_PENDING_MID"
    receipt = sealed(stack.path, case)
    assert receipt["policy"] == db.policy() and receipt["case_id"] == case
    assert receipt["evidence"]["merchant"] == payload["merchant"]
    for kind, document in payload["documents"].items():
        assert receipt["evidence"]["documents"][kind] == {"revision": 1, "digest": db.digest(db.packed(document))}
    assert receipt["evidence"]["assessment_digest"] == db.digest(db.packed(receipt["evidence"]["fields"]))
    actual = attempts(stack.path, case)
    assert [w["attempt_id"] for w in receipt["checks"]] == [a["id"] for a in actual]
    assert all(w["generation"] == w["input_revision"] == 1 for w in receipt["checks"])
    jobs = query(stack.path, "SELECT operation_key,status FROM jobs WHERE case_id=?", case)
    assert jobs == [{"operation_key": "mid/" + case, "status": "READY"}]
    with db.write_tx(stack.path) as conn:
        for sql in ("UPDATE approvals SET receipt='{}'", "DELETE FROM approvals", "INSERT OR REPLACE INTO approvals SELECT * FROM approvals"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(sql)
        reconcile(conn, case, stack.now)
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 409
    assert sealed(stack.path, case) == receipt
    stack.tick()
    assert state(stack.path, case) == "ACTIVE"


def test_gate_fails_closed_for_missing_invalid_stale_or_unproven_inputs(browser, stack, payload):
    case = started(browser, stack, payload)
    advance_callbacks(stack)
    with db.write_tx(stack.path) as conn:
        def gate():
            return approval.build_receipt(conn, conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone())
        assert gate()
        app_row = dict(conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone())
        for kinds in ([], ["PAN"], ["PAN", "PAN"], ["PAN", "OTHER"], ["GST", "BANK"]):
            bad = {**db.policy(), "required": kinds, "referral_kinds": []}
            assert approval.build_receipt(conn, {**app_row, "policy": db.packed(bad)}) is None
        with pytest.raises(sqlite3.IntegrityError, match="immutable policy"):
            conn.execute("UPDATE applications SET policy='{}'")
        for statement in (
            "UPDATE checks SET input_revision=input_revision+1 WHERE kind='BANK'",
            "UPDATE attempts SET inputs='{}' WHERE kind='GST'",
            "UPDATE attempts SET status='EXPIRED' WHERE kind='GST'",
            "UPDATE attempts SET status='FAIL' WHERE kind='GST'",
            "UPDATE attempts SET status='SUPERSEDED' WHERE kind='GST'",
            "UPDATE attempts SET status='SENT' WHERE kind='GST'",
            "UPDATE attempts SET status='SCHEDULED' WHERE kind='GST'",
            "UPDATE attempts SET deadline=settled_at WHERE kind='GST'",
            "UPDATE attempts SET name_score=60 WHERE kind='BANK'",
            "DELETE FROM callback_inbox WHERE provider='PAN'",
            "UPDATE assessments SET results='[]'",
            "UPDATE documents SET content_digest='old' WHERE kind='tax'",
            "UPDATE applications SET data_revision=data_revision+1",
            "INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason) "
            "SELECT id,'PENDING','check.GST',1,'SYSTEM','OPEN','Recheck' FROM applications",
            "INSERT INTO attempts(id,case_id,kind,input_revision,generation,status,inputs) "
            "SELECT 'newest',case_id,kind,input_revision,generation+1,'SENT',inputs FROM attempts WHERE kind='PAN'",
        ):
            conn.execute("SAVEPOINT unsafe_gate")
            conn.execute(statement)
            assert gate() is None, statement
            conn.execute("ROLLBACK TO unsafe_gate")
            conn.execute("RELEASE unsafe_gate")
        # Remove the mandatory check and all of its supporting rows, keeping the snapshot intact.
        conn.execute("SAVEPOINT missing_check")
        for table, field in (("callback_inbox", "provider"), ("jobs", "kind"), ("attempts", "kind"), ("checks", "kind")):
            if table == "jobs":
                conn.execute("DELETE FROM jobs WHERE attempt_id IN (SELECT id FROM attempts WHERE kind='PAN')")
            else:
                conn.execute(f"DELETE FROM {table} WHERE {field}='PAN'")
        assert gate() is None
        conn.execute("ROLLBACK TO missing_check")
        conn.execute("RELEASE missing_check")


def test_existing_policy_snapshot_survives_configuration_change(browser, stack, payload, monkeypatch):
    original = started(browser, stack, payload)
    subset = {"version": 2, "required": ["PAN", "GST"], "referral_kinds": []}
    monkeypatch.setattr(db, "policy", lambda: subset)
    new = create_app(stack.path.parent)
    from conftest import Browser
    client = Browser(new)
    try:
        second = client.create(scenario("risk-referral"))  # a distinct identity: identical ones are held as duplicates
    finally:
        client.close()
    stack.sync()
    stack.tick()
    advance_callbacks(stack, 4)
    assert sealed(stack.path, original) is None
    assert sealed(stack.path, second)["policy"] == subset
    advance_callbacks(stack)
    assert len(sealed(stack.path, original)["checks"]) == 4


def test_role_scope_rejection_and_unrelated_callback(browser, stack):
    case = started(browser, stack, scenario("gst-fail"))
    advance_callbacks(stack, 4)
    issue = active(stack.path, case)[0]
    assert decision(browser, case, issue, "REJECT").status_code == 403
    login(browser, stack.path)
    page = browser.request("GET", "/cases/" + case).text
    assert "Request correction" in page and "Accept referral" not in page
    assert decision(browser, case, issue, "ACCEPT_REFERRAL").status_code == 409
    for suffix in ("approve", "checks/GST/pass", "issuance/issue"):
        assert browser.request("POST", f"/api/cases/{case}/{suffix}", json={}).status_code == 404
    assert decision(browser, "0" * 32, issue, "REJECT").status_code == 404
    url = f"/api/cases/{case}/issues/{issue['id']}/decisions"
    body = {"action": "REJECT", "version": issue["version"], "reason": REASONS["REJECT"][0]}
    assert browser.request("POST", url, json=body, headers={"Origin":"https://example.invalid"}).status_code == 403
    assert browser.request("POST", url, json={**body, "reason": "arbitrary input"}).status_code == 422
    advance_callbacks(stack, 6)  # BANK pass must not stale the reviewer's issue version.
    assert decision(browser, case, issue, "REJECT").status_code == 200
    advance_callbacks(stack)
    assert state(stack.path, case) == "REJECTED" and sealed(stack.path, case) is None
    assert not query(stack.path, "SELECT * FROM merchants") and stack.app.state.simulator.issuance_count(case) == 0
    assert query(stack.path, "SELECT disposition FROM callback_inbox WHERE provider='RISK'")[0]["disposition"] == "IGNORED_DECIDED"
    assert decision(browser, case, issue, "REJECT").status_code == 409


def test_request_correction_then_stale_decision_and_gst_recheck(browser, stack):
    payload = scenario("gst-fail")
    case = started(browser, stack, payload)
    advance_callbacks(stack)
    login(browser, stack.path)
    issue = active(stack.path, case)[0]
    assert decision(browser, case, issue, "REQUEST_CORRECTION").status_code == 200
    assert state(stack.path, case) == "NEEDS_INFO"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    refreshed = active(stack.path, case)[0]
    assert refreshed["id"] == issue["id"] and refreshed["owner"] == "OPERATOR" and "unchanged" in refreshed["reason"]
    payload["merchant"]["gst"] = payload["documents"]["tax"]["gst"] = "SYNGST-27-SYNPAN-0003-2"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 2)).status_code == 200
    stale = decision(browser, case, refreshed, "REJECT")
    assert stale.status_code == 409 and stale.json()["refresh"] == "/cases/" + case
    assert not sealed(stack.path, case)
    stack.tick()
    stack.now += 4
    stack.deliver()
    assert next(w["input_revision"] for w in sealed(stack.path, case)["checks"] if w["kind"] == "GST") == 2


def test_high_risk_referral_acceptance_bound_to_current_attempt(browser, stack):
    case = started(browser, stack, scenario("risk-referral"))
    advance_callbacks(stack)
    login(browser, stack.path)
    [issue] = active(stack.path, case)
    assert sealed(stack.path, case) is None
    assert decision(browser, case, issue, "ACCEPT_REFERRAL", reason="").status_code == 422
    assert decision(browser, case, issue, "ACCEPT_REFERRAL", version=issue["version"] + 1).status_code == 409
    assert decision(browser, case, issue, "ACCEPT_REFERRAL").status_code == 200
    stack.tick()
    r = next(w for w in sealed(stack.path, case)["checks"] if w["kind"] == "RISK")
    assert r["status"] == "REFERRED" and r["attempt_id"] == attempts(stack.path, case, "RISK")[0]["id"]
    assert (r["acceptance"]["actor"], r["acceptance"]["reason"], r["acceptance"]["issue_version"]) == (
        "operator", REASONS["ACCEPT_REFERRAL"][0], issue["version"])
    assert state(stack.path, case) == "ACTIVE" and stack.app.state.simulator.issuance_count(case) == 1
    assert decision(browser, case, issue, "ACCEPT_REFERRAL").status_code == 409
    page = browser.request("GET", "/cases/" + case).text
    assert "Accepted by operator" in page and "SYNMID-" in page


@pytest.mark.parametrize("accepted_first", [False, True])
def test_changed_business_profile_after_referral_needs_separate_clearance(browser, stack, accepted_first):
    payload = scenario("risk-referral")
    case = started(browser, stack, payload)
    if accepted_first:
        # Keep the bank reply in flight until after the initial referral has been accepted.
        bank_delivery = deliveries(stack, "BANK")[0]
        with db.write_tx(stack.app.state.simulator.path) as conn:
            conn.execute("UPDATE sim_deliveries SET status='HELD' WHERE id=?", (bank_delivery["id"],))
    advance_callbacks(stack)
    login(browser, stack.path)
    [original] = active(stack.path, case)
    if accepted_first:
        assert decision(browser, case, original, "ACCEPT_REFERRAL").status_code == 200
        stack.app.state.simulator.release(bank_delivery["attempt_id"], stack.now)
        assert state(stack.path, case) == "VERIFYING"
    payload["merchant"].update(category="RETAIL", zone="SYNZONE-01")
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    hold = next(i for i in active(stack.path, case) if i["cause"] == "RISK_INPUTS_CHANGED_AFTER_REFERRAL")
    assert hold["owner"] == "OPERATOR" and hold["subject_revision"] == 2
    assert decision(browser, case, original, "ACCEPT_REFERRAL").status_code == 409
    assert decision(browser, case, hold, "CLEAR_RISK_CHANGE").status_code == 409
    stack.deliver()
    stack.tick()
    stack.now += 8
    stack.deliver()
    [hold] = active(stack.path, case)
    assert attempts(stack.path, case, "RISK")[-1]["status"] == "PASS"
    assert state(stack.path, case) == "IN_REVIEW" and sealed(stack.path, case) is None
    assert decision(browser, case, hold, "CLEAR_RISK_CHANGE").status_code == 200
    assert (sealed(stack.path, case)["risk_clearances"][0]["attempt_id"]
            == attempts(stack.path, case, "RISK")[-1]["id"])
    stack.tick()
    assert state(stack.path, case) == "ACTIVE"


def test_policy_can_forbid_even_risk_referral_acceptance(browser, stack):
    payload = scenario("risk-referral")
    case, *_ = submit(stack.path, Submission(**payload), "fixture", {**db.policy(), "referral_kinds": []})
    stack.sync()
    stack.tick()
    advance_callbacks(stack)
    login(browser, stack.path)
    [issue] = active(stack.path, case)
    assert "Accept referral" not in browser.request("GET", "/cases/" + case).text
    assert decision(browser, case, issue, "ACCEPT_REFERRAL").status_code == 409
    assert not sealed(stack.path, case)


def test_conflicting_document_replacement_and_retained_passes(browser, stack, payload):
    case = started(browser, stack, payload)
    advance_callbacks(stack, 6)
    # Merchant removes the bank evidence while the last provider is outstanding.
    bank = payload["documents"].pop("bank")
    def edit(revision):
        assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, revision)).status_code == 200
    edit(1)
    advance_callbacks(stack)
    assert state(stack.path, case) == "NEEDS_INFO" and not sealed(stack.path, case)
    payload["documents"]["bank"] = {**bank, "account": "SYNACCT-0002"}
    edit(2)
    assert state(stack.path, case) == "IN_REVIEW" and len(attempts(stack.path, case)) == 4
    login(browser, stack.path)
    [issue] = active(stack.path, case)
    assert decision(browser, case, issue, "REQUEST_CORRECTION").status_code == 200
    edit(3)  # same conflicting replacement returns to the same operator issue
    [again] = active(stack.path, case)
    assert again["id"] == issue["id"] and again["owner"] == "OPERATOR"
    assert decision(browser, case, again, "REQUEST_CORRECTION").status_code == 200
    payload["documents"]["bank"] = bank
    edit(4)
    receipt = sealed(stack.path, case)
    assert receipt["evidence"]["data_revision"] == 5
    assert receipt["evidence"]["documents"]["bank"]["revision"] == 4 and len(attempts(stack.path, case)) == 4
    assert entered(stack.path, case)[-2:] == ["NEEDS_INFO", "APPROVED_PENDING_MID"]


@pytest.mark.parametrize("recovery_pass", [True, False])
def test_authorized_recovery_is_once_per_revision(browser, stack, recovery_pass):
    case = started(browser, stack, scenario("exhausted"))
    advance_callbacks(stack)
    for gap in (2, 5, None):
        stack.now = attempts(stack.path, case, "PAN")[-1]["deadline"]
        stack.tick()
        if gap:
            stack.now += gap
            stack.tick()
    [issue] = active(stack.path, case)
    login(browser, stack.path)
    assert decision(browser, case, issue, "AUTHORIZE_RECOVERY").status_code == 200
    assert decision(browser, case, issue, "AUTHORIZE_RECOVERY").status_code == 200
    assert len(attempts(stack.path, case, "PAN")) == 4 and active(stack.path, case)[0]["owner"] == "SYSTEM"
    stack.tick()
    last = attempts(stack.path, case, "PAN")[-1]
    if recovery_pass:
        # The normally silent provider resumes and delivers a valid result for the authorized request.
        msg = dict(event_id=uuid4().hex, attempt_id=last["id"], provider="PAN", reference=last["reference"],
                   outcome="PASS", reason_code="VERIFIED", issued_at=stack.now)
        from app.verification import sign
        raw = db.packed(msg).encode()
        res = stack.post("/callbacks/PAN", raw, {"Content-Type":"application/json",
                         "X-Signature": sign(provider_secrets(stack.path)["PAN"], raw)})
        assert res.json()["disposition"] == "APPLIED"
        assert next(w for w in sealed(stack.path, case)["checks"] if w["kind"] == "PAN")["generation"] == 4
    else:
        stack.now = last["deadline"]
        stack.tick()
        [failed] = active(stack.path, case)
        assert failed["id"] == issue["id"] and "recovery failed" in failed["reason"]
        page = browser.request("GET", "/cases/" + case).text
        assert "Authorize one recovery" not in page and "Reject application" in page
        assert decision(browser, case, failed, "AUTHORIZE_RECOVERY").status_code == 409
        assert len(attempts(stack.path, case, "PAN")) == 4 and not sealed(stack.path, case)



def test_m2_upgrade_preserves_dispatch_leases_and_reconciles_only_handoff(browser, stack, payload):
    ready = started(browser, stack, payload)
    # Reproduce the M2 storage boundary through real callbacks behind its historic diagnostic.
    add_hold(stack.path, ready, stack.now)
    advance_callbacks(stack)
    pending = browser.create(scenario("late-callback"))
    genuine = browser.create(scenario("gst-fail"))
    with db.write_tx(stack.path) as conn:
        conn.execute("""UPDATE review_items SET cause='STALLED',subject='workflow',reason=?
            WHERE case_id=?""", ("All configured checks passed · approval gate is delivered in M3", ready))
        conn.execute("UPDATE jobs SET status='CANCELLED' WHERE attempt_id IN (SELECT id FROM attempts WHERE case_id=?)",
                     (genuine,))
        reconcile(conn, genuine, stack.now)
        conn.execute("""UPDATE jobs SET status='LEASED',lease_token='migration-probe',claims=2,lease_expires=?
            WHERE operation_key=(SELECT 'dispatch/'||id FROM attempts WHERE case_id=? LIMIT 1)""",
                     (stack.now + 15, pending))
        conn.execute("DELETE FROM settings WHERE name='m3_reconciled'")
        # The exact M2 jobs layout: dispatch attempt is compulsory; no case_id or requeue cycle.
        conn.execute("ALTER TABLE jobs RENAME TO new_jobs")
        conn.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY,operation_key TEXT UNIQUE NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES attempts,kind TEXT NOT NULL,status TEXT NOT NULL,due_at REAL NOT NULL,
            lease_token TEXT,lease_expires REAL,claims INTEGER NOT NULL DEFAULT 0,last_reason TEXT)""")
        cols = "id,operation_key,attempt_id,kind,status,due_at,lease_token,lease_expires,claims,last_reason"
        conn.execute(f"INSERT INTO jobs SELECT {cols} FROM new_jobs")
        conn.execute("DROP TABLE new_jobs")
        before = [tuple(r) for r in conn.execute(f"SELECT {cols} FROM jobs ORDER BY id")]
    for _ in range(2):
        create_app(stack.path.parent)
    with closing(db.connect(stack.path)) as conn:
        assert [tuple(r) for r in conn.execute(f"SELECT {cols} FROM jobs WHERE kind='DISPATCH' ORDER BY id")] == before
    assert sealed(stack.path, ready) and state(stack.path, ready) == "APPROVED_PENDING_MID"
    assert active(stack.path, genuine)[0]["cause"] == "STALLED" and not sealed(stack.path, genuine)
    assert state(stack.path, pending) == "VERIFYING"
    assert len(query(stack.path, "SELECT id FROM jobs WHERE case_id=?", ready)) == 1


def test_final_callbacks_racing_create_one_approval(browser, stack, payload):
    case = started(browser, stack, payload)
    advance_callbacks(stack, 4)
    stack.now = stack.origin + 8
    barrier = Barrier(2)
    keys = provider_secrets(stack.path)
    def deliver(provider):
        msg = deliveries(stack, provider)[0]
        barrier.wait(timeout=10)
        return receive_callback(stack.path, provider, msg["body"].encode(), msg["signature"], lambda: stack.now, keys)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert [r[1]["disposition"] for r in pool.map(deliver, ("BANK","RISK"))] == ["APPLIED","APPLIED"]
    assert len(sealed(stack.path, case)["checks"]) == 4
    assert entered(stack.path, case).count("APPROVED_PENDING_MID") == 1
    assert len(query(stack.path, "SELECT id FROM jobs WHERE case_id=?", case)) == 1


def test_final_callback_and_bank_edit_race_fifty_times(browser, stack, payload, tmp_path):
    """Start every race from the same genuine in-flight request and independently recover the winning branch."""
    case = started(browser, stack, payload)
    advance_callbacks(stack, 6)
    last = deliveries(stack, "RISK")[0]
    keys = provider_secrets(stack.path)
    stack.now = stack.origin + 8
    amended = deepcopy(payload)
    amended["documents"]["bank"]["routing"] = amended["merchant"]["routing"] = "SYNIFSC-0002"
    edit = Amendment(**correction(amended, 1))
    with closing(db.connect(stack.path)) as snapshot, ThreadPoolExecutor(max_workers=2) as pool:
        for n in range(50):
            path = tmp_path / f"race-{n}.db"
            with closing(db.connect(path)) as target:
                snapshot.backup(target)
            barrier = Barrier(2)
            def final_reply():
                barrier.wait(timeout=10)
                return receive_callback(path, "RISK", last["body"].encode(), last["signature"], lambda: stack.now, keys)
            def change_bank():
                barrier.wait(timeout=10)
                try:
                    amend(path, case, edit, now=stack.now)
                    return 200
                except Problem as exc:
                    return exc.status
            first, second = pool.submit(final_reply), pool.submit(change_bank)
            assert first.result()[1]["disposition"] == "APPLIED"
            status = second.result()
            assert status in (200, 409)
            winning_revision = 1
            if status == 200:
                assert sealed(path, case) is None
                claim = claim_dispatch(path, stack.now)
                assert claim["kind"] == "BANK"
                complete_dispatch(path, claim, send_request(claim, stack.post), stack.now)
                msg = next(r for r in deliveries(stack, "BANK") if r["attempt_id"] == claim["attempt_id"])
                assert receive_callback(path, "BANK", msg["body"].encode(), msg["signature"],
                                        lambda: stack.now + 6, keys)[1]["disposition"] == "APPLIED"
                winning_revision = 2
            receipt = sealed(path, case)
            assert receipt["evidence"]["data_revision"] == winning_revision
            assert next(w["input_revision"] for w in receipt["checks"] if w["kind"] == "BANK") == winning_revision
            assert len(query(path, "SELECT id FROM jobs WHERE case_id=?", case)) == 1


def test_issue_dismissal_scoped_to_subject_and_unrelated_callbacks(browser, stack):
    first = started(browser, stack, scenario("happy"))
    case = browser.create(scenario("dup-name-zone"))  # a real, dismissible name-and-zone collision with the first case
    [issue] = active(stack.path, case)
    assert issue["cause"] == "DUPLICATE_CANDIDATE"
    advance_callbacks(stack)  # unrelated callbacks for the first case commit; the reviewer's form is still current
    login(browser, stack.path)
    assert decision(browser, case, issue, "DISMISS_DUPLICATE").status_code == 200
    assert decision(browser, case, issue, "DISMISS_DUPLICATE").status_code == 409
    assert sealed(stack.path, first)
    row = query(stack.path, "SELECT * FROM decisions WHERE issue_id=?", issue["id"])[0]
    assert row["subject_revision"] == issue["subject_revision"] and row["case_id"] == case



def test_approval_and_dispatch_rollback_as_one_with_final_callback(browser, stack, payload):
    case = started(browser, stack, payload)
    advance_callbacks(stack, 6)
    with db.write_tx(stack.path) as conn:
        conn.execute("""CREATE TRIGGER interrupt_issuance BEFORE INSERT ON jobs WHEN NEW.kind='ISSUE_MID'
                     BEGIN SELECT RAISE(ABORT,'job unavailable'); END""")
    msg = deliveries(stack, "RISK")[0]
    keys = provider_secrets(stack.path)
    def callback():
        return receive_callback(stack.path, "RISK", msg["body"].encode(), msg["signature"], lambda: stack.origin + 8, keys)
    with pytest.raises(sqlite3.IntegrityError, match="job unavailable"):
        callback()
    assert not sealed(stack.path, case) and state(stack.path, case) == "VERIFYING"
    assert attempts(stack.path, case, "RISK")[0]["status"] == "SENT"
    assert not query(stack.path, "SELECT * FROM callback_inbox WHERE provider='RISK'")
    with db.write_tx(stack.path) as conn:
        conn.execute("DROP TRIGGER interrupt_issuance")
    assert callback()[1]["disposition"] == "APPLIED" and sealed(stack.path, case)


def test_another_referral_for_changed_inputs_requires_both_decisions(browser, stack):
    payload = scenario("risk-referral")
    case = started(browser, stack, payload)
    advance_callbacks(stack)
    login(browser, stack.path)
    payload["merchant"]["category"] = "RETAIL"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.tick()
    stack.now += 8
    stack.deliver()
    issues = {i["cause"]: i for i in active(stack.path, case)}
    assert decision(browser, case, issues["RISK_INPUTS_CHANGED_AFTER_REFERRAL"], "CLEAR_RISK_CHANGE").status_code == 409
    assert decision(browser, case, issues["CHECK_REFERRED"], "ACCEPT_REFERRAL").status_code == 200
    assert sealed(stack.path, case) is None
    assert decision(browser, case, issues["RISK_INPUTS_CHANGED_AFTER_REFERRAL"], "CLEAR_RISK_CHANGE").status_code == 200
    receipt = sealed(stack.path, case)
    risk = next(w for w in receipt["checks"] if w["kind"] == "RISK")
    assert risk["input_revision"] == 2 and risk["attempt_id"] == receipt["risk_clearances"][0]["attempt_id"]


def test_definitive_risk_fail_still_needs_new_revision_pass(browser, stack):
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    case = started(browser, stack, payload)
    advance_callbacks(stack)
    login(browser, stack.path)
    [failed] = active(stack.path, case)
    assert failed["cause"] == "CHECK_FAILED"
    assert decision(browser, case, failed, "ACCEPT_REFERRAL").status_code == 409
    payload["merchant"]["category"] = "SERVICES"
    url = "/api/cases/" + case
    assert browser.request("PATCH", url, json=correction(payload, 1)).status_code == 200
    stack.tick()
    stack.now += 8
    stack.deliver()
    issues = {i["cause"]: i for i in active(stack.path, case)}
    # M3 review B1: the change after a definitive RISK failure is itself held for an operator.
    assert {"CHECK_FAILED", "CHECK_REFERRED", "RISK_INPUTS_CHANGED_AFTER_REFERRAL"} == set(issues)
    assert decision(browser, case, issues["CHECK_REFERRED"], "ACCEPT_REFERRAL").status_code == 200
    assert sealed(stack.path, case) is None and state(stack.path, case) == "IN_REVIEW"
    payload["merchant"]["zone"] = "SYNZONE-01"
    assert browser.request("PATCH", url, json=correction(payload, 2)).status_code == 200
    stack.tick()
    stack.now += 8
    stack.deliver()
    [guard] = active(stack.path, case)
    assert guard["cause"] == "RISK_INPUTS_CHANGED_AFTER_REFERRAL"
    assert decision(browser, case, guard, "CLEAR_RISK_CHANGE").status_code == 200
    assert sealed(stack.path, case)



def test_exhausted_recheck_of_definitive_failure_can_actually_receive_its_one_recovery(browser, stack, monkeypatch):
    from app.fixtures import REGISTRY
    payload = scenario("gst-fail")
    case = started(browser, stack, payload)
    advance_callbacks(stack)
    login(browser, stack.path)
    corrected = "SYNGST-27-SYNPAN-0003-2"
    silent = dict(outcome="PASS", reason="VERIFIED", delay=1, faults=["SILENT"])
    monkeypatch.setitem(REGISTRY["GST"], corrected, [silent] * 3 + [dict(outcome="PASS", reason="VERIFIED", delay=1)])
    payload["documents"]["tax"]["gst"] = payload["merchant"]["gst"] = corrected
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.tick()
    for gap in (2, 5, None):
        stack.now = attempts(stack.path, case, "GST")[-1]["deadline"]
        stack.tick()
        if gap:
            stack.now += gap
            stack.tick()
    issues = {i["cause"]: i for i in active(stack.path, case)}
    assert issues["CHECK_FAILED"]["owner"] == "SYSTEM"
    assert decision(browser, case, issues["CHECK_EXHAUSTED"], "AUTHORIZE_RECOVERY").status_code == 200
    stack.tick()
    stack.now += 1
    stack.deliver()
    assert not active(stack.path, case)
    winner = next(w for w in sealed(stack.path, case)["checks"] if w["kind"] == "GST")
    assert (winner["input_revision"], winner["generation"], winner["status"]) == (2,4,"PASS")



def test_operator_repairs_only_unsent_work_and_keeps_attempt_ids(browser, stack, payload):
    case = browser.create(payload)
    stack.sync()
    path = stack.path
    with db.write_tx(path) as conn:
        conn.execute("UPDATE jobs SET status='CANCELLED' WHERE kind='DISPATCH'")
        conn.execute("DELETE FROM jobs WHERE id=(SELECT min(id) FROM jobs)")
        reconcile(conn, case, stack.now)
    [issue] = active(path, case)
    original = [a["id"] for a in attempts(path, case)]
    login(browser, path)
    assert decision(browser, case, issue, "REPAIR_DISPATCH").status_code == 200
    assert decision(browser, case, issue, "REPAIR_DISPATCH").status_code == 409
    assert state(path, case) == "VERIFYING" and not sealed(path, case)
    assert [a["id"] for a in attempts(path, case)] == original
    stack.tick()
    advance_callbacks(stack)
    assert [w["attempt_id"] for w in sealed(path, case)["checks"]] == original



def test_low_confidence_beneficiary_requires_operator_correction(browser, stack):
    from app.fixtures import documents
    payload = scenario("bank-name-review")
    case = started(browser, stack, payload)
    advance_callbacks(stack)
    login(browser, stack.path)
    [issue] = active(stack.path, case)
    assert issue["cause"] == "BENEFICIARY_REVIEW" and "70.4" in issue["reason"]
    assert decision(browser, case, issue, "ACCEPT_REFERRAL").status_code == 409
    assert decision(browser, case, issue, "REQUEST_CORRECTION").status_code == 200
    assert state(stack.path, case) == "NEEDS_INFO" and not sealed(stack.path, case)
    payload["merchant"]["name"] = "Aster Valley Stores"
    payload["documents"] = documents(payload["merchant"])
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.tick()
    stack.now += 6
    stack.deliver()
    # M3 review B2: renaming the business to fit the bank record now contradicts the PAN/GST registries,
    # so it cannot approve. The positive route is the operator's reasoned beneficiary acceptance (test_hardening).
    renamed = {a["kind"]: (a["status"], a["reason_code"]) for a in attempts(stack.path, case) if a["input_revision"] == 2}
    assert renamed["PAN"] == ("FAIL", "NAME_MISMATCH") and renamed["GST"] == ("FAIL", "NAME_MISMATCH")
    assert renamed["BANK"] == ("PASS", "VERIFIED") and not sealed(stack.path, case)
    assert state(stack.path, case) == "IN_REVIEW"


def test_risk_rejection_is_terminal_and_audited(browser, stack):
    case = started(browser, stack, scenario("risk-referral"))
    advance_callbacks(stack)
    login(browser, stack.path)
    [issue] = active(stack.path, case)
    reason = "Risk outside policy tolerance"
    assert decision(browser, case, issue, "REJECT", reason=reason).status_code == 200
    assert decision(browser, case, issue, "ACCEPT_REFERRAL").status_code == 409
    stack.tick()
    assert state(stack.path, case) == "REJECTED" and not sealed(stack.path, case)
    assert stack.app.state.simulator.issuance_count(case) == 0
    assert query(stack.path, "SELECT actor,reason FROM decisions WHERE case_id=?", case) == [
        {"actor":"operator", "reason":reason}]
