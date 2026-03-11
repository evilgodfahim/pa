#!/usr/bin/env python3
"""
Prothom Alo Opinion/Editorial scraper → RSS feed
Parses the embedded Quintype JSON blob from prothomalo.com/opinion.

- Appends new articles to opinion.xml on each run (deduped by story id via guid)
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

BASE_URL    = "https://www.prothomalo.com"
OPINION_URL = f"{BASE_URL}/opinion"
OUTPUT_FILE = Path("opinion.xml")
IMAGE_CDN   = "https://media.prothomalo.com"
IMAGE_WIDTH = 480
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

# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_quintype_json(html: str) -> dict:
    """Find the large Quintype <script> block and parse it."""
    match = re.search(
        r'<script[^>]*>\s*(\{"qt"\s*:.*?)\s*</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Quintype JSON blob not found in page HTML")
    return json.loads(match.group(1))


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


def story_to_item(story: dict) -> ET.Element | None:
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
    section_name = (sections[0].get("display-name") or sections[0].get("name", "")) if sections else ""
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

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "dc":    "http://purl.org/dc/elements/1.1/",
}

def load_existing_guids() -> set[str]:
    """Return set of guid strings already in opinion.xml, or empty set."""
    if not OUTPUT_FILE.exists():
        return set()
    try:
        # Register namespaces so round-trip doesn't mangle them
        for prefix, uri in NS.items():
            ET.register_namespace(prefix, uri)
        tree = ET.parse(OUTPUT_FILE)
        return {g.text for g in tree.findall(".//guid") if g.text}
    except ET.ParseError:
        print("WARNING: existing opinion.xml is malformed; starting fresh.", file=sys.stderr)
        return set()


def load_existing_items() -> list[ET.Element]:
    """Return list of existing <item> elements preserving order (newest first)."""
    if not OUTPUT_FILE.exists():
        return []
    try:
        tree = ET.parse(OUTPUT_FILE)
        return list(tree.findall(".//item"))
    except ET.ParseError:
        return []


def build_rss(items: list[ET.Element]) -> str:
    """Wrap item list in a channel + rss envelope and return pretty XML string."""
    ET.register_namespace("",      "")           # default ns
    ET.register_namespace("media", NS["media"])
    ET.register_namespace("dc",    NS["dc"])

    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:media", NS["media"])
    rss.set("xmlns:dc",    NS["dc"])

    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text       = "প্রথম আলো মতামত"
    ET.SubElement(channel, "link").text        = OPINION_URL
    ET.SubElement(channel, "description").text = "Prothom Alo opinion and editorial columns"
    ET.SubElement(channel, "language").text    = "bn"
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

    print("Parsing Quintype JSON ...", file=sys.stderr)
    data       = extract_quintype_json(html)
    collection = data["qt"]["data"]["collection"]

    raw_stories: list[dict] = []
    collect_stories(collection, raw_stories)
    print(f"Scraped {len(raw_stories)} raw stories from page", file=sys.stderr)

    # Load what we already have
    existing_guids = load_existing_guids()
    existing_items = load_existing_items()

    # Build new items (skip already-seen guids; dedupe within this batch too)
    seen_in_batch: set[str] = set()
    new_items: list[ET.Element] = []

    for story in raw_stories:
        url = story.get("url", "") or f"{BASE_URL}/{story.get('slug', '')}"
        if url in existing_guids or url in seen_in_batch:
            continue
        seen_in_batch.add(url)
        item = story_to_item(story)
        if item is not None:
            new_items.append(item)

    print(f"New articles to append: {len(new_items)}", file=sys.stderr)

    # Newest first: prepend new items to existing list
    merged = new_items + existing_items

    # Cap at MAX_ARTICLES — drop oldest (tail) when over limit
    if len(merged) > MAX_ARTICLES:
        dropped = len(merged) - MAX_ARTICLES
        merged  = merged[:MAX_ARTICLES]
        print(f"Cap reached: dropped {dropped} oldest articles", file=sys.stderr)

    rss_xml = build_rss(merged)
    OUTPUT_FILE.write_text(rss_xml, encoding="utf-8")
    print(f"Written {len(merged)} articles → {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
