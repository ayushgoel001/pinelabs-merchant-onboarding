"""Re-read the stored images of OCR extractions written before contradiction tracking. Changes nothing.

    python tools/revalidate_evidence.py private/demo

Extractions stored before final-audit finding B1 kept only the first labelled line of each field, so a second, different
value printed on the same page may never have been recorded. This tool opens the run directory read-only, re-reads the
stored normalized image of every such record with the current parser and lists each field where the page shows a
different value from the stored one, or more than one value. Open applications that rest on such a record are already
held for a new upload (workflow.upgrade_legacy_evidence). This is the way to check applications that were approved
earlier. It needs the local Tesseract engine.

Exit status: 0 nothing found, 1 one or more fields differ, 2 the run directory or the OCR engine is unavailable.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ocr  # noqa: E402
from app.extraction import is_legacy, masked  # noqa: E402


def revalidate(run_dir):
    """Returns a list of findings, one per field whose stored value the stored image does not support."""
    path = Path(run_dir) / "onboarding.db"
    if not path.is_file():
        raise FileNotFoundError(f"No onboarding.db in {run_dir}")
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    findings, checked = [], 0
    try:
        for row in conn.execute("""SELECT e.case_id,e.kind,e.revision,e.fields,e.render_digest,a.state FROM extractions e
                                 JOIN applications a ON a.id=e.case_id ORDER BY e.case_id,e.kind,e.revision"""):
            stored = json.loads(row["fields"])
            if not is_legacy({"fields": stored}):
                continue
            blob = conn.execute("SELECT data FROM files WHERE digest=?", (row["render_digest"],)).fetchone()
            if blob is None:
                findings.append(dict(case=row["case_id"], state=row["state"], kind=row["kind"], label="*",
                                     stored="", found=["stored image missing"]))
                continue
            checked += 1
            fresh = ocr.parse_fields(row["kind"], ocr.read_words(blob["data"]))
            for label, was in stored.items():
                now = fresh.get(label, {})
                values = [c["value"] for c in now.get("candidates", [])]
                if now.get("value") is None or now.get("value") != was.get("value"):
                    findings.append(dict(case=row["case_id"], state=row["state"], kind=row["kind"], label=label,
                                         stored=was.get("value") or "", found=values or ["nothing readable"]))
    finally:
        conn.close()
    return findings, checked


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        findings, checked = revalidate(args.run_dir)
    except (FileNotFoundError, sqlite3.Error, ocr.Unavailable, ocr.Rejected) as exc:
        print(f"Cannot revalidate: {exc}")
        return 2
    print(f"Re-read {checked} OCR record(s) written before contradiction checks.")
    for f in findings:
        print(f"  case {f['case'][:10]} ({f['state']}) {f['kind']}.{f['label']}: stored {masked(f['stored']) or '-'}; "
              f"the image now reads {' / '.join(masked(v) for v in f['found'])}")
    print("No stored value is contradicted by its image." if not findings else f"{len(findings)} field(s) differ.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
