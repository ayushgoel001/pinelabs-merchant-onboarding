"""Deliver the approved case's single MID job across the issuer boundary."""
import json
import logging
from uuid import uuid4

import httpx
from pydantic import Field, ValidationError

from .db import packed, setting, timing, write_tx
from .extraction import StrictModel
from .faults import crash_point
from .review import REASONS
from .verification import MAX_CLAIMS
from .workflow import APPROVED, FAILED, Problem, event, transition


LOG = logging.getLogger("worker")


class IssuerReceipt(StrictModel):
    case_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    approval_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mid: str = Field(pattern=r"^SYNMID-[A-F0-9]{16}$")


class Requeue(StrictModel):
    cycle: int = Field(strict=True, ge=0)
    reason: str = Field(min_length=1, max_length=100)


def failure(conn, job, now, reason, unknown, permanent=False):
    case = job["case_id"]
    state = conn.execute("SELECT state FROM applications WHERE id=?", (case,)).fetchone()[0]
    if state == APPROVED:
        transition(conn, case, FAILED, "issuance error/unknown", at=now)
    dead = permanent or job["claims"] >= MAX_CLAIMS
    delay = timing(conn)["backoff_s"][min(max(job["claims"] - 1, 0), 1)]
    detail = ("OUTCOME_UNKNOWN · " if unknown else "ISSUER_ERROR · ") + reason
    next_state = "DEAD" if dead else "READY"
    conn.execute("""UPDATE jobs SET status=?,last_reason=?,due_at=?,lease_token=NULL,lease_expires=NULL WHERE id=?""",
                 (next_state, detail, now + delay, job["id"]))
    event(conn, case, "MID_DEAD_LETTERED" if dead else "MID_RETRY_SCHEDULED",
          f"{detail} · claim {job['claims']} of {MAX_CLAIMS}" + ("" if dead else f" · retry in {delay:g} s"), at=now)


def claim_mid(path, now):
    with write_tx(path) as conn:
        job = conn.execute("""SELECT j.*, p.receipt_digest, p.receipt FROM jobs j
            JOIN approvals p ON p.case_id=j.case_id JOIN applications a ON a.id=j.case_id
            WHERE j.kind='ISSUE_MID' AND a.state IN ('APPROVED_PENDING_MID','PROVISIONING_FAILED')
            AND ((j.status='READY' AND j.due_at<=?) OR (j.status='LEASED' AND j.lease_expires<=?))
            ORDER BY j.due_at,j.id LIMIT 1""", (now, now)).fetchone()
        if not job:
            return None
        if job["status"] == "LEASED":
            event(conn, job["case_id"], "MID_LEASE_EXPIRED", "Issuer outcome unconfirmed on expired lease", at=now)
            LOG.warning("reclaimed 1 expired issuance lease (job %d, claim %d)", job["id"], job["claims"])
            # Even an unrecorded final claim must enter FAILED and remain visible.
            failure(conn, job, now, "previous claim lost its lease", unknown=True)
            return {"skip": True}
        token = uuid4().hex
        cfg = timing(conn)
        conn.execute("""UPDATE jobs SET status='LEASED',lease_token=?,lease_expires=?,claims=claims+1 WHERE id=?""",
                     (token, now + cfg["lease_s"], job["id"]))
        event(conn, job["case_id"], "MID_REQUESTED", f"Issuer claim {job['claims'] + 1} · cycle {job['requeues']}", at=now)
        return dict(id=job["id"], case=job["case_id"], key=job["operation_key"], token=token,
                    approval_digest=job["receipt_digest"], api_key=setting(conn, "sim_api_key"),
                    fixture=json.loads(job["receipt"])["evidence"]["merchant"]["pan"], timeout=cfg["http_timeout_s"])


def send_mid(claim, post):
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + claim["api_key"],
               "Idempotency-Key": claim["key"]}
    body = packed(dict(case_id=claim["case"], approval_digest=claim["approval_digest"], fixture_token=claim["fixture"]))
    try:
        response = post("/sim/mids", body.encode(), headers, claim["timeout"])
    except (httpx.TransportError, OSError):
        return "unknown", "issuer response unavailable", None
    if response.status_code in (200, 201):
        try:
            receipt = IssuerReceipt.model_validate_json(response.content)
        except ValidationError:
            return "unknown", "unusable issuer receipt", None
        if receipt.case_id != claim["case"] or receipt.approval_digest != claim["approval_digest"]:
            return "unknown", "issuer receipt did not match this approval", None
        return "issued", "issuer receipt matched", receipt.mid
    if response.status_code == 503:
        try:
            if response.json() == {"code": "NOT_ISSUED"}:
                return "retry", "issuer unavailable before creation", None
        except ValueError:
            pass
    if response.status_code == 429 or response.status_code >= 500:
        return "unknown", f"issuer acknowledgement unavailable (HTTP {response.status_code})", None
    return "permanent", f"issuer rejected request (HTTP {response.status_code})", None


def finish_mid(path, claim, result, now):
    outcome, reason, mid = result
    with write_tx(path) as conn:
        job = conn.execute("""SELECT * FROM jobs WHERE id=? AND status='LEASED' AND lease_token=?""",
                           (claim["id"], claim["token"])).fetchone()
        if not job:
            return "stale"
        if outcome != "issued":
            failure(conn, job, now, reason, unknown=outcome == "unknown", permanent=outcome == "permanent")
            return outcome
        if conn.execute("SELECT 1 FROM merchants WHERE mid=? AND case_id<>?", (mid, job["case_id"])).fetchone():
            failure(conn, job, now, "issuer returned an already assigned MID", unknown=True, permanent=True)
            return "conflict"
        conn.execute("INSERT INTO merchants VALUES (?,?,?)", (job["case_id"], mid, now))
        conn.execute("""UPDATE jobs SET status='DONE',lease_token=NULL,lease_expires=NULL,last_reason=? WHERE id=?""",
                     (reason, job["id"]))
        event(conn, job["case_id"], "MID_RECEIVED", f"Simulated issuer receipt · {mid}", at=now)
        transition(conn, job["case_id"], "ACTIVE", "issuer receipt", at=now)
        return "issued"


def provision_due(path, clock, post, limit=20):
    for _ in range(limit):
        claim = claim_mid(path, clock())
        if claim is None:
            break
        if not claim.get("skip"):
            crash_point(path, "after_claim", "ISSUE_MID")
            result = send_mid(claim, post)
            crash_point(path, "after_remote_effect", "ISSUE_MID")
            finish_mid(path, claim, result, clock())


def requeue_mid(path, case, command, now):
    if command.reason not in REASONS["REQUEUE_MID"]:
        raise Problem(422, "Select an issuance recovery reason.")
    with write_tx(path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE case_id=? AND kind='ISSUE_MID'", (case,)).fetchone()
        if not job:
            raise Problem(409, "This case has no issuance job.", "/cases/" + case)
        if job["requeues"] == command.cycle + 1:
            return  # a previous click already requeued this dead-letter cycle
        changed = conn.execute("""UPDATE jobs SET status='READY',claims=0,due_at=?,requeues=requeues+1,
            last_reason='Operator requeued existing key' WHERE id=? AND status='DEAD' AND requeues=?
            AND EXISTS (SELECT 1 FROM applications WHERE id=? AND state='PROVISIONING_FAILED')""",
                               (now, job["id"], command.cycle, case)).rowcount
        if changed != 1:
            raise Problem(409, "Only the current dead-lettered issuance can be requeued.", "/cases/" + case)
        event(conn, case, "MID_REQUEUED", command.reason, "operator", at=now)


def delivery_status(job):
    if job["status"] == "DEAD":
        return "DEAD_LETTERED"
    if job["status"] == "DONE":
        return "ISSUED"
    if (job["last_reason"] or "").startswith("OUTCOME_UNKNOWN"):
        return "OUTCOME_UNKNOWN"
    if job["status"] == "LEASED":
        return "ISSUING"
    return "RETRY_SCHEDULED" if job["last_reason"] else "QUEUED"
