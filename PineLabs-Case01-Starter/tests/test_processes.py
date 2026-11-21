"""Real loopback HTTP, two OS processes, shutdown and restart on the same database."""
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing

import httpx

from app import db
from app.access import issue_ticket
from app.extraction import SAMPLE_DOCS
from app.run import supervise
from app.workflow import STRANDED
from conftest import correction


def wait_until(check, timeout=15):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        try:
            if check():
                return
        except (httpx.TransportError, OSError):
            pass
        time.sleep(.1)
    raise AssertionError("Local processes failed to reach the expected state before the deadline.")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def count(path, sql, *args):
    with closing(db.connect(path)) as conn:
        return conn.execute(sql, args).fetchone()[0]


def observe_pending_before_callback(path, case, timeout=15):
    """Poll the database directly: a request is SENT and acknowledged while no callback has arrived yet."""
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        sent = count(path, "SELECT count(*) FROM attempts WHERE case_id=? AND status='SENT'", case)
        received = count(path, "SELECT count(*) FROM callback_inbox WHERE case_id=?", case)
        if sent and not received:
            return True
        if received:
            raise AssertionError("A callback arrived before a pending request was observed.")
        time.sleep(0.01)
    raise AssertionError("No request was observed in flight.")


def test_two_process_http_restart_and_clean_shutdown(tmp_path, payload, capfd):
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    path = db.initialize(tmp_path, "fast")
    with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=2, trust_env=False) as client:
        for cycle in range(2):
            started_at = time.time()
            stop = threading.Event()
            launcher = threading.Thread(target=supervise, args=(tmp_path, port, stop))
            launcher.start()
            try:
                def current_worker():
                    status = client.get("/health").json()
                    return status["worker_online"] and time.time() - status["age_seconds"] >= started_at
                wait_until(current_worker)
                if cycle == 0:
                    assert client.get("/").status_code == 200
                    invalid_id = "Z" * 5 + "0" * 4 + "Z"
                    bad = {**payload, "merchant": {**payload["merchant"], "pan": invalid_id}}
                    invalid = client.post("/api/cases", json=bad)
                    assert invalid.status_code == 422 and invalid_id not in invalid.text
                    del payload["documents"]["tax"]
                    created = client.post("/api/cases", json=payload)
                    assert created.status_code == 201
                    case = created.json()["case_id"]
                    ticket = created.json()["location"].split("#")[1]
                    redirect = client.post("/access", json={"ticket":ticket})
                    assert redirect.status_code == 303 and "#" not in redirect.headers["location"]
                page = client.get("/cases/" + case)
                assert page.status_code == 200
                assert ("NEEDS_INFO" if cycle == 0 else "ACTIVE") in page.text
                login = (tmp_path / "operator-link.txt").read_text(encoding="utf-8").split("#")[1]
                assert client.post("/access", json={"ticket":login}).status_code == 303
                desk = client.get("/desk").text
                assert "Worker online" in desk and case in desk
                if cycle == 0:
                    assert "tax.document" in desk
                    payload["documents"]["tax"] = SAMPLE_DOCS["tax"]
                    assert client.patch("/api/cases/" + case, json=correction(payload, 1)).status_code == 200
                    # Real worker -> HTTP provider request -> delayed signed HTTP callback from the web process.
                    observe_pending_before_callback(path, case)
                    wait_until(lambda: count(path, """SELECT count(*) FROM callback_inbox WHERE case_id=?
                                                    AND disposition='APPLIED'""", case) == 4)
                    wait_until(lambda: count(path, "SELECT count(*) FROM merchants WHERE case_id=?", case) == 1)
                    done = client.get("/cases/" + case).text
                    assert done.count(">APPLIED<") == 4 and "Approval receipt" in done and "SYNMID-" in done
                else:
                    # Restart: the verified outcome, receipts and references persisted on disk.
                    assert "Data revision 2" in page.text and page.text.count('a-PASS">PASS<') == 4
                    assert page.text.count(">APPLIED<") == 4
            finally:
                stop.set()
                launcher.join(10)
            assert not launcher.is_alive()
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
            with closing(db.connect(path)) as conn:
                assert not conn.execute(STRANDED).fetchall()
                assert conn.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
                stamp = conn.execute("SELECT value FROM settings WHERE name='heartbeat'").fetchone()[0]
                time.sleep(1.15)
                assert conn.execute("SELECT value FROM settings WHERE name='heartbeat'").fetchone()[0] == stamp
        captured = capfd.readouterr()
        logs = captured.out + captured.err
        assert "access POST /api/cases 422" in logs and invalid_id not in logs and ticket not in logs


def test_scheduled_work_waits_for_absent_worker_then_completes(tmp_path, payload):
    """I9 with real processes: no worker for longer than the attempt timeout. Nothing is sent, nothing
    expires, and a worker started later dispatches generation 1 and receives real callbacks."""
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    path = db.initialize(tmp_path, "fast")
    web = subprocess.Popen([sys.executable, "-m", "app.web", "--run-dir", str(tmp_path), "--port", str(port)])
    worker = None
    try:
        with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=2, trust_env=False) as client:
            wait_until(lambda: client.get("/health").status_code == 200)
            client.get("/")
            created = client.post("/api/cases", json=payload)
            assert created.status_code == 201
            case = created.json()["case_id"]
            time.sleep(3.0)  # attempt timeout is 2.0 s in the fast profile
            assert count(path, "SELECT count(*) FROM attempts WHERE case_id=? AND status='SCHEDULED'", case) == 4
            assert count(path, "SELECT count(*) FROM events WHERE event='ATTEMPT_EXPIRED'") == 0
            worker = subprocess.Popen([sys.executable, "-m", "app.worker", "--run-dir", str(tmp_path)])
            wait_until(lambda: count(path, "SELECT count(*) FROM attempts WHERE case_id=? AND status='PASS'", case) == 4)
            assert count(path, "SELECT count(*) FROM attempts WHERE case_id=? AND generation>1", case) == 0
            assert count(path, """SELECT count(*) FROM events WHERE case_id=? AND event='REQUEST_SENT'
                               AND detail LIKE '%dispatch delayed%'""", case) == 4
            assert not count(path, "SELECT count(*) FROM (" + STRANDED + ")")
    finally:
        for process in (worker, web):
            if process is not None:
                process.terminate()
                process.wait(timeout=10)


def test_stopped_worker_appears_offline(browser, app, tmp_path):
    browser.request("POST", "/access", json={"ticket": issue_ticket(app.state.database, "operator")})
    worker = subprocess.Popen([sys.executable, "-m", "app.worker", "--run-dir", str(tmp_path)])
    try:
        wait_until(lambda: browser.request("GET", "/health").json()["worker_online"])
        assert "Worker online" in browser.request("GET", "/desk").text
    finally:
        worker.terminate()
        worker.wait(timeout=5)
    wait_until(lambda: "Worker offline" in browser.request("GET", "/desk").text, timeout=11)
