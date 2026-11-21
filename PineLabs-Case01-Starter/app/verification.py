"""Provider requests, signed callbacks, deadlines and retries (milestone 2).

Attempt lifecycle, every step compare-and-set:
    SCHEDULED -> SENT -> PASS | FAIL | REFERRED | TRANSIENT | EXPIRED, or SUPERSEDED on lost authority.
SCHEDULED never expires. The deadline starts when the attempt is marked SENT, immediately before the
HTTP request leaves. Callbacks correlate by attempt ID; the provider's clock never confers authority.
"""
import hashlib
import hmac
import json
from contextlib import closing
from typing import Literal, Optional
from uuid import uuid4

import httpx
from pydantic import Field, ValidationError

import logging

from . import db
from .faults import crash_point
from .db import PROVIDERS, connect, packed, timing, write_tx
from .extraction import StrictModel, name_band, name_score
from .workflow import (MAX_AUTOMATIC, PRE, dispatch_allowed, event, lapse_pending_uploads, open_check_issue, reconcile,
                       resolve_check_issues)

MAX_CLAIMS = 3
MAX_DUPLICATE_EVENTS = 5
LOG = logging.getLogger("worker")
SETTLED = ("PASS", "FAIL", "REFERRED", "TRANSIENT")


class Callback(StrictModel):
    event_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    reference: str = Field(pattern=r"^[A-Z]{3,4}-[0-9A-F]{12}$")
    provider: Literal["PAN", "GST", "BANK", "RISK"]
    outcome: Literal["PASS", "FAIL", "REFERRED", "TRANSIENT"]
    reason_code: str = Field(pattern=r"^[A-Z_]{2,48}$")
    registered_name: Optional[str] = Field(default=None, max_length=120, pattern=r"^[A-Za-z .,&'-]+$")
    issued_at: float


def sign(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def provider_secrets(path):
    with closing(connect(path)) as conn:
        return {p: db.setting(conn, "callback_secret:" + p) for p in PROVIDERS}


def reject(conn, provider, reason, now):
    conn.execute("""INSERT INTO rejected_callbacks VALUES (?,?,1,?)
        ON CONFLICT(provider,reason) DO UPDATE SET count=count+1,last_at=excluded.last_at""",
                 (provider, reason, now))


# ---------------------------------------------------------------- inbound callbacks

def receive_callback(path, provider, raw, signature, clock, secrets):
    """Authenticate, then record and apply one delivery in a single transaction.
    Returns (HTTP status, body). Success is returned only after the transaction commits."""
    if provider not in PROVIDERS:
        return 404, {"detail": "Unknown provider."}
    expected = sign(secrets[provider], raw)
    if not signature or not hmac.compare_digest(signature.encode(), expected.encode()):
        with write_tx(path) as conn:
            reject(conn, provider, "signature", clock())
        return 401, {"detail": "Invalid callback signature."}
    try:
        message = Callback.model_validate_json(raw)
    except ValidationError:
        with write_tx(path) as conn:
            reject(conn, provider, "schema", clock())
        return 422, {"detail": "Callback does not match the provider contract."}
    if message.provider != provider:
        with write_tx(path) as conn:
            reject(conn, provider, "provider mismatch", clock())
        return 422, {"detail": "Callback provider does not match its endpoint."}
    body_digest = hashlib.sha256(raw).hexdigest()
    with write_tx(path) as conn:
        now = clock()  # server transaction time decides lateness
        prior = conn.execute("SELECT * FROM callback_inbox WHERE provider=? AND event_id=?",
                             (provider, message.event_id)).fetchone()
        if prior:
            if prior["body_digest"] == body_digest:
                conn.execute("UPDATE callback_inbox SET receipts=receipts+1 WHERE provider=? AND event_id=?",
                             (provider, message.event_id))
                if prior["receipts"] < MAX_DUPLICATE_EVENTS:  # later replays are only counted, never flood the audit
                    event(conn, prior["case_id"], "CALLBACK_DUPLICATE",
                          f"{provider} event {message.event_id[:8]} · receipt {prior['receipts'] + 1} acknowledged, "
                          "no business effect", provider.lower(), at=now)
                return 200, {"disposition": "DUPLICATE"}
            event(conn, prior["case_id"], "CALLBACK_CONFLICT",
                  f"{provider} event {message.event_id[:8]} reused with a different body · rejected",
                  provider.lower(), at=now)
            return 409, {"detail": "Event ID already recorded with different content."}
        attempt = conn.execute("""SELECT a.*, c.input_revision AS current_revision, ap.state AS case_state
            FROM attempts a JOIN checks c USING(case_id,kind) JOIN applications ap ON ap.id=a.case_id
            WHERE a.id=?""", (message.attempt_id,)).fetchone()
        if attempt is None:
            reject(conn, provider, "unknown attempt", now)
            return 404, {"detail": "Unknown attempt."}
        if attempt["kind"] != provider:
            reject(conn, provider, "provider mismatch", now)
            return 422, {"detail": "Callback provider does not match the attempt."}
        if attempt["reference"] and attempt["reference"] != message.reference:
            reject(conn, provider, "reference mismatch", now)
            return 422, {"detail": "Callback reference does not match the request."}
        disposition = classify(attempt, now)
        conn.execute("INSERT INTO callback_inbox VALUES (?,?,?,?,?,?,?,?,1)",
                     (provider, message.event_id, body_digest, attempt["id"], attempt["case_id"],
                      message.outcome, disposition, now))
        event(conn, attempt["case_id"], "CALLBACK_RECEIVED",
              f"{provider} attempt {attempt['generation']} · {message.outcome} · {disposition.replace('_', ' ').lower()}",
              provider.lower(), at=now)
        if disposition == "APPLIED":
            settle(conn, attempt, message, now)
        elif disposition == "LATE":
            expire_attempt(conn, attempt["id"], now, "reply arrived at or after the deadline")
        return 200, {"disposition": disposition}


def classify(attempt, now):
    if attempt["case_state"] not in PRE:
        return "IGNORED_DECIDED"
    if attempt["status"] == "SUPERSEDED" or attempt["input_revision"] != attempt["current_revision"]:
        return "IGNORED_SUPERSEDED"
    if attempt["status"] == "SCHEDULED":
        return "IGNORED_NOT_SENT"
    if attempt["status"] == "EXPIRED":
        return "IGNORED_EXPIRED"
    if attempt["status"] in SETTLED:
        return "IGNORED_SETTLED"
    if attempt["status"] != "SENT" or attempt["deadline"] is None:
        return "IGNORED_NOT_SENT"
    return "LATE" if now >= attempt["deadline"] else "APPLIED"


def settle(conn, attempt, message, now):
    changed = conn.execute("""UPDATE attempts SET status=?,settled_at=?,reason_code=?,registered_name=?,
        reference=coalesce(reference,?) WHERE id=? AND status='SENT'""",
                           (message.outcome, now, message.reason_code, message.registered_name,
                            message.reference, attempt["id"])).rowcount
    if changed != 1:
        raise RuntimeError("Attempt left SENT inside the callback transaction.")
    case, kind, revision = attempt["case_id"], attempt["kind"], attempt["input_revision"]
    label = f"{kind} · input revision {revision} attempt {attempt['generation']}"
    event(conn, case, "ATTEMPT_SETTLED", f"{label} · {message.outcome} · {message.reason_code}", at=now)
    outcome = message.outcome
    if outcome == "PASS" and kind == "BANK":
        merchant = json.loads(conn.execute("SELECT merchant FROM applications WHERE id=?", (case,)).fetchone()[0])
        score = name_score(merchant["name"], message.registered_name or "")
        conn.execute("UPDATE attempts SET name_score=? WHERE id=?", (score, attempt["id"]))
        band = name_band(score)
        if band != "CONSISTENT":
            # The provider result stays PASS; the beneficiary comparison is our own judgement and holds review.
            open_check_issue(conn, case, kind, "BENEFICIARY_REVIEW", revision,
                             f"check.BANK · registered beneficiary {band.lower()} · score {score}", now)
            reconcile(conn, case, now)
            return
    if outcome == "PASS":
        resolve_check_issues(conn, case, kind, revision, now)
    elif outcome == "FAIL":
        open_check_issue(conn, case, kind, "CHECK_FAILED", revision,
                         f"check.{kind} · definitive fail · {message.reason_code}", now)
    elif outcome == "REFERRED":
        open_check_issue(conn, case, kind, "CHECK_REFERRED", revision,
                         f"check.{kind} · referred for review · {message.reason_code}", now)
    elif attempt["generation"] >= MAX_AUTOMATIC:
        exhausted(conn, case, kind, revision, attempt["generation"], now)
    reconcile(conn, case, now)


def exhausted(conn, case, kind, revision, generation, now):
    detail = ("recovery failed · rejection only" if generation > MAX_AUTOMATIC
              else f"{MAX_AUTOMATIC} automatic attempts without a definitive result")
    open_check_issue(conn, case, kind, "CHECK_EXHAUSTED", revision, f"check.{kind} · {detail}", now)


def expire_attempt(conn, attempt_id, now, why):
    """The one guarded expiry, shared by the deadline timer and a late callback. Only one caller wins."""
    if conn.execute("""UPDATE attempts SET status='EXPIRED',settled_at=? WHERE id=? AND status='SENT'
                    AND deadline<=?""", (now, attempt_id, now)).rowcount != 1:
        return False
    row = conn.execute("SELECT case_id,kind,generation,input_revision FROM attempts WHERE id=?",
                       (attempt_id,)).fetchone()
    event(conn, row["case_id"], "ATTEMPT_EXPIRED",
          f"{row['kind']} · input revision {row['input_revision']} attempt {row['generation']} · {why}", at=now)
    if row["generation"] >= MAX_AUTOMATIC:
        exhausted(conn, row["case_id"], row["kind"], row["input_revision"], row["generation"], now)
    reconcile(conn, row["case_id"], now)
    return True


def expire_due(path, now):
    # Declared document uploads that never arrived are handed back to the merchant (final release audit R1).
    lapse_pending_uploads(path, now)
    with closing(connect(path)) as conn:
        due = [r[0] for r in conn.execute("""SELECT id FROM attempts WHERE status='SENT' AND deadline<=?
                                          ORDER BY deadline""", (now,))]
    count = 0
    for attempt_id in due:
        with write_tx(path) as conn:
            count += expire_attempt(conn, attempt_id, now, "no reply by the deadline")
    return count


# ---------------------------------------------------------------- outbound dispatch (worker side)

def claim_dispatch(path, now):
    """Lease one due dispatch job. Returns None when idle, {'skip': True} after a non-sending decision,
    or the claim needed to send outside the transaction."""
    with write_tx(path) as conn:
        settings = timing(conn)
        job = conn.execute("""SELECT * FROM jobs WHERE kind='DISPATCH' AND
            ((status='READY' AND due_at<=?) OR (status='LEASED' AND lease_expires<=?))
            ORDER BY due_at,id LIMIT 1""", (now, now)).fetchone()
        if job is None:
            return None
        attempt = conn.execute("""SELECT a.*, c.input_revision AS current_revision, ap.state AS case_state
            FROM attempts a JOIN checks c USING(case_id,kind) JOIN applications ap ON ap.id=a.case_id
            WHERE a.id=?""", (job["attempt_id"],)).fetchone()
        case, kind = attempt["case_id"], attempt["kind"]
        reclaimed = job["status"] == "LEASED"
        if reclaimed:
            event(conn, case, "LEASE_RECLAIMED", f"{kind} dispatch lease expired · claim {job['claims']} abandoned",
                  at=now)
            LOG.warning("reclaimed 1 expired dispatch lease (job %d, claim %d)", job["id"], job["claims"])
        if job["claims"] >= MAX_CLAIMS:
            dead_letter(conn, job, case, kind, "lease expired on the final claim", now)
            return {"skip": True}
        current = (attempt["status"] in ("SCHEDULED", "SENT") and attempt["case_state"] in PRE
                   and attempt["input_revision"] == attempt["current_revision"])
        if not current:
            conn.execute("""UPDATE jobs SET status='CANCELLED',lease_token=NULL,last_reason='attempt no longer current'
                         WHERE id=?""", (job["id"],))
            return {"skip": True}
        if attempt["status"] == "SCHEDULED" and not dispatch_allowed(conn, case, kind):
            conn.execute("UPDATE jobs SET status='HELD',lease_token=NULL,last_reason='case hold' WHERE id=?",
                         (job["id"],))
            event(conn, case, "DISPATCH_HELD", f"{kind} attempt {attempt['generation']} paused by an open hold", at=now)
            return {"skip": True}
        token = uuid4().hex
        conn.execute("""UPDATE jobs SET status='LEASED',lease_token=?,lease_expires=?,claims=claims+1
                     WHERE id=?""", (token, now + settings["lease_s"], job["id"]))
        if attempt["status"] == "SCHEDULED":
            deadline = now + settings["attempt_timeout_s"]
            conn.execute("""UPDATE attempts SET status='SENT',sent_at=?,deadline=? WHERE id=? AND status='SCHEDULED'""",
                         (now, deadline, attempt["id"]))
            detail = f"{kind} · input revision {attempt['input_revision']} attempt {attempt['generation']} · " \
                     f"reply due within {settings['attempt_timeout_s']:g} s"
            if now - job["due_at"] >= 1:
                detail += f" · dispatch delayed {now - job['due_at']:.1f} s (no retry budget used)"
            event(conn, case, "REQUEST_SENT", detail, at=now)
        return dict(job_id=job["id"], token=token, attempt_id=attempt["id"], kind=kind, case_id=case,
                    inputs=json.loads(attempt["inputs"]), api_key=db.setting(conn, "sim_api_key"),
                    http_timeout=settings["http_timeout_s"])


def send_request(claim, post):
    """HTTP happens outside any transaction. The attempt ID is the provider idempotency key."""
    body = packed({"attempt_id": claim["attempt_id"], "inputs": claim["inputs"]}).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + claim["api_key"],
               "Idempotency-Key": claim["attempt_id"]}
    try:
        response = post(f"/sim/{claim['kind']}/requests", body, headers, claim["http_timeout"])
    except (httpx.TransportError, OSError) as exc:
        return "retry", type(exc).__name__, None
    if 200 <= response.status_code < 300:
        try:
            return "done", f"HTTP {response.status_code}", response.json()["reference"]
        except (ValueError, KeyError, TypeError):
            return "dead", "malformed provider acknowledgement", None
    if response.status_code >= 500 or response.status_code == 429:
        return "retry", f"HTTP {response.status_code}", None
    return "dead", f"HTTP {response.status_code}", None


def complete_dispatch(path, claim, result, now):
    verdict, reason, reference = result
    with write_tx(path) as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=? AND lease_token=? AND status='LEASED'",
                           (claim["job_id"], claim["token"])).fetchone()
        if job is None:
            return "stale"  # another claim owns this job now; its completion decides
        case, kind = claim["case_id"], claim["kind"]
        if verdict == "done":
            conn.execute("UPDATE jobs SET status='DONE',lease_token=NULL,last_reason=? WHERE id=?",
                         (reason, job["id"]))
            # Recording the reference never changes the attempt status; an early callback may already have settled it.
            stored = conn.execute("SELECT reference FROM attempts WHERE id=?", (claim["attempt_id"],)).fetchone()[0]
            if stored is None:
                conn.execute("UPDATE attempts SET reference=? WHERE id=? AND reference IS NULL",
                             (reference, claim["attempt_id"]))
            elif stored != reference:
                event(conn, case, "PROVIDER_ANOMALY", f"{kind} acknowledged with a different reference", at=now)
            event(conn, case, "REQUEST_ACCEPTED", f"{kind} provider reference {reference}", kind.lower(), at=now)
        elif verdict == "retry" and job["claims"] < MAX_CLAIMS:
            retry_at = now + timing(conn)["dispatch_retry_s"]
            conn.execute("""UPDATE jobs SET status='READY',due_at=?,lease_token=NULL,last_reason=? WHERE id=?""",
                         (retry_at, reason, job["id"]))
            event(conn, case, "REQUEST_RETRY", f"{kind} transport failure ({reason}) · claim {job['claims']} of "
                  f"{MAX_CLAIMS} · same idempotency key", at=now)
        else:
            dead_letter(conn, job, case, kind, reason, now)
        return verdict


def dead_letter(conn, job, case, kind, reason, now):
    conn.execute("UPDATE jobs SET status='DEAD',lease_token=NULL,last_reason=? WHERE id=?", (reason, job["id"]))
    event(conn, case, "JOB_DEAD_LETTERED", f"{kind} dispatch · {reason} · the attempt deadline still governs retry",
          at=now)


def dispatch_due(path, clock, post, limit=20):
    sent = 0
    for _ in range(limit):
        claim = claim_dispatch(path, clock())
        if claim is None:
            break
        if claim.get("skip"):
            continue
        crash_point(path, "after_claim", "DISPATCH")
        result = send_request(claim, post)
        crash_point(path, "after_remote_effect", "DISPATCH")
        complete_dispatch(path, claim, result, clock())
        sent += 1
    return sent
