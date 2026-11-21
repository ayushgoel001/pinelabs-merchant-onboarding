"""Deterministic local PAN, GST, BANK and RISK providers.

They run inside the web process for setup convenience but keep their own SQLite file and talk to the
onboarding service only over HTTP: requests arrive at /sim/{provider}/requests and results leave as signed
POSTs to /callbacks/{provider}. Each callback body is signed when the request is accepted, so the delivery
loop only moves bytes and never needs a provider secret.
"""
import hashlib
import hmac
import json
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .db import PROVIDERS, connect, packed, write_tx
from .extraction import StrictModel
from .fixtures import MID_FAULTS, behaviour, provider_key

MAX_TRIES = 5
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sim_requests (
       provider TEXT NOT NULL, attempt_id TEXT NOT NULL, inputs_digest TEXT NOT NULL, key_token TEXT NOT NULL,
       ordinal INTEGER NOT NULL, reference TEXT NOT NULL UNIQUE, behaviour TEXT NOT NULL, accepted_at REAL NOT NULL,
       PRIMARY KEY(provider,attempt_id))""",
    """CREATE TABLE IF NOT EXISTS sim_deliveries (
       id INTEGER PRIMARY KEY, provider TEXT NOT NULL, attempt_id TEXT NOT NULL, event_id TEXT NOT NULL,
       kind TEXT NOT NULL, body TEXT NOT NULL, signature TEXT NOT NULL, due_at REAL NOT NULL,
       status TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0, last_status INTEGER, disposition TEXT,
       delivered_at REAL)""",
    """CREATE INDEX IF NOT EXISTS sim_due ON sim_deliveries(status,due_at)""",
    """CREATE TABLE IF NOT EXISTS sim_mid_requests (
       operation_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL, calls INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sim_issuances (
       operation_key TEXT PRIMARY KEY REFERENCES sim_mid_requests, mid TEXT NOT NULL UNIQUE, issued_at REAL NOT NULL)""",
)


class ProviderRequest(StrictModel):
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    inputs: dict[str, str]


class IssuerRequest(StrictModel):
    case_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    approval_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_token: str = Field(pattern=r"^SYNPAN-[0-9]{4}$")


class Release(StrictModel):
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class Replay(StrictModel):
    event_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    mode: Literal["identical", "new-event"]


class Simulator:
    def __init__(self, run_dir, secrets, api_key, timing):
        self.path = Path(run_dir) / "simulator.db"
        self.secrets, self.api_key, self.timing = secrets, api_key, timing
        with closing(connect(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        with write_tx(self.path) as conn:
            for ddl in SCHEMA:
                conn.execute(ddl)
            # A delivery interrupted mid-send is retried; the onboarding inbox absorbs any duplicate.
            conn.execute("UPDATE sim_deliveries SET status='PENDING' WHERE status='SENDING'")

    def authorized(self, header):
        return hmac.compare_digest((header or "").encode(), ("Bearer " + self.api_key).encode())

    # ------------------------------------------------------------ provider API

    def accept(self, provider, request, now):
        digest = hashlib.sha256(packed(request.inputs).encode()).hexdigest()
        with write_tx(self.path) as conn:
            seen = conn.execute("SELECT * FROM sim_requests WHERE provider=? AND attempt_id=?",
                                (provider, request.attempt_id)).fetchone()
            if seen:
                if seen["inputs_digest"] != digest:
                    return 409, {"detail": "Idempotency key reused with different inputs."}
                return 200, {"reference": seen["reference"], "status": "ACCEPTED", "replayed": True}
            try:
                key = provider_key(provider, request.inputs)
            except KeyError:
                return 422, {"detail": "Inputs do not match this provider's contract."}
            ordinal = conn.execute("SELECT count(*) FROM sim_requests WHERE provider=? AND key_token=?",
                                   (provider, key)).fetchone()[0]
            plan = behaviour(provider, request.inputs, ordinal)
            reference = f"{provider}-{uuid4().hex[:12].upper()}"
            conn.execute("INSERT INTO sim_requests VALUES (?,?,?,?,?,?,?,?)",
                         (provider, request.attempt_id, digest, key, ordinal, reference, packed(plan), now))
            self.schedule(conn, provider, request.attempt_id, reference, plan, now)
        return 202, {"reference": reference, "status": "ACCEPTED"}

    def issue_mid(self, key, request, now):
        if key != "mid/" + request.case_id:
            return 409, {"detail": "Issuance key does not match application."}
        fingerprint = hashlib.sha256(packed(request.model_dump()).encode()).hexdigest()
        with write_tx(self.path) as conn:
            previous = conn.execute("SELECT * FROM sim_mid_requests WHERE operation_key=?", (key,)).fetchone()
            if previous and previous["request_digest"] != fingerprint:
                return 409, {"detail": "Issuance key belongs to a different request."}
            call = previous["calls"] + 1 if previous else 1
            conn.execute("""INSERT INTO sim_mid_requests VALUES (?,?,1) ON CONFLICT(operation_key)
                         DO UPDATE SET calls=calls+1""", (key, fingerprint))
            issued = conn.execute("SELECT mid FROM sim_issuances WHERE operation_key=?", (key,)).fetchone()
            if issued:
                return 200, dict(case_id=request.case_id, approval_digest=request.approval_digest, mid=issued[0])
            faults = MID_FAULTS.get(request.fixture_token, ())
            fault = faults[call - 1] if call <= len(faults) else None
            if fault == "BEFORE_COMMIT":
                return 503, {"code": "NOT_ISSUED"}
            mid = "SYNMID-" + uuid4().hex[:16].upper()
            conn.execute("INSERT INTO sim_issuances VALUES (?,?,?)", (key, mid, now))
        # Commit the external effect independently before simulating an upstream acknowledgement loss.
        if fault == "LOSE_ACK":
            return 504, {"code": "ACK_UNAVAILABLE"}
        return 201, dict(case_id=request.case_id, approval_digest=request.approval_digest, mid=mid)

    def issuance_count(self, case):
        with closing(connect(self.path)) as conn:
            return conn.execute("SELECT count(*) FROM sim_issuances WHERE operation_key=?", ("mid/" + case,)).fetchone()[0]

    def schedule(self, conn, provider, attempt_id, reference, plan, now):
        faults = set(plan.get("faults", ()))
        if "SILENT" in faults:
            return
        scale = self.timing["delay_scale"]
        due = now + (1.5 * self.timing["attempt_timeout_s"] if "LATE" in faults else plan["delay"] * scale)
        gap = 0.5 * scale
        event_id = uuid4().hex
        message = dict(event_id=event_id, attempt_id=attempt_id, reference=reference, provider=provider,
                       outcome=plan["outcome"], reason_code=plan["reason"],
                       registered_name=plan.get("registered_name"), issued_at=due)
        status = "HELD" if "HOLD" in faults else "PENDING"
        self.queue(conn, provider, attempt_id, event_id, "PRIMARY", message, due, status)
        if "DUPLICATE" in faults:
            self.queue(conn, provider, attempt_id, event_id, "DUPLICATE", message, due + gap, status)
        if "SECOND_EVENT" in faults:
            second = uuid4().hex
            self.queue(conn, provider, attempt_id, second, "SECOND_EVENT", {**message, "event_id": second},
                       due + 2 * gap, status)
        if "CONFLICT" in faults:
            tampered = {**message, "outcome": "FAIL", "reason_code": "TAMPERED_REUSE"}
            self.queue(conn, provider, attempt_id, event_id, "CONFLICT", tampered, due + 3 * gap, status)

    def queue(self, conn, provider, attempt_id, event_id, kind, message, due, status):
        body = packed({k: v for k, v in message.items() if v is not None})
        signature = "sha256=" + hmac.new(self.secrets[provider].encode(), body.encode(), hashlib.sha256).hexdigest()
        conn.execute("""INSERT INTO sim_deliveries(provider,attempt_id,event_id,kind,body,signature,due_at,status)
                     VALUES (?,?,?,?,?,?,?,?)""", (provider, attempt_id, event_id, kind, body, signature, due, status))

    # ------------------------------------------------------------ outbound callbacks

    def deliver_due(self, now, post, limit=50):
        with closing(connect(self.path)) as conn:
            due = conn.execute("""SELECT * FROM sim_deliveries WHERE status='PENDING' AND due_at<=?
                               ORDER BY due_at,id LIMIT ?""", (now, limit)).fetchall()
        delivered = 0
        for row in due:
            with write_tx(self.path) as conn:
                if conn.execute("UPDATE sim_deliveries SET status='SENDING' WHERE id=? AND status='PENDING'",
                                (row["id"],)).rowcount != 1:
                    continue
            try:
                response = post(f"/callbacks/{row['provider']}", row["body"].encode(),
                                {"Content-Type": "application/json", "X-Signature": row["signature"]},
                                self.timing["http_timeout_s"])
            except (httpx.TransportError, OSError):
                response = None
            with write_tx(self.path) as conn:
                if response is not None and response.status_code < 500:
                    try:
                        disposition = response.json().get("disposition")
                    except ValueError:
                        disposition = None
                    status = "DELIVERED" if response.status_code < 300 else "REJECTED"
                    conn.execute("""UPDATE sim_deliveries SET status=?,tries=tries+1,last_status=?,disposition=?,
                                 delivered_at=? WHERE id=?""",
                                 (status, response.status_code, disposition, now, row["id"]))
                    delivered += 1
                else:
                    retry = row["tries"] + 1 < MAX_TRIES
                    conn.execute("""UPDATE sim_deliveries SET status=?,tries=tries+1,last_status=?,due_at=? WHERE id=?""",
                                 ("PENDING" if retry else "FAILED",
                                  response.status_code if response is not None else None,
                                  now + max(0.2, self.timing["dispatch_retry_s"]), row["id"]))
        return delivered

    # ------------------------------------------------------------ demo controls

    def release(self, attempt_id, now):
        with write_tx(self.path) as conn:
            return conn.execute("UPDATE sim_deliveries SET status='PENDING',due_at=? WHERE attempt_id=? AND status='HELD'",
                                (now, attempt_id)).rowcount

    def replay(self, event_id, mode, now):
        with write_tx(self.path) as conn:
            original = conn.execute("""SELECT * FROM sim_deliveries WHERE event_id=? AND kind IN ('PRIMARY','SECOND_EVENT')
                                    ORDER BY id LIMIT 1""", (event_id,)).fetchone()
            if original is None:
                return 0
            if mode == "identical":
                conn.execute("""INSERT INTO sim_deliveries(provider,attempt_id,event_id,kind,body,signature,due_at,status)
                             VALUES (?,?,?,'REPLAY',?,?,?,'PENDING')""",
                             (original["provider"], original["attempt_id"], event_id, original["body"],
                              original["signature"], now))
            else:
                message = json.loads(original["body"])
                message["event_id"] = uuid4().hex
                self.queue(conn, original["provider"], original["attempt_id"], message["event_id"], "REPLAY_NEW",
                           message, now, "PENDING")
            return 1

    def console(self, attempt_ids):
        """Provider-side view for the demo panel: held deliveries and sent event IDs per attempt."""
        if not attempt_ids:
            return {}
        marks = ",".join("?" * len(attempt_ids))
        with closing(connect(self.path)) as conn:
            rows = conn.execute(f"""SELECT attempt_id,event_id,kind,status FROM sim_deliveries
                                WHERE attempt_id IN ({marks}) ORDER BY id""", list(attempt_ids)).fetchall()
        view = {}
        for row in rows:
            view.setdefault(row["attempt_id"], []).append(dict(row))
        return view

    # ------------------------------------------------------------ background delivery loop (web process)

    def start(self, origin, stop):
        def loop():
            with httpx.Client(base_url=origin, trust_env=False) as client:
                def post(path, body, headers, timeout):
                    return client.post(path, content=body, headers=headers, timeout=timeout)
                while not stop.wait(min(0.25, self.timing["tick_s"])):
                    try:
                        self.deliver_due(time.time(), post)
                    except Exception:  # keep the provider loop alive; the next tick retries
                        time.sleep(0.5)
        thread = threading.Thread(target=loop, name="provider-callbacks", daemon=True)
        thread.start()
        return thread


def router(require_operator):
    routes = APIRouter()

    @routes.post("/sim/{provider}/requests")
    def provider_request(provider: str, body: ProviderRequest, request: Request):
        simulator = request.app.state.simulator
        if provider not in PROVIDERS:
            return JSONResponse({"detail": "Unknown provider."}, status_code=404)
        if not simulator.authorized(request.headers.get("authorization")):
            return JSONResponse({"detail": "Provider client authentication failed."}, status_code=401)
        status, content = simulator.accept(provider, body, request.app.state.clock())
        return JSONResponse(content, status_code=status)

    @routes.post("/sim/mids")
    def issue_mid(body: IssuerRequest, request: Request):
        simulator = request.app.state.simulator
        if not simulator.authorized(request.headers.get("authorization")):
            return JSONResponse({"detail": "Issuer client authentication required."}, status_code=401)
        status, content = simulator.issue_mid(request.headers.get("idempotency-key"), body, request.app.state.clock())
        return JSONResponse(content, status_code=status)

    @routes.post("/sim/control/release")
    def release(body: Release, request: Request):
        require_operator(request, demo=True)
        count = request.app.state.simulator.release(body.attempt_id, request.app.state.clock())
        return {"released": count}

    @routes.post("/sim/control/replay")
    def replay(body: Replay, request: Request):
        require_operator(request, demo=True)
        count = request.app.state.simulator.replay(body.event_id, body.mode, request.app.state.clock())
        return {"queued": count}

    return routes
