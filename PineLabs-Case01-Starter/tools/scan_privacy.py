"""Scan public candidate text and all Git patch history without printing matched values."""
import argparse
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "tax identifier": r"(?<![A-Za-z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Za-z0-9])",
    "tax registration": r"(?<![A-Za-z0-9])[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z](?![A-Za-z0-9])",
    "routing identifier": r"(?<![A-Za-z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Za-z0-9])",
    "account-like number": r"(?<![A-Za-z0-9])[0-9]{9,18}(?![A-Za-z0-9])",
    "private key material": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "provider credential": r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,})",
}
COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def hits(text):
    return [name for name, pattern in COMPILED.items() if pattern.search(text)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", default="git")
    args = parser.parse_args()
    root = Path(subprocess.check_output([args.git, "rev-parse", "--show-toplevel"], text=True).strip())
    def git(*params):
        return subprocess.check_output([args.git, "-C", str(root), *params])
    paths = set(git("ls-files", "-co", "--exclude-standard", "-z").decode().split("\0")) - {""}
    findings = 0
    checked = 0
    for name in sorted(paths):
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:512]:
            continue
        checked += 1
        for category in hits(data.decode("utf-8", errors="replace")):
            print(f"Review {name} ({category})")
            findings += 1
    patch = git("log", "--all", "--no-ext-diff", "--format=", "-p").decode("utf-8", errors="replace")
    for category in hits(patch):
        print(f"Review Git history ({category})")
        findings += 1
    print(f"Scanned {checked} candidate text files and all Git patch history. Findings: {findings}.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
