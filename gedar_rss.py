#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests

BASE = "https://gedar.eus"
READER = "https://r.jina.ai/"
STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")

SEARCH_PAGES = 3
MAX_FEED_ITEMS = 300
TIMEOUT = 60
REQUEST_DELAY = 0.8

ARTICLE_PREFIXES = {
    "/aktualitatea/": "AKTUALITATEA",
    "/arteka/": "ARTEKA",
    "/dokumentuak/": "DOKUMENTUAK",
    "/editoriala/": "EDITORIALA",
    "/ikuspuntua/": "IKUSPUNTUA",
    "/ildo-politikoa/": "ILDO POLITIKOA",
    "/koiuntura-politikoa/": "KOIUNTURA POLITIKOA",
    "/kolaborazioak/": "KOLABORAZIOAK",
    "/langile-zientzia/": "LANGILE ZIENTZIA",
}

MD_LINK_RE = re.compile(r"\[([^\]\n]{2,300})\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/139.0 Safari/537.36",
    "Accept": "text/plain, text/markdown, */*",
})


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"items": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data.get("items"), dict):
            data["items"] = {}
        return data
    except Exception:
        return {"items": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def canonicalize(url: str, base_url: str = BASE) -> str | None:
    if not url:
        return None
    url = urljoin(base_url, url.strip())
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in ("gedar.eus", "www.gedar.eus"):
        return None

    path = re.sub(r"/{2,}", "/", parsed.path)
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    return urlunparse(("https", "gedar.eus", path, "", "", ""))


def fetch_via_reader(target_url: str) -> str:
    reader_url = READER + target_url
    headers = {
        "X-No-Cache": "true",
        "X-Proxy": "auto",
    }

    print(f"[reader] {target_url}")
    response = session.get(reader_url, headers=headers, timeout=TIMEOUT)
    print(f"[reader-http] {response.status_code} {target_url}")
    response.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return response.text


def section_from_url(url: str) -> str | None:
    path = urlparse(url).path
    for prefix, section in ARTICLE_PREFIXES.items():
        if path.startswith(prefix):
            return section
    return None


def looks_like_article(url: str) -> bool:
    path = urlparse(url).path
    section = section_from_url(url)
    if not section:
        return False

    for prefix in ARTICLE_PREFIXES:
        if path.startswith(prefix):
            tail = path[len(prefix):].strip("/")
            if not tail:
                return False

            first = tail.split("/", 1)[0].lower()
            if first in {
                "page", "orria", "etiketa", "etiketak", "tag", "tags",
                "egilea", "autor", "author", "category", "kategoria"
            }:
                return False
            return True

    return False


def clean_title(title: str) -> str:
    title = re.sub(r"!\[[^\]]*\]", "", title)
    title = re.sub(r"\s+", " ", title).strip(" \t\r\n-|·")
    return title[:300]


def extract_markdown_links(markdown: str, source_url: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen = set()

    for title, href in MD_LINK_RE.findall(markdown):
        url = canonicalize(href, source_url)
        if not url or url in seen:
            continue

        title = clean_title(title)
        if len(title) < 4:
            continue

        seen.add(url)
        found.append((url, title))

    return found


def add_new_item(
    state: dict,
    url: str,
    title: str,
    section: str,
    ordinal: int,
) -> None:
    if url in state["items"]:
        return

    discovered = now_utc() - timedelta(seconds=ordinal)

    state["items"][url] = {
        "title": title,
        "url": url,
        "section": section,
        "description": "",
        "published": discovered.isoformat(),
        "first_seen": discovered.isoformat(),
    }
    print(f"[new] {section}: {title}")


def collect_bilatzailea(state: dict) -> None:
    pages = [f"{BASE}/bilatzailea"] + [
        f"{BASE}/bilatzailea/page/{n}" for n in range(2, SEARCH_PAGES + 1)
    ]

    ordinal = 0

    for page_url in pages:
        try:
            markdown = fetch_via_reader(page_url)
        except Exception as exc:
            print(f"[warn] Bilatzailea {page_url}: {exc}", file=sys.stderr)
            continue

        links = extract_markdown_links(markdown, page_url)
        matched = 0

        for url, title in links:
            if not looks_like_article(url):
                continue

            section = section_from_url(url)
            if not section:
                continue

            add_new_item(state, url, title, section, ordinal)
            ordinal += 1
            matched += 1

        print(f"[info] Bilatzailea: {matched} enlaces editoriales detectados en {page_url}")


def collect_telebista(state: dict) -> None:
    page_url = f"{BASE}/telebista"

    try:
        markdown = fetch_via_reader(page_url)
    except Exception as exc:
        print(f"[warn] Telebista: {exc}", file=sys.stderr)
        return

    links = extract_markdown_links(markdown, page_url)
    ordinal = 1000
    matched = 0

    for url, title in links:
        path = urlparse(url).path
        if not path.startswith("/telebista/") or path == "/telebista":
            continue

        tail = path[len("/telebista/"):].strip("/")
        if not tail or tail.startswith(("page/", "programak/", "programas/")):
            continue

        add_new_item(state, url, title, "GEDAR TB", ordinal)
        ordinal += 1
        matched += 1

    print(f"[info] Telebista: {matched} enlaces detectados")


def collect_agenda(state: dict) -> None:
    page_url = f"{BASE}/agenda"

    try:
        markdown = fetch_via_reader(page_url)
    except Exception as exc:
        print(f"[warn] Agenda: {exc}", file=sys.stderr)
        return

    links = extract_markdown_links(markdown, page_url)
    ordinal = 2000
    matched = 0

    for url, title in links:
        path = urlparse(url).path
        if not path.startswith("/agenda/") or path == "/agenda":
            continue

        tail = path[len("/agenda/"):].strip("/")
        if not tail or tail.startswith(("page/", "orria/")):
            continue

        add_new_item(state, url, title, "AGENDA", ordinal)
        ordinal += 1
        matched += 1

    print(f"[info] Agenda: {matched} enlaces detectados")


def parse_item_datetime(item: dict) -> datetime:
    raw = item.get("published") or item.get("first_seen")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return now_utc()


def add_text(parent: ET.Element, tag: str, text: str) -> ET.Element:
    node = ET.SubElement(parent, tag)
    node.text = text or ""
    return node


def build_feed(state: dict) -> None:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    add_text(channel, "title", "Gedar — RSS completo no oficial")
    add_text(channel, "link", BASE)
    add_text(
        channel,
        "description",
        "RSS no oficial de Gedar: Bilatzailea + Telebista + Agenda."
    )
    add_text(channel, "language", "eu")
    add_text(channel, "lastBuildDate", format_datetime(now_utc()))

    items = sorted(
        state["items"].values(),
        key=parse_item_datetime,
        reverse=True,
    )[:MAX_FEED_ITEMS]

    for item in items:
        node = ET.SubElement(channel, "item")
        add_text(node, "title", f"[{item['section']}] {item['title']}")
        add_text(node, "link", item["url"])
        add_text(node, "guid", item["url"])
        add_text(node, "pubDate", format_datetime(parse_item_datetime(item)))
        add_text(node, "category", item["section"])

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(FEED_FILE, encoding="utf-8", xml_declaration=True)

    print(f"[ok] feed.xml generado con {len(items)} entradas")


def main() -> int:
    state = load_state()

    collect_bilatzailea(state)
    collect_telebista(state)
    collect_agenda(state)

    save_state(state)
    build_feed(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
    
