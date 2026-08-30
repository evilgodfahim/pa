#!/usr/bin/env python3
"""
Prothom Alo Opinion scraper → RSS feeds

Sources:
  - https://prothomalo.com/opinion     → opinion.xml
  - https://en.prothomalo.com/opinion  → opinion_en.xml

What it does:
- Fetches each opinion page
- Extracts Quintype JSON from the page
- Builds/updates the corresponding XML
- Dedupes by stable GUID
- Keeps only the newest MAX_ARTICLES items
- Uses metadata.excerpt as description when available
- Falls back to author avatar if no story image exists
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any
from xml.dom.minidom import parseString
from xml.etree import ElementTree as ET

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SOURCES = [
    {
        "base_url":   "https://prothomalo.com",
        "opinion_url": "https://prothomalo.com/opinion",
        "output_file": Path("opinion.xml"),
    },
    {
        "base_url":   "https://en.prothomalo.com",
        "opinion_url": "https://en.prothomalo.com/opinion",
        "output_file": Path("opinion_en.xml"),
    },
]

IMAGE_CDN   = "https://media.prothomalo.com"
IMAGE_WIDTH = 600
MAX_ARTICLES = 500
REQUEST_TIMEOUT = 30
RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
}

NS_MEDIA = "http://search.yahoo.com/mrss/"
NS_DC    = "http://purl.org/dc/elements/1.1/"
NS_ATOM  = "http://www.w3.org/2005/Atom"

ET.register_namespace("media", NS_MEDIA)
ET.register_namespace("dc",    NS_DC)
ET.register_namespace("atom",  NS_ATOM)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return normalize_ws(value)
    return normalize_ws(str(value))


def ms_to_rfc2822(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return formatdate(dt.timestamp(), usegmt=True)


def build_image_url(s3_key: str, width: int = IMAGE_WIDTH) -> str:
    s3_key = safe_text(s3_key)
    if not s3_key:
        return ""
    return f"{IMAGE_CDN}/{s3_key}?w={width}&auto=format"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_html(url: str, retries: int = RETRIES) -> str:
    last_err: Exception | None = None
    session = requests.Session()
    session.headers.update({**HEADERS, "Referer": "/".join(url.split("/")[:3])})

    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()

            if not resp.encoding:
                resp.encoding = resp.apparent_encoding or "utf-8"

            html = resp.text
            if len(html) < 500:
                raise ValueError(f"Response too small ({len(html)} chars)")

            print(f"  Fetched {len(html):,} chars (attempt {attempt})", file=sys.stderr)
            return html
        except Exception as e:
            last_err = e
            print(f"  Attempt {attempt} failed: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(2 * attempt)

    assert last_err is not None
    raise last_err


# ---------------------------------------------------------------------------
# Quintype JSON extraction
# ---------------------------------------------------------------------------

def _try_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        return None
    return None


def extract_quintype_json(html: str) -> dict:
    m = re.search(
        r'<script[^>]+id=["\']static-page["\'][^>]*>(\{.*?\})</script>',
        html, re.DOTALL,
    )
    if m:
        obj = _try_json(m.group(1))
        if obj is not None:
            return obj

    for body in re.findall(r'<script[^>]*>\s*(\{"qt".*?\})\s*</script>', html, re.DOTALL):
        obj = _try_json(body)
        if obj is not None:
            return obj

    decoder = json.JSONDecoder()
    for body in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        if '"qt"' not in body:
            continue
        for i, ch in enumerate(body):
            if ch != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(body, i)
                if isinstance(obj, dict) and "qt" in obj:
                    return obj
            except json.JSONDecodeError:
                continue

    print("\n--- DIAGNOSTIC ---", file=sys.stderr)
    print(f"HTML length: {len(html)}", file=sys.stderr)
    print(f"<script> tags: {html.count('<script')}", file=sys.stderr)
    print(f'"qt": {html.count(chr(34) + "qt" + chr(34))}', file=sys.stderr)
    print("First 1200 chars:", file=sys.stderr)
    print(html[:1200], file=sys.stderr)
    raise ValueError("Quintype JSON blob not found. See diagnostic output above.")


# ---------------------------------------------------------------------------
# Story collection
# ---------------------------------------------------------------------------

def collect_stories(obj: object, out: list[dict]) -> None:
    if isinstance(obj, dict):
        if obj.get("type") == "story" and isinstance(obj.get("story"), dict):
            out.append(obj["story"])
        for v in obj.values():
            collect_stories(v, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_stories(item, out)


# ---------------------------------------------------------------------------
# Story field helpers  (base_url passed explicitly — no global dependency)
# ---------------------------------------------------------------------------

def get_description(story: dict) -> str:
    metadata = story.get("metadata") or {}
    excerpt = safe_text(metadata.get("excerpt"))
    if excerpt:
        return excerpt

    subheadline = safe_text(story.get("subheadline"))
    if subheadline and subheadline.lower() not in {"opinion", "op-ed", "editorial", "column"}:
        return subheadline

    sections = story.get("sections") or []
    if sections and isinstance(sections, list):
        first = sections[0] or {}
        return safe_text(first.get("display-name") or first.get("name"))

    return ""


def get_thumbnail(story: dict) -> str:
    hero = safe_text(story.get("hero-image-s3-key"))
    if hero:
        return build_image_url(hero)

    for author in story.get("authors") or []:
        if not isinstance(author, dict):
            continue
        avatar_url = safe_text(author.get("avatar-url"))
        if avatar_url:
            return avatar_url
        avatar_key = safe_text(author.get("avatar-s3-key"))
        if avatar_key:
            return build_image_url(avatar_key, width=200)

    return ""


def get_authors(story: dict) -> list[str]:
    result: list[str] = []
    for author in story.get("authors") or []:
        if not isinstance(author, dict):
            continue
        name = safe_text(author.get("name"))
        if name and name not in {"লেখা:", ""}:
            result.append(name)
    return result


def get_tags(story: dict) -> list[str]:
    return [
        safe_text(tag.get("name"))
        for tag in (story.get("tags") or [])
        if isinstance(tag, dict) and safe_text(tag.get("name"))
    ]


def get_story_url(story: dict, base_url: str) -> str:
    url = safe_text(story.get("url"))
    if url:
        return url
    slug = safe_text(story.get("slug"))
    if slug:
        if slug.startswith("http://") or slug.startswith("https://"):
            return slug
        return f"{base_url}/{slug.lstrip('/')}"
    return ""


def get_story_guid(story: dict, base_url: str) -> str:
    story_id = safe_text(story.get("id"))
    if story_id:
        return f"{base_url}/story/{story_id}"
    return get_story_url(story, base_url)


def get_pub_ms(story: dict) -> int | None:
    for key in ("published-at", "last-published-at", "first-published-at"):
        value = story.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            num = int(value)
            if num > 0:
                return num
    return None


# ---------------------------------------------------------------------------
# Story → RSS item
# ---------------------------------------------------------------------------

def story_to_item(story: dict, base_url: str) -> ET.Element | None:
    headline = safe_text(story.get("headline"))
    url      = get_story_url(story, base_url)
    guid     = get_story_guid(story, base_url)

    if not headline or not url or not guid:
        return None

    item = ET.Element("item")
    ET.SubElement(item, "title").text = headline
    ET.SubElement(item, "link").text  = url
    ET.SubElement(item, "guid", isPermaLink="false").text = guid

    description = get_description(story)
    if description:
        ET.SubElement(item, "description").text = description

    pub_ms = get_pub_ms(story)
    if pub_ms:
        ET.SubElement(item, "pubDate").text = ms_to_rfc2822(pub_ms)

    authors = get_authors(story)
    if authors:
        ET.SubElement(item, f"{{{NS_DC}}}creator").text = ", ".join(authors)

    sections = story.get("sections") or []
    if sections and isinstance(sections, list):
        first = sections[0] or {}
        section_name = safe_text(first.get("display-name") or first.get("name"))
        section_url  = safe_text(first.get("section-url"))
        if section_name:
            cat = ET.SubElement(item, "category")
            cat.text = section_name
            if section_url:
                cat.set("domain", section_url)

    for tag in get_tags(story):
        ET.SubElement(item, "category").text = tag

    thumbnail = get_thumbnail(story)
    if thumbnail:
        mc = ET.SubElement(item, f"{{{NS_MEDIA}}}content")
        mc.set("url", thumbnail)
        mc.set("medium", "image")
        ET.SubElement(mc, f"{{{NS_MEDIA}}}title").text = headline

    return item


# ---------------------------------------------------------------------------
# RSS persistence
# ---------------------------------------------------------------------------

def load_existing(file: Path) -> tuple[set[str], list[ET.Element]]:
    if not file.exists():
        return set(), []
    try:
        tree  = ET.parse(file)
        root  = tree.getroot()
        items = list(root.findall("./channel/item"))
        guids = {
            safe_text(item.findtext("guid"))
            for item in items
            if safe_text(item.findtext("guid"))
        }
        return guids, items
    except ET.ParseError as e:
        print(f"  WARNING: {file} is malformed ({e}); starting fresh.", file=sys.stderr)
        return set(), []


def build_rss(items: list[ET.Element], opinion_url: str, label: str) -> str:
    rss     = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text       = f"Prothom Alo — {label}"
    ET.SubElement(channel, "link").text        = opinion_url
    ET.SubElement(channel, "description").text = f"Opinion articles from {label}"
    ET.SubElement(channel, "language").text    = "en"
    ET.SubElement(channel, "lastBuildDate").text = formatdate(usegmt=True)

    atom_link = ET.SubElement(channel, f"{{{NS_ATOM}}}link")
    atom_link.set("href", opinion_url)
    atom_link.set("rel",  "self")
    atom_link.set("type", "application/rss+xml")

    for item in items:
        channel.append(item)

    raw = ET.tostring(rss, encoding="unicode")
    return parseString(raw).toprettyxml(indent="  ")


# ---------------------------------------------------------------------------
# Per-source runner
# ---------------------------------------------------------------------------

def run_source(base_url: str, opinion_url: str, output_file: Path) -> None:
    label = opinion_url.replace("https://", "").replace("/opinion", "")
    print(f"\n=== {opinion_url} → {output_file} ===", file=sys.stderr)

    html = fetch_html(opinion_url)

    print("  Extracting Quintype JSON ...", file=sys.stderr)
    data = extract_quintype_json(html)

    try:
        collection = data["qt"]["data"]["collection"]
    except Exception as e:
        raise KeyError(f"Could not find qt.data.collection: {e}")

    raw_stories: list[dict] = []
    collect_stories(collection, raw_stories)
    print(f"  Stories scraped: {len(raw_stories)}", file=sys.stderr)

    existing_guids, existing_items = load_existing(output_file)
    print(f"  Existing in feed: {len(existing_items)}", file=sys.stderr)

    seen_in_batch: set[str] = set()
    new_items: list[ET.Element] = []

    for story in raw_stories:
        item = story_to_item(story, base_url)
        if item is None:
            continue
        guid = safe_text(item.findtext("guid"))
        if not guid or guid in existing_guids or guid in seen_in_batch:
            continue
        seen_in_batch.add(guid)
        new_items.append(item)

    print(f"  New articles: {len(new_items)}", file=sys.stderr)

    merged = new_items + existing_items
    if len(merged) > MAX_ARTICLES:
        dropped = len(merged) - MAX_ARTICLES
        merged  = merged[:MAX_ARTICLES]
        print(f"  Cap reached: dropped {dropped} oldest", file=sys.stderr)

    rss_xml = build_rss(merged, opinion_url, label)
    output_file.write_text(rss_xml, encoding="utf-8")
    print(f"  Done — {len(merged)} articles → {output_file}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    for src in SOURCES:
        run_source(
            base_url    = src["base_url"],
            opinion_url = src["opinion_url"],
            output_file = src["output_file"],
        )


if __name__ == "__main__":
    main()