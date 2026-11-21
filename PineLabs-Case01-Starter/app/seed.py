"""Submit every fixture scenario into one run through the same domain function as the intake API.

Re-running is safe: each scenario uses a stable submission key, so a repeat returns the original case.
Private merchant access links are written to <run-dir>/seed-links.txt, never printed.
"""
import argparse
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from . import db
from .access import issue_ticket
from .extraction import Submission
from .fixtures import SCENARIOS
from .workflow import submit


def seed(run_dir, only=None, origin=None):
    path = db.initialize(run_dir)
    chosen = [s for s in SCENARIOS if only in (None, s["slug"])]
    if not chosen:
        raise SystemExit(f"Unknown scenario. Choose one of: {', '.join(s['slug'] for s in SCENARIOS)}")
    results, links = [], []
    for s in chosen:
        body = Submission(submission_key=uuid5(NAMESPACE_URL, "pine-labs-case-01/seed/" + s["slug"]),
                          merchant=s["merchant"], documents=s["documents"])
        case, created, _ = submit(path, body, "seed:" + s["slug"], db.policy())
        results.append((s, case, created))
        if origin:
            links.append(f"{s['slug']}\t{origin}/access#{issue_ticket(path, 'merchant:' + case)}")
    if links:
        (Path(run_dir) / "seed-links.txt").write_text("\n".join(links) + "\n", encoding="utf-8")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=Path("private/demo"))
    parser.add_argument("--only", help="seed a single scenario slug")
    parser.add_argument("--port", type=int, default=8000, help="port used in the private merchant links")
    args = parser.parse_args()
    for s, case, created in seed(args.run_dir, args.only, f"http://127.0.0.1:{args.port}"):
        print(f"{'created ' if created else 'existing'} {case[:10]}  {s['slug']:<20} {s['label']}")
    print("Merchant links (single use, 30 minutes) saved in", args.run_dir / "seed-links.txt")


if __name__ == "__main__":
    main()
