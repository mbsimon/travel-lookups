"""One-time / periodic session capture for travel_lookups.fora_flights.

flights.fora.travel auth is Google SSO via NextAuth — no email/password flow
to automate, so this opens a real (headed) Chrome profile, waits for Michael
to sign in if needed, and saves the flights.fora.travel cookies to
SESSION_FILE. The NextAuth session cookie is good for about a month; re-run
this when fora_flights.SessionExpiredError starts firing.

    python3 scripts/fora_flights_save_session.py                  # capture the session cookie
    python3 scripts/fora_flights_save_session.py --capture-action # also re-learn the Server Action hash
                                                                    # (run when NextActionStaleError fires —
                                                                    # it means Fora redeployed flights.fora.travel)

--capture-action drives one live search (MAD->JFK, 30 days out) and reads the
`Next-Action` request header off it, then prints the line to paste into
fora_flights.NEXT_ACTION (or export as FORA_FLIGHTS_NEXT_ACTION). It does not
edit anything for you — that constant is small and worth a human glance
before it changes.

Needs `pip install playwright && playwright install chromium` locally — this
is a one-off Mac-side tool, not a dependency of the fora_flights module
itself or of anything that runs in a container.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

SESSION_FILE = Path(os.getenv(
    "FORA_FLIGHTS_SESSION_FILE",
    os.path.expanduser("~/.config/michaelsimon/fora_flights_session.json"),
))
PROFILE_DIR = os.path.expanduser("~/.config/michaelsimon/chrome-fora-debug")


def _save_cookies(context):
    cookies = [
        {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c["path"]}
        for c in context.cookies()
        if c["domain"].endswith("flights.fora.travel")
    ]
    if not any(c["name"] == "__Secure-authjs.session-token" for c in cookies):
        print("No NextAuth session cookie found — sign in didn't complete.", file=sys.stderr)
        sys.exit(1)
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({"cookies": cookies}, indent=2))
    os.chmod(SESSION_FILE, 0o600)
    print(f"Saved {len(cookies)} cookies to {SESSION_FILE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture-action", action="store_true",
                     help="also drive a live search and print the current Next-Action hash")
    args = ap.parse_args()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://flights.fora.travel/query")

        if page.get_by_text("Please sign in to continue").count():
            print("Sign in with Google in the browser window, then press Enter here...")
            input()
            page.goto("https://flights.fora.travel/query")

        page.wait_for_selector("text=Search for flights and fares", timeout=15000)
        _save_cookies(context)

        if args.capture_action:
            captured = {}

            def on_request(req):
                if req.method == "POST" and "/flights/MAD-JFK/" in req.url and "_rsc" not in req.url:
                    captured["hash"] = req.headers.get("next-action")

            page.on("request", on_request)
            dep = date.today() + timedelta(days=30)
            ret = dep + timedelta(days=7)
            path = f"MAD-JFK/{dep.isoformat()}+Y/JFK-MAD/{ret.isoformat()}+Y"
            page.goto(f"https://flights.fora.travel/flights/{path}?p=1,0&s=3&r=200&omitbasic=true")
            page.wait_for_timeout(9000)
            if "hash" in captured:
                print(f"\nNEXT_ACTION = \"{captured['hash']}\"")
                print("Paste into fora_flights.py's NEXT_ACTION default, or export FORA_FLIGHTS_NEXT_ACTION.")
            else:
                print("Didn't see a POST to capture — try again or check the UI still looks the same.",
                      file=sys.stderr)

        context.close()


if __name__ == "__main__":
    main()
