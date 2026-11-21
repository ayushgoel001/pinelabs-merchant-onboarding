"""View-model helpers: plain functions that turn persisted case data into what the pages show.

Nothing here decides anything. Every status is read from rows the workflow already wrote; these functions only
choose words, order and tone for the operator and merchant pages.
"""
import time

from .extraction import name_band

STATE_TEXT = {
    "NEEDS_INFO": "Waiting for the merchant",
    "IN_REVIEW": "Needs an operator decision",
    "VERIFYING": "Verification in progress",
    "APPROVED_PENDING_MID": "Approved, issuing the Merchant ID",
    "PROVISIONING_FAILED": "Approved, Merchant ID issuance needs attention",
    "ACTIVE": "Active, Merchant ID issued",
    "REJECTED": "Rejected",
}
CHECK_WORDS = {
    "PASS": ("Passed", "ok"), "FAIL": ("Failed", "bad"), "REFERRED": ("Referred for review", "bad"),
    "SENT": ("Waiting for reply", "run"), "SCHEDULED": ("Queued", "wait"), "EXPIRED": ("Timed out", "wait"),
    "TRANSIENT": ("Provider error", "wait"), "SUPERSEDED": ("Superseded", "wait"),
}
SHORT = {"PAN": "PAN", "GST": "GST", "BANK": "BNK", "RISK": "RSK"}

# ---- plain language. The primary view says what happened and what to do; the codes stay in the technical sections.
DOC_NAMES = {"identity": "identity document", "tax": "tax (GST) document", "bank": "bank document"}
FIELD_NAMES = {"name": "business name", "pan": "PAN", "gst": "GST number", "beneficiary": "account holder name",
               "account": "bank account number", "routing": "routing code", "document": "document",
               "embedded_pan": "PAN inside the GST number"}
CHECK_NAMES = {"PAN": "PAN check", "GST": "GST check", "BANK": "bank account check", "RISK": "business risk check"}
CODES = {
    "GST_REGISTRATION_CANCELLED": "This GST registration has been cancelled.",
    "NAME_MISMATCH": "The business name does not match the name registered for this number.",
    "NOT_IN_SYNTHETIC_REGISTRY": "This number was not found in the registry.",
    "PROHIBITED_PROFILE": "This business category is not permitted in this postal zone.",
    "HIGH_RISK_ZONE": "This postal zone is high risk, so a person has to confirm the application.",
    "PROVIDER_TEMPORARILY_UNAVAILABLE": "The service had a temporary problem. We try again automatically.",
}
MERCHANT_HEADLINES = {
    "NEEDS_INFO": "We need something from you", "IN_REVIEW": "Our review team is looking at your application",
    "VERIFYING": "We are checking your details", "APPROVED_PENDING_MID": "Approved. We are creating your Merchant ID",
    "PROVISIONING_FAILED": "Approved. Your Merchant ID is taking longer than expected",
    "ACTIVE": "You are approved", "REJECTED": "Your application was not approved",
}
CONFIRMED = {"PAN": "The PAN is valid and belongs to this business.", "GST": "The GST registration is active.",
             "BANK": "The bank account exists and the account holder name matches.", "RISK": "No risk concerns were found."}


# The structured document the intake form builds from the business details ("use the details above").
TYPED_FIELDS = {"identity": {"name": "name", "pan": "pan"}, "tax": {"name": "name", "pan": "pan", "gst": "gst"},
                "bank": {"beneficiary": "name", "account": "account", "routing": "routing"}}


def typed_document(kind, merchant):
    return {field: merchant.get(source) for field, source in TYPED_FIELDS[kind].items()}


def document_rows(data):
    """Where each of the three documents stands, for the primary view: read from a file, typed, or missing.

    A typed document whose text differs from the business details (a name variant, or a demo conflict) says so,
    so the form does not claim it is "the details above"."""
    rows = []
    merchant = data.get("merchant")
    uploads = {i["subject"].split(".")[0]: i["cause"] for i in data.get("issues", ())
               if i["cause"] in ("UPLOAD_PENDING", "UPLOAD_FAILED")}
    for kind, title in (("identity", "Identity"), ("tax", "Tax (GST)"), ("bank", "Bank")):
        extraction = data.get("extractions", {}).get(kind)
        doc = data.get("documents", {}).get(kind)
        if kind in uploads:
            failed = uploads[kind] == "UPLOAD_FAILED"
            rows.append(dict(kind=kind, title=title, source="file" if extraction else ("typed" if doc else "missing"),
                             differs=False, tone="wait",
                             label="New file could not be used" if failed else "New file still uploading",
                             note=("Choose it again, or save without it to keep the copy we hold" if failed
                                   else "Checks and approval wait for it")))
        elif extraction:
            rows.append(dict(kind=kind, title=title, source="file", tone="ok", label="Read from your file", differs=False,
                             note=f"{extraction['source_type']} · reading confidence {extraction['min_confidence']:g}"))
        elif doc:
            differs = bool(merchant) and doc != typed_document(kind, merchant)
            # Neutral on purpose: a different spelling of the name can still be consistent; the checks decide that.
            rows.append(dict(kind=kind, title=title, source="typed", differs=differs, tone="info",
                             label="Typed separately" if differs else "Typed details",
                             note="Its text is not the same as the business details" if differs else "Entered on the form, not a file"))
        else:
            rows.append(dict(kind=kind, title=title, source="missing", tone="wait", label="Not provided yet", differs=False,
                             note="Upload a PNG, JPEG or PDF"))
    return rows


def code_of(text):
    return next((code for code in CODES if code in (text or "")), None)


def explain_issue(issue):
    """What an open issue means, in words a merchant or an operator can act on. `technical` keeps the exact record."""
    cause, subject, owner = issue["cause"], issue["subject"], issue["owner"]
    reason = issue.get("reason") or ""
    kind, _, label = subject.partition(".")
    doc, field = DOC_NAMES.get(kind, kind), FIELD_NAMES.get(label, label)
    check = CHECK_NAMES.get(subject.removeprefix("check."), "check")
    meaning = CODES.get(code_of(reason), "")
    out = dict(owner=owner, technical=reason if reason.startswith(subject) else f"{subject} · {reason}", title=reason,
               why="", merchant="", operator="")
    if cause == "EVIDENCE_MISSING":
        whole = label == "document"
        out.update(title=f"The {doc} has not been provided" if whole else f"The {field} is missing from the {doc}",
                   why="The checks cannot start until it is supplied.",
                   merchant=f"Upload the {doc} (a PNG, JPEG or PDF) or use the details you typed.",
                   operator="Waiting for the merchant. There is nothing to decide yet.")
    elif cause == "EVIDENCE_CONFLICT":
        several = "different values" in reason
        out.update(title=f"The {field} on the {doc} does not match the application",
                   why=(f"The document shows more than one value for the {field}, so none of them can be trusted."
                        if several else "The value in the document differs from the one entered in the application."),
                   merchant="Upload a clear, corrected document or correct the details in the application.",
                   operator="A contradiction cannot be accepted. Ask for a corrected document or reject the application.")
    elif cause == "EVIDENCE_UNREADABLE":
        old = "before contradiction checks" in reason or "predates" in reason
        out.update(title=f"The {field} on the {doc} could not be read with enough confidence",
                   why=("This document was read by an earlier version of the reader and has to be uploaded again."
                        if old else "The scan may be blurry, or the value is not in the expected format."),
                   merchant="Our team will review it. You can speed this up by uploading a sharper copy.",
                   operator="Ask for a new upload. If the image is clearly correct and the value was only read with low confidence, you may accept it.")
    elif cause == "DUPLICATE_CANDIDATE":
        hard = "cannot be dismissed" in reason
        out.update(title="This looks like an application we already have",
                   why=reason.split(" · ")[0].replace(" (exact after normalization)", "") + (
                       ". The same PAN or GSTIN cannot be treated as a different business." if hard
                       else ". It may still be a different business."),
                   merchant="Nothing to do. Our review team will contact you if they need anything.",
                   operator="Dismiss it only if it is clearly a separate business. Otherwise ask for a correction or reject.")
    elif cause == "CHECK_FAILED":
        out.update(title=f"The {check} failed", why=meaning or "The provider did not accept the details.",
                   merchant="You can correct the details and save. The check then runs again.",
                   operator="Ask the merchant to correct the details, or reject the application.")
    elif cause == "CHECK_REFERRED":
        out.update(title=f"The {check} needs a person to confirm", why=meaning or "The provider asked for a human decision.",
                   merchant="Nothing to do. A reviewer will decide.",
                   operator="Accept the referral if the business activity is confirmed, ask for a correction, or reject.")
    elif cause == "CHECK_EXHAUSTED":
        out.update(title=f"The {check} did not get an answer in time",
                   why="The provider did not reply after several automatic tries.",
                   merchant="Nothing to do for now.", operator="Authorize one more try, or reject the application.")
    elif cause == "BENEFICIARY_REVIEW":
        out.update(title="The bank's account holder name is not an exact match",
                   why="The name registered with the bank differs from the business name.",
                   merchant="Nothing to do. A reviewer will confirm the name.",
                   operator="Accept the match if both names clearly refer to the same business, otherwise ask for a correction.")
    elif cause == "RISK_INPUTS_CHANGED_AFTER_REFERRAL":
        out.update(title="An earlier risk result needs a person's confirmation",
                   why=("The same legal entity had an application rejected for risk. " if reason.startswith("Same legal entity")
                        else "The business category or zone changed after an adverse risk result. ") +
                       "A reviewer has to confirm the new profile.",
                   merchant="Nothing to do. A reviewer will decide.",
                   operator="Clear it only if the updated business profile is corroborated by evidence.")
    elif cause == "UPLOAD_PENDING":
        out.update(title=f"The new {doc} is still being uploaded",
                   why="Nothing is checked or approved until it has been read, so the earlier copy cannot be used instead.",
                   merchant=f"If the upload was interrupted, open Update my application and choose the file again, or save without it to keep the {doc} we hold.",
                   operator="Waiting for the merchant's file. There is nothing to decide.")
    elif cause == "UPLOAD_FAILED":
        out.update(title=f"The new {doc} could not be used",
                   why=(reason.split("could not be used: ", 1)[1].rstrip(".") + "." if "could not be used: " in reason
                        else "The file could not be read."),
                   merchant=f"Choose the file again (a PNG, JPEG or PDF up to 2 MiB) and save, or save without a file to keep the {doc} we hold.",
                   operator="Waiting for the merchant to send the document again or keep the earlier one.")
    elif cause == "STALLED":
        out.update(title="This application is stuck", why="Nothing is queued and it cannot be approved yet.",
                   merchant="Nothing to do. Our team will look at it.", operator="Restore the unsent work, or reject the application.")
    return out


def plain_check(kind, status, reason, qualified, band, held):
    if status is None:
        return "Not started yet."
    if held:
        return "Paused while another question on this application is open."
    if status == "PASS" and band == "REVIEW":
        return "The account exists, but the account holder name needs a person to confirm."
    if status == "PASS" and band == "CONFLICT":
        return "The account exists, but the account holder name does not match."
    if status == "PASS":
        return CONFIRMED.get(kind, "Confirmed.")
    if status in ("FAIL", "REFERRED"):
        return CODES.get(code_of(reason), "The provider did not accept the details." if status == "FAIL" else "A person has to confirm this.")
    return {"SENT": "Waiting for the provider's reply.", "SCHEDULED": "Queued to be sent.",
            "EXPIRED": "No reply in time. We try again automatically.",
            "TRANSIENT": "The service had a temporary problem. We try again automatically.",
            "SUPERSEDED": "Replaced by a newer check after the details changed."}.get(status, "")


EVIDENCE_CAUSES = ("EVIDENCE_MISSING", "EVIDENCE_CONFLICT", "EVIDENCE_UNREADABLE", "DUPLICATE_CANDIDATE")


def accepted_kinds(data):
    """Check kinds whose sealed approval carries an operator acceptance (a referral or a weak beneficiary match)."""
    approval = data.get("approval")
    if not approval:
        return set()
    return {w["kind"] for w in approval["receipt"]["checks"] if w.get("acceptance")}


def latest_checks(attempts, required, accepted=(), accepted_attempts=frozenset()):
    """The newest generation of each required check at its current input revision, as card data.

    `qualified` follows approval.qualified_check, not the provider's word alone (final audit I5): a BANK PASS whose
    registered beneficiary is outside the CONSISTENT name band does not qualify until an operator accepts that
    attempt, so it is shown as passed but still waiting for review. `accepted_attempts` are the attempts an operator
    accepted before any approval exists (a referral or a beneficiary match)."""
    cards = []
    for kind in required:
        current = [a for a in attempts if a["kind"] == kind and a["input_revision"] == a["current_revision"]]
        if not current:
            cards.append(dict(kind=kind, short=SHORT.get(kind, kind), word="Not started", tone="wait", status=None,
                              generation=None, latency=None, reason=None, reference=None, held=False, qualified=False, plain="Not started yet."))
            continue
        latest = max(current, key=lambda a: a["generation"])
        word, tone = CHECK_WORDS.get(latest["status"], (latest["status"].title(), "wait"))
        held = latest["status"] == "SCHEDULED" and latest.get("job_status") == "HELD"
        if held:
            word, tone = "Paused by an open hold", "wait"
        score = latest.get("name_score")
        band = name_band(score) if kind == "BANK" and latest["status"] == "PASS" and score is not None else "CONSISTENT"
        taken = kind in accepted or latest.get("id") in accepted_attempts
        qualified = taken or (latest["status"] == "PASS" and band == "CONSISTENT")
        if band != "CONSISTENT" and not taken:
            word, tone = ("Passed · beneficiary name needs review", "wait") if band == "REVIEW" else                 ("Passed · beneficiary name conflicts", "bad")
        if taken:
            word, tone = word + " · accepted by an operator", "ok"
        latency = (latest["settled_at"] - latest["sent_at"]) if latest["sent_at"] and latest["settled_at"] \
            and latest["status"] in ("PASS", "FAIL", "REFERRED", "TRANSIENT") else None
        cards.append(dict(kind=kind, short=SHORT.get(kind, kind), word=word, tone=tone, status=latest["status"],
                          generation=latest["generation"], latency=latency, reason=latest["reason_code"],
                          reference=latest["reference"], held=held, qualified=qualified,
                          plain=plain_check(kind, latest["status"], latest["reason_code"], qualified, band, held)))
    return cards


def progress(data, checks):
    """Five steps from submission to Merchant ID. Each is done, active, attention, bad or todo."""
    state = data["state"]
    causes = {i["cause"] for i in data["issues"]}
    evidence_open = bool(causes & set(EVIDENCE_CAUSES))
    accepted = accepted_kinds(data)
    passed = sum(1 for c in checks if c["qualified"] or c["kind"] in accepted)
    failed = any(c["status"] in ("FAIL", "REFERRED") and not c["qualified"] and c["kind"] not in accepted for c in checks)
    verified = bool(checks) and passed == len(checks)
    steps = [dict(label="Submitted", status="done", note="")]
    if state == "REJECTED":
        steps += [dict(label="Evidence", status="todo", note=""), dict(label="Verification", status="todo", note=""),
                  dict(label="Approval", status="todo", note=""), dict(label="Merchant ID", status="todo", note="")]
        steps[3]["status"] = "bad"  # a rejection is the decision, so it shows on the Approval step
        steps[3]["note"] = "Rejected"
        return steps
    steps.append(dict(label="Evidence", status="attention" if evidence_open else "done",
                      note="Needs action" if evidence_open else "Consistent"))
    if verified:
        extra = f", {len(accepted)} accepted by an operator" if accepted else ""
        verification = dict(status="done", note=f"{passed} of {len(checks)} qualified{extra}")
    elif failed or "CHECK_EXHAUSTED" in causes or "CHECK_REFERRED" in causes or "BENEFICIARY_REVIEW" in causes:
        verification = dict(status="attention", note=f"{passed} of {len(checks)} passed")
    elif evidence_open and not passed:
        verification = dict(status="todo", note="Waiting for evidence")
    else:
        verification = dict(status="active", note=f"{passed} of {len(checks)} passed")
    steps.append(dict(label="Verification", **verification))
    approved = bool(data.get("approval"))
    steps.append(dict(label="Approval", status="done" if approved else ("attention" if state == "IN_REVIEW" and verified else "todo"),
                      note="Receipt sealed" if approved else ""))
    if data.get("mid"):
        steps.append(dict(label="Merchant ID", status="done", note="Issued"))
    elif state == "PROVISIONING_FAILED":
        steps.append(dict(label="Merchant ID", status="attention", note="Recovering"))
    elif state == "APPROVED_PENDING_MID":
        steps.append(dict(label="Merchant ID", status="active", note="Issuing"))
    else:
        steps.append(dict(label="Merchant ID", status="todo", note=""))
    return steps


def summary(data, checks):
    """One sentence for the top of the page: who has to do what, or what is happening."""
    state = data["state"]
    owners = [i for i in data["issues"] if i["status"] != "RESOLVED"]
    operator = [i for i in owners if i["owner"] == "OPERATOR"]
    merchant = [i for i in owners if i["owner"] == "MERCHANT"]
    accepted = accepted_kinds(data)
    passed = sum(1 for c in checks if c["status"] == "PASS")
    qualified = sum(1 for c in checks if c["qualified"])
    if state == "ACTIVE":
        how = ("Every required check qualified: " + f"{passed - len([c for c in checks if c['status'] == 'PASS' and c['kind'] in accepted])} passed outright and "
               f"{len(accepted)} accepted by an operator, with the reason on the receipt.") if accepted else "Every required check passed."
        return dict(tone="ok", headline=STATE_TEXT[state], detail=how + " The issuer receipt is recorded.")
    if state == "REJECTED":
        return dict(tone="bad", headline=STATE_TEXT[state], detail="The decision and its reason are in the case history.")
    if state in ("APPROVED_PENDING_MID", "PROVISIONING_FAILED"):
        return dict(tone="info" if state == "APPROVED_PENDING_MID" else "bad", headline=STATE_TEXT[state],
                    detail="The approval receipt is sealed. Issuance retries with the same key, so a Merchant ID is never created twice.")
    if state == "IN_REVIEW" and operator:
        first = explain_issue(operator[0])
        return dict(tone="bad", headline=STATE_TEXT[state], detail=f"{first['title']}. {first['why']}".strip())
    if state == "NEEDS_INFO" and merchant:
        first = explain_issue(merchant[0])
        return dict(tone="wait", headline=STATE_TEXT[state], detail=f"{first['title']}. {first['merchant']}".strip())
    if state == "VERIFYING":
        return dict(tone="info", headline=STATE_TEXT[state], detail=f"{qualified} of {len(checks)} required checks have qualified.")
    return dict(tone="wait", headline=STATE_TEXT.get(state, state), detail="")


def event_kind(event, actor):
    if actor == "operator":
        return "human"
    if event == "STATE_ENTERED":
        return "state"
    if event.startswith(("ISSUE", "DUPLICATE", "RISK_CHANGE", "DISPATCH_HELD", "ATTEMPT_EXPIRED", "JOB_DEAD", "MID_DEAD")):
        return "issue"
    if event in ("CALLBACK_CONFLICT", "REQUEST_RETRY", "MID_LEASE_EXPIRED", "PROVIDER_ANOMALY"):
        return "fail"
    if event.startswith(("CALLBACK", "ATTEMPT", "REQUEST", "MID", "APPROVAL")):
        return "provider"
    return "plain"


def ago(value, now=None):
    if value is None:
        return "—"
    seconds = max(0, (time.time() if now is None else now) - value)
    if seconds < 60:
        return f"{int(seconds)} s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def duration(seconds):
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60):02d} s"
    return f"{int(seconds // 3600)} h {int(seconds % 3600 // 60):02d} min"


def percent(value):
    return "—" if value is None else f"{value * 100:.0f}%"
