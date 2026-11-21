from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy

import pytest

from app import db, workflow
from app.access import issue_ticket
from app.extraction import Amendment, Submission, SAMPLE_DOCS
from app.web import case_view, create_app
from app.workflow import Problem, amend, submit
from conftest import correction


def test_missing_tax_correct_same_case_and_reopen(browser, app, payload, tmp_path):
    del payload["documents"]["tax"]
    case = browser.create(payload)
    detail = browser.request("GET", "/cases/" + case)
    assert detail.status_code == 200 and "NEEDS_INFO" in detail.text and "tax.document" in detail.text
    assert payload["merchant"]["account"] not in detail.text
    path = app.state.database
    initial = case_view(path, case)
    assert initial["attempts"] == []
    assert [(i["owner"], i["version"]) for i in initial["issues"]] == [("MERCHANT", 1)]
    payload["documents"]["tax"] = deepcopy(SAMPLE_DOCS["tax"])
    assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, 1)).status_code == 200
    updated = case_view(create_app(tmp_path).state.database, case)
    assert (updated["state"], updated["data_revision"], updated["issues"]) == ("VERIFYING", 2, [])
    assert [e["detail"] for e in updated["events"] if e["event"] == "STATE_ENTERED"] == ["NEEDS_INFO", "VERIFYING"]
    assert {a["kind"] for a in updated["attempts"] if a["status"] == "SCHEDULED"} == {"PAN","GST","BANK","RISK"}
    assert {f["score"] for f in updated["fields"] if f["label"] in {"name","beneficiary"}} == {100.0}
    with closing(db.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM jobs WHERE status='READY'").fetchone()[0] == 4
        assert conn.execute("SELECT version FROM review_items WHERE status='RESOLVED'").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 3


def test_replay_and_payload_conflict(browser, app, payload):
    case = browser.create(payload)
    same = browser.request("POST", "/api/cases", json=payload)
    assert same.status_code == 200 and same.json()["case_id"] == case
    payload["merchant"]["zone"] = "SYNZONE-02"
    assert browser.request("POST", "/api/cases", json=payload).status_code == 409
    with closing(db.connect(app.state.database)) as conn:
        assert conn.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 4


def test_intake_rolls_back_partial_dispatch_and_retry_recovers(browser, app, payload, monkeypatch):
    original_event = workflow.event

    def interrupt(conn, case, kind, detail, actor="system", **timestamp):
        if kind == "DISPATCH_QUEUED":
            raise RuntimeError("Injected failure after first job insert")
        original_event(conn, case, kind, detail, actor, **timestamp)

    with monkeypatch.context() as patch:
        patch.setattr(workflow, "event", interrupt)
        with pytest.raises(RuntimeError, match="after first job insert"):
            submit(app.state.database, Submission(**payload), "test browser", db.policy())
    with closing(db.connect(app.state.database)) as conn:
        for table in ("applications","documents","assessments","checks","attempts","jobs","events"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    case = browser.create(payload)
    assert len(case_view(app.state.database, case)["attempts"]) == 4


@pytest.mark.parametrize(("field","value","status"), [
    ("beneficiary", "Acme Market Private Limited", "REVIEW"),
    ("beneficiary", "Cedar Paper Works", "CONFLICT"),
    ("account", "SYNACCT-0002", "CONFLICT"),
    ("account", "", "MISSING"),
])
def test_independent_bank_fields(browser, app, payload, field, value, status):
    payload["documents"]["bank"][field] = value
    case = browser.create(payload)
    view = case_view(app.state.database, case)
    field_result = next(f for f in view["fields"] if f["document"] == "bank" and f["label"] == field)
    assert field_result["status"] == status and field_result["source_revision"] == 1
    assert view["state"] == ("NEEDS_INFO" if status == "MISSING" else "IN_REVIEW")
    assert not view["attempts"]
    if status == "REVIEW":
        assert 65 <= field_result["score"] < 95
        ticket = issue_ticket(app.state.database, "operator")
        browser.request("POST", "/access", json={"ticket": ticket})
        assert "bank.beneficiary" in browser.request("GET", "/desk").text


def test_embedded_pan_mismatch_before_dispatch(browser, app, payload):
    gst = "SYNGST-27-SYNPAN-0002-1"
    payload["documents"]["tax"]["gst"] = payload["merchant"]["gst"] = gst
    view = case_view(app.state.database, browser.create(payload))
    assert view["state"] == "IN_REVIEW" and not view["attempts"]
    assert {i["subject"] for i in view["issues"]} == {"tax.embedded_pan", "form.embedded_pan"}


def test_issue_versions_and_history(browser, app, payload):
    payload["documents"]["bank"]["account"] = "SYNACCT-0002"
    case = browser.create(payload)
    path = app.state.database
    issue = case_view(path, case)["issues"][0]
    payload["merchant"]["zone"] = "SYNZONE-02"
    amend(path, case, Amendment(**correction(payload, 1)))
    assert case_view(path, case)["issues"][0]["version"] == issue["version"]
    payload["documents"]["bank"]["account"] = "SYNACCT-0003"
    amend(path, case, Amendment(**correction(payload, 2)))
    changed = case_view(path, case)["issues"][0]
    assert changed["id"] == issue["id"] and changed["version"] == 2
    payload["documents"]["bank"]["account"] = payload["merchant"]["account"]
    amend(path, case, Amendment(**correction(payload, 3)))
    payload["documents"]["bank"]["account"] = "SYNACCT-0002"
    amend(path, case, Amendment(**correction(payload, 4)))
    opened_again = case_view(path, case)["issues"]
    assert len(opened_again) == 1 and opened_again[0]["id"] != issue["id"]


def test_one_conflicting_edit_wins(app, payload):
    path = app.state.database
    case, _, _ = submit(path, Submission(**payload), "test browser", db.policy())
    edit = Amendment(**correction(payload, 1))
    def write(_):
        try:
            amend(path, case, edit)
            return 200
        except Problem as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == [200, 409]
    assert case_view(path, case)["data_revision"] == 2


def test_edit_invalidation_and_policy_snapshot(browser, app, payload):
    case = browser.create(payload)
    for revision, account in enumerate(("SYNACCT-0002", "SYNACCT-0001"), start=1):
        payload["merchant"]["account"] = payload["documents"]["bank"]["account"] = account
        assert browser.request("PATCH", "/api/cases/" + case, json=correction(payload, revision)).status_code == 200
    view = case_view(app.state.database, case)
    assert [a["input_revision"] for a in view["attempts"] if a["kind"] == "BANK"] == [1, 2, 3]
    assert [a["status"] for a in view["attempts"] if a["kind"] == "BANK"] == ["SUPERSEDED","SUPERSEDED","SCHEDULED"]
    assert len([a for a in view["attempts"] if a["kind"] != "BANK"]) == 3
    assert view["policy"]["referral_kinds"] == ["RISK"]


def test_stalled_diagnostic_reopens_work(browser, app, payload):
    case = browser.create(payload)
    path = app.state.database
    with db.write_tx(path) as conn:
        conn.execute("UPDATE jobs SET status='CANCELLED'")
    amend(path, case, Amendment(**correction(payload, 1)))
    amend(path, case, Amendment(**correction(payload, 2)))
    held = case_view(path, case)
    assert held["state"] == "IN_REVIEW" and [i["cause"] for i in held["issues"]] == ["STALLED"]
    with db.write_tx(path) as conn:
        conn.execute("UPDATE jobs SET status='READY'")
    amend(path, case, Amendment(**correction(payload, 3)))
    assert case_view(path, case)["state"] == "VERIFYING"


@pytest.mark.parametrize("bad", ["missing_form", "wrong_type", "empty_docs", "unknown_label"])
def test_malformed_envelope(browser, app, payload, bad):
    if bad == "missing_form":
        del payload["merchant"]["pan"]
    elif bad == "wrong_type":
        payload["merchant"]["account"] = 123
    elif bad == "empty_docs":
        payload["documents"] = {}
    else:
        payload["documents"]["bank"]["unknown"] = "invalid"
    assert browser.request("POST", "/api/cases", json=payload).status_code == 422
    with closing(db.connect(app.state.database)) as conn:
        assert conn.execute("SELECT count(*) FROM applications").fetchone()[0] == 0
