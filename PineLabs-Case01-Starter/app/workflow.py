"""Case mutations, issue lifecycle, the sole attempt scheduler and the pure state derivation."""
import json
import time
from contextlib import closing
from uuid import uuid4

from .approval import build_receipt, record_approval
from . import duplicates, riskhistory
from .evidence import accepted_fields, current_extractions, insert_extraction
from .db import connect, digest, packed, timing, write_tx
from .extraction import assess, is_legacy, provider_inputs

STATES = ("NEEDS_INFO", "IN_REVIEW", "VERIFYING", "APPROVED_PENDING_MID",
          "PROVISIONING_FAILED", "ACTIVE", "REJECTED")
NEEDS, REVIEW, VERIFY, APPROVED, FAILED, ACTIVE, REJECTED = STATES
PRE = {NEEDS, REVIEW, VERIFY}
EDGES = {
    NEEDS: {REVIEW: "operator hold", VERIFY: "work queued", APPROVED: "gate satisfied"},
    REVIEW: {NEEDS: "merchant action", VERIFY: "work queued", APPROVED: "gate satisfied",
             REJECTED: "operator rejection"},
    VERIFY: {NEEDS: "merchant action", REVIEW: "operator hold", APPROVED: "gate satisfied"},
    APPROVED: {FAILED: "issuance error/unknown", ACTIVE: "issuer receipt"},
    FAILED: {ACTIVE: "issuer receipt"}, ACTIVE: {}, REJECTED: {},
}
# Category and zone are self-declared, so any earlier adverse RISK outcome (review B1) makes a later change a
# human decision: a new PASS alone can never clear a referral or a definitive failure such as PROHIBITED_PROFILE.
# riskhistory.py carries the same rule from a rejected application to a new one for the same legal entity (audit B2).
RISK_CHANGE, ADVERSE_RISK = riskhistory.RISK_CHANGE, riskhistory.ADVERSE_RISK

MAX_AUTOMATIC = 3          # automatic attempts per check and input revision
RETRYABLE = ("EXPIRED", "TRANSIENT")
DEFINITIVE = ("PASS", "FAIL", "REFERRED")
CHECK_CAUSES = ("CHECK_FAILED", "CHECK_REFERRED", "CHECK_EXHAUSTED", "BENEFICIARY_REVIEW")
HOLDERS = ("MERCHANT", "OPERATOR")

LIVE_WORK = """SELECT 1 FROM attempts a JOIN checks c USING(case_id,kind,input_revision)
    WHERE a.case_id=applications.id AND (a.status='SENT' OR
        (a.status='SCHEDULED' AND EXISTS (SELECT 1 FROM jobs j WHERE j.attempt_id=a.id
         AND j.kind='DISPATCH' AND j.status IN ('READY','LEASED'))))"""
# A declared document upload that has not arrived yet (final release audit R1). It holds dispatch and the approval
# gate like any merchant-owned issue, but it is work in progress rather than a request for the merchant: the save
# is still being delivered, so it does not move the case to NEEDS_INFO. A failed or abandoned upload does.
UPLOAD_PENDING, UPLOAD_FAILED = "UPLOAD_PENDING", "UPLOAD_FAILED"
UPLOAD_GRACE_S = 120.0     # an upload declared this long ago without arriving is treated as interrupted
PENDING_UPLOAD = """SELECT 1 FROM review_items r WHERE r.case_id=applications.id AND r.cause='UPLOAD_PENDING'
    AND r.status<>'RESOLVED'"""
STRANDED = ("SELECT id FROM applications WHERE state='VERIFYING' AND NOT EXISTS (" + LIVE_WORK + ")"
            " AND NOT EXISTS (" + PENDING_UPLOAD + ")")


class Problem(Exception):
    def __init__(self, status, message, refresh=None):
        self.status = status
        self.message = message
        self.refresh = refresh
        super().__init__(message)


def event(conn, case, kind, detail, actor="system", at=None):
    conn.execute("INSERT INTO events(case_id,at,event,actor,detail) VALUES (?,?,?,?,?)",
                 (case, time.time() if at is None else at, kind, actor, detail))


def validate_edge(current, target, cause):
    initial = current is None and target in PRE and cause == "submitted"
    allowed = EDGES.get(current, {})
    if not initial and (target not in allowed or allowed[target] != cause):
        raise ValueError("Forbidden state transition or trigger.")


def transition(conn, case, target, cause, at=None):
    current = conn.execute("SELECT state FROM applications WHERE id=?", (case,)).fetchone()[0]
    validate_edge(current, target, cause)
    if target in {APPROVED, FAILED} and not conn.execute("SELECT 1 FROM approvals WHERE case_id=?", (case,)).fetchone():
        raise ValueError("A sealed approval receipt is required.")
    if target == ACTIVE and not conn.execute("SELECT 1 FROM merchants WHERE case_id=?", (case,)).fetchone():
        raise ValueError("A recorded issuer receipt is required.")
    conn.execute("UPDATE applications SET state=? WHERE id=?", (target, case))
    event(conn, case, "STATE_ENTERED", target, at=at)


def current_documents(conn, case):
    rows = conn.execute("""SELECT d.kind,d.revision,d.payload FROM documents d JOIN
        (SELECT kind,max(revision) AS revision FROM documents WHERE case_id=? GROUP BY kind)
        USING(kind,revision) WHERE d.case_id=?""", (case, case))
    return {row["kind"]: (row["revision"], json.loads(row["payload"]) if row["payload"] else None)
            for row in rows}


def save_documents(conn, case, documents, force=()):
    """Version each changed document. `force` bumps a kind even if its values are unchanged, because a new
    upload is a new piece of evidence with its own extraction."""
    old = current_documents(conn, case)
    revisions = {}
    for kind in sorted(old.keys() | documents.keys()):
        revision, previous = old.get(kind, (0, None))
        payload = documents.get(kind)
        if previous != payload or kind in force:
            revision += 1
            encoded = packed(payload) if payload is not None else None
            conn.execute("INSERT INTO documents VALUES (?,?,?,?,?)",
                         (case, kind, revision, encoded, digest(encoded) if encoded else None))
        revisions[kind] = revision
    return revisions


def reconcile_evidence(conn, case, revision, findings, previous):
    active = {(r["cause"], r["subject"]): r for r in conn.execute(
        """SELECT * FROM review_items WHERE case_id=? AND status<>'RESOLVED'
           AND cause IN ('EVIDENCE_MISSING','EVIDENCE_CONFLICT','EVIDENCE_UNREADABLE')""", (case,))}
    awaited = pending_upload_kinds(conn, case)
    for f in findings:
        if f["status"] == "CONSISTENT":
            continue
        if f["status"] == "MISSING" and f["document"] in awaited:
            continue  # the declared file is on its way; its own hold blocks the case until it is read
        subject = f"{f['document']}.{f['label']}"
        cause, owner = {"MISSING": ("EVIDENCE_MISSING", "MERCHANT"),
                        "UNREADABLE": ("EVIDENCE_UNREADABLE", "OPERATOR")}.get(f["status"], ("EVIDENCE_CONFLICT", "OPERATOR"))
        reason = f"{subject} · {f['status'].lower()}"
        if f.get("legacy"):
            reason += " · read before contradiction checks, upload the document again"
        elif f.get("candidates"):
            reason += f" · the document shows {len(f['candidates'])} different values for this field"
        elif f.get("confidence") is not None:
            reason += f" · OCR confidence {f['confidence']:g} (review below {f['threshold']:g})"
        elif f.get("source") == "OCR":
            reason += " · not readable in the uploaded image"
        existing = active.pop((cause, subject), None)
        if existing is None:
            conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
                         VALUES (?,?,?,?,?,'OPEN',?)""", (case, cause, subject, revision, owner, reason))
            event(conn, case, "ISSUE_OPENED", reason)
        elif subject_changed(previous.get(subject), f) or existing["owner"] != owner:
            if existing["owner"] == "MERCHANT" and owner == "OPERATOR":
                reason += " · replacement still requires review"
            conn.execute("""UPDATE review_items SET subject_revision=?,owner=?,status='OPEN',
                         version=version+1,reason=? WHERE id=?""",
                         (revision, owner, reason, existing["id"]))
            event(conn, case, "ISSUE_UPDATED", reason)
    for item in active.values():
        conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (item["id"],))
        event(conn, case, "ISSUE_RESOLVED", item["subject"])


def subject_changed(previous, current):
    if previous is None:
        return True
    # A form-wide revision can advance while this particular comparison stays identical.
    return any(previous.get(key) != value for key, value in current.items()
               if key != "source_revision" or current["document"] != "form")


# ---------------------------------------------------------------- provider check issues (B1 table)

def active_check_issues(conn, case, kind):
    return conn.execute("""SELECT * FROM review_items WHERE case_id=? AND subject=? AND status<>'RESOLVED'
                        AND cause IN (?,?,?,?) ORDER BY id""", (case, "check." + kind, *CHECK_CAUSES)).fetchall()


def open_check_issue(conn, case, kind, cause, revision, reason, at):
    """Update the existing cause, preserving any definitive failure until a new revision passes."""
    subject = "check." + kind
    matching = None
    for item in active_check_issues(conn, case, kind):
        if item["cause"] == cause:
            matching = item
        elif item["cause"] == "CHECK_FAILED":
            # An exhausted recheck may still be recovered. Keep its prerequisite failure pending.
            owner = "SYSTEM" if cause == "CHECK_EXHAUSTED" else "OPERATOR"
            conn.execute("""UPDATE review_items SET owner=?,version=version+?,
                         reason='Corrected inputs still require a definitive pass' WHERE id=?""",
                         (owner, int(item["owner"] != owner), item["id"]))
        else:
            conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (item["id"],))
            event(conn, case, "ISSUE_RESOLVED", f"{subject} · {item['cause']} superseded by {cause}", at=at)
    if matching:
        changed = matching["owner"] != "OPERATOR" or matching["subject_revision"] != revision
        conn.execute("""UPDATE review_items SET owner='OPERATOR',status='OPEN',subject_revision=?,reason=?,
                     version=version+? WHERE id=?""", (revision, reason, int(changed), matching["id"]))
        event(conn, case, "ISSUE_UPDATED", reason, at=at)
        return
    conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
                 VALUES (?,?,?,?,'OPERATOR','OPEN',?)""", (case, cause, subject, revision, reason))
    event(conn, case, "ISSUE_OPENED", reason, at=at)


def resolve_check_issues(conn, case, kind, revision, at):
    """Only a current PASS can resolve these provider issues; the independent risk-change hold survives."""
    for item in active_check_issues(conn, case, kind):
        if item["cause"] == "CHECK_REFERRED" and item["owner"] != "SYSTEM":
            continue
        conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (item["id"],))
        event(conn, case, "ISSUE_RESOLVED", f"check.{kind} · input revision {revision} passed", at=at)


def transfer_on_new_inputs(conn, case, kind, revision, at):
    if kind == "RISK" and conn.execute(f"""SELECT 1 FROM attempts WHERE case_id=? AND kind='RISK'
            AND status IN {ADVERSE_RISK} AND input_revision<?""", (case, revision)).fetchone():
        reason = f"Business category or zone changed after an adverse RISK result · review RISK revision {revision}"
        conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
            VALUES (?,?,'check.RISK',?,'OPERATOR','OPEN',?)
            ON CONFLICT(case_id,cause,subject) WHERE status<>'RESOLVED'
            DO UPDATE SET subject_revision=excluded.subject_revision, reason=excluded.reason,version=version+1""",
                     (case, RISK_CHANGE, revision, reason))
        event(conn, case, "RISK_CHANGE_HELD", reason, "merchant", at=at)
    for item in active_check_issues(conn, case, kind):
        reason = f"check.{kind} · waiting on recheck of input revision {revision}"
        conn.execute("""UPDATE review_items SET owner='SYSTEM',subject_revision=?,reason=?,version=version+1
                     WHERE id=?""", (revision, reason, item["id"]))
        event(conn, case, "ISSUE_TRANSFERRED", reason, "merchant", at=at)


def note_unchanged_resubmission(conn, case, kind, at):
    for item in active_check_issues(conn, case, kind):
        if item["owner"] == "SYSTEM":
            continue  # an earlier correction is still being rechecked
        reason = item["reason"].split(" · resubmitted unchanged")[0] + " · resubmitted unchanged"
        to_operator = item["owner"] != "OPERATOR"
        conn.execute("UPDATE review_items SET owner='OPERATOR',reason=?,version=version+? WHERE id=?",
                     (reason, int(to_operator), item["id"]))
        event(conn, case, "ISSUE_UPDATED", reason, "merchant", at=at)


# ---------------------------------------------------------------- declared document uploads (final release audit R1)

def pending_upload_kinds(conn, case):
    return {r[0].split(".")[0] for r in conn.execute("""SELECT subject FROM review_items WHERE case_id=?
        AND cause=? AND status<>'RESOLVED'""", (case, UPLOAD_PENDING))}


def upload_holds(conn, case, kind=None):
    rows = conn.execute("""SELECT * FROM review_items WHERE case_id=? AND cause IN (?,?) AND status<>'RESOLVED'
        ORDER BY id""", (case, UPLOAD_PENDING, UPLOAD_FAILED)).fetchall()
    return [r for r in rows if kind is None or r["subject"] == kind + ".document"]


def close_issue(conn, case, item, detail, at):
    conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (item["id"],))
    event(conn, case, "ISSUE_RESOLVED", detail, at=at)


def stage_uploads(conn, case, kinds, revision, now):
    """Record, in the saving transaction, which documents this save sends as files. Each becomes an open hold
    before the save's own evaluation runs, so nothing can be dispatched or approved on the evidence held so far.
    A later save replaces the declaration: a document it no longer sends is withdrawn and its current evidence
    counts again; one it sends again is awaited afresh (a failed attempt is superseded)."""
    for item in upload_holds(conn, case):
        kind = item["subject"].split(".")[0]
        if kind not in kinds:
            close_issue(conn, case, item, f"{kind} document upload withdrawn by a new save", now)
        elif item["cause"] == UPLOAD_FAILED:
            close_issue(conn, case, item, f"{kind} document upload is being retried", now)
    awaited = pending_upload_kinds(conn, case)
    for kind in kinds:
        reason = f"{kind}.document · new file declared at data revision {revision}, waiting for it to be read"
        if kind in awaited:
            conn.execute("""UPDATE review_items SET subject_revision=?,reason=?,version=version+1 WHERE case_id=?
                AND cause=? AND subject=? AND status<>'RESOLVED'""", (revision, reason, case, UPLOAD_PENDING, kind + ".document"))
        else:
            conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
                VALUES (?,?,?,?,'MERCHANT','OPEN',?)""", (case, UPLOAD_PENDING, kind + ".document", revision, reason))
    if kinds:
        event(conn, case, "UPLOADS_DECLARED", ", ".join(kinds), "merchant", at=now)


def fail_upload(conn, case, item, kind, message, now):
    reason = f"{kind}.document · the new file could not be used: {message}"
    if item["cause"] == UPLOAD_FAILED:
        conn.execute("UPDATE review_items SET reason=?,version=version+1 WHERE id=?", (reason, item["id"]))
        event(conn, case, "ISSUE_UPDATED", reason, at=now)
        return
    close_issue(conn, case, item, f"{kind} document upload ended without a usable file", now)
    conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
        VALUES (?,?,?,?,'MERCHANT','OPEN',?)""", (case, UPLOAD_FAILED, item["subject"], item["subject_revision"], reason))
    event(conn, case, "ISSUE_OPENED", reason, at=now)


def record_upload_failure(path, case, kind, message, now=None):
    """A declared file was refused (unreadable, corrupt, oversized, unsupported, OCR unavailable). The hold stays,
    now as something the merchant must do, so the case leaves verification for NEEDS_INFO and stays correctable.
    An undeclared upload that fails changes nothing: the document it would have replaced was never set aside."""
    now = time.time() if now is None else now
    with write_tx(path) as conn:
        row = conn.execute("SELECT state FROM applications WHERE id=?", (case,)).fetchone()
        if not row or row["state"] not in PRE:
            return False
        holds = upload_holds(conn, case, kind)
        if not holds:
            return False
        fail_upload(conn, case, holds[0], kind, message, now)
        reconcile(conn, case, now)
        return True


def lapse_pending_uploads(path, now):
    """Worker sweep: an upload declared more than UPLOAD_GRACE_S ago that never arrived (closed tab, lost
    connection, restarted server) becomes the merchant's to repeat. Only the case's state changes; approval was
    impossible all along because the hold never left."""
    with closing(connect(path)) as conn:
        due = [r[0] for r in conn.execute("""SELECT r.case_id FROM review_items r WHERE r.cause=? AND r.status<>'RESOLVED'
            GROUP BY r.case_id HAVING coalesce((SELECT max(at) FROM events e WHERE e.case_id=r.case_id
            AND e.event='UPLOADS_DECLARED'), 0) <= ?""", (UPLOAD_PENDING, now - UPLOAD_GRACE_S))]
    lapsed = 0
    for case in due:
        with write_tx(path) as conn:
            state = conn.execute("SELECT state FROM applications WHERE id=?", (case,)).fetchone()["state"]
            if state not in PRE:
                continue
            for item in [h for h in upload_holds(conn, case) if h["cause"] == UPLOAD_PENDING]:
                fail_upload(conn, case, item, item["subject"].split(".")[0],
                            "the file did not arrive (the upload was interrupted)", now)
                lapsed += 1
            reconcile(conn, case, now)
    return lapsed


# ---------------------------------------------------------------- scheduling and derivation

def dispatch_allowed(conn, case, kind):
    # The changed-input review needs a RISK result before the operator can act.
    return not conn.execute("""SELECT 1 FROM review_items WHERE case_id=? AND status<>'RESOLVED'
        AND owner IN ('OPERATOR','MERCHANT') AND cause<>'STALLED' AND NOT (cause=? AND ?='RISK')""",
                            (case, RISK_CHANGE, kind)).fetchone()


def schedule_checks(conn, case, merchant, now):
    """Sole attempt creator. Pause unsent work under a hold and resume it without spending a claim."""
    backoff = timing(conn)["backoff_s"]
    for kind, revision in conn.execute("SELECT kind,input_revision FROM checks WHERE case_id=? ORDER BY rowid",
                                       (case,)).fetchall():
        last = conn.execute("""SELECT * FROM attempts WHERE case_id=? AND kind=?
                            AND input_revision=? ORDER BY generation DESC LIMIT 1""", (case, kind, revision)).fetchone()
        allowed = dispatch_allowed(conn, case, kind)
        if last and last["status"] == "SCHEDULED":
            if allowed:
                changed = conn.execute("""UPDATE jobs SET status='READY',due_at=max(due_at,?),last_reason=NULL
                    WHERE attempt_id=? AND status='HELD'""", (now, last["id"])).rowcount
                if changed:
                    event(conn, case, "DISPATCH_RELEASED", f"{kind} hold released", at=now)
            else:
                changed = conn.execute("""UPDATE jobs SET status='HELD',last_reason='case hold'
                    WHERE attempt_id=? AND status='READY'""", (last["id"],)).rowcount
                if changed:
                    event(conn, case, "DISPATCH_HELD", f"{kind} unsent request paused", at=now)
        if not allowed:
            continue
        if last is None:
            generation, due = 1, now
        elif last["status"] in RETRYABLE and last["generation"] < MAX_AUTOMATIC:
            generation = last["generation"] + 1
            due = max(now, last["settled_at"] + backoff[min(last["generation"], len(backoff)) - 1])
        elif (last["status"] in RETRYABLE and last["generation"] == MAX_AUTOMATIC
              and conn.execute("""SELECT 1 FROM decisions WHERE case_id=? AND attempt_id=?
                    AND action='AUTHORIZE_RECOVERY'""", (case, last["id"])).fetchone()):
            generation, due = MAX_AUTOMATIC + 1, now
        else:
            continue
        attempt = uuid4().hex
        conn.execute("""INSERT INTO attempts(id,case_id,kind,input_revision,generation,status,inputs)
            VALUES (?,?,?,?,?,'SCHEDULED',?)""",
                     (attempt, case, kind, revision, generation, packed(provider_inputs(kind, merchant))))
        conn.execute("""INSERT INTO jobs(operation_key,attempt_id,kind,status,due_at)
            VALUES (?,?,'DISPATCH','READY',?)""", ("dispatch/" + attempt, attempt, due))
        detail = f"{kind} · input revision {revision} · attempt {generation}"
        if due > now:
            detail += f" · due after {due - now:.1f} s backoff"
        event(conn, case, "DISPATCH_QUEUED", detail, at=now)


def derive(owners, gate_satisfied, has_work):
    """Pure projection over current issues, the approval gate and durable work."""
    if "OPERATOR" in owners:
        return REVIEW, "operator hold", False
    if "MERCHANT" in owners:
        return NEEDS, "merchant action", False
    if gate_satisfied:
        return APPROVED, "gate satisfied", False
    if has_work:
        return VERIFY, "work queued", False
    return REVIEW, "operator hold", True


def reconcile(conn, case, now=None):
    """Run at the end of every pre-approval mutation: holds, scheduler, derivation, transition."""
    now = time.time() if now is None else now
    row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
    if row["state"] is not None and row["state"] not in PRE:
        return
    # Duplicate detection runs first, in the same transaction, so a held duplicate neither dispatches nor approves.
    duplicates.scan(conn, case, now, event)
    # An adverse RISK result on a rejected application of the same legal entity needs an operator decision here too.
    riskhistory.hold(conn, case, now, event)
    schedule_checks(conn, case, json.loads(row["merchant"]), now)
    owners = {r[0] for r in conn.execute("""SELECT owner FROM review_items WHERE case_id=?
        AND status<>'RESOLVED' AND cause NOT IN ('STALLED','UPLOAD_PENDING')""", (case,))}
    work = conn.execute("SELECT EXISTS (" + LIVE_WORK + ") OR EXISTS (" + PENDING_UPLOAD + ") FROM applications WHERE id=?",
                        (case,)).fetchone()[0]
    diagnostic = conn.execute("""SELECT id,reason FROM review_items WHERE case_id=? AND cause='STALLED'
                               AND status<>'RESOLVED'""", (case,)).fetchone()
    if diagnostic and (work or owners & set(HOLDERS)):
        conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (diagnostic["id"],))
        event(conn, case, "ISSUE_RESOLVED", "Workflow accounted for", at=now)
        diagnostic = None
    receipt = build_receipt(conn, row) if not owners and not work else None
    target, cause, stalled = derive(owners, receipt is not None, bool(work))
    if receipt is not None:
        record_approval(conn, case, receipt, now)
        event(conn, case, "APPROVAL_RECORDED", "Evidence and winning check attempts sealed; MID issuance queued", at=now)
    if stalled:
        reason = "No eligible dispatch work or qualifying approval evidence"
        if not diagnostic:
            conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
                         VALUES (?,'STALLED','workflow',?,'OPERATOR','OPEN',?)""",
                         (case, row["data_revision"], reason))
            event(conn, case, "ISSUE_OPENED", reason, at=now)
        elif diagnostic["reason"] != reason:
            conn.execute("UPDATE review_items SET reason=?,version=version+1 WHERE id=?", (reason, diagnostic[0]))
            event(conn, case, "ISSUE_UPDATED", reason, at=now)
    if row["state"] != target:
        transition(conn, case, target, cause if row["state"] is not None else "submitted", at=now)


def refresh_peers(conn, case, identities, now):
    """Re-evaluate the open applications whose duplicate holds depend on this case's identity, before or after an edit
    (final audit I2). A hold is only recomputed when its own application next mutates, so without this a later copy
    stayed held behind a case that no longer matched it, and a case edited into a waiting application's identity left
    that application unflagged until its own next save. Runs in the editing transaction."""
    for peer in conn.execute("""SELECT id,merchant FROM applications WHERE id<>? AND state IN ('NEEDS_INFO','IN_REVIEW','VERIFYING')""",
                             (case,)).fetchall():
        other = json.loads(peer["merchant"])
        if any(duplicates.match_rules(identity, other) for identity in identities):
            reconcile(conn, peer["id"], now)


def build_findings(conn, case, merchant, revision):
    """The one place evidence is judged: current documents, their stored OCR extractions and operator acceptances."""
    current = current_documents(conn, case)
    documents = {kind: payload for kind, (_, payload) in current.items() if payload is not None}
    findings = assess(merchant, documents, current_extractions(conn, case, current), accepted_fields(conn, case, current))
    for f in findings:
        f["source_revision"] = revision if f["document"] == "form" else current.get(f["document"], (0, None))[0]
    return findings


def refresh_assessment(conn, case):
    """Re-judge the current revision after an operator acceptance. The saved assessment must equal what the gate recomputes."""
    row = conn.execute("SELECT merchant,data_revision FROM applications WHERE id=?", (case,)).fetchone()
    findings = build_findings(conn, case, json.loads(row["merchant"]), row["data_revision"])
    conn.execute("UPDATE assessments SET results=? WHERE case_id=? AND data_revision=?",
                 (packed(findings), case, row["data_revision"]))


def evaluate(conn, case, merchant, documents, revision, previous, now=None, uploads=None):
    uploads = uploads or {}
    versions = save_documents(conn, case, documents, force=set(uploads))
    for kind, (record, png) in uploads.items():
        insert_extraction(conn, case, kind, versions[kind], record, png, time.time() if now is None else now)
    findings = build_findings(conn, case, merchant, revision)
    conn.execute("INSERT INTO assessments VALUES (?,?,?)", (case, revision, packed(findings)))
    reconcile_evidence(conn, case, revision, findings, previous)
    reconcile(conn, case, now)


def upgrade_identity_claims(path):
    """Once per run directory: runs written before identity claims existed have approvals without claims, so the
    database-level rule "one approval per PAN" did not cover them (final audit I8). Restore each missing claim from
    the PAN in its sealed receipt. If another approval already holds that PAN the collision is not hidden: it is
    written to that case's history for an operator, and nothing already approved or issued is touched. Returns
    (restored, collisions)."""
    with write_tx(path) as conn:
        if conn.execute("SELECT 1 FROM settings WHERE name='identity_claims_checked'").fetchone():
            return (0, 0)
        restored = collisions = 0
        now = time.time()
        for row in conn.execute("""SELECT a.case_id,a.receipt FROM approvals a LEFT JOIN identity_claims c ON c.case_id=a.case_id
                                 WHERE c.case_id IS NULL ORDER BY a.rowid""").fetchall():
            pan = json.loads(row["receipt"])["evidence"]["merchant"]["pan"]
            if conn.execute("SELECT 1 FROM identity_claims WHERE pan=?", (pan,)).fetchone():
                collisions += 1
                event(conn, row["case_id"], "IDENTITY_CLAIM_COLLISION",
                      "Another approved application already holds this PAN. Both approvals stand; an operator must decide "
                      "which entity is real", at=now)
            else:
                conn.execute("INSERT INTO identity_claims VALUES (?,?)", (pan, row["case_id"]))
                restored += 1
        conn.execute("INSERT INTO settings VALUES ('identity_claims_checked',?)", (f"{restored},{collisions}",))
        return (restored, collisions)


def upgrade_open_cases(path):
    """Re-evaluate M2's explicit handoff cases once, with the normal M3 gate and their original inputs."""
    with write_tx(path) as conn:
        if conn.execute("SELECT 1 FROM settings WHERE name='m3_reconciled'").fetchone():
            return
        now = time.time()
        for row in conn.execute("""SELECT id FROM applications WHERE state IN ('NEEDS_INFO','IN_REVIEW','VERIFYING')""").fetchall():
            case = row["id"]
            converted = conn.execute("""UPDATE review_items SET status='RESOLVED',version=version+1 WHERE case_id=?
                AND status<>'RESOLVED' AND cause='STALLED' AND reason=?""",
                (case, "All configured checks passed · approval gate is delivered in M3")).rowcount
            risk = conn.execute(f"""SELECT input_revision FROM checks WHERE case_id=? AND kind='RISK' AND EXISTS
                (SELECT 1 FROM attempts WHERE case_id=? AND kind='RISK' AND status IN {ADVERSE_RISK}
                 AND attempts.input_revision<checks.input_revision)""", (case, case)).fetchone()
            if risk:
                transfer_on_new_inputs(conn, case, "RISK", risk[0], now)
            if converted:
                event(conn, case, "ISSUE_RESOLVED", "M2 approval handoff received; checking original evidence", at=now)
            if risk or converted:
                reconcile(conn, case, now)
        conn.execute("INSERT INTO settings VALUES ('m3_reconciled','1')")


def upgrade_legacy_evidence(path):
    """Once per run directory, re-judge applications that are still open and rest on an OCR record written before
    contradiction tracking. The record cannot qualify (extraction.ocr_observation), so this only makes the consequence
    visible at once: an operator-owned issue per field, instead of a case that would otherwise wait for its next
    mutation. Applications that are already approved are left alone. tools/revalidate_evidence.py re-reads their stored
    images without changing anything. Returns the number of applications reopened."""
    with write_tx(path) as conn:
        if conn.execute("SELECT 1 FROM settings WHERE name='legacy_evidence_checked'").fetchone():
            return 0
        now, reopened = time.time(), 0
        for row in conn.execute("SELECT id,merchant,data_revision FROM applications WHERE state IN ('NEEDS_INFO','IN_REVIEW','VERIFYING')").fetchall():
            case, revision = row["id"], row["data_revision"]
            current = current_documents(conn, case)
            if not any(is_legacy(ex) for ex in current_extractions(conn, case, current).values()):
                continue
            saved = conn.execute("SELECT results FROM assessments WHERE case_id=? AND data_revision=?", (case, revision)).fetchone()
            previous = {f"{f['document']}.{f['label']}": f for f in json.loads(saved[0])} if saved else {}
            findings = build_findings(conn, case, json.loads(row["merchant"]), revision)
            conn.execute("UPDATE assessments SET results=? WHERE case_id=? AND data_revision=?", (packed(findings), case, revision))
            reconcile_evidence(conn, case, revision, findings, previous)
            event(conn, case, "EVIDENCE_REVALIDATED", "OCR evidence read before contradiction checks must be uploaded again", at=now)
            reconcile(conn, case, now)
            reopened += 1
        conn.execute("INSERT INTO settings VALUES ('legacy_evidence_checked',?)", (str(reopened),))
        return reopened


def submit(path, submission, submitter, policy):
    merchant = submission.merchant.model_dump()
    documents = submission.documents
    content = [merchant, documents] + ([submission.uploads] if submission.uploads else [])
    fingerprint = digest(packed(content))
    with write_tx(path) as conn:
        existing = conn.execute("SELECT * FROM applications WHERE submission_key=?",
                                (str(submission.submission_key),)).fetchone()
        if existing:
            if existing["request_digest"] != fingerprint:
                raise Problem(409, "Submission key already belongs to different content.")
            return existing["id"], False, existing["submitter_hash"] == digest(submitter)
        case = uuid4().hex
        conn.execute("INSERT INTO applications VALUES (?,?,?,?,?,?,1,NULL,?)",
                     (case, str(submission.submission_key), fingerprint, digest(submitter),
                      packed(merchant), packed(policy), time.time()))
        conn.executemany("INSERT INTO checks VALUES (?,?,1)", [(case, k) for k in policy["required"]])
        event(conn, case, "SUBMITTED", "Structured application received", "merchant")
        stage_uploads(conn, case, submission.uploads, 1, time.time())
        evaluate(conn, case, merchant, documents, 1, {})
        return case, True, True


def amend(path, case, amendment, now=None):
    with write_tx(path) as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
        if not row:
            raise Problem(404, "Case unavailable.")
        if row["state"] not in PRE:
            message = "Case rejected." if row["state"] == REJECTED else "Approved: servicing required."
            raise Problem(409, message, "/cases/" + case)
        if row["data_revision"] != amendment.data_revision:
            raise Problem(409, "Evidence changed. Reload the case before editing.", "/cases/" + case)
        old, new = json.loads(row["merchant"]), amendment.merchant.model_dump()
        revision = row["data_revision"] + 1
        now = time.time() if now is None else now
        for kind in json.loads(row["policy"])["required"]:
            if provider_inputs(kind, old) == provider_inputs(kind, new):
                note_unchanged_resubmission(conn, case, kind, now)
                continue
            # Unsent and in-flight requests for the old inputs lose authority. Settled outcomes stay as history.
            conn.execute("""UPDATE jobs SET status='CANCELLED',last_reason='inputs superseded'
                WHERE status IN ('READY','HELD') AND attempt_id IN
                (SELECT id FROM attempts WHERE case_id=? AND kind=? AND status IN ('SCHEDULED','SENT'))""",
                         (case, kind))
            superseded = conn.execute("""UPDATE attempts SET status='SUPERSEDED',settled_at=?
                WHERE case_id=? AND kind=? AND status IN ('SCHEDULED','SENT')""", (now, case, kind)).rowcount
            conn.execute("UPDATE checks SET input_revision=input_revision+1 WHERE case_id=? AND kind=?", (case, kind))
            current = conn.execute("SELECT input_revision FROM checks WHERE case_id=? AND kind=?",
                                   (case, kind)).fetchone()[0]
            if superseded:
                event(conn, case, "ATTEMPT_SUPERSEDED", f"{kind} · {superseded} pending attempt(s) lost authority",
                      "merchant", at=now)
            transfer_on_new_inputs(conn, case, kind, current, now)
        before = json.loads(conn.execute("SELECT results FROM assessments WHERE case_id=? AND data_revision=?",
                                         (case, row["data_revision"])).fetchone()[0])
        previous = {f"{f['document']}.{f['label']}": f for f in before}
        conn.execute("UPDATE applications SET merchant=?,data_revision=? WHERE id=?", (packed(new), revision, case))
        event(conn, case, "EVIDENCE_UPDATED", f"Data revision {revision}", "merchant")
        stage_uploads(conn, case, amendment.uploads, revision, now)
        evaluate(conn, case, new, amendment.documents, revision, previous, now)
        if duplicates.identity(old) != duplicates.identity(new):
            refresh_peers(conn, case, (old, new), now)


def upload_document(path, case, kind, record, png, expected_revision, now=None):
    """Attach a server-produced OCR extraction as the new current revision of one document. The merchant's own
    application fields are untouched, so provider results stay valid; the new evidence is judged against them."""
    with write_tx(path) as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
        if not row:
            raise Problem(404, "Case unavailable.")
        if row["state"] not in PRE:
            message = "Case rejected." if row["state"] == REJECTED else "Approved: servicing required."
            raise Problem(409, message, "/cases/" + case)
        if row["data_revision"] != expected_revision:
            raise Problem(409, "Evidence changed. Reload the case before uploading.", "/cases/" + case)
        now = time.time() if now is None else now
        revision = row["data_revision"] + 1
        merchant = json.loads(row["merchant"])
        before = json.loads(conn.execute("SELECT results FROM assessments WHERE case_id=? AND data_revision=?",
                                         (case, row["data_revision"])).fetchone()[0])
        previous = {f"{f['document']}.{f['label']}": f for f in before}
        payload = {label: f["value"] for label, f in record["fields"].items() if f["value"] is not None}
        documents = {k: p for k, (_, p) in current_documents(conn, case).items() if p is not None}
        documents[kind] = payload
        conn.execute("UPDATE applications SET data_revision=? WHERE id=?", (revision, case))
        event(conn, case, "EVIDENCE_UPDATED",
              f"Data revision {revision} · {kind} document uploaded as {record['source_type'].upper()} and read by "
              f"{record['engine']} (lowest field confidence {record['min_confidence']:g})", "merchant", at=now)
        for item in upload_holds(conn, case, kind):
            close_issue(conn, case, item, f"{kind} document received and read", now)
        evaluate(conn, case, merchant, documents, revision, previous, now, uploads={kind: (record, png)})
