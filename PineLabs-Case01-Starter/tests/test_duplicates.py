"""M4-A duplicate detection: explainable, backend-enforced, serialized under concurrency, with a human path."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import uuid4

import pytest

from app import approval, db, duplicates
from app.extraction import Amendment, Submission
from app.review import REASONS
from app.workflow import amend, reconcile, submit
from conftest import correction
from test_hardening import counts, decide, issue, login
from test_verification import active, attempts, query, scenario, started, state


def dup_issues(stack, case):
    return [i for i in active(stack.path, case) if i["cause"] == "DUPLICATE_CANDIDATE"]


def fresh(payload):
    return {**deepcopy(payload), "submission_key": str(uuid4())}


def synthetic(number, name="Valley Cedar Works Private Limited", zone="SYNZONE-01", state_code="27"):
    """A distinct, valid synthetic identity for collision tests. Providers do not know it; these cases only need flags."""
    from app.fixtures import documents
    merchant = dict(name=name, pan=f"SYNPAN-{number:04d}", gst=f"SYNGST-{state_code}-SYNPAN-{number:04d}-1",
                    account=f"SYNACCT-{number:04d}", routing=f"SYNIFSC-{number:04d}", category="RETAIL", zone=zone)
    return dict(submission_key=str(uuid4()), merchant=merchant, documents=documents(merchant))


# ---------------------------------------------------------------- repeated vs separate applications

def test_same_key_is_one_application_but_a_new_key_with_the_same_identity_is_held(browser, stack, payload):
    first = started(browser, stack, payload)
    assert browser.request("POST", "/api/cases", json=payload).json()["case_id"] == first  # transport retry: same case
    assert dup_issues(stack, first) == []
    second = browser.create(fresh(payload))
    assert second != first
    [flag] = dup_issues(stack, second)
    assert flag["owner"] == "OPERATOR" and flag["subject"] == "candidate." + first
    assert "PAN, GSTIN, bank account and routing, legal name and zone" in flag["reason"]
    assert "likely a repeat submission" in flag["reason"] and "cannot be dismissed" in flag["reason"]
    assert state(stack.path, second) == "IN_REVIEW" and attempts(stack.path, second) == []  # nothing is sent for it
    stack.sync()
    stack.run_to(12)
    assert state(stack.path, first) == "ACTIVE" and counts(stack, first) == (1, 1, 1)
    assert counts(stack, second) == (0, 0, 0) and state(stack.path, second) == "IN_REVIEW"


def test_hard_match_cannot_be_dismissed_only_fixed_or_rejected(browser, stack, payload):
    first = started(browser, stack, payload)
    second = browser.create(fresh(payload))
    login(browser, stack.path)
    [flag] = dup_issues(stack, second)
    page = browser.request("GET", "/cases/" + second).text
    assert "Dismiss candidate" not in page and "Reject application" in page and f"Matches case {first[:10]}" in page
    assert decide(browser, second, flag, "DISMISS_DUPLICATE").status_code == 409  # enforced by the backend, not the page
    assert decide(browser, second, flag, "REJECT").status_code == 200
    assert state(stack.path, second) == "REJECTED"
    assert state(stack.path, first) != "REJECTED"
    stack.sync()
    stack.run_to(12)
    assert counts(stack, first) == (1, 1, 1) and counts(stack, second) == (0, 0, 0)


def test_correcting_a_mistyped_identity_releases_the_hold_and_work_proceeds(browser, stack, payload):
    started(browser, stack, payload)
    clash = fresh(scenario("late-callback"))
    clash["merchant"]["pan"] = payload["merchant"]["pan"]  # a mistyped PAN that happens to be someone else's
    clash["merchant"]["gst"] = "SYNGST-07-SYNPAN-0001-1"
    clash["documents"]["identity"]["pan"] = clash["documents"]["tax"]["pan"] = payload["merchant"]["pan"]
    clash["documents"]["tax"]["gst"] = clash["merchant"]["gst"]
    second = browser.create(clash)
    [flag] = dup_issues(stack, second)
    assert "PAN" in flag["reason"] and "GSTIN" not in flag["reason"] and attempts(stack.path, second) == []
    fixed = scenario("late-callback")
    assert browser.request("PATCH", "/api/cases/" + second, json=correction(fixed, 1)).status_code == 200
    assert dup_issues(stack, second) == [] and state(stack.path, second) == "VERIFYING"
    assert len(attempts(stack.path, second)) == 4
    assert "DUPLICATE_CLEARED" in [e["event"] for e in query(stack.path, "SELECT event FROM events WHERE case_id=?", second)]


# ---------------------------------------------------------------- soft matches: false positives and human review

def test_name_and_zone_collision_is_dismissible_and_both_merchants_get_one_id_each(browser, stack):
    legit = scenario("happy")
    other = fresh(scenario("dup-name-zone"))
    first = started(browser, stack, legit)
    second = browser.create(other)
    [flag] = dup_issues(stack, second)
    assert "legal name and zone" in flag["reason"] and "PAN" not in flag["reason"].split("(")[0]
    assert "operator may dismiss with a reason" in flag["reason"]
    login(browser, stack.path)
    assert "Dismiss candidate" in browser.request("GET", "/cases/" + second).text
    assert decide(browser, second, flag, "DISMISS_DUPLICATE", reason="not offered").status_code == 422
    assert decide(browser, second, flag, "DISMISS_DUPLICATE", version=flag["version"] + 1).status_code == 409
    assert decide(browser, second, flag, "DISMISS_DUPLICATE", reason=REASONS["DISMISS_DUPLICATE"][0]).status_code == 200
    row = query(stack.path, "SELECT action,actor,reason FROM decisions WHERE issue_id=?", flag["id"])
    assert row == [{"action": "DISMISS_DUPLICATE", "actor": "operator", "reason": REASONS["DISMISS_DUPLICATE"][0]}]
    stack.sync()
    stack.tick()
    stack.run_to(14)
    assert counts(stack, first) == (1, 1, 1) and counts(stack, second) == (1, 1, 1)
    mids = query(stack.path, "SELECT mid FROM merchants")
    assert len({m["mid"] for m in mids}) == 2  # two legal entities, two Merchant IDs
    assert dup_issues(stack, second) == []  # later reconciliations do not reopen a dismissed pair


def test_near_misses_are_not_flagged(browser, stack):
    started(browser, stack, scenario("happy"))
    different_name = synthetic(31, name="Acme Trading Private Limited")  # same zone, a different name
    different_zone = synthetic(32, name="Acme Traders Pvt Ltd", zone="SYNZONE-02")  # same name, different zone
    for body in (different_name, different_zone):
        case = browser.create(body)
        assert dup_issues(stack, case) == [] and state(stack.path, case) == "VERIFYING"
    formatted = browser.create(synthetic(33, name="ACME TRADERS PVT LTD"))  # a formatting variant of the same name
    assert [i["reason"].split(" on ")[1].split(" (")[0] for i in dup_issues(stack, formatted)] == ["legal name and zone"]


def test_dismissal_lapses_when_either_identity_changes(browser, stack):
    first = started(browser, stack, scenario("happy"))
    body = synthetic(34, name="Acme Traders Private Limited")
    body["merchant"].update(account="SYNACCT-0001", routing="SYNIFSC-0001")
    body["documents"]["bank"].update(account="SYNACCT-0001", routing="SYNIFSC-0001")
    second = browser.create(body)
    [flag] = dup_issues(stack, second)
    assert "bank account and routing, legal name and zone" in flag["reason"]
    login(browser, stack.path)
    assert decide(browser, second, flag, "DISMISS_DUPLICATE").status_code == 200
    assert dup_issues(stack, second) == []
    # The shared account is corrected: the identity differs from what the operator reviewed, so it is judged again.
    body["merchant"].update(account="SYNACCT-0034", routing="SYNIFSC-0034")
    body["documents"]["bank"].update(account="SYNACCT-0034", routing="SYNIFSC-0034")
    assert browser.request("PATCH", "/api/cases/" + second, json=correction(body, 1)).status_code == 200
    [again] = dup_issues(stack, second)
    assert again["id"] != flag["id"] and "legal name and zone" in again["reason"]
    assert "bank account" not in again["reason"].split(" on ")[1].split(" (")[0]
    assert len(query(stack.path, "SELECT 1 FROM duplicate_dismissals WHERE case_id=?", second)) == 1


# ---------------------------------------------------------------- concurrency

def test_simultaneous_submissions_of_one_identity_flag_exactly_one(app):
    path = app.state.database
    words = ["Aster", "Meadow", "Market", "Stores", "River", "Cedar", "Supplies", "Works", "Valley", "Paper"]
    for number in range(10):
        # A distinct name per round, so rounds cannot collide with each other (name and zone is a soft match).
        body = synthetic(40 + number, name=f"{words[number]} {words[(number * 3 + 1) % 10]} Private Limited")
        barrier = Barrier(2)

        def send(label):
            barrier.wait(timeout=10)
            return submit(path, Submission(**{**deepcopy(body), "submission_key": str(uuid4())}), "browser " + label,
                          db.policy())[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            cases = list(pool.map(send, ("a", "b")))
        flagged = [c for c in cases if query_flagged(path, c)]
        assert len(flagged) == 1, number  # exactly one is held, never both and never neither
        winner = next(c for c in cases if c not in flagged)
        assert query_state(path, winner) == "VERIFYING" and query_state(path, flagged[0]) == "IN_REVIEW"


def query_flagged(path, case):
    return query_rows(path, "SELECT 1 FROM review_items WHERE case_id=? AND cause='DUPLICATE_CANDIDATE' AND status<>'RESOLVED'", case)


def query_state(path, case):
    return query_rows(path, "SELECT state FROM applications WHERE id=?", case)[0][0]


def query_rows(path, sql, *args):
    from contextlib import closing
    with closing(db.connect(path)) as conn:
        return conn.execute(sql, args).fetchall()


def test_two_cases_edited_into_the_same_identity_concurrently_end_with_one_flagged(app):
    path = app.state.database
    one = submit(path, Submission(**synthetic(50)), "a", db.policy())[0]
    two = submit(path, Submission(**synthetic(51, name="Valley Paper Works Private Limited")), "b", db.policy())[0]
    target = synthetic(52)
    barrier = Barrier(2)

    def edit(case):
        barrier.wait(timeout=10)
        amend(path, case, Amendment(**{"merchant": target["merchant"], "documents": target["documents"], "data_revision": 1}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(edit, (one, two)))
    # Whichever edit landed second saw the first. The earlier case scans again on its next mutation, as any
    # callback or decision would, and the later case is the one that stays held.
    for case in (one, two):
        with db.write_tx(path) as conn:
            reconcile(conn, case)
    assert [bool(query_flagged(path, c)) for c in (one, two)] == [False, True]


# ---------------------------------------------------------------- one Merchant ID per verified identity, enforced below the UI

def test_a_verified_identity_cannot_receive_a_second_merchant_id(browser, stack, payload):
    first = started(browser, stack, payload)
    stack.run_to(12)
    assert counts(stack, first) == (1, 1, 1)
    second = browser.create(fresh(payload))
    [flag] = dup_issues(stack, second)
    assert "likely a repeat submission" in flag["reason"] and "cannot be dismissed" in flag["reason"]
    # Even with the hold force-resolved in SQL (as a bug or a hostile edit might), the gate refuses to approve.
    with db.write_tx(stack.path) as conn:
        conn.execute("SAVEPOINT force")
        conn.execute("UPDATE review_items SET status='RESOLVED' WHERE case_id=?", (second,))
        row = conn.execute("SELECT * FROM applications WHERE id=?", (second,)).fetchone()
        assert approval.build_receipt(conn, row) is None
        conn.execute("ROLLBACK TO force")
        conn.execute("RELEASE force")
    # The final backstop is a unique claim on the PAN that nothing can update, delete or replace.
    with db.write_tx(stack.path) as conn:
        pan = json.loads(conn.execute("SELECT merchant FROM applications WHERE id=?", (first,)).fetchone()[0])["pan"]
        for sql in (f"INSERT INTO identity_claims VALUES ('{pan}','{second}')", "UPDATE identity_claims SET pan='X'",
                    "DELETE FROM identity_claims"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    assert query(stack.path, "SELECT count(*) AS n FROM merchants")[0]["n"] == 1


def test_an_older_in_progress_case_edited_into_an_approved_identity_is_held(browser, stack):
    older = scenario("missing-tax")
    first = browser.create(older)  # NEEDS_INFO, created before the approved case below
    approved = started(browser, stack, scenario("happy"))
    stack.run_to(12)
    assert counts(stack, approved) == (1, 1, 1)
    target = scenario("happy")
    assert browser.request("PATCH", "/api/cases/" + first, json=correction(target, 1)).status_code == 200
    [flag] = dup_issues(stack, first)
    assert flag["subject"] == "candidate." + approved and state(stack.path, first) == "IN_REVIEW"
    assert attempts(stack.path, first) == [] and counts(stack, first) == (0, 0, 0)


def test_rejected_cases_do_not_block_a_fresh_application(browser, stack, payload):
    first = started(browser, stack, payload)
    login(browser, stack.path)
    with db.write_tx(stack.path) as conn:
        conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
                     VALUES (?,'EVIDENCE_CONFLICT','bank.account',1,'OPERATOR','OPEN','test hold')""", (first,))
        reconcile(conn, first)
    held = [i for i in active(stack.path, first) if i["cause"] == "EVIDENCE_CONFLICT"][0]
    assert decide(browser, first, held, "REJECT").status_code == 200
    again = browser.create(fresh(payload))
    assert dup_issues(stack, again) == [] and state(stack.path, again) == "VERIFYING"


def test_claim_layer_blocks_a_second_approval_even_if_detection_is_bypassed(browser, stack, payload, monkeypatch):
    """Layer three. Detection is switched off entirely, so an identical second application runs all four checks.
    The gate still refuses it because the PAN is already claimed, and no second Merchant ID can exist."""
    first = started(browser, stack, payload)
    stack.run_to(12)
    assert counts(stack, first) == (1, 1, 1)
    monkeypatch.setattr(duplicates, "scan", lambda *args, **kwargs: 0)
    second = browser.create(fresh(payload))
    assert dup_issues(stack, second) == [] and state(stack.path, second) == "VERIFYING"
    stack.sync()
    stack.run_to(12)
    assert [a["status"] for a in attempts(stack.path, second)] == ["PASS"] * 4  # every check genuinely passed
    assert counts(stack, second) == (0, 0, 0) and state(stack.path, second) == "IN_REVIEW"
    assert query(stack.path, "SELECT count(*) AS n FROM merchants")[0]["n"] == 1
