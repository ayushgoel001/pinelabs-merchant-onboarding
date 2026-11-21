"""Test-only crash points (M4 / review I12). Production runs never set these variables.

    PINELABS_TEST_FAULTS=1  CRASH_AT=<point>[:<job kind>]

A point is `after_claim` (a job has been leased and committed, nothing has been sent) or `after_remote_effect`
(the remote system has acted and answered, the local completion has not been recorded). The optional job kind
is DISPATCH or ISSUE_MID. The first matching occurrence exits the whole process with code 70, with no cleanup,
as a power loss would. A marker file next to the database makes it one-shot, so a supervised restart that
inherits the same environment does not crash again.
"""
import os
from pathlib import Path

EXIT_CODE = 70


def crash_point(path, point, kind):
    spec = os.environ.get("CRASH_AT")
    if not spec or os.environ.get("PINELABS_TEST_FAULTS") != "1":
        return
    wanted, _, wanted_kind = spec.partition(":")
    if wanted != point or (wanted_kind and wanted_kind != kind):
        return
    marker = Path(path).parent / f"crashed-{wanted}-{kind}"
    if marker.exists():
        return
    marker.write_text("one-shot crash point reached\n", encoding="utf-8")
    os._exit(EXIT_CODE)
