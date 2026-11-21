"""Final release audit R1: a save that names document files must be complete before anything is verified or approved.

Before the fix the form saved the case first and uploaded each file afterwards, and the save itself evaluated the
approval gate. A case whose only open issues the save resolved was sealed on the evidence it already held, and the
file that followed was refused with 409 on an ACTIVE case. Now the save declares the files it is about to send; the
declaration is a durable hold written in the saving transaction, so the gate cannot run on the evidence held so far
until each file has been read, or a later save stops naming it. Every test here uses the real OCR engine on
generated synthetic documents.
"""
import hashlib
import threading
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from app import db, documents, fixtures, metrics, ocr, views, workflow
from app.web import create_app
from conftest import Browser
from test_documents import document, revision, sealed, upload
from test_hardening import counts, decide, issue, login
from test_verification import active, query, scenario, started, state

pytestmark = pytest.mark.skipif(not ocr.available(), reason="Tesseract is not installed on this host")


def causes(stack, case):
    return sorted(i["cause"] for i in active(stack.path, case))


def save(browser, stack, case, payload, docs, uploads):
    body = dict(merchant=payload["merchant"], documents=docs, uploads=uploads, data_revision=revision(stack, case))
    return browser.request("PATCH", "/api/cases/" + case, json=body)


def typed(payload, kind):
    return views.typed_document(kind, payload["merchant"])


def sha(data):
    return hashlib.sha256(data).hexdigest()


def entered(stack, case):
    return [r["detail"] for r in query(stack.path, "SELECT detail FROM events WHERE case_id=? AND event='STATE_ENTERED' ORDER BY seq", case)]


def bank_evidence(receipt):
    return receipt["evidence"]["documents"]["bank"]["revision"], receipt["evidence"].get("extractions", {}).get("bank", {}).get("original_digest")


# ---------------------------------------------------------------- the audit's reproduction, valid replacement

def test_r1_tax_correction_and_bank_pdf_replacement_in_one_save_wait_for_the_pdf(browser, stack):
    """I7, then happy-tax.png as tax, then the beneficiary acceptance: the tax conflicts are the only open issues.
    One save ticks typed tax and names bank-name-review-bank.pdf. The receipt must carry that PDF."""
    payload = scenario("bank-name-review")
    case = started(browser, stack, payload)
    stack.run_to(8.5)
    assert upload(browser, stack, case, "tax", document(scenario("happy"), "tax")).status_code == 200
    login(browser, stack.path)
    assert decide(browser, case, issue(stack, case, "BENEFICIARY_REVIEW"), "ACCEPT_BENEFICIARY_MATCH").status_code == 200
    assert set(causes(stack, case)) == {"EVIDENCE_CONFLICT"} and not sealed(stack, case)

    pdf = document(payload, "bank", "pdf")
    docs = dict(identity=payload["documents"]["identity"], tax=typed(payload, "tax"), bank=payload["documents"]["bank"])
    assert save(browser, stack, case, payload, docs, ["bank"]).status_code == 200
    # Before the fix this save sealed the approval. Now the bank file is awaited and nothing is sealed.
    assert causes(stack, case) == ["UPLOAD_PENDING"] and not sealed(stack, case)
    assert state(stack.path, case) == "VERIFYING"  # the save is still being delivered: not an exception
    stack.run_to(20)
    assert not sealed(stack, case) and causes(stack, case) == ["UPLOAD_PENDING"]

    response = upload(browser, stack, case, "bank", pdf, "pdf")
    assert response.status_code == 200, response.text
    receipt = sealed(stack, case)
    current_bank = query(stack.path, "SELECT max(revision) AS r FROM documents WHERE case_id=? AND kind='bank'", case)[0]["r"]
    assert bank_evidence(receipt) == (current_bank, sha(pdf)) and current_bank > 1
    stack.run_to(25)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


# ---------------------------------------------------------------- invalid replacement, then a successful retry

def test_r1_corrupt_bank_pdf_in_a_combined_save_keeps_the_case_correctable_and_a_retry_completes(browser, stack):
    payload = scenario("missing-tax")
    case = browser.create(payload)
    stack.sync()
    assert state(stack.path, case) == "NEEDS_INFO"
    good = document(payload, "bank", "pdf")
    docs = dict(identity=payload["documents"]["identity"], tax=typed(payload, "tax"), bank=payload["documents"]["bank"])
    assert save(browser, stack, case, payload, docs, ["bank"]).status_code == 200
    assert causes(stack, case) == ["UPLOAD_PENDING"] and state(stack.path, case) == "VERIFYING"

    bad = upload(browser, stack, case, "bank", good[: len(good) // 2], "pdf")
    assert bad.status_code == 422
    assert causes(stack, case) == ["UPLOAD_FAILED"] and state(stack.path, case) == "NEEDS_INFO"
    stack.run_to(40)  # the worker keeps running: the earlier bank evidence never qualifies
    assert not sealed(stack, case) and state(stack.path, case) == "NEEDS_INFO"
    page = browser.request("GET", "/cases/" + case).text
    assert "The new bank document could not be used" in page and "Update my application" in page
    edit = browser.request("GET", f"/cases/{case}/edit")
    assert edit.status_code == 200 and "New file could not be used" in edit.text

    # Retry the way the form does: save again naming the file, then send a readable one.
    assert save(browser, stack, case, payload, docs, ["bank"]).status_code == 200
    assert causes(stack, case) == ["UPLOAD_PENDING"]
    assert upload(browser, stack, case, "bank", good, "pdf").status_code == 200
    stack.run_to(90)
    receipt = sealed(stack, case)
    assert bank_evidence(receipt)[1] == sha(good)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


def test_r1_a_refused_file_can_be_sent_again_without_a_new_save_and_a_save_without_it_withdraws_it(browser, stack):
    payload = scenario("missing-tax")
    case = browser.create(payload)
    stack.sync()
    docs = dict(identity=payload["documents"]["identity"], tax=typed(payload, "tax"), bank=payload["documents"]["bank"])
    assert save(browser, stack, case, payload, docs, ["bank", "identity"]).status_code == 200
    assert upload(browser, stack, case, "identity", b"not an image", ctype="image/png").status_code == 422
    assert causes(stack, case) == ["UPLOAD_FAILED", "UPLOAD_PENDING"] and state(stack.path, case) == "NEEDS_INFO"
    # The same revision is still current, so the refused document can be sent again directly.
    assert upload(browser, stack, case, "identity", document(payload, "identity")).status_code == 200
    assert causes(stack, case) == ["UPLOAD_PENDING"] and state(stack.path, case) == "VERIFYING"
    # The merchant gives up on the bank file: a save that no longer names it lets the typed bank document count.
    assert save(browser, stack, case, payload, docs, []).status_code == 200
    assert causes(stack, case) == []
    stack.run_to(60)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)
    receipt = sealed(stack, case)
    assert receipt["evidence"]["extractions"]["identity"]["original_digest"] == sha(document(payload, "identity"))


# ---------------------------------------------------------------- timing: callbacks and a racing worker

def identity_payload(number):
    m = fixtures.load_identity(number)
    return dict(submission_key=str(uuid4()), merchant=m, documents=fixtures.documents(m))


def test_r1_callbacks_arriving_while_a_file_is_pending_do_not_approve(browser, stack):
    payload = identity_payload(112)
    case = started(browser, stack, payload)  # four requests are in flight
    assert save(browser, stack, case, payload, payload["documents"], ["bank"]).status_code == 200
    stack.run_to(12)
    latest = {a["kind"]: a["status"] for a in query(stack.path, "SELECT kind,status FROM attempts WHERE case_id=?", case)}
    assert latest == dict(PAN="PASS", GST="PASS", BANK="PASS", RISK="PASS")
    assert not sealed(stack, case) and state(stack.path, case) == "VERIFYING"
    png = document(payload, "bank")
    assert upload(browser, stack, case, "bank", png).status_code == 200
    assert bank_evidence(sealed(stack, case))[1] == sha(png)
    stack.run_to(16)
    assert counts(stack, case) == (1, 1, 1)


def test_r1_a_concurrent_worker_cannot_approve_while_the_upload_is_pending(browser, stack):
    payload = identity_payload(117)
    case = started(browser, stack, payload)
    assert save(browser, stack, case, payload, payload["documents"], ["bank"]).status_code == 200
    stack.run_to(12)  # every check has passed; only the declared file is missing
    pdf = document(payload, "bank", "pdf")
    attempts, stop = [], threading.Event()

    def worker():  # the approval path every callback and decision takes, as fast as the lock allows
        while not stop.is_set():
            with db.write_tx(stack.path) as conn:
                workflow.reconcile(conn, case, stack.now)
                attempts.append(conn.execute("SELECT receipt FROM approvals WHERE case_id=?", (case,)).fetchone())

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while len(attempts) < 25 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(attempts) >= 25 and not any(attempts)
        # OCR runs outside the database lock while the worker keeps reconciling, then attaches in one transaction.
        documents.upload(stack.path, case, "bank", revision(stack, case), pdf, stack.now)
    finally:
        stop.set()
        thread.join()
    approved = query(stack.path, "SELECT receipt FROM approvals WHERE case_id=?", case)
    assert len(approved) == 1 and bank_evidence(sealed(stack, case))[1] == sha(pdf)
    stack.run_to(16)
    assert counts(stack, case) == (1, 1, 1)


# ---------------------------------------------------------------- an upload that never arrives

def test_r1_a_restart_during_an_incomplete_upload_keeps_the_hold_and_hands_it_back_to_the_merchant(browser, stack):
    payload = scenario("missing-tax")
    case = browser.create(payload)
    stack.sync()
    docs = dict(identity=payload["documents"]["identity"], tax=typed(payload, "tax"), bank=payload["documents"]["bank"])
    assert save(browser, stack, case, payload, docs, ["bank"]).status_code == 200
    # The tab closes and the server restarts before the file is sent. The hold is in SQLite, not in memory.
    db.close_writers()
    restarted = create_app(Path(stack.path).parent, "reference")
    merchant = Browser(restarted)
    try:
        merchant.client.cookies.update(browser.client.cookies)
        assert causes(stack, case) == ["UPLOAD_PENDING"] and not sealed(stack, case)
        stack.run_to(workflow.UPLOAD_GRACE_S - 5)
        assert state(stack.path, case) == "VERIFYING" and not sealed(stack, case)
        stack.run_to(workflow.UPLOAD_GRACE_S + 5)  # the worker hands the interrupted upload back
        assert causes(stack, case) == ["UPLOAD_FAILED"] and state(stack.path, case) == "NEEDS_INFO"
        assert "the file did not arrive" in active(stack.path, case)[0]["reason"]
        assert merchant.request("GET", f"/cases/{case}/edit").status_code == 200
        body = dict(merchant=payload["merchant"], documents=docs, uploads=["bank"], data_revision=revision(stack, case))
        assert merchant.request("PATCH", "/api/cases/" + case, json=body).status_code == 200
        png = document(payload, "bank")
        assert upload(merchant, stack, case, "bank", png).status_code == 200
    finally:
        merchant.close()
    stack.run_to(workflow.UPLOAD_GRACE_S + 60)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)
    assert bank_evidence(sealed(stack, case))[1] == sha(png)


# ---------------------------------------------------------------- metrics: uploading is not an exception

def test_r1_file_intake_is_not_counted_as_an_exception(browser, stack):
    payload = identity_payload(120)
    body = dict(submission_key=payload["submission_key"], merchant=payload["merchant"], documents={},
                uploads=["identity", "tax", "bank"])
    case = browser.create(body)
    stack.sync()
    assert state(stack.path, case) == "VERIFYING" and causes(stack, case) == ["UPLOAD_PENDING"] * 3
    for kind in ("identity", "tax", "bank"):
        assert upload(browser, stack, case, kind, document(payload, kind)).status_code == 200
    stack.run_to(12)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)
    assert "NEEDS_INFO" not in entered(stack, case)
    with closing(db.connect(stack.path)) as conn:
        result = metrics.compute(conn, now=stack.now)
    assert result["exceptions"]["cases"] == 0 and result["exceptions"]["by_cause"] == {}
    assert result["outcomes"]["approved"] == 1


def test_r1_a_save_must_still_supply_something(browser):
    m = fixtures.load_identity(121)
    empty = dict(submission_key=str(uuid4()), merchant=m, documents={})
    assert browser.request("POST", "/api/cases", json=empty).status_code == 422
    repeated = dict(empty, uploads=["bank", "bank"])
    assert browser.request("POST", "/api/cases", json=repeated).status_code == 422
