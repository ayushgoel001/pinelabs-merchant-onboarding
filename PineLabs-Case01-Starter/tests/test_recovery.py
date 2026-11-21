"""M4-B crash recovery with real OS processes: kill the worker at exact workflow boundaries and restart it.

Fast timing: attempt timeout 2 s, lease 3 s, tick 0.1 s, backoff 0.2 s / 0.5 s. A crash point exits the worker with
code 70 and no cleanup (app/faults.py). Each scenario is compared with an uninterrupted run of the same case.
"""
import json
import os
import subprocess
import sys
import threading
import time
from copy import deepcopy
from uuid import uuid4

import pytest

from app import db
from app.faults import EXIT_CODE
from app.run import EXIT_WORKER_FAILED, RestartBudget, port_open, supervise
from conftest import check_invariants
from procs import Live, free_port, rows, scalar, wait_for
from test_verification import scenario

LEASE_S, TICK_S = 3.0, 0.1
FAULTS = {"PINELABS_TEST_FAULTS": "1"}


def run_case(folder, crash=None):
    """Submit one application, optionally crash the worker at a point, restart it, and return the evidence."""
    live = Live(folder)
    try:
        body = scenario("happy")
        case = live.submit(body)
        restarted_at = None
        if crash:
            worker = live.start_worker({**FAULTS, "CRASH_AT": crash})
            wait_for(lambda: worker.poll() is not None, 30, message="the worker to hit its crash point")
            assert worker.returncode == EXIT_CODE
            assert live.state(case) != "ACTIVE"  # it really died before finishing the journey
            snapshot = dict(
                leased=scalar(live.path, "SELECT count(*) FROM jobs WHERE status='LEASED'"),
                issuer=scalar(folder / "simulator.db", "SELECT count(*) FROM sim_issuances"),
                requests=scalar(folder / "simulator.db", "SELECT count(*) FROM sim_requests"))
            restarted_at = time.monotonic()
            live.start_worker()
        else:
            snapshot = None
            live.start_worker()
        finished = wait_for(lambda: live.state(case) == "ACTIVE", 40, message="the case to become ACTIVE")
        elapsed = finished - restarted_at if restarted_at else None
        time.sleep(1.0)  # let any straggling duplicate work show up before counting
        result = dict(
            counts=live.counts(case), elapsed=elapsed, snapshot=snapshot,
            merchants=scalar(live.path, "SELECT count(*) FROM merchants"),
            mid_jobs=scalar(live.path, "SELECT count(*) FROM jobs WHERE kind='ISSUE_MID'"),
            active_entries=scalar(live.path, "SELECT count(*) FROM events WHERE event='STATE_ENTERED' AND detail='ACTIVE'"),
            approved_entries=scalar(live.path, "SELECT count(*) FROM events WHERE event='STATE_ENTERED' AND detail='APPROVED_PENDING_MID'"),
            attempts=rows(live.path, "SELECT kind,generation,status FROM attempts WHERE status='PASS' ORDER BY kind"),
            duplicate_provider_requests=scalar(folder / "simulator.db",
                                               "SELECT count(*) FROM (SELECT attempt_id FROM sim_requests GROUP BY provider,attempt_id HAVING count(*)>1)"),
            worker_log=live.worker_log(), pre_restart_log=live.worker_log(0))
        check_invariants(live.path)
        return result
    finally:
        live.close()


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    return run_case(tmp_path_factory.mktemp("baseline"))


def test_uninterrupted_run_is_the_reference(baseline):
    assert baseline["counts"] == (1, 1, 1) and baseline["merchants"] == 1 and baseline["mid_jobs"] == 1
    assert baseline["active_entries"] == baseline["approved_entries"] == 1
    assert [a["kind"] for a in baseline["attempts"]] == ["BANK", "GST", "PAN", "RISK"]


@pytest.mark.parametrize("point", [
    "after_claim:DISPATCH", "after_remote_effect:DISPATCH", "after_claim:ISSUE_MID", "after_remote_effect:ISSUE_MID"])
def test_worker_killed_at_each_boundary_recovers_to_the_uninterrupted_outcome(tmp_path, baseline, point):
    crashed = run_case(tmp_path, point)
    # Exactly the effect counts of the uninterrupted run: one approval, one issuer MID, one local MID.
    assert crashed["counts"] == baseline["counts"] == (1, 1, 1)
    assert crashed["merchants"] == baseline["merchants"] and crashed["mid_jobs"] == baseline["mid_jobs"] == 1
    assert crashed["active_entries"] == crashed["approved_entries"] == 1
    assert crashed["duplicate_provider_requests"] == 0  # a re-sent request reuses its idempotency key and reference
    # Every required check still has a passing attempt, and recovery completes within a lease, a tick and the
    # time the retry rules legitimately need (an expired attempt waits its deadline plus one backoff).
    assert {a["kind"] for a in crashed["attempts"]} == {"PAN", "GST", "BANK", "RISK"}
    print(f"recovery after {point}: {crashed['elapsed']:.1f} s (budget {LEASE_S + TICK_S + 5.5:.1f} s)")
    budget = LEASE_S + TICK_S + 2.0 + 0.5 + 3.0
    assert crashed["elapsed"] <= budget, f"recovery took {crashed['elapsed']:.1f} s, budget {budget:.1f} s"
    kind = point.split(":")[1]
    # The restarted worker found the abandoned lease and said so.
    assert "reclaimed 1 expired" in crashed["worker_log"]
    snapshot = crashed["snapshot"]
    assert snapshot["leased"] >= 1  # a job was genuinely left leased by the dead worker
    if point == "after_remote_effect:ISSUE_MID":
        assert snapshot["issuer"] == 1  # the issuer had already committed the MID when the worker died
    if point == "after_claim:ISSUE_MID":
        assert snapshot["issuer"] == 0  # nothing had reached the issuer yet
    if kind == "DISPATCH" and point.startswith("after_remote_effect"):
        assert snapshot["requests"] >= 1


def test_unfinished_work_survives_killing_every_process(tmp_path):
    """Kill the web process and the worker together mid-journey, bring both back on the same database."""
    live = Live(tmp_path)
    try:
        case = live.submit(scenario("happy"))
        live.start_worker()
        wait_for(lambda: scalar(live.path, "SELECT count(*) FROM callback_inbox WHERE case_id=?", case) >= 1, 20,
                 message="a first callback")
        from app.run import stop_tree
        stop_tree(live.worker)
        stop_tree(live.web)
        assert live.state(case) != "ACTIVE"
        before = scalar(live.path, "SELECT count(*) FROM attempts WHERE case_id=?", case)
        # Same run directory, new processes.
        live.web = subprocess.Popen([sys.executable, "-m", "app.web", "--run-dir", str(tmp_path), "--port", str(live.port)],
                                    stdout=live.web_log, stderr=subprocess.STDOUT)
        wait_for(lambda: live.client.get("/health").status_code == 200, message="the restarted web process")
        live.start_worker()
        wait_for(lambda: live.state(case) == "ACTIVE", 40, message="the case to finish after a full restart")
        assert live.counts(case) == (1, 1, 1)
        assert scalar(live.path, "SELECT count(*) FROM attempts WHERE case_id=? AND generation>1", case) <= before
        check_invariants(live.path)
    finally:
        live.close()


# ---------------------------------------------------------------- supervision, retry limits and the launcher

def test_launcher_stops_after_the_restart_budget_and_frees_its_port(tmp_path):
    db.initialize(tmp_path, "fast")
    port = free_port()
    stop = threading.Event()
    result = {}
    launcher = threading.Thread(target=lambda: result.update(code=supervise(
        tmp_path, port, stop, budget=RestartBudget(limit=2, window=60),
        worker_command=[sys.executable, "-c", "import sys; sys.exit(7)"])))
    started = time.monotonic()
    launcher.start()
    launcher.join(40)
    assert not launcher.is_alive() and result["code"] == EXIT_WORKER_FAILED  # gave up instead of serving a dead pipeline
    assert not port_open(port)  # and its web process is gone, immediately
    assert time.monotonic() - started < 40


def test_shutdown_releases_the_port_and_leaves_no_process_behind(tmp_path):
    """Regression for the intermittent restart test: the Windows virtual-environment python.exe is a launcher that
    starts the real interpreter. Stopping only the launcher returned while the server was still listening."""
    db.initialize(tmp_path, "fast")
    for _ in range(4):
        port = free_port()
        stop = threading.Event()
        launcher = threading.Thread(target=supervise, args=(tmp_path, port, stop))
        launcher.start()
        wait_for(lambda: port_open(port), 20, message="the web server")
        time.sleep(0.5)
        stop.set()
        launcher.join(20)
        assert not launcher.is_alive()
        assert not port_open(port)  # checked immediately after supervise returned, with no waiting
    if os.name == "nt":
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{tmp_path.name}*' }}).Count"],
                             capture_output=True, text=True).stdout.strip()
        assert out in ("", "0")  # nothing from this run directory is still alive


def test_dead_lettered_issuance_survives_a_restart_and_requeue_finishes_it(tmp_path):
    """Retry limits, dead letter and operator recovery across real processes and a restart in between."""
    live = Live(tmp_path)
    try:
        from app.fixtures import SCENARIOS
        body = scenario("mid-dead-letter")
        case = live.submit(body)
        live.start_worker()
        wait_for(lambda: scalar(live.path, "SELECT status FROM jobs WHERE case_id=?", case) == "DEAD", 40,
                 message="the issuance job to be dead-lettered")
        assert live.state(case) == "PROVISIONING_FAILED" and live.counts(case) == (1, 0, 0)
        claims = scalar(live.path, "SELECT claims FROM jobs WHERE case_id=?", case)
        assert claims == 3  # the retry limit, never exceeded
        from app.run import stop_tree
        stop_tree(live.worker)
        live.start_worker()  # a restarted worker must not touch a dead-lettered job
        time.sleep(1.5)
        assert scalar(live.path, "SELECT claims FROM jobs WHERE case_id=?", case) == 3
        operator = live.operator()
        job = rows(live.path, "SELECT requeues FROM jobs WHERE case_id=?", case)[0]
        from app.review import REASONS
        requeue = operator.post(f"/api/cases/{case}/issuance/requeue",
                                json={"cycle": job["requeues"], "reason": REASONS["REQUEUE_MID"][0]})
        assert requeue.status_code == 200
        wait_for(lambda: live.state(case) == "ACTIVE", 30, message="recovery after the requeue")
        assert live.counts(case) == (1, 1, 1)
        check_invariants(live.path)
    finally:
        live.close()
