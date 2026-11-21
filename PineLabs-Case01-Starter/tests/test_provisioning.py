"""F05 and I11. Count independently committed issuer MIDs and local mappings on either side of a retry."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest

from app import db
from app.provisioning import claim_mid, finish_mid, send_mid
from app.review import REASONS
from conftest import Browser
from test_approval import advance_callbacks, login
from test_verification import entered, query, scenario, started, state


def counts(stack, case):
    def count(table):
        return len(query(stack.path, f"SELECT case_id FROM {table} WHERE case_id=?", case))
    return (count("approvals"), stack.app.state.simulator.issuance_count(case), count("merchants"))


def approved(browser, stack, slug):
    case = started(browser, stack, scenario(slug))
    advance_callbacks(stack)
    assert counts(stack, case) == (1, 0, 0)
    return case


def job(stack, case):
    return query(stack.path, "SELECT * FROM jobs WHERE case_id=?", case)[0]


def requeue(client, case, cycle=0):
    return client.request("POST", f"/api/cases/{case}/issuance/requeue",
                          json={"cycle": cycle, "reason": REASONS["REQUEUE_MID"][0]})


@pytest.mark.parametrize(("slug", "intermediate", "substatus"), [
    ("mid-lost-ack", (1, 1, 0), "OUTCOME_UNKNOWN"), ("mid-before-commit", (1, 0, 0), "RETRY_SCHEDULED")])
def test_recover_issuance_over_http_without_reapproval(browser, stack, slug, intermediate, substatus):
    case = approved(browser, stack, slug)
    receipt = query(stack.path, "SELECT * FROM approvals WHERE case_id=?", case)[0]
    stack.tick()
    assert counts(stack, case) == intermediate and state(stack.path, case) == "PROVISIONING_FAILED"
    pending = job(stack, case)
    assert pending["status"] == "READY" and pending["claims"] == 1
    assert requeue(browser, case).status_code == 403
    login(browser, stack.path)
    page = browser.request("GET", "/cases/" + case).text
    assert substatus in page and "Requeue issuance" not in page
    assert substatus in browser.request("GET", "/desk").text
    assert requeue(browser, case).status_code == 409
    key = "mid/" + case
    issuer_path = stack.app.state.simulator.path
    original = query(issuer_path, "SELECT mid FROM sim_issuances WHERE operation_key=?", key)
    stack.now = pending["due_at"] - 0.001
    stack.tick()
    assert job(stack, case)["claims"] == 1
    stack.now = pending["due_at"]
    stack.tick()
    completed = job(stack, case)
    assert (completed["id"], completed["operation_key"], completed["claims"], completed["status"]) == (
        pending["id"], key, 2, "DONE")
    assert counts(stack, case) == (1, 1, 1)
    assert entered(stack.path, case) == ["VERIFYING", "APPROVED_PENDING_MID", "PROVISIONING_FAILED", "ACTIVE"]
    assert query(stack.path, "SELECT * FROM approvals WHERE case_id=?", case)[0] == receipt
    local = query(stack.path, "SELECT mid FROM merchants WHERE case_id=?", case)
    assert local == query(issuer_path, "SELECT mid FROM sim_issuances WHERE operation_key=?", key)
    if original:
        assert original == local
    assert query(issuer_path, "SELECT calls FROM sim_mid_requests WHERE operation_key=?", key)[0]["calls"] == 2


def test_dead_only_requeue_double_click_and_recovery(browser, stack):
    case = approved(browser, stack, "mid-dead-letter")
    for n in range(3):
        stack.tick()
        current = job(stack, case)
        assert current["claims"] == n + 1 and state(stack.path, case) == "PROVISIONING_FAILED"
        stack.now = current["due_at"]
    assert counts(stack, case) == (1, 0, 0) and current["status"] == "DEAD"
    stack.now += 100
    stack.tick()
    assert job(stack, case) == current
    assert requeue(browser, case).status_code == 403
    login(browser, stack.path)
    assert "DEAD_LETTERED" in browser.request("GET", "/desk").text
    page = browser.request("GET", "/cases/" + case).text
    assert "Requeue issuance" in page and "DEAD_LETTERED" in page
    barrier = Barrier(2)
    def click(_):
        client = Browser(stack.app)
        try:
            login(client, stack.path)
            barrier.wait(timeout=10)
            return requeue(client, case).status_code
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(click, range(2))) == [200, 200]
    reset = job(stack, case)
    assert (reset["id"], reset["status"], reset["claims"], reset["requeues"]) == (current["id"], "READY", 0, 1)
    assert requeue(browser, case, cycle=1).status_code == 409
    assert len(query(stack.path, "SELECT * FROM events WHERE event='MID_REQUEUED' AND actor='operator'")) == 1
    stack.tick()
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)
    assert requeue(browser, case).status_code == 200 and job(stack, case)["status"] == "DONE"


def test_actual_transport_loss_and_late_worker_completion(browser, stack):
    case = approved(browser, stack, "happy")
    claim = claim_mid(stack.path, stack.now)
    receipts = []
    def lose_response(route, body, headers, timeout):
        receipts.append(stack.post(route, body, headers).json())
        raise httpx.ReadError("transport lost after remote commit")
    result = send_mid(claim, lose_response)
    assert result[0] == "unknown" and counts(stack, case) == (1, 1, 0)
    # Abandoned claim: a new worker discovers the expired lease and reconciles using the same key.
    stack.now += 15
    stack.tick()
    assert state(stack.path, case) == "PROVISIONING_FAILED"
    stack.now = job(stack, case)["due_at"]
    stack.tick()
    assert finish_mid(stack.path, claim, result, stack.now) == "stale"
    assert counts(stack, case) == (1, 1, 1) and state(stack.path, case) == "ACTIVE"
    assert query(stack.path, "SELECT mid FROM merchants")[0]["mid"] == receipts[0]["mid"]


def test_unrecorded_error_on_final_claim_dead_letters(browser, stack):
    case = approved(browser, stack, "happy")
    for n in range(3):
        claim = claim_mid(stack.path, stack.now)
        assert claim and job(stack, case)["claims"] == n + 1
        stack.now += 15
        assert claim_mid(stack.path, stack.now) == {"skip": True}
        stack.now = job(stack, case)["due_at"]
    assert state(stack.path, case) == "PROVISIONING_FAILED" and job(stack, case)["status"] == "DEAD"
    assert counts(stack, case) == (1, 0, 0)


def test_invalid_receipts_and_issuer_contract(browser, stack):
    case = approved(browser, stack, "happy")
    claim = claim_mid(stack.path, stack.now)
    body = db.packed(dict(case_id=case, approval_digest=claim["approval_digest"], fixture_token=claim["fixture"])).encode()
    headers = {"Content-Type":"application/json", "Idempotency-Key": claim["key"]}
    assert stack.post("/sim/mids", body, headers).status_code == 401
    auth = {**headers, "Authorization": "Bearer " + claim["api_key"]}
    assert stack.post("/sim/mids", body, {**auth, "Idempotency-Key":"wrong"}).status_code == 409
    assert counts(stack, case) == (1, 0, 0)
    def wrong(*args):
        return httpx.Response(200, json={"case_id": "0" * 32, "approval_digest":claim["approval_digest"],
                                         "mid":"SYNMID-" + "A" * 16})
    assert send_mid(claim, wrong)[0] == "unknown"
    assert send_mid(claim, lambda *args: httpx.Response(200, text=""))[0] == "unknown"
    one = stack.post("/sim/mids", body, auth)
    two = stack.post("/sim/mids", body, auth)
    assert (one.status_code, two.status_code) == (201, 200) and one.json() == two.json()
    changed = db.packed({**json.loads(body), "approval_digest": "0" * 64}).encode()
    assert stack.post("/sim/mids", changed, auth).status_code == 409
    assert counts(stack, case) == (1, 1, 0)
    assert finish_mid(stack.path, claim, send_mid(claim, stack.post), stack.now) == "issued"
    assert counts(stack, case) == (1, 1, 1)


@pytest.mark.parametrize(("slug", "pan_generation"), [("duplicate-callback", 1), ("late-callback", 2)])
def test_duplicate_and_late_events_cannot_issue_twice(browser, stack, slug, pan_generation):
    case = started(browser, stack, scenario(slug))
    stack.run_to(16, step=.5)
    assert counts(stack, case) == (1, 1, 1)
    before = query(stack.path, "SELECT * FROM approvals WHERE case_id=?", case)
    receipt = json.loads(before[0]["receipt"])
    pan = next(w for w in receipt["checks"] if w["kind"] == "PAN")
    assert pan["generation"] == pan_generation
    simulator = stack.app.state.simulator
    assert simulator.replay(pan["event_id"], "identical", stack.now) == 1
    assert simulator.replay(pan["event_id"], "new-event", stack.now) == 1
    stack.deliver()
    stack.tick()
    assert query(stack.path, "SELECT * FROM approvals WHERE case_id=?", case) == before
    assert entered(stack.path, case).count("ACTIVE") == 1 and counts(stack, case) == (1, 1, 1)
