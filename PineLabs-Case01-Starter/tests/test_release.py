"""Release readiness: what a reviewer cloning the repository would hit first."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_check  # noqa: E402


def test_no_sensitive_file_is_tracked_by_git():
    assert release_check.tracked_sensitive() == []


def test_sensitive_patterns_would_catch_the_usual_leaks():
    for name in ("private/demo/onboarding.db", "run/operator-link.txt", "x/seed-links.txt", ".env", "a/b.key", "w.log",
                 "private/run/simulator.db-wal", ".claude/launch.json"):
        assert any(__import__("re").search(p, name) for p in release_check.SENSITIVE), name
    assert not any(__import__("re").search(p, "app/web.py") for p in release_check.SENSITIVE)


def test_every_readme_command_exists_and_starts():
    modules, scripts, broken = release_check.broken_commands()
    assert {"run", "seed", "samples", "web", "worker"} <= set(modules)
    assert "tools/scan_privacy.py" in scripts and "tools/serve_samples.py" in scripts
    assert broken == []


def test_pinned_requirements_match_the_installed_environment():
    assert release_check.requirement_mismatches() == []


def test_gitignore_covers_run_artifacts():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("private/", "*.db", "operator-link.txt", "seed-links.txt", ".env", "*.log", "crashed-*", "*.pem"):
        assert pattern in ignored, pattern
    probe = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "private/demo/operator-link.txt"])
    assert probe.returncode == 0  # the operator login link in a run directory is never committed


def test_operator_link_is_not_printed_when_output_is_not_a_terminal(tmp_path):
    """The launcher prints the one-time operator link only to a real terminal; otherwise it is saved privately."""
    run = subprocess.run([sys.executable, "-m", "app.web", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0
    source = (ROOT / "app" / "web.py").read_text(encoding="utf-8")
    assert "sys.stdout.isatty()" in source and "Private operator login saved in the run directory." in source
