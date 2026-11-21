"""Audit follow-up: OCR records written before contradiction tracking cannot authorize an approval.

An extraction stored by the earlier parser kept only the first labelled line of each field, so a second, different value
on the same page may have been dropped before the record was stored. Such a record has no `candidates` in its fields.
It cannot qualify and cannot be accepted by an operator, open applications that rest on one are reopened once at start-up,
and tools/revalidate_evidence.py re-reads the stored image of any of them without changing anything.
"""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from app import approval, db, ocr, workflow
from app.db import packed
from test_audit_blockers import (audit_png, bank_lines, happy_without_bank_document, needs_ocr, nothing_approved, revision,
                                 stored, upload_lines)
from test_hardening import counts, decide, issue, login
from test_verification import active, attempts, query, started, state

TOOL = Path(__file__).resolve().parents[1] / "tools" / "revalidate_evidence.py"


def legacy_shape(fields):
    """What the earlier parser stored: no candidate list, no occurrence counts."""
    return {label: {key: f[key] for key in ("value", "confidence", "found", "format_ok")} for label, f in fields.items()}


def upload_legacy(stack, case, kind, *texts):
    """A record exactly as the earlier build would have stored it for these printed lines."""
    image = Image.new("L", (40, 20), 200)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    png = buffer.getvalue()
    fields = legacy_shape(ocr.parse_fields(kind, [[(10 * i, w, 92.0) for i, w in enumerate(t.split())] for t in texts]))
    record = ocr.build_record(kind, "png", b"legacy original " + "|".join(texts).encode(), png, fields)
    workflow.upload_document(stack.path, case, kind, record, png, revision(stack, case), now=stack.now)


def make_legacy(path, case, kind):
    """Rewrite a stored extraction as the earlier build wrote it (the table is immutable, so this stands in for a
    database that was written before the change)."""
    with db.write_tx(path) as conn:
        row = conn.execute("SELECT id,fields FROM extractions WHERE case_id=? AND kind=? ORDER BY revision DESC",
                           (case, kind)).fetchone()
        conn.execute("DROP TRIGGER immutable_extractions_UPDATE")
        conn.execute("UPDATE extractions SET fields=? WHERE id=?", (packed(legacy_shape(json.loads(row["fields"]))), row["id"]))
        conn.execute("""CREATE TRIGGER immutable_extractions_UPDATE BEFORE UPDATE ON extractions FOR EACH ROW
                     BEGIN SELECT RAISE(ABORT,'immutable record'); END""")


def forget_startup_pass(path):
    with db.write_tx(path) as conn:
        conn.execute("DELETE FROM settings WHERE name='legacy_evidence_checked'")


def test_a_legacy_record_cannot_qualify_even_when_every_value_matches_the_application(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_legacy(stack, case, "bank", *bank_lines("SYNACCT-0001"))
    issues = [i for i in active(stack.path, case) if i["cause"] == "EVIDENCE_UNREADABLE"]
    assert {i["subject"] for i in issues} == {"bank.beneficiary", "bank.account", "bank.routing"}
    assert all(i["owner"] == "OPERATOR" and "upload the document again" in i["reason"] for i in issues)
    assert state(stack.path, case) == "IN_REVIEW"
    stack.sync()
    stack.run_to(30)
    nothing_approved(stack, case)
    assert attempts(stack.path, case) == []


def test_an_operator_cannot_accept_a_legacy_record_only_replace_or_reject_it(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_legacy(stack, case, "bank", *bank_lines("SYNACCT-0001"))
    login(browser, stack.path)
    first = next(i for i in active(stack.path, case) if i["subject"] == "bank.account")
    page = browser.request("GET", "/cases/" + case).text
    assert "upload the document again" in page and "Accept low-confidence value" not in page
    assert "Request correction" in page and "Reject application" in page
    assert decide(browser, case, first, "ACCEPT_EXTRACTION").status_code == 409


def test_a_new_upload_replaces_a_legacy_record_and_the_case_approves(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_legacy(stack, case, "bank", *bank_lines("SYNACCT-0001"))
    login(browser, stack.path)
    assert decide(browser, case, next(i for i in active(stack.path, case) if i["subject"] == "bank.account"),
                  "REQUEST_CORRECTION").status_code == 200
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0001"))  # read with the current parser
    assert active(stack.path, case) == []
    stack.sync()
    stack.tick()
    stack.run_to(30)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)
    assert "candidates" in stored(stack, case, "bank")["fields"]["account"]


def test_the_gate_alone_refuses_a_legacy_record_even_if_nothing_else_noticed(browser, stack):
    """An application already under way when an old record is found: no start-up pass, no new issue, checks all pass."""
    case = started(browser, stack, happy_without_bank_document())
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0001"))
    assert active(stack.path, case) == []
    make_legacy(stack.path, case, "bank")
    stack.sync()
    stack.tick()
    stack.run_to(30)
    assert {a["kind"] for a in attempts(stack.path, case) if a["status"] == "PASS"} == {"PAN", "GST", "BANK", "RISK"}
    nothing_approved(stack, case)
    with db.write_tx(stack.path) as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
        assert approval.evidence_snapshot(conn, case, row["data_revision"], json.loads(row["merchant"])) is None


def test_start_up_reopens_open_applications_that_rest_on_a_legacy_record_once(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0001"))
    assert state(stack.path, case) == "VERIFYING" and active(stack.path, case) == []
    make_legacy(stack.path, case, "bank")
    forget_startup_pass(stack.path)
    assert workflow.upgrade_legacy_evidence(stack.path) == 1
    assert {i["subject"] for i in active(stack.path, case) if i["cause"] == "EVIDENCE_UNREADABLE"} == {
        "bank.beneficiary", "bank.account", "bank.routing"}
    assert state(stack.path, case) == "IN_REVIEW"
    assert "EVIDENCE_REVALIDATED" in [e["event"] for e in query(stack.path, "SELECT event FROM events WHERE case_id=?", case)]
    stack.sync()
    stack.run_to(30)
    nothing_approved(stack, case)
    assert workflow.upgrade_legacy_evidence(stack.path) == 0  # recorded: it does not run again


def test_start_up_leaves_an_application_that_was_already_approved_alone(browser, stack):
    case = started(browser, stack, happy_without_bank_document())
    upload_lines(stack, case, "bank", *bank_lines("SYNACCT-0001"))
    stack.sync()
    stack.tick()
    stack.run_to(30)
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)
    make_legacy(stack.path, case, "bank")
    forget_startup_pass(stack.path)
    assert workflow.upgrade_legacy_evidence(stack.path) == 0
    assert state(stack.path, case) == "ACTIVE" and counts(stack, case) == (1, 1, 1)
    assert active(stack.path, case) == []


@needs_ocr
def test_revalidation_tool_reads_the_stored_image_again_and_changes_nothing(browser, stack, tmp_path):
    """The historical exploit: a contradictory advice stored by the earlier parser as one clean matching account."""
    case = started(browser, stack, happy_without_bank_document())
    png = ocr.prepare(Image.open(io.BytesIO(audit_png(["SYNACCT-0001", "SYNACCT-0099"]))))
    first_only = legacy_shape(ocr.parse_fields("bank", ocr.read_words(png)))
    first_only["account"].update(value="SYNACCT-0001")  # the first labelled line won, as it did then
    record = ocr.build_record("bank", "png", b"historical original", png, first_only)
    workflow.upload_document(stack.path, case, "bank", record, png, revision(stack, case), now=stack.now)
    run_dir = Path(stack.path).parent
    before = query(stack.path, "SELECT count(*) AS n FROM events")[0]["n"], state(stack.path, case)
    done = subprocess.run([sys.executable, str(TOOL), str(run_dir)], capture_output=True, text=True, cwd=TOOL.parents[1])
    assert done.returncode == 1, done.stdout + done.stderr
    assert "bank.account" in done.stdout and "SYNACCT ··01 / SYNACCT ··99" in done.stdout
    assert (query(stack.path, "SELECT count(*) AS n FROM events")[0]["n"], state(stack.path, case)) == before  # read-only
    missing = subprocess.run([sys.executable, str(TOOL), str(tmp_path / "nothing")], capture_output=True, text=True,
                             cwd=TOOL.parents[1])
    assert missing.returncode == 2 and "Cannot revalidate" in missing.stdout
