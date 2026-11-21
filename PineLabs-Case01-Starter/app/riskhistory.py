"""An adverse RISK result belongs to the legal entity, not to one application (final audit B2).

Category and postal zone are self-declared, so a RISK failure or referral can be "cleared" by describing the business
differently. Inside one application the workflow already turns that change into an operator decision (a changed-input
hold plus a clearance bound to the winning attempt). This module carries the same rule across applications:

    An application whose PAN or GSTIN belongs to a REJECTED application that received an adverse RISK result
    (REFERRED or FAIL) needs a reasoned operator clearance on its own current RISK assessment before it can be approved.

Linking uses the two exact identifiers that already mean "one legal entity gets one Merchant ID" (see duplicates.py), so
an unrelated business, or a business whose earlier application was rejected for evidence that has since been corrected,
is not touched. A rejection without an adverse RISK result never creates a link.

Both layers run inside the caller's BEGIN IMMEDIATE transaction, so two simultaneous submissions, or a submission racing
the rejection it depends on, are serialized and judged against committed facts:
    hold()            opens or refreshes the operator issue (called from workflow.reconcile on every mutation and on
                      every re-evaluation that a rejection triggers);
    linked_adverse()  is read again by the approval gate (approval.build_receipt), which refuses to seal a receipt
                      without a clearance bound to the winning RISK attempt even if the issue were missing.

A clearance is also bound to what it set aside. The RISK input revision and attempt depend only on the self-declared
category and zone, not on who the applicant is, so on their own they would let a clearance given for one legal entity's
rejected history survive an edit that turns the application into another entity with a rejected history of its own.
Each CLEAR_RISK_CHANGE decision therefore stores a scope (the RISK input revision and the rejected applications whose
adverse results it set aside), and it counts only while that scope is still the current one. Correcting a typo that
keeps the same linked history, or a field that does not change it, needs no new decision.
"""
import json

from .db import digest, packed

CAUSE = "RISK_INPUTS_CHANGED_AFTER_REFERRAL"
RISK_CHANGE = CAUSE
ADVERSE_RISK = "('REFERRED','FAIL')"
LINK_REASON = "Same legal entity as rejected case "


def linked_adverse(conn, case, merchant):
    """Rejected applications of the same legal entity (same PAN or GSTIN) with an adverse RISK result.
    Returns [(case_id, provider_reason_code)], oldest first."""
    pan, gst = merchant["pan"].strip().upper(), merchant["gst"].strip().upper()
    return [(row["id"], row["reason_code"]) for row in conn.execute(f"""
        SELECT o.id, (SELECT a.reason_code FROM attempts a WHERE a.case_id=o.id AND a.kind='RISK'
                      AND a.status IN {ADVERSE_RISK} ORDER BY a.rowid LIMIT 1) AS reason_code
        FROM applications o WHERE o.id<>? AND o.state='REJECTED'
          AND (upper(trim(json_extract(o.merchant,'$.pan')))=? OR upper(trim(json_extract(o.merchant,'$.gst')))=?)
          AND EXISTS (SELECT 1 FROM attempts a WHERE a.case_id=o.id AND a.kind='RISK' AND a.status IN {ADVERSE_RISK})
        ORDER BY o.rowid""", (case, pan, gst))]


def scope(conn, case, merchant, revision):
    """What a clearance of this RISK assessment covers right now: the assessment (its input revision) and the rejected
    applications of the same legal entity whose adverse results it sets aside."""
    history = sorted(other for other, _ in linked_adverse(conn, case, merchant))
    return digest(packed(dict(risk_revision=revision, history=history)))


def record_scope(conn, case, decision_id, merchant, revision):
    """Called in the same transaction as the CLEAR_RISK_CHANGE decision it describes."""
    conn.execute("INSERT INTO risk_clearance_scope VALUES (?,?)", (decision_id, scope(conn, case, merchant, revision)))


def clearance_valid(conn, decision_id, case, merchant, revision):
    """The decision still covers the history this application has now. A decision recorded before scopes existed
    cannot vouch for any rejected history, so it only counts while there is none."""
    stored = conn.execute("SELECT scope FROM risk_clearance_scope WHERE decision_id=?", (decision_id,)).fetchone()
    if stored is None:
        return not linked_adverse(conn, case, merchant)
    return stored[0] == scope(conn, case, merchant, revision)


def cleared(conn, case, merchant, revision):
    """An operator already decided this case's RISK assessment for exactly this input revision and history."""
    return any(clearance_valid(conn, row[0], case, merchant, revision) for row in conn.execute(
        "SELECT id FROM decisions WHERE case_id=? AND action='CLEAR_RISK_CHANGE' AND subject_revision=?", (case, revision)))


def hold(conn, case, now, event):
    """Open, refresh or retire the operator issue that stands between a linked application and approval."""
    check = conn.execute("SELECT input_revision FROM checks WHERE case_id=? AND kind='RISK'", (case,)).fetchone()
    if check is None:
        return
    revision = check[0]
    merchant = json.loads(conn.execute("SELECT merchant FROM applications WHERE id=?", (case,)).fetchone()[0])
    linked = linked_adverse(conn, case, merchant)
    existing = conn.execute("""SELECT * FROM review_items WHERE case_id=? AND cause=? AND subject='check.RISK'
                            AND status<>'RESOLVED'""", (case, CAUSE)).fetchone()
    if not linked:
        # The identifiers were corrected to a different entity: a hold that existed only for this link ends with it.
        if existing and existing["reason"].startswith(LINK_REASON):
            conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (existing["id"],))
            event(conn, case, "ISSUE_RESOLVED", "check.RISK · no longer linked to a rejected application", at=now)
        return
    if cleared(conn, case, merchant, revision):
        return
    other, code = linked[0]
    reason = f"{LINK_REASON}{other[:10]} · adverse RISK result {code} · review RISK revision {revision}"
    if existing is None:
        conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
                     VALUES (?,?,'check.RISK',?,'OPERATOR','OPEN',?)""", (case, CAUSE, revision, reason))
        event(conn, case, "RISK_CHANGE_HELD", reason, at=now)
    elif existing["subject_revision"] != revision:
        conn.execute("UPDATE review_items SET subject_revision=?,reason=?,version=version+1 WHERE id=?",
                     (revision, reason, existing["id"]))
        event(conn, case, "RISK_CHANGE_HELD", reason, at=now)
