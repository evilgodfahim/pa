#!/usr/bin/env python3
"""
Prothom Alo Opinion/Editorial scraper → RSS feed
Parses the embedded Quintype JSON blob from prothomalo.com/opinion.

- Appends new articles to opinion.xml on each run (deduped by guid/url)
- Caps feed at MAX_ARTICLES=500; oldest articles are dropped when cap is exceeded
- Output: opinion.xml
"""

import json
import re
import sys
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.dom.minidom import parseString

import requests

BASE_URL     = "https://www.prothomalo.com"
OPINION_URL  = f"{BASE_URL}/opinion"
OUTPUT_FILE  = Path("opinion.xml")
IMAGE_CDN    = "https://media.prothomalo.com"
IMAGE_WIDTH  = 480
MAX_ARTICLES = 500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         BASE_URL,
}

NS_MEDIA = "http://search.yahoo.com/mrss/"
NS_DC    = "http://purl.org/dc/elements/1.1/"

# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_quintype_json(html: str) -> dict:
    """
    Extract the Quintype page-data JSON using three strategies, most specific first.

    Strategy 1: <script ... id="static-page" ...>{ ... }</script>
                This is the canonical form seen in saved HTML.

    Strategy 2: Any <script> whose stripped body starts with {"qt":
                Handles minor whitespace/attribute variations.

    Strategy 3: Any <script> body that contains the string '"qt":{"config"'
                Handles cases where the blob is assigned to a JS variable,
                e.g.  window.QT_DEFINED_DATA = {"qt":{"config": ...}}
                We extract the {...} portion via json.JSONDecoder.raw_decode.
    """

    # --- Strategy 1: id="static-page" ---
    m = re.search(
        r'<script[^>]+id=["\']static-page["\'][^>]*>\s*(\{.*?)\s*</script>',
        html,
        re.DOTALL,
    )
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            print(f"Strategy 1 JSON parse failed: {e}", file=sys.stderr)

    # --- Strategy 2: script body starts with {"qt": ---
    for body in re.findall(r'<script[^>]*>\s*(\{"qt"\s*:.*?)</script>', html, re.DOTALL):
        try:
            return json.loads(body.strip())
        except json.JSONDecodeError:
            continue

    # --- Strategy 3: script body contains "qt":{"config" anywhere ---
    decoder = json.JSONDecoder()
    for body in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        if '"qt"' not in body or '"config"' not in body:
            continue
        # Find the first '{' that starts a valid JSON object containing "qt"
        for i, ch in enumerate(body):
            if ch != '{':
                continue
            try:
                obj, _ = decoder.raw_decode(body, i)
                if isinstance(obj, dict) and "qt" in obj:
                    return obj
            except json.JSONDecodeError:
                continue

    # --- All strategies failed: dump diagnostics ---
    print("\n--- DIAGNOSTIC: first 2000 chars of fetched HTML ---", file=sys.stderr)
    print(html[:2000], file=sys.stderr)
    print("\n--- Script tag count:", html.count("<script"), file=sys.stderr)
    print('--- "static-page" occurrences:', html.count("static-page"), file=sys.stderr)
    print('--- "\"qt\"" occurrences:', html.count('"qt"'), file=sys.stderr)
    raise ValueError(
        "Quintype JSON blob not found. See diagnostic output above."
    )


def collect_stories(obj: object, out: list) -> None:
    """Recursively walk collection tree, collect story dicts."""
    if isinstance(obj, dict):
        if obj.get("type") == "story" and "story" in obj:
            out.append(obj["story"])
        for v in obj.values():
            collect_stories(v, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_stories(item, out)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ms_to_rfc2822(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return formatdate(dt.timestamp(), usegmt=True)


def img_url(s3_key: str) -> str:
    return f"{IMAGE_CDN}/{s3_key}?w={IMAGE_WIDTH}&auto=format" if s3_key else ""


def story_to_item(story: dict):
    """Convert a raw story dict to an RSS <item> Element, or None if invalid."""
    headline = story.get("headline", "").strip()
    url = story.get("url", "") or f"{BASE_URL}/{story.get('slug', '')}"
    if not headline or not url:
        return None

    pub_ms = story.get("published-at") or story.get("last-published-at")

    authors = [
        a.get("name", "").strip()
        for a in story.get("authors", [])
        if a.get("name", "").strip() not in ("লেখা: ", "")
    ]

    sections = story.get("sections", [])
    section_name = (
        sections[0].get("display-name") or sections[0].get("name", "")
    ) if sections else ""
    subheadline = story.get("subheadline", "").strip()
    description = subheadline or section_name

    img = img_url(story.get("hero-image-s3-key", ""))

    item = ET.Element("item")
    ET.SubElement(item, "title").text = headline
    ET.SubElement(item, "link").text  = url
    ET.SubElement(item, "guid", isPermaLink="true").text = url

    if description:
        ET.SubElement(item, "description").text = description
    if pub_ms:
        ET.SubElement(item, "pubDate").text = ms_to_rfc2822(pub_ms)
    if authors:
        ET.SubElement(item, "dc:creator").text = ", ".join(authors)
    if section_name:
        ET.SubElement(item, "category").text = section_name
    if img:
        mc = ET.SubElement(item, "media:content")
        mc.set("url", img)
        mc.set("medium", "image")

    return item

# ---------------------------------------------------------------------------
# RSS read / write with append + cap
# ---------------------------------------------------------------------------

def load_existing_guids() -> set:
    """Return set of guid strings already in opinion.xml, or empty set."""
    if not OUTPUT_FILE.exists():
        return set()
    try:
        tree = ET.parse(OUTPUT_FILE)
        return {g.text for g in tree.findall(".//guid") if g.text}
    except ET.ParseError:
        print("WARNING: existing opinion.xml is malformed; starting fresh.", file=sys.stderr)
        return set()


def load_existing_items() -> list:
    """Return list of existing <item> elements (newest first)."""
    if not OUTPUT_FILE.exists():
        return []
    try:
        tree = ET.parse(OUTPUT_FILE)
        return list(tree.findall(".//item"))
    except ET.ParseError:
        return []


def build_rss(items: list) -> str:
    """Wrap item list in a channel + rss envelope and return pretty XML string."""
    ET.register_namespace("media", NS_MEDIA)
    ET.register_namespace("dc",    NS_DC)

    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:media", NS_MEDIA)
    rss.set("xmlns:dc",    NS_DC)

    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text        = "প্রথম আলো মতামত"
    ET.SubElement(channel, "link").text         = OPINION_URL
    ET.SubElement(channel, "description").text  = "Prothom Alo opinion and editorial columns"
    ET.SubElement(channel, "language").text     = "bn"
    ET.SubElement(channel, "lastBuildDate").text = formatdate(usegmt=True)

    for item in items:
        channel.append(item)

    raw = ET.tostring(rss, encoding="unicode")
    return parseString(raw).toprettyxml(indent="  ")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Fetching {OPINION_URL} ...", file=sys.stderr)
    html = fetch_html(OPINION_URL)
    print(f"Received {len(html)} bytes", file=sys.stderr)

    print("Parsing Quintype JSON ...", file=sys.stderr)
    data       = extract_quintype_json(html)
    collection = data["qt"]["data"]["collection"]

    raw_stories = []
    collect_stories(collection, raw_stories)
    print(f"Scraped {len(raw_stories)} raw stories from page", file=sys.stderr)

    # Load what we already have
    existing_guids = load_existing_guids()
    existing_items = load_existing_items()

    # Build new items — skip already-seen guids, dedupe within this batch
    seen_in_batch = set()
    new_items = []

    for story in raw_stories:
        url = story.get("url", "") or f"{BASE_URL}/{story.get('slug', '')}"
        if url in existing_guids or url in seen_in_batch:
            continue
        seen_in_batch.add(url)
        item = story_to_item(story)
        if item is not None:
            new_items.append(item)

    print(f"New articles to append: {len(new_items)}", file=sys.stderr)

    # Prepend new items (newest first), then existing
    merged = new_items + existing_items

    # Cap at MAX_ARTICLES — drop oldest (tail) when over limit
    if len(merged) > MAX_ARTICLES:
        dropped = len(merged) - MAX_ARTICLES
        merged  = merged[:MAX_ARTICLES]
        print(f"Cap reached: dropped {dropped} oldest articles", file=sys.stderr)

    rss_xml = build_rss(merged)
    OUTPUT_FILE.write_text(rss_xml, encoding="utf-8")
    print(f"Written {len(merged)} articles -> {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()