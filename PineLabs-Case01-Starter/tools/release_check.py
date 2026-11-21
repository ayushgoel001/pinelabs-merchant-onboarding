"""Release readiness checks. Prints a short report and exits non-zero if anything that must be true is not.

    python tools/release_check.py

It checks that nothing sensitive is tracked by Git, that every command and script the README tells a reader to
run exists and starts, that the pinned requirements match what is installed, and whether OCR can run here.
"""
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE = [r"\.db$", r"\.db-(wal|shm|journal)$", r"operator-link", r"seed-links", r"(^|/)private/", r"(^|/)\.env($|\.)",
             r"\.pem$", r"\.key$", r"\.log$", r"crashed-", r"(^|/)\.claude/", r"credentials\.json$"]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def tracked_sensitive():
    names = [n for n in git("ls-files").splitlines() if n]
    return [n for n in names if any(re.search(p, n) for p in SENSITIVE)]


def readme_targets(text):
    modules = sorted(set(re.findall(r"-m app\.(\w+)", text)))
    scripts = sorted(set(re.findall(r"(tools/\w+\.py)", text)))
    return modules, scripts


def broken_commands(readme=ROOT / "README.md"):
    """Every `-m app.<module>` must answer --help, and every tools/ script the README names must exist."""
    modules, scripts = readme_targets(readme.read_text(encoding="utf-8"))
    broken = []
    for module in modules:
        run = subprocess.run([sys.executable, "-m", f"app.{module}", "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            broken.append(f"python -m app.{module} --help exited {run.returncode}")
    broken += [f"{script} is mentioned but missing" for script in scripts if not (ROOT / script).is_file()]
    return modules, scripts, broken


def requirement_mismatches(path=ROOT / "requirements.txt"):
    problems = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-r")):
            continue
        name, _, wanted = line.partition("==")
        if not wanted:
            problems.append(f"{name} is not pinned")
            continue
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"{name} is not installed")
            continue
        if installed != wanted:
            problems.append(f"{name} {installed} installed, {wanted} pinned")
    return problems


def ocr_report():
    sys.path.insert(0, str(ROOT))
    from app import ocr
    if not ocr.available():
        return "Tesseract not found. Uploads answer 503 and structured evidence still works. Install it (see README)."
    version = ocr.engine_name()
    try:
        languages = ocr.engine().get_languages()
    except Exception:
        languages = []
    return f"{version}, command {ocr.tesseract_command()}, English data {'present' if 'eng' in languages else 'MISSING'}"


def main():
    failures = []
    tracked = tracked_sensitive()
    print("Tracked sensitive files:", tracked or "none")
    failures += [f"tracked sensitive file {n}" for n in tracked]
    modules, scripts, broken = broken_commands()
    print("README commands checked:", ", ".join(f"app.{m}" for m in modules), "·", ", ".join(scripts))
    failures += broken
    problems = requirement_mismatches()
    print("Pinned requirements:", problems or "all match the installed versions")
    failures += problems
    print("OCR:", ocr_report())
    if failures:
        print("\nFAILED:\n  " + "\n  ".join(failures))
        return 1
    print("\nRelease checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
