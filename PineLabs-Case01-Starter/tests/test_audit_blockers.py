"""Final independent audit, blockers B1 and B2.

B1  A bank document with two different legible account numbers used to be judged by whichever line came first, so
    the order of two printed lines decided between a Merchant ID and an operator conflict.
B2  A merchant rejected for risk could apply again with the same PAN and GSTIN and only a different zone, and the new
    application was approved without any operator decision, because the adverse history belonged to the old case.

The reproductions use the audit's exact scenarios. Where real OCR is needed the tests run the installed Tesseract on
the audit's own image generator; the rest drive the same upload path with parsed lines so they run without it.
"""
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import approval, db, fixtures, ocr, samples, workflow
from app.extraction import Submission, assess
from app.review import Decision, REASONS, allowed_actions, decide as decide_issue
from conftest import correction
from test_hardening import counts, decide, issue, login
from test_verification import active, attempts, query, scenario, started, state

needs_ocr = pytest.mark.skipif(not ocr.available(), reason="Tesseract is not installed on this host")
RISK_HOLD = "RISK_INPUTS_CHANGED_AFTER_REFERRAL"  # the changed-input hold, now also raised across applications


def bank_lines(*accounts):
    """The printed lines of a bank advice: one Account line per value, in the order given."""
    return ["Beneficiary: ACME TRADERS PRIVATE LIMITED", *["Account: " + value for value in accounts], "Routing: SYNIFSC-0001"]


# ---------------------------------------------------------------- shared helpers

def read_lines(*texts, conf=92.0):
    """What ocr.read_words returns: one list of (left, word, confidence) per printed line."""
    return [[(10 * i, word, conf) for i, word in enumerate(text.split())] for text in texts]


def revision(stack, case):
    return query(stack.path, "SELECT data_revision FROM applications WHERE id=?", case)[0]["data_revision"]


def stored(stack, case, kind):
    rows = query(stack.path, "SELECT * FROM extractions WHERE case_id=? AND kind=? ORDER BY revision DESC", case, kind)
    return dict(rows[0], fields=json.loads(rows[0]["fields"])) if rows else None


def upload_lines(stack, case, kind, *texts, conf=92.0):
    """Attach an extraction exactly as the upload endpoint would, from lines instead of Tesseract."""
    image = Image.new("L", (40, 20), 255 - len(texts))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    png = buffer.getvalue()
    record = ocr.build_record(kind, "png", b"original " + "|".join(texts).encode(), png,
                              ocr.parse_fields(kind, read_lines(*texts, conf=conf)))
    workflow.upload_document(stack.path, case, kind, record, png, revision(stack, case), now=stack.now)


def lines_png(lines):
    """The audit's generator: fictional text only, the project's installed Pillow, 40 px default font."""
    page = Image.new("RGB", (1300, 90 + 90 * len(lines)), "white")
    draw = ImageDraw.Draw(page)
    for i, line in enumerate(lines):
        draw.text((36, 30 + 90 * i), line, fill="black", font=ImageFont.load_default(size=40))
    buffer = io.BytesIO()
    page.save(buffer, "PNG")
    return buffer.getvalue()


def audit_png(accounts):
    """The audit's exact bank advice: two account lines (1300 x 525 pixels)."""
    return lines_png(["BANK ACCOUNT ADVICE (SYNTHETIC)", "Beneficiary: ACME TRADERS PRIVATE LIMITED",
                      *["Account: " + value for value in accounts], "Routing: SYNIFSC-0001"])


def upload_png(browser, stack, case, kind, data):
    return browser.request("POST", f"/api/cases/{case}/documents/{kind}?revision={revision(stack, case)}",
                           content=data, headers={"Content-Type": "image/png"})


def happy_without_bank_document():
    payload = scenario("happy")
    payload["documents"].pop("bank")
    return payload


def nothing_approved(stack, case):
    assert counts(stack, case) == (0, 0, 0)
    assert query(stack.path, "SELECT 1 FROM merchants WHERE case_id=?", case) == []
    assert query(stack.path, "SELECT 1 FROM approvals WHERE case_id=?", case) == []
    assert state(stack.path, case) not in ("APPROVED_PENDING_MID", "ACTIVE")


# ================================================================ B1 · a document that contradicts itself

def test_b1_parser_keeps_every_labelled_line_and_chooses_none_of_two_different_values():
    for accounts in (("SYNACCT-0001", "SYNACCT-0099"), ("SYNACCT-0099", "SYNACCT-0001")):  # reversed ordering
        fields = ocr.parse_fields("bank", read_lines(*bank_lines(*accounts)))
        account = fields["account"]
        assert account["value"] is None and account["format_ok"] is False
        assert [c["value"] for c in account["candidates"]] == list(accounts)  # both kept, in reading order
        assert account["occurrences"] == 2 and fields["routing"]["value"] == "SYNIFSC-0001"  # others unaffected


def test_b1_a_contradiction_is_a_conflict_whatever_the_application_says():
    merchant = dict(scenario("happy")["merchant"])
    for accounts in (("SYNACCT-0001", "SYNACCT-0099"), ("SYNACCT-0099", "SYNACCT-0001")):
        fields = ocr.parse_fields("bank", read_lines(*bank_lines(*accounts)))
        extraction = dict(threshold=50.0, fields=fields)
        found = {f["label"]: f for f in assess(merchant, {"bank": {}}, {"bank": extraction}) if f["document"] == "bank"}
        assert found["account"]["status"] == "CONFLICT" and found["account"]["actual"] == " / ".join(accounts)
        assert found["account"]["candidates"] == list(accounts)
        assert found["beneficiary"]["status"] == "CONSISTENT" and found["routing"]["status"] == "CONSISTENT"


def test_b1_normal_documents_read_exactly_as_before():
    fields = ocr.parse_fields("bank", read_lines(*bank_lines("SYNACCT-0001")))
    assert {label: f["value"] for label, f in fields.items()} == {
        "beneficiary": "ACME TRADERS PRIVATE LIMITED", "account": "SYNACCT-0001", "routing": "SYNIFSC-0001"}
    assert all(f["confidence"] == 92.0 and len(f["candidates"]) == 1 and f["unread"] == 0 for f in fields.values())


def test_b1_the_same_value_twice_is_one_candidate_read_as_unsurely_as_its_weakest_line():
    lines = read_lines("Account: SYNACCT-0001") + read_lines("Account: SYNACCT-0001", conf=61.0)
    field = ocr.parse_fields("bank", lines)["account"]
    assert field["value"] == "SYNACCT-0001" and field["confidence"] == 61.0 and len(field["candidates"]) == 1


def test_b1_two_aliases_of_one_field_are_compared_with_each_other():
    agree = ocr.parse_fields("bank", read_lines("Account: SYNACCT-0001", "Account No: SYNACCT-0001"))["account"]
    differ = ocr.parse_fields("bank", read_lines("Account: SYNACCT-0001", "Account Number: SYNACCT-0099"))["account"]
    assert agree["value"] == "SYNACCT-0001" and differ["value"] is None and len(differ["candidates"]) == 2
    names = ocr.parse_fields("identity", read_lines("Name: ACME TRADERS PVT LTD", "Legal Name: Acme Traders Private Limited",
                                                    "PAN: SYNPAN-0001"))["name"]
    assert names["value"] == "ACME TRADERS PVT LTD" and len(names["candidates"]) == 1  # same name after normalization
    other = ocr.parse_fields("identity", read_lines("Name: ACME TRADERS PVT LTD", "Business Name: ASTER MEADOW STORES PVT LTD",
                                                    "PAN: SYNPAN-0001"))["name"]
    assert other["value"] is None and len(other["candidates"]) == 2


def test_b1_a_second_identifier_under_an_unrecognised_label_is_a_contradiction_not_ignored():
    field = ocr.parse_fields("bank", read_lines(*bank_lines("SYNACCT-0001"), "Settlement A/C: SYNACCT-0099"))["account"]
    assert field["value"] is None and [c["value"] for c in field["candidates"]] == ["SYNACCT-0001", "SYNACCT-0099"]
    same = ocr.parse_fields("bank", read_lines(*bank_lines("SYNACCT-0001"), "Stamp SYNACCT-0001"))["account"]
    assert same["value"] == "SYNACCT-0001" and len(same["candidates"]) == 1  # the same number again is harmless


def test_b1_an_unlabelled_token_never_supplies_a_value_and_an_embedded_pan_is_not_a_second_pan():
    only = ocr.parse_fields("bank", read_lines("Beneficiary: ACME TRADERS PRIVATE LIMITED", "Acct SYNACCT-0001",
                                               "Routing: SYNIFSC-0001"))["account"]
    assert only["value"] is None and only["found"] is False  # the label was not found, so it stays unreadable
    tax = ocr.parse_fields("tax", read_lines("Name: ACME TRADERS PVT LTD", "PAN: SYNPAN-0001",
                                             "GST: SYNGST-27-SYNPAN-0001-1"))
    assert tax["pan"]["value"] == "SYNPAN-0001" and tax["gst"]["value"] == "SYNGST-27-SYNPAN-0001-1"
    assert len(tax["pan"]["candidates"]) == 1 and len(tax["gst"]["candidates"]) == 1


def test_b1_a_readable_value_beside_an_unreadable_line_is_not_trusted():
    field = ocr.parse_fields("bank", read_lines("Account: SYNACCT-0001", "Account: SYNACCT-0O99"))["account"]
    assert field["value"] is None and field["unread"] == 1 and len(field["candidates"]) == 1
    merchant = dict(scenario("happy")["merchant"])
    row = next(f for f in assess(merchant, {"bank": {}}, {"bank": dict(threshold=50.0, fields={
        "beneficiary": ocr.parse_fields("bank", read_lines("Beneficiary: ACME TRADERS PRIVATE LIMITED"))["beneficiary"],
        "account": field, "routing": ocr.parse_fields("bank", read_lines("Routing: SYNIFSC-0001"))["routing"]})})
        if f["document"] == "bank" and f["label"] == "account")
    assert row["status"] == "UNREADABLE" and "another line" in row["method"]


@pytest.mark.parametrize("accounts", [("SYNACCT-0001", "SYNACCT-0099"), ("SYNACCT-0099", "SYNACCT-0001")],
                         ids=["matching-value-first", "matching-value-last"])
def test_b1_contradictory_bank_evidence_never_reaches_approval_in_either_order(browser, stack, accounts):
    """The audit's reproduction through the real upload path: identity and tax supplied, bank document uploaded."""
    case = started(browser, stack, happy_without_bank_document())
    assert state(stack.path, case) == "NEEDS_INFO"
    upload_lines(stack, case, "bank", *bank_lines(*accounts))
    account = stored(stack, case, "bank")["fields"]["account"]
    assert [c["value"] for c in account["candidates"]] == list(accounts) and account["value"] is None
    # The same operator-owned conflict in both orders, and nothing is sent to any provider while it stands.
    conflict = issue(stack, case, "EVIDENCE_CONFLICT")
    assert conflict["subject"] == "bank.account" and conflict["owner"] == "OPERATOR"
    assert "2 different values" in conflict["reason"] and state(stack.path, case) == "IN_REVIEW"
    stack.sync()
    stack.run_to(30)
    nothing_approved(stack, case)
    assert attempts(stack.path, case) == [] and state(stack.path, case) == "IN_REVIEW"
    with db.write_tx(stack.path) as conn:  # the approval gate recomputes the evidence and refuses it on its own
        row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
        assert approval.evidence_snapshot(conn, case, row["data_revision"], json.loads(row["merchant"])) is None
        assert approval.build_receipt(conn, row) is None


def test_b1_a_contradiction_cannot_be_accepted_only_corrected_or_rejected(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0001", "SYNACCT-0099"))
    login(browser, stack.path)
    conflict = issue(stack, case, "EVIDENCE_CONFLICT")
    page = browser.request("GET", "/cases/" + case).text
    assert "SYNACCT ··01 / SYNACCT ··99" in page and "2 different values read for this field" in page
    assert "Request correction" in page and "Reject application" in page and "Accept low-confidence value" not in page
    for action in ("ACCEPT_EXTRACTION", "ACCEPT_REFERRAL", "CLEAR_RISK_CHANGE", "DISMISS_DUPLICATE"):
        assert decide(browser, case, conflict, action).status_code == 409  # refused by the backend, not just hidden
    with db.write_tx(stack.path) as conn:
        app_row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
        item = conn.execute("SELECT * FROM review_items WHERE id=?", (conflict["id"],)).fetchone()
        assert allowed_actions(conn, item, app_row) == ["REQUEST_CORRECTION", "REJECT"]
    nothing_approved(stack, case)


def test_b1_correction_with_a_clear_single_valued_replacement_still_approves(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0099", "SYNACCT-0001"))
    login(browser, stack.path)
    assert decide(browser, case, issue(stack, case, "EVIDENCE_CONFLICT"), "REQUEST_CORRECTION").status_code == 200
    # Re-submitting the same contradictory evidence does not help.
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0099", "SYNACCT-0001"))
    assert [i["cause"] for i in active(stack.path, case)] == ["EVIDENCE_CONFLICT"]
    upload_lines(stack, case, "bank", "Beneficiary: ACME TRADERS PRIVATE LIMITED", "Account: SYNACCT-0001",
                 "Routing: SYNIFSC-0001")
    assert active(stack.path, case) == []
    stack.sync()
    stack.tick()
    stack.run_to(30)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


def test_b1_a_repeated_identical_account_and_an_alias_for_it_do_not_block_a_normal_approval(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_lines(stack, case, "bank", "Beneficiary: ACME TRADERS PRIVATE LIMITED", "Account: SYNACCT-0001",
                 "Account No: SYNACCT-0001", "Account: SYNACCT-0001", "Routing: SYNIFSC-0001")
    assert active(stack.path, case) == []
    stack.sync()
    stack.tick()
    stack.run_to(30)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


def test_b1_an_account_number_under_another_label_blocks_approval_too(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0001"), "Settlement A/C: SYNACCT-0099")
    conflict = issue(stack, case, "EVIDENCE_CONFLICT")
    assert conflict["subject"] == "bank.account" and state(stack.path, case) == "IN_REVIEW"
    stack.sync()
    stack.run_to(30)
    nothing_approved(stack, case)


def test_b1_a_second_contradicting_identifier_on_the_identity_document_is_not_discarded_either(browser, stack):
    payload = scenario("happy")
    payload["documents"].pop("identity")
    case = started(browser, stack, payload)
    upload_lines(stack, case, "identity", "Name: ACME TRADERS PVT LTD", "PAN: SYNPAN-0001", "PAN: SYNPAN-0002")
    conflict = issue(stack, case, "EVIDENCE_CONFLICT")
    assert conflict["subject"] == "identity.pan" and state(stack.path, case) == "IN_REVIEW"
    stack.sync()
    stack.run_to(30)
    nothing_approved(stack, case)


@needs_ocr
@pytest.mark.parametrize("accounts", [["SYNACCT-0001", "SYNACCT-0099"], ["SYNACCT-0099", "SYNACCT-0001"]],
                         ids=["audit-matching-first", "audit-matching-last"])
def test_b1_audit_images_read_by_tesseract_open_the_same_conflict_and_never_approve(browser, stack, accounts):
    """The audit's exact reproduction with the installed engine: Tesseract reads both lines above the threshold."""
    case = started(browser, stack, happy_without_bank_document())
    response = upload_png(browser, stack, case, "bank", audit_png(accounts))
    assert response.status_code == 200, response.text
    account = stored(stack, case, "bank")["fields"]["account"]
    assert [c["value"] for c in account["candidates"]] == accounts  # both observations survive, in reading order
    assert all(c["confidence"] >= 50.0 for c in account["candidates"]) and account["value"] is None
    conflict = issue(stack, case, "EVIDENCE_CONFLICT")
    assert conflict["subject"] == "bank.account" and conflict["owner"] == "OPERATOR"
    stack.sync()
    stack.run_to(30)
    nothing_approved(stack, case)
    assert state(stack.path, case) == "IN_REVIEW" and attempts(stack.path, case) == []
    # A clear, single-valued scan of the same document replaces it and the case progresses normally.
    clear = samples.make_document("bank", dict(fixtures_bank(), account="SYNACCT-0001"), "png", 0.0)
    login(browser, stack.path)
    assert decide(browser, case, issue(stack, case, "EVIDENCE_CONFLICT"), "REQUEST_CORRECTION").status_code == 200
    assert upload_png(browser, stack, case, "bank", clear).status_code == 200
    assert not [i for i in active(stack.path, case) if i["cause"] == "EVIDENCE_CONFLICT"]
    stack.sync()
    stack.tick()
    stack.run_to(30)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)


@needs_ocr
def test_b1_audit_style_image_with_a_second_account_under_another_label_is_held(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    image = lines_png(["BANK ACCOUNT ADVICE (SYNTHETIC)", "Beneficiary: ACME TRADERS PRIVATE LIMITED",
                       "Account: SYNACCT-0001", "Settlement A/C: SYNACCT-0099", "Routing: SYNIFSC-0001"])
    assert upload_png(browser, stack, case, "bank", image).status_code == 200
    account = stored(stack, case, "bank")["fields"]["account"]
    assert [c["value"] for c in account["candidates"]] == ["SYNACCT-0001", "SYNACCT-0099"] and account["value"] is None
    stack.sync()
    stack.run_to(30)
    nothing_approved(stack, case)
    assert state(stack.path, case) == "IN_REVIEW"


def fixtures_bank():
    return dict(fixtures.documents(scenario("happy")["merchant"])["bank"])


# ================================================================ B2 · a risk rejection follows the legal entity

def reject_for_risk(browser, stack):
    """The audit's setup: RISK referral fixture with WHOLESALE in SYNZONE-03 fails with PROHIBITED_PROFILE and the
    operator rejects it for 'Risk outside policy tolerance'."""
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    first = started(browser, stack, payload)
    stack.run_to(9)
    assert attempts(stack.path, first, "RISK")[0]["reason_code"] == "PROHIBITED_PROFILE"
    login(browser, stack.path)
    assert decide(browser, first, issue(stack, first, "CHECK_FAILED"), "REJECT", REASONS["REJECT"][1]).status_code == 200
    assert state(stack.path, first) == "REJECTED"
    return payload, first


def reapply(payload, **merchant_changes):
    again = deepcopy(payload)
    again["submission_key"] = str(uuid4())
    again["merchant"].update(merchant_changes)
    return again


def risk_hold(stack, case):
    return [i for i in active(stack.path, case) if i["cause"] == RISK_HOLD]


def test_b2_exploit_a_new_application_with_only_the_zone_changed_needs_operator_clearance(browser, stack):
    """The audit's exact exploit: reject for risk, then apply again with the same identity and only the zone changed."""
    payload, first = reject_for_risk(browser, stack)
    second = started(browser, stack, reapply(payload, zone="SYNZONE-01"))
    stack.run_to(12)
    risk = attempts(stack.path, second, "RISK")
    assert [a["status"] for a in risk] == ["PASS"]  # the new RISK check really ran and passed
    assert state(stack.path, second) == "IN_REVIEW" and counts(stack, second) == (0, 0, 0)  # the audit saw ACTIVE, 1/1/1
    [hold] = risk_hold(stack, second)
    assert hold["owner"] == "OPERATOR" and first[:10] in hold["reason"] and "PROHIBITED_PROFILE" in hold["reason"]
    assert query(stack.path, "SELECT * FROM decisions WHERE case_id=?", second) == []
    assert [a["kind"] for a in attempts(stack.path, second)] == ["RISK"]  # nothing else is sent before a decision
    assert state(stack.path, first) == "REJECTED"  # the original rejection is untouched
    page = browser.request("GET", "/cases/" + second).text
    assert "Clear changed risk inputs" in page and f"Same legal entity as rejected case {first[:10]}" in page
    # The operator's reasoned decision on the new assessment is what restores eligibility, and it is on the receipt.
    assert decide(browser, second, hold, "CLEAR_RISK_CHANGE").status_code == 200
    stack.sync()
    stack.tick()
    stack.run_to(14)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)
    receipt = json.loads(query(stack.path, "SELECT receipt FROM approvals WHERE case_id=?", second)[0]["receipt"])
    assert [c["attempt_id"] for c in receipt["risk_clearances"]] == [risk[0]["id"]]
    assert counts(stack, first) == (0, 0, 0) and state(stack.path, first) == "REJECTED"


def test_b2_a_new_intake_session_and_a_category_change_are_held_the_same_way(browser, stack):
    from conftest import Browser
    payload, first = reject_for_risk(browser, stack)
    stranger = Browser(stack.app)  # another browser profile, no cookie from the first application
    try:
        again = reapply(payload, category="RETAIL", zone="SYNZONE-01")  # a different profile on every RISK input
        second = stranger.create(again)
    finally:
        stranger.close()
    stack.sync()
    stack.tick()
    stack.run_to(20)
    assert [a["status"] for a in attempts(stack.path, second, "RISK")] == ["PASS"]
    assert len(risk_hold(stack, second)) == 1 and counts(stack, second) == (0, 0, 0)
    assert state(stack.path, second) == "IN_REVIEW"


def test_b2_the_gate_refuses_on_its_own_when_the_hold_never_opens(browser, stack, monkeypatch):
    """Defence in depth: even with the issue layer disabled, a linked application cannot be sealed without a clearance."""
    from app import riskhistory
    payload, first = reject_for_risk(browser, stack)
    monkeypatch.setattr(riskhistory, "hold", lambda *args, **kwargs: None)
    second = started(browser, stack, reapply(payload, zone="SYNZONE-01"))
    stack.run_to(14)
    assert {a["kind"]: a["status"] for a in attempts(stack.path, second)} == {
        "PAN": "PASS", "GST": "PASS", "BANK": "PASS", "RISK": "PASS"}
    nothing_approved(stack, second)
    assert [i["cause"] for i in active(stack.path, second)] == ["STALLED"]  # nothing eligible to approve
    with db.write_tx(stack.path) as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (second,)).fetchone()
        conn.execute("SAVEPOINT bypass")
        conn.execute("UPDATE review_items SET status='RESOLVED' WHERE case_id=?", (second,))
        assert approval.build_receipt(conn, row) is None  # no open issue at all, still refused by the clearance layer
        conn.execute("ROLLBACK TO bypass")
        conn.execute("RELEASE bypass")


def test_b2_the_prior_rejection_is_found_even_if_the_new_application_was_already_waiting(browser, stack):
    """Submitted first (held as a duplicate of the open case), then the open case is rejected for risk."""
    payload = scenario("risk-referral")
    payload["merchant"]["category"] = "WHOLESALE"
    first = started(browser, stack, payload)
    stack.run_to(9)
    second = browser.create(reapply(payload, zone="SYNZONE-01"))
    assert [i["cause"] for i in active(stack.path, second)] == ["DUPLICATE_CANDIDATE"] and attempts(stack.path, second) == []
    login(browser, stack.path)
    assert decide(browser, first, issue(stack, first, "CHECK_FAILED"), "REJECT", REASONS["REJECT"][1]).status_code == 200
    # Rejecting the first re-evaluates the waiting one in the same transaction: no duplicate any more, but a risk hold.
    assert [i["cause"] for i in active(stack.path, second)] == [RISK_HOLD]
    stack.sync()
    stack.tick()
    stack.run_to(20)
    assert [a["status"] for a in attempts(stack.path, second, "RISK")] == ["PASS"]
    nothing_approved(stack, second)


def entity(k, **changes):
    """A distinct registered synthetic entity whose profile (WHOLESALE in SYNZONE-03) fails RISK."""
    m = dict(fixtures.load_identity(100 + k), category="WHOLESALE", zone="SYNZONE-03")
    m.update(changes)
    return dict(submission_key=str(uuid4()), merchant=m, documents=fixtures.documents(m))


def test_b2_a_submission_racing_the_rejection_is_judged_after_it_in_either_order(browser, stack):
    """Three rounds on three entities: the rejection and the new application are committed by two threads at once."""
    login(browser, stack.path)
    for k in range(3):
        original = entity(k)
        first = started(browser, stack, original)
        stack.run_to(9)
        item = issue(stack, first, "CHECK_FAILED")
        barrier = threading.Barrier(2)
        newcomer = Submission(**{**deepcopy(original), "submission_key": str(uuid4()),
                                 "merchant": {**original["merchant"], "zone": "SYNZONE-01"}})

        def reject():
            barrier.wait(timeout=10)
            decide_issue(stack.path, first, item["id"], Decision(action="REJECT", version=item["version"],
                                                                 reason=REASONS["REJECT"][1]), stack.now)

        def apply():
            barrier.wait(timeout=10)
            return workflow.submit(stack.path, newcomer, f"profile-{k}", db.policy())[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            rejected, applied = pool.submit(reject), pool.submit(apply)
            rejected.result(20)
            second = applied.result(20)
        assert state(stack.path, first) == "REJECTED"
        stack.sync()
        stack.tick()
        stack.run_to(20)
        assert len(risk_hold(stack, second)) == 1 and state(stack.path, second) == "IN_REVIEW", k
        nothing_approved(stack, second)


def test_b2_simultaneous_applications_after_a_rejection_all_need_clearance_and_only_one_can_ever_be_approved(browser, stack):
    payload, first = reject_for_risk(browser, stack)
    barrier = threading.Barrier(4)

    def apply(n):
        barrier.wait(timeout=10)
        body = reapply(payload, zone="SYNZONE-01")
        return workflow.submit(stack.path, Submission(**body), f"profile-{n}", db.policy())[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        cases = list(pool.map(apply, range(4)))
    assert len(set(cases)) == 4
    stack.sync()
    stack.tick()
    stack.run_to(25)
    for case in cases:
        assert len(risk_hold(stack, case)) == 1
        nothing_approved(stack, case)
    oldest = query(stack.path, "SELECT id FROM applications WHERE id IN (%s) ORDER BY rowid LIMIT 1" % ",".join(
        "'%s'" % c for c in cases))[0]["id"]
    assert [i["cause"] for i in active(stack.path, oldest)].count("DUPLICATE_CANDIDATE") == 0  # it is the first of them
    assert [a["status"] for a in attempts(stack.path, oldest, "RISK")] == ["PASS"]
    assert decide(browser, oldest, risk_hold(stack, oldest)[0], "CLEAR_RISK_CHANGE").status_code == 200
    stack.sync()
    stack.tick()
    stack.run_to(14)
    assert counts(stack, oldest) == (1, 1, 1)
    assert sum(counts(stack, c)[0] for c in cases) == 1  # exactly one approval for the identity; the rest stay held


def test_b2_legitimate_reapplication_after_an_unrelated_rejection_is_not_slowed_down(browser, stack):
    """Rejected for a correctable GST error (RISK had passed): the corrected new application finishes autonomously."""
    payload = scenario("gst-fail")
    first = started(browser, stack, payload)
    stack.run_to(9)
    login(browser, stack.path)
    assert decide(browser, first, issue(stack, first, "CHECK_FAILED"), "REJECT", REASONS["REJECT"][0]).status_code == 200
    merchant = dict(payload["merchant"], gst="SYNGST-27-SYNPAN-0003-2")
    again = dict(submission_key=str(uuid4()), merchant=merchant, documents=fixtures.documents(merchant))
    second = started(browser, stack, again)
    stack.run_to(12)
    assert risk_hold(stack, second) == [] and state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)
    assert query(stack.path, "SELECT * FROM decisions WHERE case_id=?", second) == []


def test_b2_an_unrelated_entity_is_unaffected_by_someone_elses_risk_rejection(browser, stack):
    reject_for_risk(browser, stack)
    other = started(browser, stack, scenario("happy"))
    stack.run_to(12)
    assert risk_hold(stack, other) == [] and counts(stack, other) == (1, 1, 1)


def test_b2_reapplying_with_the_same_risky_profile_fails_risk_again_and_can_only_be_rejected(browser, stack):
    payload, first = reject_for_risk(browser, stack)
    second = started(browser, stack, reapply(payload))  # nothing changed
    stack.run_to(12)
    assert [a["reason_code"] for a in attempts(stack.path, second, "RISK")] == ["PROHIBITED_PROFILE"]
    nothing_approved(stack, second)
    assert {i["cause"] for i in active(stack.path, second)} >= {"CHECK_FAILED"}
    login(browser, stack.path)
    assert decide(browser, second, issue(stack, second, "CHECK_FAILED"), "CLEAR_RISK_CHANGE").status_code == 409


def test_b2_correcting_the_identity_to_a_different_entity_ends_the_link(browser, stack):
    """A mistyped PAN that belonged to the rejected merchant is corrected: the application is a different entity."""
    payload, first = reject_for_risk(browser, stack)
    second_payload = reapply(payload, zone="SYNZONE-01")
    second = started(browser, stack, second_payload)
    assert len(risk_hold(stack, second)) == 1
    clean = entity(30, category="RETAIL", zone="SYNZONE-01")
    assert browser.request("PATCH", "/api/cases/" + second, json=correction(clean, 1)).status_code == 200
    assert risk_hold(stack, second) == []  # the hold existed only for the link
    stack.sync()
    stack.tick()
    stack.run_to(20)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)
