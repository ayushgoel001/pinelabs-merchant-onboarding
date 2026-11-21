"""M1 feasibility experiment. This is separate from the intake application's upload path."""
import argparse
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw, ImageFont
import pytesseract

EXPECTED = ["Beneficiary: ACME TRADERS PVT LTD", "Account: SYNACCT-0001", "Routing: SYNIFSC-0001"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tesseract-cmd", default=shutil.which("tesseract"))
    args = parser.parse_args()
    if not args.tesseract_cmd or not Path(args.tesseract_cmd).is_file():
        print("OCR unavailable: structured evidence required")
        return 2
    pytesseract.pytesseract.tesseract_cmd = args.tesseract_cmd
    image = Image.new("RGB", (1120, 280), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    for index, line in enumerate(EXPECTED):
        draw.text((32, 24 + 80 * index), line, font=font, fill="black")
    # Pass an actual encoded PNG through the OCR engine, using an isolated temporary file.
    with TemporaryDirectory() as folder:
        filename = Path(folder) / "synthetic-bank.png"
        image.save(filename, "PNG")
        result = pytesseract.image_to_string(str(filename), lang="eng", config="--psm 6", timeout=5)
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    print("Tesseract", pytesseract.get_tesseract_version())
    print("\n".join(lines))
    passed = lines == EXPECTED
    print("A01 spike:", "GO" if passed else "NO-GO")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
