"""Audit follow-up: an operator clearance covers one risk assessment of one legal entity's history, not the application.

The clearance for a RISK assessment used to be bound to the RISK input revision and the winning attempt only. Those
depend on the self-declared category and zone, not on who the applicant is, so a clearance given while the application
carried one identity survived when the merchant edited the application into another identity that had also been
rejected for risk. These tests reproduce that and prove the clearance now names the history it set aside.
"""
import json

import pytest

from app import approval, db, fixtures, riskhistory
from app.review import REASONS
from conftest import correction
from test_audit_blockers import (RISK_HOLD, entity, nothing_approved, reapply, reject_for_risk, revision, risk_hold)
from test_hardening import counts, decide, issue, login
from test_verification import active, attempts, query, scenario, started, state


def reject_entity_for_risk(browser, stack, k):
    """Another legal entity, rejected by an operator after RISK failed it (WHOLESALE in SYNZONE-03)."""
    original = entity(k)
    case = started(browser, stack, original)
    stack.run_to(9)
    assert attempts(stack.path, case, "RISK")[0]["reason_code"] == "PROHIBITED_PROFILE"
    login(browser, stack.path)
    assert decide(browser, case, issue(stack, case, "CHECK_FAILED"), "REJECT", REASONS["REJECT"][1]).status_code == 200
    assert state(stack.path, case) == "REJECTED"
    return original, case


def switch_identity(browser, stack, case, target, **risk_inputs):
    """Edit the application into `target`'s identity while keeping the category and zone the RISK check already saw."""
    merchant = dict(target["merchant"], **risk_inputs)
    body = dict(merchant=merchant, documents=fixtures.documents(merchant), data_revision=revision(stack, case))
    response = browser.request("PATCH", "/api/cases/" + case, json=body)
    assert response.status_code == 200, response.text
    return merchant


def test_exploit_a_clearance_for_one_rejected_identity_cannot_approve_another(browser, stack):
    payload, first = reject_for_risk(browser, stack)  # entity A (SYNPAN-0008), rejected for PROHIBITED_PROFILE
    other, other_case = reject_entity_for_risk(browser, stack, 31)  # entity C, also rejected for risk
    second = started(browser, stack, reapply(payload, zone="SYNZONE-01"))  # A applies again, only the zone changed
    stack.run_to(12)
    [hold] = risk_hold(stack, second)
    assert first[:10] in hold["reason"] and other_case[:10] not in hold["reason"]
    assert decide(browser, second, hold, "CLEAR_RISK_CHANGE").status_code == 200  # the operator clears A's history
    # Before PAN, GST and BANK answer, the merchant turns the application into entity C. The RISK inputs are unchanged.
    switch_identity(browser, stack, second, other, zone="SYNZONE-01")
    assert [a["input_revision"] for a in attempts(stack.path, second, "RISK")] == [1]  # same RISK assessment
    stack.sync()
    stack.tick()
    stack.run_to(25)
    # Entity C's rejection was never put in front of an operator for this application.
    assert len(query(stack.path, "SELECT 1 FROM decisions WHERE case_id=? AND action='CLEAR_RISK_CHANGE'", second)) == 1
    nothing_approved(stack, second)
    [new_hold] = risk_hold(stack, second)
    assert new_hold["owner"] == "OPERATOR" and other_case[:10] in new_hold["reason"]


def clear_linked_application(browser, stack):
    """Entities A and C are both rejected for risk. A applies again, an operator clears A's history, nothing else has run."""
    payload, first = reject_for_risk(browser, stack)
    other, other_case = reject_entity_for_risk(browser, stack, 31)
    second = started(browser, stack, reapply(payload, zone="SYNZONE-01"))
    stack.run_to(12)
    [hold] = risk_hold(stack, second)
    assert decide(browser, second, hold, "CLEAR_RISK_CHANGE").status_code == 200
    return payload, first, other, other_case, second


def test_the_new_identity_can_still_be_cleared_by_a_fresh_decision_and_the_receipt_names_both(browser, stack):
    payload, first, other, other_case, second = clear_linked_application(browser, stack)
    switched = switch_identity(browser, stack, second, other, zone="SYNZONE-01")
    stack.sync()
    stack.tick()
    stack.run_to(20)
    [hold] = risk_hold(stack, second)
    assert decide(browser, second, hold, "CLEAR_RISK_CHANGE").status_code == 200  # a decision about entity C's history
    stack.sync()
    stack.tick()
    stack.run_to(14)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)
    receipt = json.loads(query(stack.path, "SELECT receipt FROM approvals WHERE case_id=?", second)[0]["receipt"])
    clearances = receipt["risk_clearances"]
    assert len(clearances) == 2 and clearances[0]["scope"] != clearances[1]["scope"]  # entity A's, then entity C's
    risk = next(w for w in receipt["checks"] if w["kind"] == "RISK")
    assert clearances[-1]["attempt_id"] == risk["attempt_id"]
    with db.write_tx(stack.path) as conn:
        assert clearances[-1]["scope"] == riskhistory.scope(conn, second, switched, risk["input_revision"])
    assert query(stack.path, "SELECT pan FROM identity_claims WHERE case_id=?", second)[0]["pan"] == switched["pan"]


def test_the_gate_refuses_a_stale_clearance_even_if_the_hold_never_reopens(browser, stack, monkeypatch):
    """Defence in depth: with the issue layer disabled, the approval gate alone rejects the clearance for another history."""
    payload, first, other, other_case, second = clear_linked_application(browser, stack)
    monkeypatch.setattr(riskhistory, "hold", lambda *args, **kwargs: None)
    switch_identity(browser, stack, second, other, zone="SYNZONE-01")
    stack.sync()
    stack.tick()
    stack.run_to(20)
    passed = {a["kind"] for a in attempts(stack.path, second) if a["status"] == "PASS"}
    assert passed == {"PAN", "GST", "BANK", "RISK"}  # every check passed for the new identity
    nothing_approved(stack, second)
    with db.write_tx(stack.path) as conn:
        conn.execute("SAVEPOINT bypass")
        conn.execute("UPDATE review_items SET status='RESOLVED' WHERE case_id=?", (second,))
        row = conn.execute("SELECT * FROM applications WHERE id=?", (second,)).fetchone()
        assert approval.build_receipt(conn, row) is None  # no open issue at all, still refused
        conn.execute("ROLLBACK TO bypass")
        conn.execute("RELEASE bypass")


def test_a_correction_that_keeps_the_same_history_needs_no_new_decision(browser, stack):
    """Legitimate correction: a routing typo is fixed after the clearance. The entity and its history are unchanged."""
    payload, first, other, other_case, second = clear_linked_application(browser, stack)
    merchant = dict(payload["merchant"], zone="SYNZONE-01", routing="SYNIFSC-0108")
    body = dict(merchant=merchant, documents=fixtures.documents(merchant), data_revision=revision(stack, second))
    assert browser.request("PATCH", "/api/cases/" + second, json=body).status_code == 200
    assert risk_hold(stack, second) == []
    stack.sync()
    stack.tick()
    stack.run_to(20)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)
    assert len(query(stack.path, "SELECT 1 FROM decisions WHERE case_id=? AND action='CLEAR_RISK_CHANGE'", second)) == 1


def test_switching_to_an_unrelated_clean_identity_after_a_clearance_is_not_held(browser, stack):
    payload, first, other, other_case, second = clear_linked_application(browser, stack)
    clean = entity(32, category="WHOLESALE", zone="SYNZONE-01")  # same RISK inputs, an entity nobody rejected
    switch_identity(browser, stack, second, clean, zone="SYNZONE-01")
    assert risk_hold(stack, second) == []
    stack.sync()
    stack.tick()
    stack.run_to(20)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)


def test_a_same_application_clearance_does_not_cover_a_rejected_history_that_appears_later(browser, stack):
    """The clearance was for the application's own earlier adverse result and set aside no other application's history.
    The approval follows a clearance at once, so the edit cannot be made through the API here: the scope is evaluated
    for the same application as it would be if it carried the identity of an entity that was rejected for risk."""
    other, other_case = reject_entity_for_risk(browser, stack, 31)
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    case = started(browser, stack, payload)
    stack.run_to(9)
    payload["merchant"]["category"], payload["merchant"]["zone"] = "RETAIL", "SYNZONE-01"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.sync()
    stack.run_to(10)
    [hold] = risk_hold(stack, case)
    assert "adverse RISK result" in hold["reason"] and other_case[:10] not in hold["reason"]
    assert decide(browser, case, hold, "CLEAR_RISK_CHANGE").status_code == 200
    stack.sync()
    stack.tick()
    stack.run_to(14)
    assert state(stack.path, case) == "ACTIVE"
    [decision] = query(stack.path, "SELECT id,subject_revision FROM decisions WHERE case_id=? AND action='CLEAR_RISK_CHANGE'", case)
    carried = dict(payload["merchant"])
    as_rejected_entity = dict(carried, pan=other["merchant"]["pan"], gst=other["merchant"]["gst"])
    with db.write_tx(stack.path) as conn:
        assert riskhistory.clearance_valid(conn, decision["id"], case, carried, decision["subject_revision"])
        assert not riskhistory.clearance_valid(conn, decision["id"], case, as_rejected_entity, decision["subject_revision"])


def test_decisions_recorded_before_scopes_existed_only_count_while_no_history_is_linked(browser, stack, monkeypatch):
    """A clearance from an older build has no scope row. It cannot vouch for a rejected history, so a linked
    application needs a fresh decision, while an application with no linked history is unaffected."""
    payload, first = reject_for_risk(browser, stack)
    monkeypatch.setattr(riskhistory, "record_scope", lambda *args, **kwargs: None)  # as the older build behaved
    second = started(browser, stack, reapply(payload, zone="SYNZONE-01"))
    stack.run_to(12)
    [hold] = risk_hold(stack, second)
    assert decide(browser, second, hold, "CLEAR_RISK_CHANGE").status_code == 200
    stack.sync()
    stack.tick()
    stack.run_to(14)
    nothing_approved(stack, second)
    [again] = risk_hold(stack, second)  # the old-style decision did not count, the hold reopened
    assert again["id"] != hold["id"]
    monkeypatch.undo()
    assert decide(browser, second, again, "CLEAR_RISK_CHANGE").status_code == 200
    stack.sync()
    stack.tick()
    stack.run_to(14)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)


def test_an_old_style_decision_still_counts_for_an_application_with_no_linked_history(browser, stack, monkeypatch):
    monkeypatch.setattr(riskhistory, "record_scope", lambda *args, **kwargs: None)
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    case = started(browser, stack, payload)
    stack.run_to(9)
    payload["merchant"]["category"], payload["merchant"]["zone"] = "RETAIL", "SYNZONE-01"
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    stack.sync()
    stack.run_to(10)
    login(browser, stack.path)
    assert decide(browser, case, issue(stack, case, RISK_HOLD), "CLEAR_RISK_CHANGE").status_code == 200
    stack.sync()
    stack.tick()
    stack.run_to(14)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


def test_the_recorded_scope_cannot_be_changed_or_removed(browser, stack):
    clear_linked_application(browser, stack)
    with db.write_tx(stack.path) as conn:
        for sql in ("UPDATE risk_clearance_scope SET scope='x'", "DELETE FROM risk_clearance_scope"):
            with pytest.raises(Exception, match="immutable record"):
                conn.execute(sql)
