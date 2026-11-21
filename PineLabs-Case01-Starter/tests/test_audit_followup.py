"""Final audit findings I2 to I5 and I8, one reproduction and one regression test each.

I2  duplicate holds and dismissals went stale: an edit did not wake a held peer, and a dismissal survived a change of
    PAN and GST because it only covered the attributes that matched.
I3  OCR preprocessing could enlarge a page past the advertised 4 megapixel budget.
I4  metrics mixed cohorts (a dead letter outside the window) and observation times (later states in an earlier snapshot).
I5  the operator view said "4 of 4 qualified" while a beneficiary-name review was still open.
I8  a run written before identity claims existed had approvals without claims.
"""
import io
import json
import time
from contextlib import closing

import pytest
from PIL import Image

from app import db, fixtures, metrics, ocr, workflow
from app.review import REASONS
from conftest import correction
from test_duplicates import dup_issues, fresh
from test_hardening import counts, decide, issue, login
from test_metrics import conn, operator_browser, timeline  # noqa: F401  (conn is a fixture)
from test_verification import active, attempts, query, scenario, started, state


# ================================================================ I2 · stale duplicate holds and dismissals

def test_i2_a_held_application_is_released_when_the_older_case_is_corrected_to_another_identity(browser, stack):
    """Audit reproduction: an incomplete application, then a complete copy that is held behind it. The older one is
    corrected to an unrelated registered identity. The copy used to stay held until its own next save."""
    first = browser.create(scenario("missing-tax"))  # older, incomplete (NEEDS_INFO)
    second = browser.create(fresh(scenario("missing-tax", complete=True)))  # a complete copy
    [flag] = dup_issues(stack, second)
    assert flag["subject"] == "candidate." + first and attempts(stack.path, second) == []
    other = fixtures.load_identity(125)
    body = dict(merchant=other, documents=fixtures.documents(other), data_revision=1)
    assert browser.request("PATCH", "/api/cases/" + first, json=body).status_code == 200
    assert dup_issues(stack, second) == []  # re-evaluated in the same transaction as the edit
    stack.sync()
    stack.tick()
    stack.run_to(20)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)


def test_i2_an_edit_into_a_waiting_applications_identity_holds_that_application_at_once(browser, stack):
    """The other direction: an older case is edited into the identity of a later, unflagged application."""
    older = browser.create(scenario("missing-tax"))
    other = fixtures.load_identity(126)
    later = browser.create(dict(submission_key=fresh(scenario("happy"))["submission_key"], merchant=other,
                                documents=fixtures.documents(other)))
    assert dup_issues(stack, later) == []
    clash = scenario("happy")
    body = dict(merchant=other, documents=fixtures.documents(other), data_revision=1)
    assert browser.request("PATCH", "/api/cases/" + older, json=body).status_code == 200
    [flag] = dup_issues(stack, later)  # the later application is the one that carries the flag
    assert flag["subject"] == "candidate." + older and "PAN" in flag["reason"]


def dismissed_name_and_zone_candidate(browser, stack):
    first = started(browser, stack, scenario("happy"))
    stack.run_to(12)
    assert counts(stack, first) == (1, 1, 1)
    base = scenario("dup-name-zone")["merchant"]
    merchant = dict(base, pan="SYNPAN-0034", gst="SYNGST-27-SYNPAN-0034-1", account="SYNACCT-0034", routing="SYNIFSC-0034")
    second = browser.create(dict(submission_key=fresh(scenario("happy"))["submission_key"], merchant=merchant,
                                 documents=fixtures.documents(merchant)))
    [flag] = dup_issues(stack, second)
    assert "legal name and zone" in flag["reason"] and "PAN" not in flag["reason"]
    login(browser, stack.path)
    assert decide(browser, second, flag, "DISMISS_DUPLICATE", REASONS["DISMISS_DUPLICATE"][2]).status_code == 200
    return second, base


def test_i2_a_dismissal_lapses_when_the_pan_and_gst_it_was_given_for_change(browser, stack):
    """Audit reproduction: dismiss a name-and-zone match for PAN 0034, then correct PAN and GST. The dismissal only
    covered the matched name and zone, so it used to carry over to a different PAN without a new decision."""
    second, base = dismissed_name_and_zone_candidate(browser, stack)
    assert dup_issues(stack, second) == []
    stack.sync()
    stack.tick()
    corrected = dict(base)  # the registered values for the fixture (SYNPAN-0015)
    body = dict(merchant=corrected, documents=fixtures.documents(corrected), data_revision=1)
    assert browser.request("PATCH", "/api/cases/" + second, json=body).status_code == 200
    [again] = dup_issues(stack, second)  # the earlier dismissal named another PAN, so it no longer applies
    assert "legal name and zone" in again["reason"]
    stack.sync()
    stack.run_to(20)
    assert counts(stack, second) == (0, 0, 0)
    assert decide(browser, second, again, "DISMISS_DUPLICATE", REASONS["DISMISS_DUPLICATE"][2]).status_code == 200
    stack.sync()
    stack.tick()
    stack.run_to(20)
    assert state(stack.path, second) == "ACTIVE" and counts(stack, second) == (1, 1, 1)


def test_i2_a_dismissal_still_holds_while_nothing_it_was_given_for_changes(browser, stack):
    second, base = dismissed_name_and_zone_candidate(browser, stack)
    merchant = query(stack.path, "SELECT merchant FROM applications WHERE id=?", second)[0]["merchant"]
    same = json.loads(merchant)
    body = dict(merchant=same, documents=fixtures.documents(same), data_revision=1)
    assert browser.request("PATCH", "/api/cases/" + second, json=body).status_code == 200  # saved unchanged
    assert dup_issues(stack, second) == []


# ================================================================ I3 · OCR pixel budget

def test_i3_preprocessing_never_exceeds_the_pixel_budget_and_small_pages_still_get_larger():
    for size in [(600, 1800), (799, 4000), (790, 790), (1000, 1000)]:
        out = Image.open(io.BytesIO(ocr.prepare(Image.new("RGB", size, "white"))))
        assert out.width * out.height <= ocr.MAX_PIXELS, (size, out.size)
    small = Image.open(io.BytesIO(ocr.prepare(Image.new("RGB", (300, 100), "white"))))
    assert small.size == (1200, 400)  # a normal small document is still enlarged for legibility


def test_i3_a_pdf_page_too_large_to_read_inside_the_budget_is_declined_before_it_is_rendered():
    commands = "BT /F1 12 Tf 40 100 Td (Account: SYNACCT-0001) Tj ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 20000 20000] /Contents 4 0 R "
               "/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(commands)} >>\nstream\n{commands}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    pdf += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    pdf += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    with pytest.raises(ocr.Rejected, match="too large"):
        ocr.render_pdf(pdf)


# ================================================================ I4 · metrics cohorts and observation time

def test_i4_an_older_dead_letter_is_not_a_job_of_a_newer_cohort(browser, stack):
    old = started(browser, stack, scenario("mid-dead-letter"))
    stack.run_to(40)
    assert query(stack.path, "SELECT status FROM jobs WHERE case_id=?", old)[0]["status"] == "DEAD"
    time.sleep(0.05)
    new = started(browser, stack, scenario("happy"))
    stack.run_to(12)
    assert counts(stack, new) == (1, 1, 1)
    created = query(stack.path, "SELECT created FROM applications WHERE id=?", new)[0]["created"]
    with closing(db.connect(stack.path)) as connection:
        newer = metrics.compute(connection, start=created - 0.001, now=stack.now)
        everything = metrics.compute(connection, now=stack.now)
    assert newer["cohort"] == 1 and newer["workload"]["dead_lettered"] == 0 and newer["workload"]["provisioning_failed"] == 0
    assert everything["cohort"] == 2 and everything["workload"]["dead_lettered"] == 1


def test_i4_an_earlier_observation_does_not_show_later_states(conn):  # noqa: F811
    timeline(conn, "a", 0.0, [(0, "VERIFYING"), (9, "APPROVED_PENDING_MID"), (10, "ACTIVE")], "ACTIVE")
    early, late = metrics.compute(conn, now=5.0), metrics.compute(conn, now=20.0)
    assert early["status_distribution"]["VERIFYING"] == 1 and early["status_distribution"]["ACTIVE"] == 0
    assert early["outcomes"]["active"] == 0 and early["workload"]["verifying_cases"] == 1
    assert late["status_distribution"]["ACTIVE"] == 1 and late["workload"]["verifying_cases"] == 0
    mid = metrics.compute(conn, now=9.5)
    assert mid["workload"]["approved_awaiting_mid"] == 1 and mid["outcomes"]["active"] == 0


def test_i4_the_whole_run_counter_says_so(browser, stack, app):
    started(browser, stack, scenario("happy"))
    stack.run_to(12)
    op = operator_browser(app)
    try:
        page = op.request("GET", "/metrics?window=1h").text
    finally:
        op.close()
    assert "Whole run, not scoped to the window" in page


# ================================================================ I5 · the operator view must not claim a false qualification

def test_i5_a_pending_beneficiary_review_is_not_counted_as_qualified_and_the_desk_agrees(browser, stack, app):
    case = started(browser, stack, scenario("bank-name-review"))
    stack.run_to(10)
    assert {a["kind"]: a["status"] for a in attempts(stack.path, case)} == {
        "PAN": "PASS", "GST": "PASS", "BANK": "PASS", "RISK": "PASS"}  # the provider said yes four times
    assert [i["cause"] for i in active(stack.path, case)] == ["BENEFICIARY_REVIEW"] and counts(stack, case) == (0, 0, 0)
    login(browser, stack.path)
    page = browser.request("GET", "/cases/" + case).text
    assert "4 of 4 qualified" not in page and "3 of 4 passed" in page
    assert "Passed · beneficiary name needs review" in page and page.count('class="check"') == 4
    desk = browser.request("GET", "/desk").text
    assert 'title="BANK: Passed · beneficiary name needs review"' in desk
    assert decide(browser, case, issue(stack, case, "BENEFICIARY_REVIEW"), "ACCEPT_BENEFICIARY_MATCH").status_code == 200
    stack.tick()
    after = browser.request("GET", "/cases/" + case).text
    assert "4 of 4 qualified, 1 accepted by an operator" in after


# ================================================================ I8 · a run written before identity claims existed

def forget_claim(path, case):
    """A run directory from before M4-A: an approval, a Merchant ID, and no identity claim."""
    with db.write_tx(path) as connection:
        connection.execute("DROP TRIGGER immutable_identity_claims_DELETE")
        connection.execute("DELETE FROM identity_claims WHERE case_id=?", (case,))
        connection.execute("""CREATE TRIGGER immutable_identity_claims_DELETE BEFORE DELETE ON identity_claims FOR EACH ROW
                           BEGIN SELECT RAISE(ABORT,'immutable record'); END""")
        connection.execute("DELETE FROM settings WHERE name='identity_claims_checked'")


def test_i8_missing_claims_are_restored_from_the_sealed_receipts_once(browser, stack):
    case = started(browser, stack, scenario("happy"))
    stack.run_to(12)
    assert counts(stack, case) == (1, 1, 1)
    pan = query(stack.path, "SELECT pan FROM identity_claims WHERE case_id=?", case)[0]["pan"]
    forget_claim(stack.path, case)
    assert query(stack.path, "SELECT 1 FROM identity_claims") == []
    assert workflow.upgrade_identity_claims(stack.path) == (1, 0)
    assert query(stack.path, "SELECT pan,case_id FROM identity_claims") == [{"pan": pan, "case_id": case}]
    assert workflow.upgrade_identity_claims(stack.path) == (0, 0)  # recorded, so it does not run again
    # duplicate detection still holds a second application for the same PAN
    second = browser.create({**scenario("happy"), "submission_key": fresh(scenario("happy"))["submission_key"]})
    assert counts(stack, second) == (0, 0, 0) and state(stack.path, second) == "IN_REVIEW"


def test_i8_a_collision_in_an_old_run_is_surfaced_not_hidden(tmp_path):
    """Two approvals for one PAN can only exist in a run from before the claim table (the receipt of the second is
    rewritten here to stand in for it). Restoring the claims must not hide that: the first takes the claim, the second
    is reported on its case, and both approvals and Merchant IDs stay exactly as they were. This test builds its own
    application, so the suite's end-of-test invariants (which assume a clean modern run) do not apply to it."""
    from app.web import create_app
    from conftest import Browser, Stack
    app = create_app(tmp_path, "reference")
    browser = Browser(app)
    stack = Stack(app, browser.runner)
    try:
        cases = []
        for number in (130, 131):
            m = fixtures.load_identity(number)
            cases.append(started(browser, stack, dict(submission_key=fresh(scenario("happy"))["submission_key"], merchant=m,
                                                      documents=fixtures.documents(m))))
        stack.run_to(14)
        first, second = cases
        assert counts(stack, first) == counts(stack, second) == (1, 1, 1)
        pans = [json.loads(query(stack.path, "SELECT merchant FROM applications WHERE id=?", c)[0]["merchant"])["pan"] for c in cases]
        with db.write_tx(stack.path) as connection:
            for trigger in ("immutable_approvals_UPDATE", "immutable_identity_claims_DELETE"):
                connection.execute("DROP TRIGGER " + trigger)
            receipt = connection.execute("SELECT receipt FROM approvals WHERE case_id=?", (second,)).fetchone()[0]
            rewritten = receipt.replace(pans[1], pans[0])
            connection.execute("UPDATE approvals SET receipt=?,receipt_digest=? WHERE case_id=?",
                               (rewritten, db.digest(rewritten), second))
            connection.execute("DELETE FROM identity_claims")
            connection.execute("DELETE FROM settings WHERE name='identity_claims_checked'")
        assert workflow.upgrade_identity_claims(stack.path) == (1, 1)
        assert query(stack.path, "SELECT case_id FROM identity_claims") == [{"case_id": first}]
        assert "IDENTITY_CLAIM_COLLISION" in [e["event"] for e in query(stack.path, "SELECT event FROM events WHERE case_id=?", second)]
        assert counts(stack, first) == counts(stack, second) == (1, 1, 1)  # nothing already approved or issued is touched
    finally:
        stack.close()
        browser.close()
        db.close_writers()


# ================================================================ I6 · the optional browser rehearsal

def test_i6_the_rehearsal_reads_the_merchant_id_from_the_one_element_that_has_an_id(browser, stack):
    """Playwright is not a project dependency, so the rehearsal itself is not run here. What broke it was that the Merchant
    ID appears twice on an active case page (the facts row and the issuance panel), so a text locator matched two
    elements. Pin both facts: the page keeps exactly one #mid-value, and the script uses it instead of a text locator."""
    from pathlib import Path
    case = started(browser, stack, scenario("happy"))
    stack.run_to(12)
    mid = query(stack.path, "SELECT mid FROM merchants WHERE case_id=?", case)[0]["mid"]
    page = browser.request("GET", "/cases/" + case).text
    assert page.count(mid) >= 1 and page.count('id="mid-value"') == 1  # however often it is printed, one element has the id
    script = (Path(__file__).resolve().parents[1] / "tools" / "browser_m3.py").read_text(encoding="utf-8")
    assert script.count('page.locator("#mid-value")') >= 2
    assert "get_by_text(re.compile(r\"^SYNMID" not in script and "get_by_text(original" not in script
