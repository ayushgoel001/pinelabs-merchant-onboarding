"""Operator judgments are scoped to one active issue version. Reasons are synthetic, allowlisted text."""
import json
from typing import Literal

from pydantic import Field

from .approval import authentic_result, current_attempt, qualified_check
from .db import write_tx
from . import duplicates, riskhistory
from .evidence import current_extractions
from .extraction import StrictModel, is_legacy, name_band, name_score
from .workflow import (MAX_AUTOMATIC, PRE, RETRYABLE, REVIEW, RISK_CHANGE, Problem, current_documents, event,
                       reconcile, refresh_assessment, transition)

REASONS = {
    "REQUEST_CORRECTION": ("Replacement evidence required", "Provider inputs require correction"),
    "ACCEPT_REFERRAL": ("Business activity confirmed against submitted evidence", "Zone exposure reviewed and accepted"),
    "ACCEPT_EXTRACTION": ("Document image reviewed; the extracted value is correct",
                          "Image quality is acceptable and the value matches the application"),
    "ACCEPT_BENEFICIARY_MATCH": ("Registered name is an abbreviation of the legal name",
                                 "Account holder confirmed against bank evidence"),
    "CLEAR_RISK_CHANGE": ("Updated business profile corroborated by evidence", "Corrected zone independently reviewed"),
    "AUTHORIZE_RECOVERY": ("Provider service restored", "Transport fault cleared"),
    "DISMISS_DUPLICATE": ("Separate business confirmed from evidence",
                          "Shared bank account belongs to a related entity of the same group",
                          "Same legal name belongs to an unrelated entity in this zone"),
    "REPAIR_DISPATCH": ("Restore missing unsent dispatch work",),
    "REJECT": ("Evidence could not be reconciled", "Risk outside policy tolerance", "Verification could not be completed"),
    "REQUEUE_MID": ("Issuer service restored", "Reconcile the existing issuance key"),
}
LABELS = {"REQUEST_CORRECTION": "Request correction", "ACCEPT_REFERRAL": "Accept referral",
          "ACCEPT_BENEFICIARY_MATCH": "Accept beneficiary match", "ACCEPT_EXTRACTION": "Accept low-confidence value",
          "CLEAR_RISK_CHANGE": "Clear changed risk inputs", "AUTHORIZE_RECOVERY": "Authorize one recovery",
          "DISMISS_DUPLICATE": "Dismiss candidate", "REPAIR_DISPATCH": "Restore unsent work", "REJECT": "Reject application", "REQUEUE_MID": "Requeue issuance"}


class Decision(StrictModel):
    action: Literal["REQUEST_CORRECTION", "ACCEPT_REFERRAL", "ACCEPT_BENEFICIARY_MATCH", "ACCEPT_EXTRACTION",
                    "CLEAR_RISK_CHANGE",
                    "AUTHORIZE_RECOVERY",
                    "DISMISS_DUPLICATE", "REPAIR_DISPATCH", "REJECT"]
    version: int = Field(strict=True, ge=1)
    reason: str = Field(min_length=1, max_length=100)


def stranded_attempts(conn, case):
    return conn.execute("""SELECT a.id FROM attempts a JOIN checks c USING(case_id,kind,input_revision)
        LEFT JOIN jobs j ON j.attempt_id=a.id AND j.kind='DISPATCH'
        WHERE a.case_id=? AND a.status='SCHEDULED' AND a.sent_at IS NULL AND
        (j.id IS NULL OR (j.status='CANCELLED' AND j.claims=0))
        AND a.generation=(SELECT max(b.generation) FROM attempts b WHERE b.case_id=a.case_id
                          AND b.kind=a.kind AND b.input_revision=a.input_revision)""", (case,)).fetchall()


def extraction_acceptable(conn, issue, application):
    """Only a value the OCR engine did read, that agrees with the application, and was merely below the threshold.
    Unreadable or conflicting values can never be accepted; they need a replacement document."""
    kind, _, label = issue["subject"].partition(".")
    current = current_documents(conn, issue["case_id"])
    extraction = current_extractions(conn, issue["case_id"], current).get(kind)
    field = (extraction or {}).get("fields", {}).get(label) or {}
    if extraction is None or field.get("value") is None or field.get("confidence") is None or is_legacy(extraction):
        return False  # a record from before contradiction checks cannot be accepted, only replaced
    merchant = json.loads(application["merchant"])
    expected = merchant["name" if label in {"name", "beneficiary"} else label]
    agrees = (name_band(name_score(expected, field["value"])) == "CONSISTENT"
              if label in {"name", "beneficiary"} else field["value"] == expected)
    return agrees and field["confidence"] < extraction["threshold"]


def allowed_actions(conn, issue, application):
    if application["state"] != REVIEW or issue["status"] != "OPEN" or issue["owner"] != "OPERATOR":
        return []
    case, cause = issue["case_id"], issue["cause"]
    actions = []
    if cause in {"EVIDENCE_CONFLICT", "EVIDENCE_UNREADABLE", "BENEFICIARY_REVIEW", "CHECK_FAILED", "CHECK_REFERRED"}:
        actions.append("REQUEST_CORRECTION")
    if cause == "STALLED" and stranded_attempts(conn, case):
        actions.append("REPAIR_DISPATCH")
    if cause == "DUPLICATE_CANDIDATE" and duplicates.dismissible(conn, case, issue):
        actions.append("DISMISS_DUPLICATE")
    if cause == "EVIDENCE_UNREADABLE" and extraction_acceptable(conn, issue, application):
        actions.append("ACCEPT_EXTRACTION")
    if cause == "BENEFICIARY_REVIEW":
        attempt = current_attempt(conn, case, "BANK")
        merchant = json.loads(application["merchant"])
        # Only the weak-match band, only on an authentic current PASS for the issue's exact BANK revision.
        if (attempt and attempt["input_revision"] == issue["subject_revision"] and attempt["status"] == "PASS"
                and authentic_result(attempt, merchant) and attempt["name_score"] is not None
                and name_band(attempt["name_score"]) == "REVIEW"):
            actions.append("ACCEPT_BENEFICIARY_MATCH")
    if cause in {"CHECK_REFERRED", RISK_CHANGE, "CHECK_EXHAUSTED"}:
        kind = issue["subject"].removeprefix("check.")
        attempt = current_attempt(conn, case, kind)
        policy, merchant = json.loads(application["policy"]), json.loads(application["merchant"])
        if attempt and attempt["input_revision"] == issue["subject_revision"]:
            if (cause == "CHECK_REFERRED" and kind == "RISK" and kind in policy["referral_kinds"]
                    and authentic_result(attempt, merchant) and attempt["status"] == "REFERRED"):
                actions.append("ACCEPT_REFERRAL")
            if cause == RISK_CHANGE and kind == "RISK" and qualified_check(conn, case, kind, policy, merchant):
                actions.append("CLEAR_RISK_CHANGE")
            if (cause == "CHECK_EXHAUSTED" and attempt["generation"] == MAX_AUTOMATIC
                    and attempt["status"] in RETRYABLE):
                actions.append("AUTHORIZE_RECOVERY")
    return actions + ["REJECT"]


def decide(path, case, issue_id, command, now):
    location = "/cases/" + case
    with write_tx(path) as conn:
        app = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
        issue = conn.execute("SELECT * FROM review_items WHERE id=? AND case_id=?", (issue_id, case)).fetchone()
        if not app or not issue:
            raise Problem(404, "Issue unavailable.")
        action = command.action
        if command.reason not in REASONS[action]:
            raise Problem(422, "Select a reason offered for this decision.")
        if (action == "AUTHORIZE_RECOVERY" and app["state"] in PRE and issue["owner"] == "SYSTEM"
                and issue["status"] == "OPEN" and conn.execute("""SELECT 1 FROM decisions WHERE issue_id=?
                AND issue_version=? AND subject_revision=? AND action=? AND reason=?""",
                (issue_id, command.version, issue["subject_revision"], action, command.reason)).fetchone()):
            return
        if (app["state"] not in PRE or issue["status"] != "OPEN" or issue["owner"] != "OPERATOR"
                or issue["version"] != command.version):
            raise Problem(409, "This issue changed. Open the current case.", location)
        if action not in allowed_actions(conn, issue, app):
            raise Problem(409, "This decision is unavailable for the current issue.", location)
        attempt = current_attempt(conn, case, issue["subject"][6:]) if issue["subject"].startswith("check.") else None
        # An extraction acceptance binds to the revision of the document that was reviewed, not the form revision.
        bound = (current_documents(conn, case)[issue["subject"].partition(".")[0]][0]
                 if action == "ACCEPT_EXTRACTION" else issue["subject_revision"])
        pair = duplicates.dismissible(conn, case, issue) if action == "DISMISS_DUPLICATE" else None
        decision_id = conn.execute("""INSERT INTO decisions(issue_id,issue_version,case_id,subject_revision,attempt_id,action,actor,reason,at)
            VALUES (?,?,?,?,?,?,'operator',?,?)""", (issue_id, command.version, case, bound,
                    attempt["id"] if attempt else None, action, command.reason, now)).lastrowid
        if pair:  # the dismissal is valid only for these exact identities (see duplicates.pair_digest)
            conn.execute("INSERT INTO duplicate_dismissals VALUES (?,?,?,?)", (case, pair[1], pair[0], decision_id))
        if action == "CLEAR_RISK_CHANGE":  # the clearance names the history it sets aside (see riskhistory.scope)
            riskhistory.record_scope(conn, case, decision_id, json.loads(app["merchant"]), bound)
        event(conn, case, action, f"{issue['subject']} · issue {issue_id} v{command.version} · {command.reason}",
              "operator", at=now)
        if action == "REJECT":
            conn.execute("""UPDATE review_items SET status='RESOLVED',version=version+1
                         WHERE case_id=? AND status<>'RESOLVED'""", (case,))
            conn.execute("""UPDATE jobs SET status='CANCELLED',lease_token=NULL,last_reason='case rejected'
                         WHERE status IN ('READY','LEASED','HELD') AND attempt_id IN
                         (SELECT id FROM attempts WHERE case_id=?)""", (case,))
            conn.execute("""UPDATE attempts SET status='SUPERSEDED',settled_at=? WHERE case_id=?
                         AND status IN ('SCHEDULED','SENT')""", (now, case))
            transition(conn, case, "REJECTED", "operator rejection", at=now)
            # Cases that were held against this one may no longer have a duplicate: re-evaluate them now.
            for held in conn.execute("""SELECT DISTINCT case_id FROM review_items WHERE subject=? AND cause='DUPLICATE_CANDIDATE'
                                      AND status<>'RESOLVED'""", ("candidate." + case,)).fetchall():
                reconcile(conn, held[0], now)
            return
        if action == "REPAIR_DISPATCH":
            for attempt in stranded_attempts(conn, case):
                conn.execute("""INSERT INTO jobs(operation_key,attempt_id,kind,status,due_at)
                    VALUES (?,?,'DISPATCH','READY',?) ON CONFLICT(operation_key) DO UPDATE
                    SET status='READY',due_at=excluded.due_at,last_reason=NULL
                    WHERE jobs.status='CANCELLED' AND jobs.claims=0""", ("dispatch/" + attempt[0], attempt[0], now))
            # Normal derivation closes the diagnostic only after eligible work exists.
        elif action in {"REQUEST_CORRECTION", "AUTHORIZE_RECOVERY"}:
            owner = "MERCHANT" if action == "REQUEST_CORRECTION" else "SYSTEM"
            conn.execute("""UPDATE review_items SET owner=?,version=version+1,reason=? WHERE id=?""",
                         (owner, issue["reason"] + " · " + command.reason, issue_id))
        else:
            if action == "ACCEPT_EXTRACTION":
                refresh_assessment(conn, case)
            conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (issue_id,))
        reconcile(conn, case, now)
