"""Bounded local document intelligence: PNG, JPEG or PDF bytes -> labelled field candidates with confidence.

Only the server can produce an extraction: the upload endpoint passes raw bytes in and this module returns
the values, their measured Tesseract word confidence and a digest of the exact image the OCR engine saw.
Nothing a client sends is ever treated as a value or a confidence. Anything that is not a synthetic token or
a name from the demo vocabulary is dropped without being stored or echoed (no raw OCR text is retained).

Bounds: 2 MiB upload, 4 megapixels after decoding, rendering AND preprocessing, one PDF page rendered, 8 s OCR timeout.
Engine: local Tesseract through pytesseract (no network, no paid service). PDFs are rendered with pypdfium2.
"""
import hashlib
import io
import os
import re
import shutil
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .extraction import LABELS, TOKEN_PATTERNS, clean_value, normalized_name

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4_000_000
OCR_TIMEOUT_S = 8
# Review threshold for a field's lowest word confidence (0-100). A demonstration heuristic chosen from measured
# synthetic documents (see evidence/TEST_EVIDENCE.md): clear synthetic tokens read 58-96, strongly blurred ones lower.
# It is a routing aid, never an approval signal: every value must also match the application exactly (tokens) or
# inside the CONSISTENT name band, and low-confidence fields need a human.
MIN_CONFIDENCE = 50.0
SNIFF = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"%PDF-", "pdf"))
ALIASES = {
    "name": ("name", "legal name", "business name"),
    "pan": ("pan",),
    "gst": ("gst", "gstin"),
    "beneficiary": ("beneficiary", "account holder"),
    "account": ("account", "account no", "account number"),
    "routing": ("routing", "ifsc"),
}
CANDIDATE_TESSERACT = (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")


class Unavailable(Exception):
    """OCR or PDF rendering is not installed on this host."""


class Rejected(Exception):
    """The upload is not an acceptable document. The message is safe to show and never contains file content."""


def tesseract_command():
    found = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not found:
        found = next((p for p in CANDIDATE_TESSERACT if Path(p).is_file()), None)
    return found if found and Path(found).is_file() else None


def engine():
    command = tesseract_command()
    if not command:
        raise Unavailable("OCR unavailable: structured evidence required")
    try:
        import pytesseract
    except ImportError as exc:
        raise Unavailable("OCR unavailable: structured evidence required") from exc
    pytesseract.pytesseract.tesseract_cmd = command
    return pytesseract


def available():
    try:
        engine().get_tesseract_version()
        return True
    except Exception:
        return False


def sniff(data):
    for magic, kind in SNIFF:
        if data.startswith(magic):
            return kind
    raise Rejected("Upload a PNG, JPEG or PDF document.")


def decode(data):
    """Return (PIL image in RGB, original type). Enforces size and pixel bounds before any heavy work."""
    if not data or len(data) > MAX_BYTES:
        raise Rejected("The file must be between 1 byte and 2 MiB.")
    kind = sniff(data)
    if kind == "pdf":
        return render_pdf(data), kind
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in {"PNG", "JPEG"}:
                raise Rejected("Upload a PNG, JPEG or PDF document.")
            if probe.width * probe.height > MAX_PIXELS:
                raise Rejected("The image exceeds 4 megapixels.")
            probe.load()
            return ImageOps.exif_transpose(probe).convert("RGB"), kind
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError, ValueError) as exc:
        raise Rejected("The image could not be decoded.") from exc


def render_pdf(data):
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise Unavailable("PDF rendering unavailable: upload a PNG or JPEG instead") from exc
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:
        raise Rejected("The PDF could not be opened.") from exc
    try:
        if len(pdf) < 1 or len(pdf) > 3:
            raise Rejected("Upload a PDF of one to three pages; the first page is read.")
        page = pdf[0]
        width, height = page.get_size()
        # Aim for about 1400 px wide, never above the pixel bound. The bound is checked BEFORE the bitmap is
        # allocated: a page too large to render legibly inside it is declined, not rendered and then measured.
        scale = max(1.0, min(3.0, 1400 / max(width, 1)))
        while width * height * scale * scale > MAX_PIXELS and scale > 0.3:
            scale *= 0.8
        if width * height * scale * scale > MAX_PIXELS:
            raise Rejected("The PDF page is too large to read within 4 megapixels.")
        return page.render(scale=scale).to_pil().convert("RGB")
    except Rejected:
        raise
    except Exception as exc:
        raise Rejected("The PDF could not be rendered.") from exc
    finally:
        pdf.close()


def prepare(image):
    """Grayscale with autocontrast, scaled up only when the page is small. Returns the PNG bytes OCR will read."""
    grey = ImageOps.autocontrast(image.convert("L"))
    if grey.width < 800:
        # Enlarge a small page for legibility, but never past the pixel budget (final audit I3): a tall, narrow page
        # would otherwise leave the 4 megapixel check at 600 x 1800 and come out at 1200 x 3600.
        factor = min(1200 / grey.width, (MAX_PIXELS / (grey.width * grey.height)) ** 0.5)
        if factor > 1:
            grey = grey.resize((max(1, int(grey.width * factor)), max(1, int(grey.height * factor))), Image.LANCZOS)
    buffer = io.BytesIO()
    grey.save(buffer, "PNG")
    return buffer.getvalue()


def read_words(png_bytes):
    tesseract = engine()
    with Image.open(io.BytesIO(png_bytes)) as image:
        try:
            data = tesseract.image_to_data(image, lang="eng", config="--psm 6",
                                           output_type=tesseract.Output.DICT, timeout=OCR_TIMEOUT_S)
        except RuntimeError as exc:  # pytesseract raises RuntimeError on timeout
            raise Rejected("Text recognition timed out.") from exc
        except Exception as exc:
            raise Unavailable("OCR unavailable: structured evidence required") from exc
    lines = {}
    for i, text in enumerate(data["text"]):
        word = text.strip()
        if word:
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append((data["left"][i], word, float(data["conf"][i])))
    return [sorted(words) for _, words in sorted(lines.items())]


def parse_fields(kind, lines):
    """Map 'Label: value' lines onto the labels of this document kind. Returns {label: field dict}.

    Every labelled line is kept. A field carries a single `value` only when all of its labelled lines agree on one
    readable value (a name is compared after normalization, a token exactly). Two different readable values, or a
    readable value beside a labelled line that could not be read, are never settled by picking one: the field keeps
    every candidate and no `value`, so assessment treats the document as contradicting itself instead of letting
    whichever line happens to come first decide (final audit B1)."""
    seen = {label: [] for label in LABELS[kind]}  # (value or None, confidence) for each labelled line, in reading order
    stray = {label: [] for label in LABELS[kind]}  # complete tokens of the field's grammar on lines with another label
    for words in lines:
        tokens = [w for _, w, _ in words]
        confs = [c for _, _, c in words]
        labelled = set()
        for label in LABELS[kind]:
            for alias in ALIASES[label]:
                n = len(alias.split())
                head = " ".join(t.rstrip(":.;").lower() for t in tokens[:n])
                # A label must carry a colon (OCR sometimes reads it as a dot or semicolon). This keeps a title such
                # as "GST REGISTRATION CERTIFICATE" from being read as the GST field.
                if head != alias or len(tokens) <= n or tokens[n - 1][-1] not in ":.;":
                    continue
                raw = " ".join(tokens[n:]).strip().lstrip(":").strip()
                value_conf = [c for c in confs[n:] if c >= 0] or [0.0]
                try:
                    # Keep only values that fit the synthetic grammar or name vocabulary. Nothing else is stored.
                    value = clean_value(label_key(label), raw)
                except ValueError:
                    value = None
                seen[label].append((value, round(min(value_conf), 1)))
                labelled.add(label)
                break  # one line is one occurrence of one label
        # A second account number, PAN or routing code printed under some other label ("Settlement A/C:", a stamp, a
        # footer) is still a material identifier on the page. It can only ever add a contradiction: it never supplies
        # a value for a field whose own label was not found.
        for label in LABELS[kind]:
            pattern = TOKEN_PATTERNS.get(label_key(label))
            if pattern and label not in labelled:
                for _, word, conf in words:
                    token = word.strip(".,;:")
                    if re.fullmatch(pattern, token):
                        stray[label].append((token, round(conf if conf >= 0 else 0.0, 1)))
    return {label: summarize_field(label, seen[label], stray[label]) for label in LABELS[kind]}


def summarize_field(label, occurrences, stray=()):
    field = dict(value=None, confidence=None, found=bool(occurrences), format_ok=False,
                 occurrences=len(occurrences), unread=sum(1 for value, _ in occurrences if value is None), candidates=[])
    if not occurrences:
        return field
    index = {}
    for value, confidence in [o for o in occurrences if o[0] is not None] + list(stray):
        key = normalized_name(value) if label_key(label) == "name" else value
        if key in index:  # the same value again: it is still one candidate, read as unsurely as its weakest line
            candidate = field["candidates"][index[key]]
            candidate["confidence"] = min(candidate["confidence"], confidence)
        else:
            index[key] = len(field["candidates"])
            field["candidates"].append(dict(value=value, confidence=confidence))
    field["confidence"] = min(confidence for _, confidence in occurrences)
    if len(field["candidates"]) == 1 and not field["unread"]:
        only = field["candidates"][0]
        field.update(value=only["value"], confidence=only["confidence"], format_ok=True)
    return field


def label_key(label):
    return "name" if label == "beneficiary" else label


def extract(kind, data):
    """Full pipeline. Returns the extraction record to persist, plus the PNG the engine read."""
    if kind not in LABELS:
        raise Rejected("Unknown document type.")
    image, original_type = decode(data)
    png = prepare(image)
    return build_record(kind, original_type, data, png, parse_fields(kind, read_words(png))), png


def build_record(kind, original_type, data, png, fields):
    """The extraction record to persist. A field that could not be read, or that contradicts itself, counts as zero,
    so the document-level figure can never look better than its worst field."""
    found = [f["confidence"] if f["value"] is not None else 0.0 for f in fields.values()]
    return dict(
        source_type=original_type, original_digest=hashlib.sha256(data).hexdigest(),
        render_digest=hashlib.sha256(png).hexdigest(), engine=engine_name(),
        threshold=MIN_CONFIDENCE, fields=fields, min_confidence=min(found) if found else 0.0)


def engine_name():
    try:
        return "tesseract " + str(engine().get_tesseract_version()).split()[0]
    except Exception:
        return "tesseract"
