"""Generate synthetic identity, tax and bank documents as PNG, JPEG or PDF for the upload demo and tests.

Every value is a conspicuous synthetic token. Blur is applied to the finished image so a degraded document
can be uploaded through exactly the same path as a clear one.

    python -m app.samples --out private/samples --scenario happy
"""
import argparse
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .fixtures import SCENARIOS, documents, load_identity

TITLES = {"identity": "PAN IDENTITY RECORD (SYNTHETIC)", "tax": "GST REGISTRATION CERTIFICATE (SYNTHETIC)",
          "bank": "BANK ACCOUNT ADVICE (SYNTHETIC)"}
LABEL_TEXT = {"name": "Name", "pan": "PAN", "gst": "GSTIN", "beneficiary": "Beneficiary",
              "account": "Account", "routing": "Routing"}
ORDER = {"identity": ("name", "pan"), "tax": ("name", "pan", "gst"), "bank": ("beneficiary", "account", "routing")}


def render(kind, fields, blur=0.0, size=40):
    """Return a PIL image of one document. `fields` maps label -> printed value."""
    lines = [f"{LABEL_TEXT[label]}: {fields[label]}" for label in ORDER[kind] if label in fields]
    image = Image.new("RGB", (1200, 140 + 80 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    draw.text((36, 24), TITLES[kind], font=ImageFont.load_default(size=size + 6), fill="black")
    for index, line in enumerate(lines):
        draw.text((36, 120 + 80 * index), line, font=ImageFont.load_default(size=size), fill="black")
    return image.filter(ImageFilter.GaussianBlur(blur)) if blur else image


def encode(image, fmt):
    buffer = io.BytesIO()
    if fmt == "png":
        image.save(buffer, "PNG")
    elif fmt == "jpeg":
        image.save(buffer, "JPEG", quality=95)
    elif fmt == "pdf":
        image.save(buffer, "PDF", resolution=100.0)
    else:
        raise ValueError("format must be png, jpeg or pdf")
    return buffer.getvalue()


def make_document(kind, fields, fmt="png", blur=0.0):
    return encode(render(kind, fields, blur), fmt)


def scenario_documents(slug):
    if slug.startswith("load-"):  # load-110 .. load-199: one of the 100 registered load identities
        identity = load_identity(int(slug.split("-")[1]))
        return identity, documents(identity)
    found = next(s for s in SCENARIOS if s["slug"] == slug)
    return found["merchant"], documents(found["merchant"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("private/samples"))
    parser.add_argument("--scenario", default="happy", help="fixture scenario slug, or load-<100..199>")
    args = parser.parse_args()
    merchant, docs = scenario_documents(args.scenario)
    args.out.mkdir(parents=True, exist_ok=True)
    for kind, fields in docs.items():
        for fmt in ("png", "jpeg", "pdf"):
            (args.out / f"{args.scenario}-{kind}.{'jpg' if fmt == 'jpeg' else fmt}").write_bytes(
                make_document(kind, fields, fmt))
        (args.out / f"{args.scenario}-{kind}-blurred.png").write_bytes(make_document(kind, fields, "png", blur=3.0))
        # A clear document whose account number differs from the application: valid, readable and conflicting.
    bank = dict(docs["bank"], account="SYNACCT-0099")
    (args.out / f"{args.scenario}-bank-conflict.png").write_bytes(make_document("bank", bank, "png"))
    print("Wrote sample documents for", args.scenario, "to", args.out)


if __name__ == "__main__":
    main()
