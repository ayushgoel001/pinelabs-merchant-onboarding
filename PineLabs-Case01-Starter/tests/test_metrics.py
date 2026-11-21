"""M4-B operational metrics (A06): derived only from persisted rows, with explicit denominators and edge cases."""
import json
from collections import defaultdict
from contextlib import closing

import pytest

from app import db, metrics
from app.access import issue_ticket
from conftest import Browser
from test_verification import active, attempts, query, scenario, started, state


@pytest.fixture
def conn(tmp_path):
    path = db.initialize(tmp_path)
    with closing(db.connect(path)) as connection:
        yield connection


def make_case(conn, case, created, state_name="NEEDS_INFO"):
    conn.execute("INSERT INTO applications VALUES (?,?,?,?,?,?,1,?,?)",
                 (case, "key-" + case, "d", "s", json.dumps({"pan": "SYNPAN-0001"}), "{}", state_name, created))


def enter(conn, case, at, state_name):
    conn.execute("INSERT INTO events(case_id,at,event,actor,detail) VALUES (?,?,'STATE_ENTERED','system',?)", (case, at, state_name))


def timeline(conn, case, created, steps, final):
    make_case(conn, case, created, final)
    for at, name in steps:
        enter(conn, case, at, name)


# ---------------------------------------------------------------- the specified known timeline

def test_known_timeline_six_seconds_in_verification_and_half_exceptional(conn):
    # A: verifying at 0 s, review at 2 s, verifying at 5 s, approved at 9 s, active at 10 s.
    timeline(conn, "a", 0.0, [(0, "VERIFYING"), (2, "IN_REVIEW"), (5, "VERIFYING"), (9, "APPROVED_PENDING_MID"), (10, "ACTIVE")], "ACTIVE")
    # B: a clean case in the same cohort.
    timeline(conn, "b", 1.0, [(1, "VERIFYING"), (7, "APPROVED_PENDING_MID"), (8, "ACTIVE")], "ACTIVE")
    result = metrics.compute(conn, now=20.0)
    assert result["cohort"] == 2 and not result["empty"]
    verifying = result["time_in_state"]["VERIFYING"]
    # A spent 2 + 4 = 6 s in two visits, B spent 6 s in one: 12 s over 2 cases and 3 visits.
    assert verifying == dict(cases=2, visits=3, total=12.0, mean_per_case=6.0, median_per_visit=4.0, open_cases=0,
                             share=verifying["share"])
    assert result["time_in_state"]["IN_REVIEW"]["total"] == 3.0
    assert "ACTIVE" not in result["time_in_state"]  # intervals stop at a terminal entry
    assert result["exceptions"]["cases"] == 1 and result["exceptions"]["rate"] == 0.5
    assert result["exceptions"]["by_state"] == {"NEEDS_INFO": 0, "IN_REVIEW": 1, "PROVISIONING_FAILED": 0}


def test_state_dwell_for_one_case_alone_is_exact(conn):
    timeline(conn, "a", 0.0, [(0, "VERIFYING"), (2, "IN_REVIEW"), (5, "VERIFYING"), (9, "APPROVED_PENDING_MID"), (10, "ACTIVE")], "ACTIVE")
    result = metrics.compute(conn, now=100.0)
    assert result["time_in_state"]["VERIFYING"]["total"] == 6.0 and result["time_in_state"]["VERIFYING"]["mean_per_case"] == 6.0
    assert result["time_in_state"]["APPROVED_PENDING_MID"]["total"] == 1.0


# ---------------------------------------------------------------- zero, empty and incomplete data

def test_empty_database_and_empty_window_never_divide_by_zero(conn):
    for result in (metrics.compute(conn, now=100.0), metrics.compute(conn, start=1e9, end=2e9, now=3e9)):
        assert result["cohort"] == 0 and result["empty"]
        assert result["outcomes"]["approval_rate"] is None and result["outcomes"]["decided_approval_share"] is None
        assert result["exceptions"] == dict(cases=0, rate=None, by_state={}, by_cause={})
        assert result["time_in_state"] == {} and result["bottleneck"] is None
        assert result["workload"]["verifying_cases"] == 0 and result["workload"]["queues"]["operator"]["oldest"] is None
        json.dumps(result)  # always serializable


def test_open_interval_is_closed_at_observation_time_and_reported_as_open(conn):
    timeline(conn, "a", 0.0, [(0, "VERIFYING")], "VERIFYING")
    early, late = metrics.compute(conn, now=7.0), metrics.compute(conn, now=12.5)
    assert early["time_in_state"]["VERIFYING"]["total"] == 7.0 and early["time_in_state"]["VERIFYING"]["open_cases"] == 1
    assert late["time_in_state"]["VERIFYING"]["total"] == 12.5  # grows with the observation time, never invented
    assert early["workload"]["verifying_cases"] == 1


def test_observation_time_ignores_later_history_and_later_cases(conn):
    timeline(conn, "a", 0.0, [(0, "VERIFYING"), (10, "IN_REVIEW")], "IN_REVIEW")
    timeline(conn, "late", 50.0, [(50, "VERIFYING")], "VERIFYING")
    before = metrics.compute(conn, now=5.0)
    assert before["cohort"] == 1 and before["exceptions"]["cases"] == 0  # the review had not happened yet
    assert before["time_in_state"]["VERIFYING"]["total"] == 5.0
    after = metrics.compute(conn, now=60.0)
    assert after["cohort"] == 2 and after["exceptions"]["cases"] == 1 and after["exceptions"]["rate"] == 0.5


def test_window_is_half_open_on_submission_time(conn):
    for case, created in (("x", 10.0), ("y", 20.0), ("z", 30.0)):
        timeline(conn, case, created, [(created, "VERIFYING")], "VERIFYING")
    assert metrics.compute(conn, start=10.0, end=30.0, now=100.0)["cohort"] == 2  # [10, 30): z excluded
    assert metrics.compute(conn, start=10.0, end=30.000001, now=100.0)["cohort"] == 3


def test_rejection_counts_in_the_decided_share_and_not_in_dwell(conn):
    timeline(conn, "ok", 0.0, [(0, "VERIFYING"), (4, "APPROVED_PENDING_MID"), (5, "ACTIVE")], "ACTIVE")
    timeline(conn, "no", 0.0, [(0, "VERIFYING"), (3, "IN_REVIEW"), (8, "REJECTED")], "REJECTED")
    result = metrics.compute(conn, now=10.0)
    assert result["status_distribution"]["REJECTED"] == 1 and result["time_in_state"]["IN_REVIEW"]["total"] == 5.0
    assert "REJECTED" not in result["time_in_state"]
    assert result["exceptions"]["by_state"]["IN_REVIEW"] == 1


# ---------------------------------------------------------------- end to end against real workflow data

def recompute_from_events(path, now):
    """Independent re-derivation with plain Python loops: dwell per state and exception cases."""
    with closing(db.connect(path)) as c:
        events = c.execute("SELECT case_id,at,detail FROM events WHERE event='STATE_ENTERED' AND at<=? ORDER BY seq", (now,)).fetchall()
    by_case = defaultdict(list)
    for e in events:
        by_case[e["case_id"]].append((e["at"], e["detail"]))
    dwell, held = defaultdict(float), set()
    for case, entries in by_case.items():
        for i, (at, name) in enumerate(entries):
            if name in metrics.EXCEPTION_STATES:
                held.add(case)
            if i + 1 < len(entries):
                dwell[name] += entries[i + 1][0] - at
            elif name not in metrics.TERMINAL:
                dwell[name] += now - at
    return dict(dwell), held


def test_end_to_end_metrics_match_a_hand_derivation_from_the_audit_log(browser, stack):
    first = started(browser, stack, scenario("happy"))
    held = browser.create(scenario("gst-fail"))
    waiting = browser.create(scenario("missing-tax"))
    stack.sync()
    stack.tick()
    stack.run_to(14)
    with closing(db.connect(stack.path)) as c:
        result = metrics.compute(c, now=stack.now)
        approvals = c.execute("SELECT count(*) FROM approvals").fetchone()[0]
    assert result["cohort"] == 3
    assert result["status_distribution"]["ACTIVE"] == 1 and result["status_distribution"]["IN_REVIEW"] == 1
    assert result["status_distribution"]["NEEDS_INFO"] == 1
    assert result["outcomes"]["approved"] == approvals == 1 and result["outcomes"]["approval_rate"] == pytest.approx(1 / 3)
    assert result["exceptions"]["cases"] == 2 and result["exceptions"]["rate"] == pytest.approx(2 / 3)  # gst-fail and missing-tax
    assert result["exceptions"]["by_cause"]["CHECK_FAILED"] == 1 and result["exceptions"]["by_cause"]["EVIDENCE_MISSING"] == 1
    dwell, held_cases = recompute_from_events(stack.path, stack.now)
    assert len(held_cases) == result["exceptions"]["cases"]
    for name, seconds in dwell.items():
        if name in metrics.TERMINAL:
            continue
        assert result["time_in_state"][name]["total"] == pytest.approx(seconds)  # exactly the audit-log arithmetic
    # Workload at the observation time: one case waits for an operator, one for its merchant, none is verifying.
    assert result["workload"]["queues"]["operator"]["cases"] == 1 and result["workload"]["queues"]["merchant"]["cases"] == 1
    assert result["workload"]["open_issue_cases_by_owner"] == {"MERCHANT": 1, "OPERATOR": 1}
    assert result["workload"]["verifying_cases"] == 0 and result["workload"]["pending_attempts"] == {}
    # Provider latency comes from sent and settled times: the reference timeline answers PAN in 2 s and GST in 4 s.
    latency = result["durations"]["provider_latency"]
    assert latency["PAN"]["mean"] == pytest.approx(2.0, abs=0.3) and latency["GST"]["mean"] == pytest.approx(4.0, abs=0.3)
    assert result["durations"]["submission_to_active"]["n"] == 1 and result["durations"]["submission_to_active"]["mean"] > 8
    assert 0 <= result["durations"]["approval_to_mid"]["mean"] < 3  # the injected clock issues the MID in the same step
    assert result["bottleneck"]["state"] in metrics.STATE_ORDER and result["bottleneck"]["slowest_provider"] in ("RISK", "BANK", "GST")
    assert result["reliability"]["timeout_rate"] == 0.0 and result["reliability"]["retries"] == 0


def test_pending_verification_workload_counts_live_attempts(browser, stack):
    started(browser, stack, scenario("happy"))
    stack.run_to(5)  # PAN and GST have replied, BANK and RISK are still outstanding
    with closing(db.connect(stack.path)) as c:
        result = metrics.compute(c, now=stack.now)
    assert result["workload"]["verifying_cases"] == 1
    assert result["workload"]["pending_attempts"] == {"BANK": 1, "RISK": 1}
    assert result["time_in_state"]["VERIFYING"]["open_cases"] == 1


# ---------------------------------------------------------------- the API and the page

def operator_browser(app):
    client = Browser(app)
    client.request("POST", "/access", json={"ticket": issue_ticket(app.state.database, "operator")})
    return client


def test_api_requires_the_operator_and_validates_the_window(browser, app):
    assert browser.request("GET", "/api/metrics").status_code == 403
    assert "Operator access required" in browser.request("GET", "/metrics").text
    op = operator_browser(app)
    try:
        empty = op.request("GET", "/api/metrics")
        assert empty.status_code == 200 and empty.json()["empty"] and empty.json()["outcomes"]["approval_rate"] is None
        assert op.request("GET", "/api/metrics?window=24h").json()["window"]["label"] == "the last 24 hours"
        assert op.request("GET", "/api/metrics?window=bogus").status_code == 422
        assert "No applications" in op.request("GET", "/metrics").text
    finally:
        op.close()


def test_page_and_desk_show_the_derived_numbers(browser, stack, app):
    started(browser, stack, scenario("happy"))
    browser.create(scenario("gst-fail"))
    stack.sync()
    stack.run_to(14)
    op = operator_browser(app)
    try:
        page = op.request("GET", "/metrics").text
        assert "Approval rate" in page and "1 of 2 applications" in page  # the denominator is printed with the rate
        assert "Exception rate" in page and "Time in state" in page and "In review" in page
        desk = op.request("GET", "/desk").text
        assert "Applications" in desk and "Approval rate" in desk
    finally:
        op.close()


# ---------------------------------------------------------------- view-model helpers and error pages

def test_views_choose_words_from_persisted_status_only():
    from app import views
    attempts = [dict(kind="PAN", input_revision=1, current_revision=1, generation=2, status="PASS", sent_at=10.0, settled_at=12.5,
                     reason_code="VERIFIED", reference="PAN-1", job_status="DONE"),
                dict(kind="PAN", input_revision=1, current_revision=1, generation=1, status="EXPIRED", sent_at=0.0, settled_at=10.0,
                     reason_code=None, reference=None, job_status="DONE"),
                dict(kind="GST", input_revision=1, current_revision=2, generation=1, status="PASS", sent_at=1.0, settled_at=2.0,
                     reason_code="VERIFIED", reference="GST-1", job_status="DONE"),
                dict(kind="BANK", input_revision=1, current_revision=1, generation=1, status="SCHEDULED", sent_at=None,
                     settled_at=None, reason_code=None, reference=None, job_status="HELD")]
    cards = {c["kind"]: c for c in views.latest_checks(attempts, ["PAN", "GST", "BANK", "RISK"])}
    assert (cards["PAN"]["word"], cards["PAN"]["generation"], cards["PAN"]["latency"]) == ("Passed", 2, 2.5)  # newest generation wins
    assert cards["GST"]["word"] == "Not started"  # a pass for superseded inputs does not count
    assert cards["BANK"]["word"] == "Paused by an open hold" and cards["BANK"]["held"]
    assert cards["RISK"]["status"] is None
    data = dict(state="VERIFYING", issues=[], approval=None, mid=None)
    steps = {s["label"]: s["status"] for s in views.progress(data, list(cards.values()))}
    assert steps["Submitted"] == "done" and steps["Verification"] == "active" and steps["Approval"] == "todo"
    assert views.duration(None) == "—" and views.duration(2.5) == "2.5 s" and views.duration(125) == "2 min 05 s"
    assert views.percent(None) == "—" and views.percent(0.333) == "33%" and views.ago(None) == "—"


def test_browsers_get_a_readable_error_page_and_api_clients_keep_json(browser, app):
    html = {"Accept": "text/html"}
    missing = browser.request("GET", "/no-such-page", headers=html)
    assert missing.status_code == 404 and "We could not find that" in missing.text and "New application" in missing.text
    assert missing.headers["content-type"].startswith("text/html")
    assert browser.request("GET", "/no-such-page").json() == {"detail": "That page does not exist."}  # no Accept header: JSON
    denied = browser.request("GET", "/cases/" + "0" * 32, headers=html)
    assert denied.status_code == 403 and "You do not have access" in denied.text
    assert browser.request("GET", "/cases/" + "0" * 32).json()["detail"].startswith("Open this case")
    assert browser.request("PATCH", "/api/cases/" + "0" * 32, json={}, headers=html).headers["content-type"].startswith("application/json")


def test_case_page_shows_progress_checks_and_a_plain_language_summary(browser, stack):
    case = started(browser, stack, scenario("gst-fail"))
    stack.run_to(8.5)
    page = browser.request("GET", "/cases/" + case).text
    # A merchant is told in plain words; an operator sees the working headline and the explained reason.
    assert "Our review team is looking at your application" in page and "3 of 4 passed" in page
    assert "The GST check failed" in page and "This GST registration has been cancelled." in page
    assert "Needs an operator decision" not in page
    from test_hardening import login
    login(browser, stack.path)
    assert "Needs an operator decision" in browser.request("GET", "/cases/" + case).text
    assert page.count('class="check"') == 4 and "Verification at a glance" in page
    assert "Failed" in page and "GST_REGISTRATION_CANCELLED" in page
    # The chips use friendly words, so the machine-status markup in the attempts table is not duplicated.
    assert page.count(">SENT<") == 0 and page.count('a-FAIL">FAIL<') == 1


def test_desk_rows_carry_filter_data_and_check_chips(browser, stack, app):
    started(browser, stack, scenario("happy"))
    browser.create(scenario("gst-fail"))
    stack.sync()
    stack.run_to(9)
    op = operator_browser(app)
    try:
        desk = op.request("GET", "/desk").text
        assert 'data-queue="operator"' in desk and 'data-state="IN_REVIEW"' in desk and "Needs you" in desk
        assert 'class="chip chip-ok"' in desk and 'class="chip chip-bad"' in desk  # passed and failed checks at a glance
        assert 'data-filter="operator"' in desk and "Refreshes every 10 s" in desk
    finally:
        op.close()


def test_zero_denominators_inside_a_non_empty_cohort_give_no_rate_not_zero(conn):
    """One case, still verifying: nothing is approved or rejected, no attempt has been answered."""
    timeline(conn, "a", 0.0, [(0, "VERIFYING")], "VERIFYING")
    result = metrics.compute(conn, now=5.0)
    assert result["cohort"] == 1
    assert result["outcomes"]["approval_rate"] == 0.0  # 0 of 1 is a real zero
    assert result["outcomes"]["decided_approval_share"] is None  # 0 of 0 decided is no rate at all
    assert result["reliability"]["timeout_rate"] is None  # nothing answered or timed out yet
    assert result["durations"]["submission_to_approval"] == dict(n=0, mean=None, median=None, p90=None, max=None)
    assert result["exceptions"]["rate"] == 0.0 and result["exceptions"]["cases"] == 0
    assert result["time_in_state"]["VERIFYING"]["share"] == 1.0  # the only non-final time there is


def test_accepted_referral_is_shown_as_qualified_not_as_a_failed_check(browser, stack, app):
    """Found in the browser run: an accepted RISK referral used to leave Verification marked 'attention'."""
    from app.review import REASONS
    case = started(browser, stack, scenario("risk-referral"))
    stack.run_to(8.5)
    op = operator_browser(app)
    try:
        issue = active(stack.path, case)[0]
        assert op.request("POST", f"/api/cases/{case}/issues/{issue['id']}/decisions",
                          json={"action": "ACCEPT_REFERRAL", "version": issue["version"], "reason": REASONS["ACCEPT_REFERRAL"][0]}).status_code == 200
        stack.tick()
        page = op.request("GET", "/cases/" + case).text
    finally:
        op.close()
    assert "4 of 4 qualified, 1 accepted by an operator" in page
    assert "Referred for review · accepted by an operator" in page
    assert "3 passed outright and 1 accepted by an operator" in page
    import re
    # The progress track marks Verification as done, not as needing attention.
    verification = re.search(r'<li class="step" data-status="(\w+)">(?:(?!</li>).)*?Verification', page, re.S)
    assert verification and verification.group(1) == "done"
