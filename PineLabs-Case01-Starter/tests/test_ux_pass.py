"""Second UX pass: plain-language primary views, file upload in the merchant journey, technical detail kept secondary.

The pages only present persisted data. These tests pin the wording a merchant or operator acts on, and check that the
exact technical record (codes, subjects, versions) is still on the page in its secondary place.
"""
import re

import pytest

from app import ocr, views
from test_documents import document, revision, upload
from test_hardening import login
from test_metrics import operator_browser
from test_verification import scenario, started, state


def issue(cause, subject, reason, owner="OPERATOR"):
    return dict(cause=cause, subject=subject, reason=reason, owner=owner)


# ---------------------------------------------------------------- the plain-language layer

def test_every_issue_cause_gets_a_title_a_reason_and_a_next_step_for_each_audience():
    cases = [
        issue("EVIDENCE_MISSING", "tax.document", "tax.document · missing", "MERCHANT"),
        issue("EVIDENCE_CONFLICT", "bank.account", "bank.account · conflicts with application", "MERCHANT"),
        issue("EVIDENCE_UNREADABLE", "identity.pan", "identity.pan · unreadable · OCR confidence 19 (review below 50)"),
        issue("DUPLICATE_CANDIDATE", "candidate.abc", "Matches case abc on PAN (exact after normalization) · open"),
        issue("CHECK_FAILED", "check.GST", "check.GST · definitive fail · GST_REGISTRATION_CANCELLED"),
        issue("CHECK_REFERRED", "check.RISK", "check.RISK · referred · HIGH_RISK_ZONE"),
        issue("CHECK_EXHAUSTED", "check.PAN", "check.PAN · retries exhausted"),
        issue("BENEFICIARY_REVIEW", "check.BANK", "check.BANK · beneficiary name score 0.82"),
        issue("RISK_INPUTS_CHANGED_AFTER_REFERRAL", "check.RISK", "Same legal entity as rejected case xyz"),
        issue("STALLED", "case", "case · stalled"),
        issue("UPLOAD_PENDING", "bank.document", "bank.document · new file declared at data revision 4, waiting for it to be read", "MERCHANT"),
        issue("UPLOAD_FAILED", "bank.document", "bank.document · the new file could not be used: The PDF could not be opened.", "MERCHANT"),
    ]
    for item in cases:
        ex = views.explain_issue(item)
        assert ex["title"] and ex["why"] and ex["merchant"] and ex["operator"], item["cause"]
        # The primary words never carry the machine subject; the technical line always keeps the exact record.
        assert item["subject"] not in ex["title"] and item["reason"] in ex["technical"], item["cause"]
        assert not re.search(r"\b[A-Z]{3,}_[A-Z_]+\b", ex["title"] + ex["why"] + ex["merchant"] + ex["operator"]), item["cause"]
    gst = views.explain_issue(cases[4])
    assert gst["title"] == "The GST check failed" and gst["why"] == "This GST registration has been cancelled."
    missing = views.explain_issue(cases[0])
    assert missing["title"] == "The tax (GST) document has not been provided" and "PNG, JPEG or PDF" in missing["merchant"]
    # A technical line that already starts with the subject is not prefixed twice.
    assert gst["technical"] == "check.GST · definitive fail · GST_REGISTRATION_CANCELLED"
    failed = views.explain_issue(cases[-1])
    assert failed["title"] == "The new bank document could not be used" and failed["why"] == "The PDF could not be opened."
    unknown = views.explain_issue(issue("SOMETHING_NEW", "x.y", "x.y · new cause"))
    assert unknown["title"] == "x.y · new cause"  # an unmapped cause falls back to the exact record, never to nothing


def test_document_rows_say_whether_each_document_was_read_typed_or_is_missing():
    rows = views.document_rows({"documents": {"identity": {"name": "A"}, "bank": {}},
                                "extractions": {"bank": {"source_type": "image/png", "min_confidence": 91.0}}})
    by_kind = {r["kind"]: r for r in rows}
    assert [r["kind"] for r in rows] == ["identity", "tax", "bank"]
    assert by_kind["identity"]["source"] == "typed" and by_kind["identity"]["label"] == "Typed details"
    assert by_kind["tax"]["source"] == "missing" and by_kind["tax"]["label"] == "Not provided yet"
    assert by_kind["bank"]["source"] == "file" and "reading confidence 91" in by_kind["bank"]["note"]


def test_a_typed_document_that_is_not_the_business_details_is_not_presented_as_them():
    merchant = dict(name="Cedar Paper Goods Private Limited", pan="SYNPAN-0010", gst="SYNGST-24-SYNPAN-0010-1",
                    account="SYNACCT-0010", routing="SYNIFSC-0010")
    rows = {r["kind"]: r for r in views.document_rows({"merchant": merchant, "documents": {
        "identity": views.typed_document("identity", merchant),
        "bank": dict(views.typed_document("bank", merchant), account="SYNACCT-0011")}})}
    assert rows["identity"]["label"] == "Typed details" and not rows["identity"]["differs"]
    assert rows["bank"]["label"] == "Typed separately" and rows["bank"]["differs"]


def test_the_update_form_leaves_use_the_details_above_unticked_for_a_contradicting_document(browser, stack):
    case = browser.create(scenario("document-conflict"))
    edit = browser.request("GET", f"/cases/{case}/edit").text
    bank = edit[edit.index('<div class="doc-card" data-kind="bank">'):]
    assert "Typed separately" in bank
    box = re.search(r'<input type="checkbox" class="use-typed" data-kind="bank"[^>]*>', edit).group(0)
    assert "checked" not in box
    assert re.search(r'<textarea id="doc-bank" data-document="bank" data-custom="1"', edit)  # kept unless replaced


def test_check_sentences_explain_the_result_without_codes():
    assert views.plain_check("GST", "FAIL", "GST_REGISTRATION_CANCELLED", False, "CONSISTENT", False) == \
        "This GST registration has been cancelled."
    assert views.plain_check("PAN", "PASS", None, True, "CONSISTENT", False) == "The PAN is valid and belongs to this business."
    assert "needs a person to confirm" in views.plain_check("BANK", "PASS", None, False, "REVIEW", False)
    assert views.plain_check("RISK", None, None, False, "CONSISTENT", False) == "Not started yet."
    assert views.plain_check("PAN", "SCHEDULED", None, False, "CONSISTENT", True).startswith("Paused")


# ---------------------------------------------------------------- merchant intake: files first, structured JSON optional

def test_intake_offers_a_file_per_document_and_keeps_the_json_inside_a_collapsed_advanced_section(browser):
    page = browser.request("GET", "/").text
    assert page.count('type="file"') == 3 and page.count('accept="image/png,image/jpeg,application/pdf') == 3
    assert page.count('class="use-typed"') == 3 and "No file? Use the details above as this document" in page
    advanced = page.index('<details class="advanced">')
    closing = page.index("</details>", advanced)
    textareas = [m.start() for m in re.finditer("<textarea", page)]
    assert len(textareas) == 3 and all(advanced < at < closing for at in textareas)  # no JSON in the primary form
    assert '<details class="advanced" open' not in page and "Submit application" in page


# ---------------------------------------------------------------- merchant and operator case pages

def test_merchant_sees_what_is_needed_and_the_operator_sees_who_has_to_act(browser, stack):
    case = browser.create(scenario("missing-tax"))
    assert state(stack.path, case) == "NEEDS_INFO"
    page = browser.request("GET", "/cases/" + case).text
    assert "We need something from you" in page and "What we need from you" in page
    assert "The tax (GST) document has not been provided" in page and "Update my application" in page
    assert "Technical: tax.document" in page  # the record stays, in its secondary line
    assert '<section class="panel technical" id="technical"' in page and "<details open" not in page
    edit = browser.request("GET", f"/cases/{case}/edit").text
    assert "What we need from you" in edit and "Not provided yet" in edit and "Typed details" in edit
    assert 'id="file-tax"' in edit and "Save changes" in edit
    login(browser, stack.path)
    operator = browser.request("GET", "/cases/" + case).text
    assert "Waiting on the merchant" in operator and "There is nothing for you to decide until they do" in operator
    assert 'class="decision"' not in operator  # nothing to decide on a merchant-owned issue
    assert "Update my application" not in operator  # the merchant's call to action is not offered to an operator


def test_operator_decision_panel_explains_the_failure_and_keeps_the_record(browser, stack):
    case = started(browser, stack, scenario("gst-fail"))
    stack.run_to(8.5)
    login(browser, stack.path)
    page = browser.request("GET", "/cases/" + case).text
    assert "Decision needed" in page and "The GST check failed" in page and "You decide" in page
    assert "What happens next:" in page and "Ask the merchant to correct the details, or reject the application." in page
    assert 'aria-label="Decisions for: The GST check failed"' in page
    assert "Request correction" in page and "Reject application" in page
    assert "Technical: check.GST · definitive fail · GST_REGISTRATION_CANCELLED" in page
    # Callbacks, attempts, receipts and history are still on the page, collapsed under Technical details.
    tech = page[page.index('id="technical"'):]
    for label in ("Field-by-field evidence", "Verification attempts", "Callback receipts", "Case history"):
        assert label in tech, label


def test_desk_names_the_outstanding_action_in_words_and_keeps_the_code_on_hover(browser, stack, app):
    started(browser, stack, scenario("gst-fail"))
    stack.run_to(8.5)
    op = operator_browser(app)
    try:
        desk = op.request("GET", "/desk").text
        assert ">The GST check failed</span>" in desk and "Needs an operator" in desk
        assert re.search(r'title="[^"]*GST_REGISTRATION_CANCELLED[^"]*"', desk)
    finally:
        op.close()


@pytest.mark.skipif(not ocr.available(), reason="Tesseract is not installed on this host")
def test_a_document_uploaded_after_intake_is_shown_as_read_from_a_file(browser, stack):
    payload = scenario("missing-tax")
    case = browser.create(payload)
    stack.sync()
    assert upload(browser, stack, case, "tax", document(payload, "tax")).status_code == 200
    assert revision(stack, case) == 2
    edit = browser.request("GET", f"/cases/{case}/edit").text
    assert "Read from your file" in edit and "Replace with a file" in edit
    page = browser.request("GET", "/cases/" + case).text
    assert "Uploaded documents" in page and "The tax (GST) document has not been provided" not in page
