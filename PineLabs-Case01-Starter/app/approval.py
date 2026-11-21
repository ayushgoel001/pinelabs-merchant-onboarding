"""Evaluate the saved merchant policy and assemble the private, write-once decision receipt."""
import json

from . import riskhistory
from .db import digest, packed, valid_policy
from .evidence import accepted_fields, current_extractions, verify_integrity
from .extraction import LABELS, assess, name_band, provider_inputs


def current_attempt(conn, case, kind):
    """Select the newest generation first, even if it has no qualifying callback."""
    return conn.execute("""SELECT a.*, i.event_id, i.body_digest, i.received_at, i.outcome AS callback_outcome
        FROM checks c JOIN attempts a ON a.id=(
          SELECT id FROM attempts WHERE case_id=c.case_id AND kind=c.kind AND input_revision=c.input_revision
          ORDER BY generation DESC LIMIT 1)
        LEFT JOIN callback_inbox i ON i.attempt_id=a.id AND i.case_id=a.case_id AND i.provider=a.kind
            AND i.disposition='APPLIED'
        WHERE c.case_id=? AND c.kind=?""", (case, kind)).fetchone()


def authentic_result(attempt, merchant):
    return (attempt is not None and attempt["event_id"] is not None
            and attempt["status"] == attempt["callback_outcome"]
            and attempt["sent_at"] is not None and attempt["deadline"] is not None
            and attempt["sent_at"] <= attempt["received_at"] < attempt["deadline"]
            and attempt["settled_at"] == attempt["received_at"]
            and attempt["inputs"] == packed(provider_inputs(attempt["kind"], merchant)))


def qualified_check(conn, case, kind, policy, merchant):
    attempt = current_attempt(conn, case, kind)
    if not authentic_result(attempt, merchant):
        return None
    acceptance = None
    if attempt["status"] == "REFERRED" and kind == "RISK" and kind in policy["referral_kinds"]:
        acceptance = conn.execute("""SELECT id,issue_id,issue_version,actor,reason,at FROM decisions
            WHERE case_id=? AND attempt_id=? AND subject_revision=? AND action='ACCEPT_REFERRAL'""",
                                  (case, attempt["id"], attempt["input_revision"])).fetchone()
        if not acceptance:
            return None
    elif attempt["status"] != "PASS":
        return None
    if kind == "BANK":
        band = name_band(attempt["name_score"]) if attempt["name_score"] is not None else None
        if band == "REVIEW":
            # Review I1: a weak registered-beneficiary match qualifies only with an operator acceptance bound to
            # this exact BANK attempt and input revision. CONFLICT-band scores and non-PASS results never qualify.
            acceptance = beneficiary_acceptance(conn, case, attempt)
            if not acceptance:
                return None
        elif band != "CONSISTENT":
            return None
    proof = {k: attempt[k] for k in ("kind", "input_revision", "generation", "status", "reference",
                                      "reason_code", "registered_name", "name_score", "event_id", "body_digest",
                                      "sent_at", "deadline", "received_at", "settled_at")}
    proof.update(attempt_id=attempt["id"], inputs_digest=digest(attempt["inputs"]))
    if acceptance:
        proof["acceptance"] = dict(acceptance)
    return proof


def beneficiary_acceptance(conn, case, attempt):
    return conn.execute("""SELECT id,issue_id,issue_version,actor,reason,at FROM decisions
        WHERE case_id=? AND attempt_id=? AND subject_revision=? AND action='ACCEPT_BENEFICIARY_MATCH'""",
                        (case, attempt["id"], attempt["input_revision"])).fetchone()


def evidence_snapshot(conn, case, revision, merchant):
    docs = conn.execute("""SELECT d.* FROM documents d WHERE case_id=? AND revision=(
        SELECT max(revision) FROM documents WHERE case_id=d.case_id AND kind=d.kind)""", (case,)).fetchall()
    if {d["kind"] for d in docs if d["payload"]} != set(LABELS):
        return None
    versions = {d["kind"]: d["revision"] for d in docs}
    if any(d["content_digest"] != digest(d["payload"]) for d in docs if d["payload"]):
        return None
    payloads = {d["kind"]: json.loads(d["payload"]) for d in docs if d["payload"]}
    current = {d["kind"]: (d["revision"], json.loads(d["payload"]) if d["payload"] else None) for d in docs}
    extractions = current_extractions(conn, case, current)
    # OCR-derived evidence only counts if the stored image and values are exactly what the engine produced, and
    # every field was read at or above its stored threshold (or accepted by an operator for this revision).
    if not verify_integrity(conn, extractions, payloads):
        return None
    accepted = accepted_fields(conn, case, current)
    fields = assess(merchant, payloads, extractions, accepted)
    for field in fields:
        if field["status"] != "CONSISTENT":
            return None
        field["source_revision"] = revision if field["document"] == "form" else versions[field["document"]]
    saved = conn.execute("SELECT results FROM assessments WHERE case_id=? AND data_revision=?", (case, revision)).fetchone()
    if not saved or saved["results"] != packed(fields):
        return None
    snapshot = dict(data_revision=revision, merchant=merchant, assessment_digest=digest(saved["results"]),
                    documents={d["kind"]: dict(revision=d["revision"], digest=d["content_digest"]) for d in docs},
                    fields=fields)
    if extractions:
        snapshot["extractions"] = {kind: dict(
            id=ex["id"], source_type=ex["source_type"], engine=ex["engine"], original_digest=ex["original_digest"],
            render_digest=ex["render_digest"], threshold=ex["threshold"], min_confidence=ex["min_confidence"],
            field_confidence={label: f["confidence"] for label, f in ex["fields"].items()},
            low_confidence_accepted=sorted(label for k, label in accepted if k == kind))
            for kind, ex in extractions.items()}
    return snapshot


def build_receipt(conn, row):
    case = row["id"]
    if conn.execute("SELECT 1 FROM review_items WHERE case_id=? AND status<>'RESOLVED'", (case,)).fetchone():
        return None
    policy, merchant = json.loads(row["policy"]), json.loads(row["merchant"])
    if not valid_policy(policy):
        return None
    # One Merchant ID per verified identity: refuse if another case already holds an approval for this PAN.
    if conn.execute("SELECT 1 FROM identity_claims WHERE pan=? AND case_id<>?", (merchant["pan"], case)).fetchone():
        return None
    kinds = {r[0] for r in conn.execute("SELECT kind FROM checks WHERE case_id=?", (case,))}
    if kinds != set(policy["required"]):
        return None
    winners = []
    for kind in policy["required"]:
        winner = qualified_check(conn, case, kind, policy, merchant)
        if not winner:
            return None
        winners.append(winner)
    evidence = evidence_snapshot(conn, case, row["data_revision"], merchant)
    if not evidence:
        return None
    clearances = [dict(r) for r in conn.execute("""SELECT d.id,d.issue_id,d.issue_version,d.subject_revision,d.attempt_id,
        d.actor,d.reason,d.at,s.scope FROM decisions d LEFT JOIN risk_clearance_scope s ON s.decision_id=d.id
        WHERE d.case_id=? AND d.action='CLEAR_RISK_CHANGE' ORDER BY d.id""", (case,))]
    # A historical adverse RISK result (referral or definitive failure) under different, self-declared inputs
    # requires an operator clearance bound to the current winning attempt (review B1). The history is the legal
    # entity's, not the application's: a rejected application with the same PAN or GSTIN counts the same way (audit B2).
    risk = next((w for w in winners if w["kind"] == "RISK"), None)
    if risk and (conn.execute("""SELECT 1 FROM attempts WHERE case_id=? AND kind='RISK'
            AND status IN ('REFERRED','FAIL') AND input_revision<?""", (case, risk["input_revision"])).fetchone()
                 or riskhistory.linked_adverse(conn, case, merchant)):
        # The clearance must be for this winning attempt AND still cover this application's rejected history.
        if not any(c["attempt_id"] == risk["attempt_id"]
                   and riskhistory.clearance_valid(conn, c["id"], case, merchant, risk["input_revision"])
                   for c in clearances):
            return None
    for clearance in clearances:
        del clearance["id"]
    return dict(case_id=case, policy=policy, evidence=evidence, checks=winners, risk_clearances=clearances)


def record_approval(conn, case, receipt, now):
    encoded = packed({**receipt, "approved_at": now})
    conn.execute("INSERT INTO approvals VALUES (?,?,?,?)", (case, now, encoded, digest(encoded)))
    conn.execute("INSERT INTO identity_claims VALUES (?,?)", (receipt["evidence"]["merchant"]["pan"], case))
    conn.execute("""INSERT INTO jobs(operation_key,case_id,kind,status,due_at)
                    VALUES (?,?,'ISSUE_MID','READY',?)""", ("mid/" + case, case, now))
