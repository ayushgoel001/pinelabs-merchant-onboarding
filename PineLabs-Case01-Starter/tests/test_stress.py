"""M4-B sustained concurrency with real processes.

Test 1 (the specified run): a real web process, a real worker and the simulated providers, plus 20 client threads
that each send one signed callback every second at the same instant (20 concurrent callbacks per second), a
submitter adding new cases, and the operator desk polled at 5 Hz, for 30 seconds. Zero 5xx, zero lock errors, no
stranded cases, no duplicate approvals or MIDs and consistent receipts.

Test 2 (overload): the same system with the 20 threads sending as fast as they can for 10 seconds. That exceeds what
one SQLite writer can commit, so deadlines may expire and cases may escalate to an operator. The requirement is
safety: the effects stay exactly-once and every record stays consistent.

Set STRESS_SECONDS to shorten or lengthen test 1 (default 30).
"""
import os
import random
import threading
import time
from collections import Counter
from uuid import uuid4

import httpx

from app import fixtures
from conftest import check_invariants
from procs import Live, rows, scalar, wait_for

SECONDS = float(os.environ.get("STRESS_SECONDS", "30"))
REPLAYERS = 20


def load_body(number):
    identity = fixtures.load_identity(number)
    return dict(submission_key=str(uuid4()), merchant=identity, documents=fixtures.documents(identity))


def drive(live, tmp_path, seconds, paced):
    """Run the load. Returns (status counter, client-side errors, number of cases submitted)."""
    statuses, errors, stop = Counter(), [], threading.Event()
    lock = threading.Lock()
    pool = {"items": []}
    operator = live.operator()

    def record(label, code):
        with lock:
            statuses[(label, code // 100)] += 1

    def refresh_pool():
        while not stop.is_set():
            try:
                pool["items"] = rows(tmp_path / "simulator.db", """SELECT provider,body,signature FROM sim_deliveries
                                     WHERE status='DELIVERED' ORDER BY id DESC LIMIT 200""")
            except Exception as exc:
                errors.append(("pool", repr(exc)))
            stop.wait(0.5)

    def replayer(n, begin):
        rnd = random.Random(n)
        client = httpx.Client(base_url=live.origin, timeout=15, trust_env=False)
        tick = 0
        try:
            while not stop.is_set():
                if paced:  # every thread fires at the same instant, once per second
                    tick += 1
                    wait = begin + tick - time.monotonic()
                    if wait > 0 and stop.wait(wait):
                        break
                items = pool["items"]
                if not items:
                    time.sleep(0.05)
                    continue
                item = rnd.choice(items)
                body, signature = item["body"].encode(), item["signature"]
                roll = rnd.random()
                if roll < 0.1:
                    body = body.replace(b'"outcome":"PASS"', b'"outcome":"FAIL"')  # tampered body, genuine old signature
                elif roll < 0.15:
                    signature = "sha256=" + "0" * 64
                try:
                    response = client.post(f"/callbacks/{item['provider']}", content=body,
                                           headers={"Content-Type": "application/json", "X-Signature": signature})
                    record("callback", response.status_code)
                except httpx.HTTPError as exc:
                    errors.append(("callback", repr(exc)))
        finally:
            client.close()

    def poller():
        while not stop.is_set():
            started = time.monotonic()
            for path in ("/desk", "/health"):
                try:
                    record(path, operator.get(path).status_code)
                except httpx.HTTPError as exc:
                    errors.append((path, repr(exc)))
            stop.wait(max(0, 0.2 - (time.monotonic() - started)))  # 5 Hz

    submitted = []

    def submitter():
        number = 100
        client = httpx.Client(base_url=live.origin, headers={"Origin": live.origin}, timeout=15, trust_env=False)
        client.get("/")
        try:
            while not stop.is_set() and number < 200:
                response = client.post("/api/cases", json=load_body(number))
                record("submit", response.status_code)
                if response.status_code == 201:
                    submitted.append(response.json()["case_id"])
                number += 1
                stop.wait(0.4)
        finally:
            client.close()

    begin = time.monotonic() + 1.0
    threads = [threading.Thread(target=refresh_pool), threading.Thread(target=poller), threading.Thread(target=submitter)]
    threads += [threading.Thread(target=replayer, args=(n, begin)) for n in range(REPLAYERS)]
    for thread in threads:
        thread.start()
    time.sleep(seconds)
    stop.set()
    for thread in threads:
        thread.join(40)
    assert not any(thread.is_alive() for thread in threads)
    return statuses, errors


def assert_exactly_once_and_consistent(live, tmp_path):
    total = scalar(live.path, "SELECT count(*) FROM applications")
    issuer = scalar(tmp_path / "simulator.db", "SELECT count(*) FROM sim_issuances")
    approvals = scalar(live.path, "SELECT count(*) FROM approvals")
    assert approvals == issuer == scalar(live.path, "SELECT count(*) FROM merchants")  # one approval, issuer MID, local MID
    assert approvals == scalar(live.path, "SELECT count(*) FROM applications WHERE state='ACTIVE'")
    assert scalar(live.path, "SELECT count(*) FROM jobs WHERE status IN ('LEASED','READY','HELD')") == 0  # nothing lost
    assert scalar(tmp_path / "simulator.db", "SELECT count(*) FROM (SELECT operation_key FROM sim_issuances GROUP BY operation_key HAVING count(*)>1)") == 0
    assert scalar(live.path, """SELECT count(*) FROM (SELECT attempt_id FROM callback_inbox WHERE disposition='APPLIED'
                               GROUP BY attempt_id HAVING count(*)>1)""") == 0  # every attempt settled at most once
    check_invariants(live.path)  # receipts, claims, stranded cases, foreign keys
    return total, approvals


def test_thirty_seconds_of_concurrent_callbacks_desk_polling_and_new_cases(tmp_path):
    live = Live(tmp_path)
    try:
        live.start_worker()
        statuses, errors = drive(live, tmp_path, SECONDS, paced=True)
        wait_for(lambda: scalar(live.path, """SELECT count(*) FROM applications WHERE state NOT IN
                 ('ACTIVE','REJECTED','IN_REVIEW','NEEDS_INFO')""") == 0, 60, message="in-flight cases to settle")
        total, approvals = assert_exactly_once_and_consistent(live, tmp_path)
        print(f"\nstress: {total} cases, {approvals} approvals, statuses {dict(statuses)}")
        assert errors == [], errors[:5]
        assert {key: n for key, n in statuses.items() if key[1] >= 5} == {}  # zero 5xx: callbacks, desk, health, submit
        assert statuses[("callback", 2)] > SECONDS * REPLAYERS * 0.6 and statuses[("callback", 4)] > 0
        assert statuses[("/desk", 2)] >= SECONDS * 3.5  # polled at about 5 Hz without a miss
        unfinished = rows(live.path, """SELECT a.state,group_concat(DISTINCT r.cause) AS causes,count(DISTINCT a.id) AS n
            FROM applications a LEFT JOIN review_items r ON r.case_id=a.id AND r.status<>'RESOLVED'
            WHERE a.state<>'ACTIVE' GROUP BY a.state""")
        assert total >= min(25, SECONDS) and approvals == total, unfinished  # every submitted case finished
        for text in (live.web_text(), live.worker_log()):
            assert "database is locked" not in text and "database busy" not in text
            assert "Traceback" not in text and "failed:" not in text
        assert scalar(live.path, "SELECT value FROM settings WHERE name='worker_error'") is None
        assert scalar(live.path, "SELECT count(*) FROM jobs WHERE status='DEAD'") == 0
        replayed = scalar(live.path, "SELECT coalesce(sum(receipts-1),0) FROM callback_inbox")
        assert replayed > 100 and scalar(live.path, "SELECT count(*) FROM callback_inbox WHERE disposition='APPLIED'") >= total * 4
        # Audit noise from replays is capped per event, however many arrive.
        assert scalar(live.path, "SELECT max(n) FROM (SELECT count(*) n FROM events WHERE event='CALLBACK_DUPLICATE' GROUP BY case_id)") <= 4 * 5
    finally:
        live.close()


def test_overload_degrades_to_operator_review_never_to_wrong_effects(tmp_path):
    live = Live(tmp_path)
    try:
        live.start_worker()
        statuses, errors = drive(live, tmp_path, 10, paced=False)
        wait_for(lambda: scalar(live.path, """SELECT count(*) FROM applications WHERE state NOT IN
                 ('ACTIVE','REJECTED','IN_REVIEW','NEEDS_INFO')""") == 0, 90, message="in-flight cases to settle")
        total, approvals = assert_exactly_once_and_consistent(live, tmp_path)
        escalated = scalar(live.path, "SELECT count(*) FROM applications WHERE state='IN_REVIEW'")
        print(f"\noverload: {total} cases, {approvals} approved, {escalated} escalated, statuses {dict(statuses)}")
        assert total >= 3 and approvals + escalated == total  # each case ended approved or visibly held, never lost
        assert errors == [], errors[:5]
        # Any case that did not finish says why: its attempts timed out under load and an operator must act.
        causes = rows(live.path, "SELECT DISTINCT cause FROM review_items WHERE status<>'RESOLVED'")
        assert {c["cause"] for c in causes} <= {"CHECK_EXHAUSTED"}
    finally:
        live.close()
