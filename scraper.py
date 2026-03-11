#!/usr/bin/env python3
"""
Prothom Alo Opinion/Editorial scraper → RSS feed
Parses the embedded Quintype JSON blob from prothomalo.com/opinion.

Output: prothomalo_opinion.xml
"""

import json
import re
import sys
from datetime import datetime, timezone
from email.utils import formatdate
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom.minidom import parseString

import requests

BASE_URL = "https://www.prothomalo.com"
OPINION_URL = f"{BASE_URL}/opinion"
OUTPUT_FILE = "prothomalo_opinion.xml"
IMAGE_CDN = "https://media.prothomalo.com"
IMAGE_WIDTH = 480

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bn-BD,bn;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": BASE_URL,
}


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_quintype_json(html: str) -> dict:
    """
    Find the large Quintype data script tag and parse it.
    It's an undecorated <script> block containing a JSON object starting with {"qt":...}
    """
    # Find script tag whose content starts with {"qt": (after optional whitespace)
    pattern = re.compile(
        r'<script[^>]*>\s*(\{"qt"\s*:.*?)\s*</script>',
        re.DOTALL,
    )
    match = pattern.search(html)
    if not match:
        raise ValueError("Could not find Quintype JSON blob in page HTML")
    return json.loads(match.group(1))


def collect_stories(obj: object, stories: list) -> None:
    """Recursively walk the collection tree and collect all story items."""
    if isinstance(obj, dict):
        if obj.get("type") == "story" and "story" in obj:
            stories.append(obj["story"])
        for v in obj.values():
            collect_stories(v, stories)
    elif isinstance(obj, list):
        for item in obj:
            collect_stories(item, stories)


def ms_to_rfc2822(ms: int) -> str:
    """Convert epoch-milliseconds to RFC 2822 date string for RSS pubDate."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return formatdate(dt.timestamp(), usegmt=True)


def ms_to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def image_url(s3_key: str) -> str:
    if not s3_key:
        return ""
    return f"{IMAGE_CDN}/{s3_key}?w={IMAGE_WIDTH}&auto=format"


def build_rss(stories: list) -> str:
    rss = Element("rss", version="2.0")
    rss.set("xmlns:media", "http://search.yahoo.com/mrss/")
    rss.set("xmlns:dc", "http://purl.org/dc/elements/1.1/")

    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "প্রথম আলো মতামত"
    SubElement(channel, "link").text = OPINION_URL
    SubElement(channel, "description").text = (
        "Prothom Alo opinion and editorial columns"
    )
    SubElement(channel, "language").text = "bn"
    SubElement(channel, "lastBuildDate").text = formatdate(usegmt=True)

    seen_ids: set[str] = set()

    for story in stories:
        story_id = story.get("id", "")
        if story_id in seen_ids:
            continue
        seen_ids.add(story_id)

        headline = story.get("headline", "").strip()
        url = story.get("url", "")
        if not url:
            slug = story.get("slug", "")
            url = f"{BASE_URL}/{slug}" if slug else ""
        if not headline or not url:
            continue

        pub_ms = story.get("published-at") or story.get("last-published-at")
        pub_date = ms_to_rfc2822(pub_ms) if pub_ms else ""

        authors = story.get("authors", [])
        author_name = ", ".join(
            a.get("name", "").strip()
            for a in authors
            if a.get("name", "").strip() not in ("লেখা: ", "")
        )

        section_name = ""
        sections = story.get("sections", [])
        if sections:
            section_name = sections[0].get("display-name") or sections[0].get("name", "")

        subheadline = story.get("subheadline", "").strip()
        description = subheadline if subheadline else section_name

        img_key = story.get("hero-image-s3-key", "")
        img = image_url(img_key)

        item = SubElement(channel, "item")
        SubElement(item, "title").text = headline
        SubElement(item, "link").text = url
        SubElement(item, "guid", isPermaLink="true").text = url

        if description:
            SubElement(item, "description").text = description
        if pub_date:
            SubElement(item, "pubDate").text = pub_date
        if author_name:
            SubElement(item, "dc:creator").text = author_name
        if section_name:
            SubElement(item, "category").text = section_name
        if img:
            media_content = SubElement(item, "media:content")
            media_content.set("url", img)
            media_content.set("medium", "image")

    xml_bytes = tostring(rss, encoding="unicode")
    return parseString(xml_bytes).toprettyxml(indent="  ")


def main():
    print(f"Fetching {OPINION_URL} ...", file=sys.stderr)
    html = fetch_html(OPINION_URL)

    print("Parsing Quintype JSON ...", file=sys.stderr)
    data = extract_quintype_json(html)

    collection = data["qt"]["data"]["collection"]
    stories: list = []
    collect_stories(collection, stories)
    print(f"Found {len(stories)} stories", file=sys.stderr)

    rss_xml = build_rss(stories)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    print(f"Written → {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
