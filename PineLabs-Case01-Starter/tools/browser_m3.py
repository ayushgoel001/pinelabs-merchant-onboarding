"""Optional Chrome rehearsal against a running local launcher with --demo-controls and a fresh run directory."""
import argparse
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    origin = f"http://127.0.0.1:{args.port}"
    login = (args.run_dir / "operator-link.txt").read_text(encoding="utf-8").strip()
    if not login.startswith(origin + "/access#"):
        raise SystemExit("Use the port belonging to this local run.")
    with sync_playwright() as manager:
        browser = manager.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1200, "height": 1150})
        errors = []
        page.on("pageerror", lambda exc: errors.append(type(exc).__name__))
        state = page.locator("#case-root .state")
        page.goto(login)
        expect(page.get_by_role("heading", name="Applications", exact=True)).to_be_visible()

        def create(slug):
            page.goto(origin)
            page.locator("#demo-panel > summary").click()  # the scenario picker is in a collapsed demo section
            page.locator("#scenario").select_option(slug)
            page.get_by_role("button", name="Submit application").click()
            page.wait_for_url(re.compile(r"/cases/[0-9a-f]{32}$"))
            return page.url.rsplit("/", 1)[1]

        def check_state(expected):
            expect(state).to_have_text(expected, timeout=35_000)

        def technical(section):
            """Open one collapsed section under Technical details (evidence, attempts, receipts, history)."""
            page.locator("#technical summary", has_text=section).click()

        def scalar(file, sql, *params):
            uri = (args.run_dir / file).resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                return conn.execute(sql, params).fetchone()[0]

        def counts(case):
            return (scalar("onboarding.db", "SELECT count(*) FROM approvals WHERE case_id=?", case),
                    scalar("simulator.db", "SELECT count(*) FROM sim_issuances WHERE operation_key=?", "mid/" + case),
                    scalar("onboarding.db", "SELECT count(*) FROM merchants WHERE case_id=?", case))

        case = create("missing-tax")
        check_state("NEEDS_INFO")
        page.get_by_role("link", name="Amend evidence").click()
        # No tax file: the merchant uses the typed details as the tax document.
        page.locator('.use-typed[data-kind="tax"]').check()
        page.get_by_role("button", name="Save changes").click()
        check_state("VERIFYING")
        check_state("ACTIVE")
        assert page.url.endswith(case) and counts(case) == (1, 1, 1)
        expect(page.locator("#technical summary", has_text="Approval receipt")).to_be_visible()
        technical("Verification attempts")
        expect(page.get_by_text("PROVIDER_TEMPORARILY_UNAVAILABLE", exact=True)).to_be_visible()
        # The Merchant ID can also appear in the technical records (issuance record, history), so a text locator may
        # be ambiguous. The Merchant ID panel's element has an id.
        expect(page.locator("#mid-value")).to_have_text(re.compile(r"^SYNMID-[A-F0-9]{16}$"))
        mid = page.locator("#mid-value").inner_text()
        if args.screenshot:
            page.screenshot(path=str(args.screenshot))
        print(f"D01 {case[:10]} revision 2, transient PAN, ACTIVE {mid}, counts {counts(case)}")

        failed = create("gst-fail")
        check_state("IN_REVIEW")
        expect(page.get_by_role("button", name="Request correction")).to_be_visible()
        expect(page.get_by_role("button", name="Accept referral")).to_have_count(0)
        expect(page.get_by_role("heading", name="The GST check failed")).to_be_visible()  # the plain reason
        technical("Verification attempts")
        expect(page.get_by_text("GST_REGISTRATION_CANCELLED", exact=True)).to_be_visible()  # the exact record
        assert counts(failed) == (0, 0, 0)
        print(f"F01 {failed[:10]} GST_REGISTRATION_CANCELLED, IN_REVIEW, counts {counts(failed)}")

        lost = create("mid-lost-ack")
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if counts(lost) == (1, 1, 0):
                break
            time.sleep(.05)
        else:
            raise AssertionError("Lost acknowledgement was never observed before recovery.")
        original = scalar("simulator.db", "SELECT mid FROM sim_issuances WHERE operation_key=?", "mid/" + lost)
        page.reload()
        check_state("PROVISIONING_FAILED")
        expect(page.get_by_text("OUTCOME_UNKNOWN", exact=True)).to_be_visible()
        check_state("ACTIVE")
        assert counts(lost) == (1, 1, 1)
        expect(page.locator("#mid-value")).to_have_text(original)
        print(f"F05 {lost[:10]} PROVISIONING_FAILED (1, 1, 0) -> ACTIVE {original} (1, 1, 1)")

        dead = create("mid-dead-letter")
        requeue = page.get_by_role("button", name="Requeue issuance")
        expect(requeue).to_be_visible(timeout=35_000)
        check_state("PROVISIONING_FAILED")
        assert counts(dead) == (1, 0, 0)
        page.get_by_role("combobox", name="Reason to requeue issuance").select_option(label="Issuer service restored")
        requeue.click()
        check_state("ACTIVE")
        assert counts(dead) == (1, 1, 1)
        print(f"F05 {dead[:10]} DEAD_LETTERED (1, 0, 0) -> operator requeue -> ACTIVE (1, 1, 1)")

        referred = create("risk-referral")
        check_state("IN_REVIEW")
        page.get_by_role("combobox", name="Reason to accept referral").select_option(label="Zone exposure reviewed and accepted")
        page.get_by_role("button", name="Accept referral", exact=True).click()
        check_state("ACTIVE")
        technical("Approval receipt")
        expect(page.get_by_text(re.compile("^Accepted by operator"))).to_be_visible()
        assert counts(referred) == (1, 1, 1)
        print(f"I10 {referred[:10]} reasoned RISK referral -> ACTIVE (1, 1, 1)")
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    main()
