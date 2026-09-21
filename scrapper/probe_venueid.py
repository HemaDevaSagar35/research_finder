"""Probe OpenReview for the venueid(s) a venue's papers actually carry.

Useful when `content.venueid=<group id>` returns 0: queries notes by
`domain=<group id>` instead, and reports every distinct content.venueid
found, with counts. Run it, then feed the right venueid to
browser_download_papers.py.

Usage:
    uv run scrapper/probe_venueid.py ACM.org/SIGIR/2026/Conference
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scrapper.browser_download_papers import (API_BASE, SITE_BASE,
                                              in_page_fetch,
                                              wait_for_challenge)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("group", help="OpenReview group id, e.g. "
                                      "ACM.org/SIGIR/2026/Conference")
    parser.add_argument("--pages", type=int, default=5,
                        help="Pages of 1000 notes to scan (default: 5)")
    args = parser.parse_args()

    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            browser = p.chromium.launch(headless=False, channel="chrome",
                                        args=launch_args)
        except Exception:
            browser = p.chromium.launch(headless=False, args=launch_args)
        page = browser.new_page()
        print("Opening OpenReview to pass the Cloudflare check...")
        page.goto(f"{SITE_BASE}/group?id={args.group}",
                  wait_until="domcontentloaded")
        wait_for_challenge(page, args.group)

        # 1. The venue's group object holds its config, including the
        #    venueid strings it assigns notes at each stage.
        resp = in_page_fetch(page, f"{API_BASE}/groups?id={args.group}")
        candidates = {args.group}
        if resp["status"] == 200:
            groups = json.loads(resp["text"]).get("groups", [])
            content = (groups[0].get("content") or {}) if groups else {}
            print(f"\nGroup content fields mentioning 'venue':")
            for key, val in sorted(content.items()):
                if "venue" in key.lower():
                    value = val.get("value") if isinstance(val, dict) else val
                    print(f"  {key} = {value}")
                    if isinstance(value, str) and value:
                        candidates.add(value)
        else:
            print(f"group lookup failed: {resp['status']}")

        # 2. Count notes for each candidate venueid.
        print("\nNote counts per candidate venueid:")
        found = {}
        for vid in sorted(candidates):
            resp = in_page_fetch(
                page, f"{API_BASE}/notes?content.venueid={vid}&limit=3")
            if resp["status"] != 200:
                print(f"  {vid}: HTTP {resp['status']}")
                continue
            data = json.loads(resp["text"])
            count = data.get("count", len(data.get("notes", [])))
            print(f"  {count:6d}  {vid}")
            if count:
                found[vid] = data["notes"][0]["content"].get(
                    "title", {}).get("value", "?")
        browser.close()

    if found:
        print("\nUsable venueid(s):")
        for vid, title in found.items():
            print(f"  {vid}  (e.g. \"{title}\")")
    else:
        print("\nNo public accepted papers found under any candidate "
              "venueid — this venue likely doesn't publish accepted "
              "papers on OpenReview.")


if __name__ == "__main__":
    main()
