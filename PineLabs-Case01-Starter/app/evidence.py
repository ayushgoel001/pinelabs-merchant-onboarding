"""Persistence helpers for OCR extractions and the operator acceptances that qualify them."""
import hashlib
import json

from .db import packed


def insert_extraction(conn, case, kind, revision, record, png, now):
    """Write the extraction and the exact PNG the OCR engine read. Both are immutable once stored."""
    conn.execute("""INSERT INTO files(digest,mime,data) SELECT ?,'image/png',? WHERE NOT EXISTS
                    (SELECT 1 FROM files WHERE digest=?)""", (record["render_digest"], png, record["render_digest"]))
    conn.execute("""INSERT INTO extractions(case_id,kind,revision,source_type,original_digest,render_digest,engine,
                    threshold,min_confidence,fields,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                 (case, kind, revision, record["source_type"], record["original_digest"], record["render_digest"],
                  record["engine"], record["threshold"], record["min_confidence"], packed(record["fields"]), now))


def current_extractions(conn, case, current):
    """Extractions attached to the *current* revision of each document. `current` is {kind: (revision, payload)}."""
    found = {}
    for row in conn.execute("SELECT * FROM extractions WHERE case_id=?", (case,)):
        revision = current.get(row["kind"], (None, None))[0]
        if revision == row["revision"] and current[row["kind"]][1] is not None:
            found[row["kind"]] = dict(row, fields=json.loads(row["fields"]))
    return found


def accepted_fields(conn, case, current):
    """(kind, label) pairs an operator accepted for the document revision that is current now."""
    accepted = set()
    for row in conn.execute("""SELECT r.subject,d.subject_revision FROM decisions d JOIN review_items r ON r.id=d.issue_id
                            WHERE d.case_id=? AND d.action='ACCEPT_EXTRACTION'""", (case,)):
        kind, _, label = row["subject"].partition(".")
        if current.get(kind, (None,))[0] == row["subject_revision"]:
            accepted.add((kind, label))
    return accepted


def verify_integrity(conn, extractions, documents):
    """The stored image must still hash to its recorded digest, and every extracted value must equal the saved
    document payload. A mismatch means the record is not what the OCR engine produced and cannot qualify."""
    for kind, ex in extractions.items():
        blob = conn.execute("SELECT data FROM files WHERE digest=?", (ex["render_digest"],)).fetchone()
        if blob is None or hashlib.sha256(blob["data"]).hexdigest() != ex["render_digest"]:
            return False
        payload = documents.get(kind)
        expected = {label: f["value"] for label, f in ex["fields"].items() if f["value"] is not None}
        if payload is None or payload != expected:
            return False
        if not isinstance(ex["min_confidence"], (int, float)):
            return False
    return True
