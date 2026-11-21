"""Merchant document upload: bounded bytes in, a server-produced extraction out."""
from contextlib import closing

from . import db, ocr
from .workflow import PRE, REJECTED, Problem, record_upload_failure, upload_document

KINDS = ("identity", "tax", "bank")


def upload(path, case, kind, expected_revision, data, now=None):
    """Read the document with the local OCR engine (outside any database transaction), then attach the result.
    The client supplies only bytes. Values, confidences and the image digest come from the engine."""
    if kind not in KINDS:
        raise Problem(404, "Unknown document type.")
    with closing(db.connect(path)) as conn:  # cheap early refusal; the write transaction re-checks everything
        row = conn.execute("SELECT state,data_revision FROM applications WHERE id=?", (case,)).fetchone()
    if row is None:
        raise Problem(404, "Case unavailable.")
    if row["state"] not in PRE:
        raise Problem(409, "Case rejected." if row["state"] == REJECTED else "Approved: servicing required.", "/cases/" + case)
    if row["data_revision"] != expected_revision:
        raise Problem(409, "Evidence changed. Reload the case before uploading.", "/cases/" + case)
    try:
        record, png = ocr.extract(kind, data)
    except ocr.Rejected as exc:
        # A refused file never replaces anything. If this save declared it, its hold stays (now as the merchant's
        # to repeat), so the evidence held before cannot be approved in its place (final release audit R1).
        record_upload_failure(path, case, kind, str(exc), now)
        raise Problem(422, str(exc)) from exc
    except ocr.Unavailable as exc:
        record_upload_failure(path, case, kind, "the document reader is unavailable", now)
        raise Problem(503, str(exc)) from exc
    upload_document(path, case, kind, record, png, expected_revision, now)
    return record
