"""Capture citra-flows: landing, workflow list, the AI building a workflow, the graph.

Runs on the host at 1440x900 @2x. This UI is ordinary React with real buttons,
so normal clicks work -- unlike the RN Web shell in citra-decision-system.
"""
import os, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

UI = "http://localhost:8088"
EMAIL = os.environ["FLOWS_EMAIL"]
PW = os.environ["FLOWS_PW"]
OUT = Path(r"C:\Github\citra-flows\assets\screens")

BRIEF = ("Every weekday at 7am, pull new vendor invoices from the SQL database. "
         "For each one, check the invoice total against the matching purchase order "
         "and flag any mismatch over 2%. Send the flagged ones to a finance approver "
         "for review, and post a daily summary of what was processed.")


def shot(page, name, note):
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{name}.png"))
    print(f"  captured {name:<22} {note}")


def main() -> int:
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = br.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)

        page.goto(UI, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)
        shot(page, "00-landing", "what the project is, before you sign in")

        page.click("button:has-text('Sign in')", timeout=15000)
        page.wait_for_timeout(2000)
        page.fill("input[type=email]", EMAIL)
        page.fill("input[type=password]", PW)
        shot(page, "01-signin", "local accounts via citra-user-service")
        page.click("button:has-text('Sign in')", timeout=15000)
        page.wait_for_timeout(6000)
        shot(page, "02-workflows", "the workflow list")

        # New Workflow -> the builder
        for sel in ("text=Create Workflow", "text=New Workflow"):
            try:
                page.click(sel, timeout=6000); break
            except Exception:
                continue
        page.wait_for_timeout(5000)
        shot(page, "03-builder-empty", "typed node palette + AI assistant")

        ta = page.locator("textarea").first
        ta.fill(BRIEF)
        page.wait_for_timeout(500)
        shot(page, "04-brief", "the workflow described in plain English")
        ta.press("Enter")
        print("  assistant working (up to 4 min)...")
        # The assistant PROPOSES first -- the graph only reaches the canvas when
        # you press Apply. Waiting for canvas nodes here would time out on a
        # working run, which is what the first version of this did.
        try:
            page.wait_for_selector("text=Apply to Canvas", timeout=240000)
        except Exception:
            print("  [!!] no proposal appeared", file=sys.stderr)
            page.wait_for_timeout(3000)
            shot(page, "05-proposal", "INCOMPLETE - check before use")
            br.close()
            return 1
        page.wait_for_timeout(2500)
        shot(page, "05-proposal", "the plan: nodes, setup gaps, and Apply")

        page.click("text=Apply to Canvas", timeout=15000)
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('.react-flow__node').length > 1",
                timeout=60000)
        except Exception:
            print("  [!!] nodes did not land on the canvas", file=sys.stderr)
        page.wait_for_timeout(3500)
        shot(page, "06-workflow", "the graph on the canvas, editable")
        br.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
