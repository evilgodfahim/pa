#!/usr/bin/env python3
"""
Prothom Alo English Opinion scraper → RSS feed
Extracts the embedded Quintype JSON blob from en.prothomalo.com/opinion.

- Appends new articles to opinion.xml, deduped by URL
- Caps feed at MAX_ARTICLES=500; oldest articles dropped when cap exceeded
- Uses metadata.excerpt as description (real editorial summary)
- Falls back to author avatar when no hero image exists
- Output: opinion.xml (Inoreader-compatible RSS 2.0)
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.dom.minidom import parseString

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL     = "https://en.prothomalo.com"
OPINION_URL  = f"{BASE_URL}/opinion"
OUTPUT_FILE  = Path("opinion.xml")
IMAGE_CDN    = "https://media.prothomalo.com"
IMAGE_WIDTH  = 600
MAX_ARTICLES = 500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         BASE_URL,
    "Cache-Control":   "no-cache",
}

NS_MEDIA = "http://search.yahoo.com/mrss/"
NS_DC    = "http://purl.org/dc/elements/1.1/"
NS_ATOM  = "http://www.w3.org/2005/Atom"

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_html(url: str, retries: int = 3) -> str:
    """Fetch page HTML with retries. Raises on persistent failure."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            html = resp.text
            print(f"Fetched {len(html):,} bytes (attempt {attempt})", file=sys.stderr)
            return html
        except requests.RequestException as e:
            last_err = e
            print(f"Attempt {attempt} failed: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(3 * attempt)
    raise last_err

# ---------------------------------------------------------------------------
# Quintype JSON extraction (3-strategy cascade)
# ---------------------------------------------------------------------------

def extract_quintype_json(html: str) -> dict:
    """
    Extract Quintype page-data JSON. Tries three strategies, most specific first.

    Strategy 1: <script id="static-page">{ ... }</script>   ← canonical
    Strategy 2: Any <script> body starting with {"qt":
    Strategy 3: Any <script> body containing "qt":{"config" (raw_decode scan)
    """

    # Strategy 1
    m = re.search(
        r'<script[^>]+id=["\']static-page["\'][^>]*>\s*(\{.*?)\s*</script>',
        html, re.DOTALL,
    )
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            print(f"Strategy 1 parse failed: {e}", file=sys.stderr)

    # Strategy 2
    for body in re.findall(r'<script[^>]*>\s*(\{"qt"\s*:.*?)</script>', html, re.DOTALL):
        try:
            return json.loads(body.strip())
        except json.JSONDecodeError:
            continue

    # Strategy 3
    decoder = json.JSONDecoder()
    for body in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        if '"qt"' not in body or '"config"' not in body:
            continue
        for i, ch in enumerate(body):
            if ch != '{':
                continue
            try:
                obj, _ = decoder.raw_decode(body, i)
                if isinstance(obj, dict) and "qt" in obj:
                    return obj
            except json.JSONDecodeError:
                continue

    # All strategies failed — dump diagnostics
    print("\n--- DIAGNOSTIC: first 2000 chars ---", file=sys.stderr)
    print(html[:2000], file=sys.stderr)
    print(f'\n--- <script> tags: {html.count("<script")}', file=sys.stderr)
    print(f'--- "static-page": {html.count("static-page")}', file=sys.stderr)
    print(f'--- "qt": {html.count(chr(34)+"qt"+chr(34))}', file=sys.stderr)
    raise ValueError("Quintype JSON blob not found. See diagnostic output above.")

# ---------------------------------------------------------------------------
# Story collection
# ---------------------------------------------------------------------------

def collect_stories(obj: object, out: list) -> None:
    """Recursively walk the collection tree and collect story dicts."""
    if isinstance(obj, dict):
        if obj.get("type") == "story" and "story" in obj:
            out.append(obj["story"])
        for v in obj.values():
            collect_stories(v, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_stories(item, out)

# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def ms_to_rfc2822(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return formatdate(dt.timestamp(), usegmt=True)


def build_image_url(s3_key: str, width: int = IMAGE_WIDTH) -> str:
    if not s3_key:
        return ""
    return f"{IMAGE_CDN}/{s3_key}?w={width}&auto=format"


def get_description(story: dict) -> str:
    """
    Priority:
    1. metadata.excerpt  — real editorial summary written by editors
    2. subheadline       — fallback (often just "Opinion", not useful)
    3. section name      — last resort
    """
    excerpt = (story.get("metadata") or {}).get("excerpt", "").strip()
    if excerpt:
        return excerpt

    subheadline = story.get("subheadline", "").strip()
    # subheadline is often just "Opinion" — skip if it's a single generic word
    if subheadline and subheadline.lower() not in ("opinion", "op-ed", "editorial", "column"):
        return subheadline

    sections = story.get("sections", [])
    if sections:
        return sections[0].get("display-name") or sections[0].get("name", "")

    return ""


def get_thumbnail(story: dict) -> str:
    """
    Priority:
    1. hero-image-s3-key  — article thumbnail
    2. author avatar-url  — fallback when no hero image (direct URL, no CDN build needed)
    3. author avatar-s3-key — build from CDN
    """
    hero = story.get("hero-image-s3-key", "")
    if hero:
        return build_image_url(hero)

    for author in story.get("authors", []):
        av_url = author.get("avatar-url", "")
        if av_url:
            return av_url
        av_key = author.get("avatar-s3-key", "")
        if av_key:
            return build_image_url(av_key, width=200)

    return ""


def get_authors(story: dict) -> list:
    return [
        a.get("name", "").strip()
        for a in story.get("authors", [])
        if a.get("name", "").strip()
        and a.get("name", "").strip() not in ("লেখা: ", "")
    ]


def get_tags(story: dict) -> list:
    return [t.get("name", "").strip() for t in story.get("tags", []) if t.get("name")]

# ---------------------------------------------------------------------------
# Story → RSS <item>
# ---------------------------------------------------------------------------

def story_to_item(story: dict) -> ET.Element | None:
    headline = story.get("headline", "").strip()
    url      = story.get("url", "").strip()

    # Fallback URL from slug
    if not url:
        slug = story.get("slug", "")
        url  = f"{BASE_URL}/{slug}" if slug else ""

    if not headline or not url:
        return None

    pub_ms = story.get("published-at") or story.get("last-published-at") or story.get("first-published-at")

    authors     = get_authors(story)
    tags        = get_tags(story)
    description = get_description(story)
    thumbnail   = get_thumbnail(story)

    sections     = story.get("sections", [])
    section_name = ""
    section_url  = ""
    if sections:
        section_name = sections[0].get("display-name") or sections[0].get("name", "")
        section_url  = sections[0].get("section-url", "")

    story_id = story.get("id", "")

    # Build <item>
    item = ET.Element("item")

    ET.SubElement(item, "title").text = headline
    ET.SubElement(item, "link").text  = url

    # guid: use story ID if available for stable deduplication,
    # otherwise fall back to URL (Inoreader uses guid for dedup)
    guid_val = f"{BASE_URL}/story/{story_id}" if story_id else url
    ET.SubElement(item, "guid", isPermaLink="false").text = guid_val

    if description:
        ET.SubElement(item, "description").text = description

    if pub_ms:
        ET.SubElement(item, "pubDate").text = ms_to_rfc2822(pub_ms)

    if authors:
        ET.SubElement(item, "dc:creator").text = ", ".join(authors)

    if section_name:
        ET.SubElement(item, "category").text = section_name
        if section_url:
            cat = item.find("category")
            cat.set("domain", section_url)

    for tag in tags:
        ET.SubElement(item, "category").text = tag

    if thumbnail:
        mc = ET.SubElement(item, "media:content")
        mc.set("url", thumbnail)
        mc.set("medium", "image")
        # Add media:title for Inoreader display
        ET.SubElement(mc, "media:title").text = headline

    return item

# ---------------------------------------------------------------------------
# RSS persistence — load / save with append + cap
# ---------------------------------------------------------------------------

def load_existing(file: Path) -> tuple[set, list]:
    """
    Returns (existing_guids: set, existing_items: list[Element]).
    Handles missing or malformed file gracefully.
    """
    if not file.exists():
        return set(), []
    try:
        ET.register_namespace("media", NS_MEDIA)
        ET.register_namespace("dc",    NS_DC)
        ET.register_namespace("atom",  NS_ATOM)
        tree  = ET.parse(file)
        items = list(tree.findall(".//item"))
        guids = {g.text for g in tree.findall(".//guid") if g.text}
        return guids, items
    except ET.ParseError as e:
        print(f"WARNING: {file} is malformed ({e}); starting fresh.", file=sys.stderr)
        return set(), []


def build_rss(items: list) -> str:
    """Wrap item list in RSS 2.0 envelope and return pretty-printed XML."""
    ET.register_namespace("media", NS_MEDIA)
    ET.register_namespace("dc",    NS_DC)
    ET.register_namespace("atom",  NS_ATOM)

    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:media", NS_MEDIA)
    rss.set("xmlns:dc",    NS_DC)
    rss.set("xmlns:atom",  NS_ATOM)

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text       = "Prothom Alo — Opinion"
    ET.SubElement(channel, "link").text        = OPINION_URL
    ET.SubElement(channel, "description").text = "Op-eds, editorials and columns from Prothom Alo English"
    ET.SubElement(channel, "language").text    = "en"
    ET.SubElement(channel, "lastBuildDate").text = formatdate(usegmt=True)

    # atom:self — recommended by Inoreader for feed identification
    atom_link = ET.SubElement(channel, "atom:link")
    atom_link.set("href", OPINION_URL)
    atom_link.set("rel",  "self")
    atom_link.set("type", "application/rss+xml")

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

    print("Extracting Quintype JSON ...", file=sys.stderr)
    data       = extract_quintype_json(html)
    collection = data["qt"]["data"]["collection"]

    raw_stories = []
    collect_stories(collection, raw_stories)
    print(f"Stories scraped from page: {len(raw_stories)}", file=sys.stderr)

    existing_guids, existing_items = load_existing(OUTPUT_FILE)
    print(f"Existing articles in feed: {len(existing_items)}", file=sys.stderr)

    # Build new items — skip already-seen, dedupe within batch
    seen_in_batch: set = set()
    new_items: list    = []

    for story in raw_stories:
        url      = story.get("url", "").strip()
        slug     = story.get("slug", "")
        story_id = story.get("id", "")

        if not url and slug:
            url = f"{BASE_URL}/{slug}"

        # Guid matches what story_to_item() produces
        guid = f"{BASE_URL}/story/{story_id}" if story_id else url

        if guid in existing_guids or guid in seen_in_batch:
            continue
        seen_in_batch.add(guid)

        item = story_to_item(story)
        if item is not None:
            new_items.append(item)

    print(f"New articles to append: {len(new_items)}", file=sys.stderr)

    # Merge: new first (newest at top), then existing
    merged = new_items + existing_items

    # Cap at MAX_ARTICLES — drop oldest (tail)
    if len(merged) > MAX_ARTICLES:
        dropped = len(merged) - MAX_ARTICLES
        merged  = merged[:MAX_ARTICLES]
        print(f"Cap reached: dropped {dropped} oldest articles", file=sys.stderr)

    rss_xml = build_rss(merged)
    OUTPUT_FILE.write_text(rss_xml, encoding="utf-8")
    print(f"Done — {len(merged)} articles written to {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
