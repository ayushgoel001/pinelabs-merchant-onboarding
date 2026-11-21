"""Explainable duplicate-application detection, enforced by the backend inside every mutation transaction.

Identity keys come from the merchant's own application fields, normalized the same way everywhere:
    PAN            exact synthetic PAN                              HARD   (same legal entity)
    GSTIN          exact synthetic GSTIN                            HARD
    BANK_ACCOUNT   exact bank account token and routing token       SOFT   (may be a legitimate shared account)
    NAME_AND_ZONE  normalized legal name and postal zone            SOFT   (different entities can share a name)

A HARD match can never be dismissed: one legal entity gets one application and one Merchant ID. The way out is to
fix the data (the merchant corrects a mistyped identifier and the match disappears on the next evaluation) or to
reject the later application. A SOFT match is a candidate an operator can dismiss with a reason; the dismissal is
bound to the full identities of the pair and lapses as soon as either identity changes in any attribute.

Which case carries the flag: the later one. Case X is held against case Y when Y is not rejected and either Y is
older (earlier insertion order) or Y already holds an approval. Both cases run this scan inside their own
BEGIN IMMEDIATE transaction before they can be approved, so two simultaneous submissions serialize and exactly
one of them sees the other.
"""
import json

from .db import digest, packed
from .extraction import normalized_name

CAUSE = "DUPLICATE_CANDIDATE"
HARD = ("PAN", "GSTIN")
DECIDED = ("APPROVED_PENDING_MID", "PROVISIONING_FAILED", "ACTIVE")


def identity(merchant):
    return dict(PAN=merchant["pan"].strip().upper(), GSTIN=merchant["gst"].strip().upper(),
                BANK_ACCOUNT=f"{merchant['account'].strip().upper()}|{merchant['routing'].strip().upper()}",
                NAME_AND_ZONE=f"{normalized_name(merchant['name'])}|{merchant['zone']}")


def match_rules(a, b):
    ia, ib = identity(a), identity(b)
    return [rule for rule in ("PAN", "GSTIN", "BANK_ACCOUNT", "NAME_AND_ZONE") if ia[rule] == ib[rule]]


def pair_digest(rules, a, b):
    """Changes whenever ANY identity value of either application changes, which ends a dismissal (final audit I2).
    It used to cover only the attributes that matched, so a dismissal given for a name-and-zone match survived a
    change of PAN and GSTIN: the operator had judged a different applicant. Restoring the reviewed values restores
    the digest, and so the dismissal, as before."""
    return digest(packed([list(rules), identity(a), identity(b)]))


def is_hard(rules):
    return bool(set(rules) & set(HARD))


def other_of(subject):
    return subject.removeprefix("candidate.")


def describe(rules, other_row, same_session):
    names = {"PAN": "PAN", "GSTIN": "GSTIN", "BANK_ACCOUNT": "bank account and routing",
             "NAME_AND_ZONE": "legal name and zone"}
    detail = f"Matches case {other_row['id'][:10]} on " + ", ".join(names[r] for r in rules) + " (exact after normalization)"
    detail += " · cannot be dismissed, one entity gets one application" if is_hard(rules) else " · operator may dismiss with a reason"
    if same_session and is_hard(rules):
        detail += " · likely a repeat submission of the same application"
    return detail


def candidates(conn, case, row):
    """Cases this one must be held against."""
    return conn.execute("""SELECT o.id,o.merchant,o.state,o.submitter_hash,o.request_digest,
            (o.rowid < ?) AS older FROM applications o WHERE o.id<>? AND o.state IS NOT NULL AND o.state<>'REJECTED'
            AND (o.rowid < ? OR o.state IN ('APPROVED_PENDING_MID','PROVISIONING_FAILED','ACTIVE'))""",
                        (row["rowid"], case, row["rowid"])).fetchall()


def scan(conn, case, now, event):
    """Open, update or resolve DUPLICATE_CANDIDATE issues for a pre-approval case. Returns the number now open."""
    row = conn.execute("SELECT rowid,* FROM applications WHERE id=?", (case,)).fetchone()
    merchant = json.loads(row["merchant"])
    wanted = {}
    for other in candidates(conn, case, row):
        rules = match_rules(merchant, json.loads(other["merchant"]))
        if not rules:
            continue
        digest_ = pair_digest(rules, merchant, json.loads(other["merchant"]))
        dismissed = conn.execute("""SELECT 1 FROM duplicate_dismissals WHERE case_id=? AND other_case_id=?
                                  AND pair_digest=?""", (case, other["id"], digest_)).fetchone()
        if dismissed and not is_hard(rules):
            continue
        same_session = other["submitter_hash"] == row["submitter_hash"] and other["request_digest"] == row["request_digest"]
        wanted["candidate." + other["id"]] = describe(rules, other, same_session)
    active = {r["subject"]: r for r in conn.execute(
        "SELECT * FROM review_items WHERE case_id=? AND cause=? AND status<>'RESOLVED'", (case, CAUSE))}
    for subject, reason in wanted.items():
        existing = active.pop(subject, None)
        if existing is None:
            conn.execute("""INSERT INTO review_items(case_id,cause,subject,subject_revision,owner,status,reason)
                         VALUES (?,?,?,?,'OPERATOR','OPEN',?)""", (case, CAUSE, subject, row["data_revision"], reason))
            event(conn, case, "DUPLICATE_FLAGGED", reason, at=now)
        elif existing["reason"] != reason:
            conn.execute("""UPDATE review_items SET reason=?,subject_revision=?,owner='OPERATOR',version=version+1
                         WHERE id=?""", (reason, row["data_revision"], existing["id"]))
            event(conn, case, "DUPLICATE_UPDATED", reason, at=now)
    for subject, item in active.items():  # no longer a match: identity changed, other case rejected or dismissed
        conn.execute("UPDATE review_items SET status='RESOLVED',version=version+1 WHERE id=?", (item["id"],))
        event(conn, case, "DUPLICATE_CLEARED", f"{subject.replace('candidate.', 'case ')[:19]} no longer matches", at=now)
    return len(wanted)


def dismissible(conn, case, issue):
    """Backend decision, recomputed at commit time: a soft match against a case that is still a live match."""
    row = conn.execute("SELECT * FROM applications WHERE id=?", (case,)).fetchone()
    other = conn.execute("SELECT * FROM applications WHERE id=?", (other_of(issue["subject"]),)).fetchone()
    if not other or other["state"] == "REJECTED":
        return None
    rules = match_rules(json.loads(row["merchant"]), json.loads(other["merchant"]))
    if not rules or is_hard(rules):
        return None
    return pair_digest(rules, json.loads(row["merchant"]), json.loads(other["merchant"])), other["id"]
