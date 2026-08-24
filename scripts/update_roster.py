#!/usr/bin/env python3
"""
Fetches the current professional roster from the LUX Salon Suites WordPress
REST API and updates the SUITE_ROSTER array inside index.html, in place.

Designed to run unattended (e.g. from a GitHub Actions schedule). It never
guesses at missing data: any listing it can't confidently parse a name and
suite number for is skipped and reported, rather than silently dropped or
filled in with a wrong value.

Exit codes:
  0 - success, index.html updated (or already up to date)
  1 - fetch/parse failure, nothing was written
  2 - fetched successfully but every listing failed to parse (suspicious —
      likely a site-side change) - nothing was written, to avoid publishing
      an empty roster
  3 - fetched fewer listings than WordPress's own X-WP-Total header reports —
      almost always a caching/CDN/security-plugin issue truncating the
      response - nothing was written, to avoid publishing an incomplete roster
"""

import json
import re
import sys
import urllib.request
from html import unescape

SITE = "https://luxsalontx.com"
REST_ENDPOINT = f"{SITE}/wp-json/wp/v2/hp_listing"
INDEX_FILE = "index.html"
ROSTER_START = "const SUITE_ROSTER = [\n"
ROSTER_END_MARKER = "\n  ];"

# Matches "Name: X; ... Find Me In Suite: NNN;" inside the Yoast description text.
NAME_RE = re.compile(r"Name:\s*([^;]+);")
SUITE_RE = re.compile(r"Find Me In Suite:\s*(\d+)\s*;")


def fetch_all_listings():
    """Fetch every published hp_listing item, following pagination.

    Also captures the X-WP-Total header WordPress always includes on this
    endpoint — the true total count of published listings, independent of
    pagination. Comparing this to what we actually received is how we catch
    a caching layer or proxy silently truncating the response (a real
    failure mode seen in production on this project — the request looked
    successful, but far fewer listings came back than actually exist).
    """
    listings = []
    reported_total = None
    page = 1
    while True:
        url = f"{REST_ENDPOINT}?per_page=100&page={page}&status=publish"
        req = urllib.request.Request(url, headers={"User-Agent": "LUX-roster-sync/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Unexpected status {resp.status} from {url}")
                if page == 1:
                    total_header = resp.headers.get("X-WP-Total")
                    reported_total = int(total_header) if total_header else None
                batch = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

        if not batch:
            break
        listings.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    return listings, reported_total


def extract_name_and_suite(item):
    """
    Parses Name and Suite Number from the Yoast SEO description text
    (e.g. "... Name: Jane Doe; ... Find Me In Suite: 123; ..."). This is the
    same text format the WordPress XML export has always used, and has
    proven reliable in production.

    Earlier versions of this script also tried the REST `meta` fields
    (hp_name / hp_suite_number) as a first choice, on the theory that the
    site might eventually expose them properly. In practice, once those
    fields did start appearing in the API response, they turned out to hold
    stale or incorrect values for some listings — silently producing WRONG
    data (not missing data), which is worse than a clean fallback failure.
    Given that risk, this now uses the Yoast text exclusively.
    """
    yoast = item.get("yoast_head_json") or {}
    description = yoast.get("description") or yoast.get("og_description") or ""

    name_match = NAME_RE.search(description)
    suite_match = SUITE_RE.search(description)
    if name_match and suite_match:
        return unescape(name_match.group(1).strip()), suite_match.group(1).strip()

    return None, None


def build_roster(listings):
    roster = []
    skipped = []

    for item in listings:
        link = item.get("link", "").strip()
        title = (item.get("title") or {}).get("rendered", "").strip()
        name, suite = extract_name_and_suite(item)

        if not link or not name or not suite:
            skipped.append(title or link or f"id={item.get('id')}")
            continue

        roster.append({"suite": suite, "name": unescape(name), "link": link})

    # ascending by suite number, matching the site convention
    roster.sort(key=lambda r: int(r["suite"]))
    return roster, skipped


def js_escape(text):
    return text.replace("\\", "\\\\").replace("'", "\\'")


def render_roster_js(roster):
    lines = []
    for i, r in enumerate(roster):
        comma = "," if i < len(roster) - 1 else ""
        lines.append(
            f"    {{ suite: '{js_escape(r['suite'])}', "
            f"name: '{js_escape(r['name'])}', "
            f"link: '{js_escape(r['link'])}' }}{comma}"
        )
    return "\n".join(lines)


def update_index_file(path, new_roster_js):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    start_idx = content.find(ROSTER_START)
    if start_idx == -1:
        raise RuntimeError(
            f"Could not find '{ROSTER_START.strip()}' in {path} — "
            "the file structure may have changed."
        )
    start_idx += len(ROSTER_START)

    end_idx = content.find(ROSTER_END_MARKER, start_idx)
    if end_idx == -1:
        raise RuntimeError(
            f"Could not find the closing '];' for SUITE_ROSTER in {path} — "
            "refusing to write, to avoid corrupting the file."
        )

    updated = content[:start_idx] + new_roster_js + content[end_idx:]

    if updated == content:
        print("No changes — roster already up to date.")
        return False

    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    return True


def main():
    try:
        listings, reported_total = fetch_all_listings()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"WordPress reports {reported_total if reported_total is not None else 'an unknown'} "
          f"total published listing(s); this run received {len(listings)}.")

    # If the site tells us how many listings actually exist and we got noticeably
    # fewer, something between us and WordPress (a cache, CDN, or security plugin)
    # is truncating the response — this is exactly the failure mode that silently
    # dropped several real people from the roster in production. Fail loudly
    # instead of publishing a roster we know is incomplete.
    if reported_total is not None and len(listings) < reported_total:
        print(f"ERROR: expected {reported_total} listing(s) per WordPress's X-WP-Total header, "
              f"but only received {len(listings)}. This usually means a cache, CDN, or security "
              f"plugin in front of the site is serving a stale/partial response to this request. "
              f"Refusing to update {INDEX_FILE} with incomplete data.", file=sys.stderr)
        sys.exit(3)

    roster, skipped = build_roster(listings)

    print(f"Fetched {len(listings)} published listing(s); parsed {len(roster)} successfully.")
    if skipped:
        print(f"WARNING: could not parse name/suite for {len(skipped)} listing(s):", file=sys.stderr)
        for s in skipped:
            print(f"  - {s}", file=sys.stderr)

    if not roster:
        print("ERROR: zero listings parsed successfully — refusing to overwrite index.html "
              "with an empty roster. Check the REST API response format.", file=sys.stderr)
        sys.exit(2)

    roster_js = render_roster_js(roster)

    try:
        changed = update_index_file(INDEX_FILE, roster_js)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if changed:
        print(f"Updated {INDEX_FILE} with {len(roster)} listing(s).")
    if skipped:
        # Still exit 0 (partial success is normal/expected — the site will always
        # have a handful of incomplete listings) but make the warning visible
        # in the Actions run summary.
        print(f"::warning::{len(skipped)} listing(s) skipped — see log above.")


if __name__ == "__main__":
    main()
