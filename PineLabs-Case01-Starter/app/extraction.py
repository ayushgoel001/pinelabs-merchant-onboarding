"""Structured document extraction and synthetic input contracts."""
import re
from difflib import SequenceMatcher
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TOKEN_PATTERNS = {
    "pan": r"SYNPAN-[0-9]{4}",
    "gst": r"SYNGST-[0-9]{2}-(SYNPAN-[0-9]{4})-[1-9]",
    "account": r"SYNACCT-[0-9]{4}",
    "routing": r"SYNIFSC-[0-9]{4}",
}
INPUTS = {"PAN": ("name", "pan"), "GST": ("name", "pan", "gst"),
          "BANK": ("name", "account", "routing"), "RISK": ("category", "zone")}
LABELS = {"identity": ("name", "pan"), "tax": ("name", "pan", "gst"),
          "bank": ("beneficiary", "account", "routing")}
NAME_WORDS = set("ACME TRADERS TRADING TRADE PRIVATE LIMITED PVT LTD ASTER MEADOW MARKET "
                 "STORES RIVER CEDAR SUPPLIES WORKS VALLEY PAPER LANTERN GOODS".split())
SAFE_MESSAGE = "Use the synthetic sample formats and business names shown on the form."


def normalized_name(value):
    words = re.findall(r"[A-Z]+", value.upper())
    expanded = {"PVT": "PRIVATE", "LTD": "LIMITED"}
    return " ".join(expanded.get(word, word) for word in words)


def provider_inputs(kind, merchant):
    return {key: normalized_name(merchant[key]) if key == "name" else merchant[key] for key in INPUTS[kind]}


def name_score(expected, observed):
    """Normalized similarity, 0–100, shared by document fields and the BANK registered beneficiary."""
    return round(100 * SequenceMatcher(None, normalized_name(expected), normalized_name(observed),
                                       autojunk=False).ratio(), 1)


def name_band(score):
    return "CONSISTENT" if score >= 95 else "REVIEW" if score >= 65 else "CONFLICT"


def clean_value(label, value):
    value = value.strip()
    if label in TOKEN_PATTERNS:
        if not re.fullmatch(TOKEN_PATTERNS[label], value):
            raise ValueError(SAFE_MESSAGE)
    else:
        words = normalized_name(value).split()
        if (len(value) > 80 or not re.fullmatch(r"[A-Za-z ., &'’-]+", value)
                or len(words) < 2 or not set(words) <= NAME_WORDS):
            raise ValueError(SAFE_MESSAGE)
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Merchant(StrictModel):
    name: str
    pan: str
    gst: str
    account: str
    routing: str
    category: Literal["RETAIL", "SERVICES", "WHOLESALE"]
    zone: Literal["SYNZONE-01", "SYNZONE-02", "SYNZONE-03"]

    @field_validator("name", "pan", "gst", "account", "routing")
    @classmethod
    def synthetic_field(cls, value, info):
        return clean_value(info.field_name, value)


class EvidenceEnvelope(StrictModel):
    merchant: Merchant
    documents: dict[str, dict[str, str]]
    # Documents this save sends as files right after it (final release audit R1). Declaring them in the same request
    # puts a durable hold on the case before anything else can happen: no dispatch, no approval and no use of the
    # document held so far, until each file has been read or the merchant saves again without it.
    uploads: list[Literal["identity", "tax", "bank"]] = Field(default_factory=list, max_length=3)

    @field_validator("documents")
    @classmethod
    def structured_only(cls, documents):
        if not documents.keys() <= LABELS.keys():
            raise ValueError("Supply at least one supported structured document.")
        for kind, payload in documents.items():
            if not payload.keys() <= set(LABELS[kind]):
                raise ValueError("Use the labels shown in the sample.")
            documents[kind] = {label: clean_value(label, value)
                               for label, value in payload.items() if value.strip()}
        return documents

    @field_validator("uploads")
    @classmethod
    def distinct_uploads(cls, uploads):
        if len(set(uploads)) != len(uploads):
            raise ValueError("Declare each uploaded document once.")
        return sorted(uploads)

    @model_validator(mode="after")
    def something_supplied(self):
        if not self.documents and not self.uploads:
            raise ValueError("Supply at least one supported structured document.")
        return self


class Submission(EvidenceEnvelope):
    submission_key: UUID


class Amendment(EvidenceEnvelope):
    data_revision: int = Field(strict=True, ge=1)


def is_legacy(extraction):
    """An extraction stored by a build that kept only the first labelled line of each field. Its fields carry no
    candidate list, so a second, different value on the same page may have been discarded before it was stored and
    nothing in the record can say otherwise."""
    return any("candidates" not in field for field in extraction["fields"].values())


def ocr_observation(observe, kind, label, expected, extraction, accepted):
    field = extraction["fields"].get(label) or {}
    if field and "candidates" not in field:
        # Unvalidated evidence cannot qualify, and neither can an operator accept it: only a new upload, read with the
        # current parser, replaces it. The value is shown so that the operator can see what was stored.
        observe(kind, label, "UNREADABLE", "OCR record predates contradiction checks · upload the document again",
                field.get("value") or "", expected, source="OCR", confidence=None, threshold=extraction["threshold"],
                legacy=True)
        return
    value, confidence, threshold = field.get("value"), field.get("confidence"), extraction["threshold"]
    meta = dict(source="OCR", confidence=confidence, threshold=threshold)
    if value is None:
        candidates = field.get("candidates") or []
        if len(candidates) > 1:
            # The image shows different readable values for one field. Choosing the first, the last or the one that
            # agrees with the application would silently discard a material identifier, so none is chosen: the
            # document contradicts itself, whatever the application says. Only a replacement document clears it.
            values = [c["value"] for c in candidates]
            confidences = ", ".join(f"{c['confidence']:g}" for c in candidates)
            observe(kind, label, "CONFLICT", f"OCR: {len(values)} different values read for this field (confidence {confidences})",
                    " / ".join(values), expected, source="OCR", confidence=None, threshold=threshold, candidates=values)
            return
        if not field.get("found"):
            method = "OCR: label not found"
        elif candidates:
            method = "OCR: another line for this label could not be read"
        else:
            method = "OCR: value not a valid synthetic value"
        observe(kind, label, "UNREADABLE", method, "", expected, **meta)
        return
    if label in {"name", "beneficiary"}:
        score = name_score(expected, value)
        status, method = name_band(score), "OCR text · normalized name similarity"
    else:
        score = None
        status, method = ("CONSISTENT" if value == expected else "CONFLICT"), "OCR text · exact agreement"
    if status == "CONSISTENT" and confidence < threshold:
        if accepted:
            method += f" · confidence {confidence:g} below {threshold:g}, accepted by operator"
        else:
            status, method = "UNREADABLE", f"OCR confidence {confidence:g} below {threshold:g}"
    observe(kind, label, status, method, value, expected, score, **meta)


def assess(merchant, documents, extractions=None, accepted=frozenset()):
    """Return individual observations; each issue can name its precise source.

    `extractions` maps a document kind to its stored OCR extraction. Those documents are judged from the OCR
    value *and* its measured confidence: a field that could not be read, or was read below the stored threshold,
    is UNREADABLE (operator review) unless it agrees with the application and an operator accepted it for this
    document revision. Structured documents carry no confidence and are compared as before."""
    rows = []
    extractions = extractions or {}

    def observe(doc, label, status, method, actual="", expected="", score=None, **extra):
        rows.append(dict(document=doc, label=label, status=status, method=method,
                         actual=actual, expected=expected, score=score, **extra))

    for kind, labels in LABELS.items():
        if kind not in documents:
            observe(kind, "document", "MISSING", "Required document")
            continue
        for label in labels:
            expected = merchant["name" if label == "beneficiary" else label]
            if kind in extractions:
                ocr_observation(observe, kind, label, expected, extractions[kind], (kind, label) in accepted)
                continue
            value = documents[kind].get(label, "")
            if not value:
                observe(kind, label, "MISSING", "Required label", expected=expected)
            elif label in {"name", "beneficiary"}:
                score = name_score(expected, value)
                observe(kind, label, name_band(score), "Normalized name similarity", value, expected, score)
            else:
                observe(kind, label, "CONSISTENT" if value == expected else "CONFLICT",
                        "Synthetic format and exact agreement", value, expected)

    # The GST contains a PAN independently of the PAN printed on the identity document.
    identity_pan = documents.get("identity", {}).get("pan", merchant["pan"])
    for source, gst in (("form", merchant["gst"]), ("tax", documents.get("tax", {}).get("gst"))):
        if gst:
            embedded = re.fullmatch(TOKEN_PATTERNS["gst"], gst).group(1)
            observe(source, "embedded_pan", "CONSISTENT" if embedded == identity_pan else "CONFLICT",
                    "GST-embedded PAN vs identity PAN", embedded, identity_pan)
    return rows


def masked(value):
    if " / " in value:  # the readable candidates of a field that contradicts itself
        return " / ".join(masked(part) for part in value.split(" / "))
    if value.startswith(("SYNPAN-", "SYNGST-", "SYNACCT-", "SYNIFSC-")):
        return value.split("-")[0] + " ··" + value[-2:]
    return value


SAMPLE_MERCHANT = dict(name="Acme Traders Private Limited", pan="SYNPAN-0001",
                       gst="SYNGST-27-SYNPAN-0001-1", account="SYNACCT-0001",
                       routing="SYNIFSC-0001", category="RETAIL", zone="SYNZONE-01")
SAMPLE_DOCS = {
    "identity": {"name": "ACME TRADERS PVT LTD", "pan": "SYNPAN-0001"},
    "tax": {"name": "ACME TRADERS PVT LTD", "pan": "SYNPAN-0001", "gst": "SYNGST-27-SYNPAN-0001-1"},
    "bank": {"beneficiary": "Acme Traders Private Limited", "account": "SYNACCT-0001",
             "routing": "SYNIFSC-0001"},
}
