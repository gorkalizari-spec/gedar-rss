#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

BASE = "https://gedar.eus"
STATE_FILE = Path("state.json")
FEED_FILE = Path("feed.xml")
SEARCH_PAGES = 3
MAX_FEED_ITEMS = 300
REQUEST_DELAY = 0.25
TIMEOUT = 30
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "eu-ES,eu;q=0.9,es-ES;q=0.8,es;q=0.7,en;q=0.5",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

ARTICLE_PREFIXES = (
    "/aktualitatea/", "/arteka/", "/dokumentuak/", "/editoriala/",
    "/ikuspuntua/", "/ildo-politikoa/", "/koiuntura-politikoa/",
    "/kolaborazioak/", "/langile-zientzia/",
)
DATE_RE = re.compile(r"\b(20\d{2})[/-](0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])\b")

session = requests.Session()
session.headers.update(HEADERS)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_state():
    if not STATE_FILE.exists():
        return {"items": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("items", {})
        return data
    except Exception:
        return {"items": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


_bootstrapped = False


def bootstrap_session():
    global _bootstrapped
    if _bootstrapped:
        return
    try:
        # Visita previa a la portada para obtener cookies y parecer una
        # navegación normal antes de solicitar los listados.
        r = session.get(BASE + "/", timeout=TIMEOUT)
        print(f"[info] portada inicial: HTTP {r.status_code}")
        if r.status_code < 400:
            _bootstrapped = True
    except Exception as exc:
        print(f"[warn] portada inicial: {exc}", file=sys.stderr)


def fetch(url):
    bootstrap_session()
    headers = {"Referer": BASE + "/"}
    r = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    print(f"[http] {r.status_code} {url}")
    r.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return r.text


def canonicalize(href, base_url=BASE):
    if not href:
        return None
    url = urljoin(base_url, href)
    p = urlparse(url)
    if p.scheme not in ("http", "https") or p.netloc.lower() not in ("gedar.eus", "www.gedar.eus"):
        return None
    path = re.sub(r"/{2,}", "/", p.path)
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse(("https", "gedar.eus", path, "", "", ""))


def clean_text(node):
    return " ".join(node.stripped_strings).strip() if node else ""


def parse_date(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return datetime(y, mo, d, 12, 0, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def meta_content(soup, selector):
    tag = soup.select_one(selector)
    if tag and tag.get("content"):
        return " ".join(tag.get("content").split())
    return None


def section_from_path(path):
    mapping = {
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
    for prefix, name in mapping.items():
        if path.startswith(prefix):
            return name
    return "GEDAR"


def article_details(url, fallback_title, section):
    first_seen = now_iso()
    title, description, published = fallback_title, "", None
    try:
        soup = BeautifulSoup(fetch(url), "html.parser")
        title = meta_content(soup, 'meta[property="og:title"]') or clean_text(soup.find("h1")) or fallback_title
        title = re.sub(r"\s*\|\s*Gedar\s*$", "", title, flags=re.I).strip()
        description = meta_content(soup, 'meta[property="og:description"]') or meta_content(soup, 'meta[name="description"]') or ""
        for selector, attr in (("time[datetime]", "datetime"), ('meta[property="article:published_time"]', "content"), ('meta[itemprop="datePublished"]', "content")):
            tag = soup.select_one(selector)
            if tag and tag.get(attr):
                published = parse_date(tag.get(attr))
                if published:
                    break
        if not published:
            published = parse_date(clean_text(soup.find("main") or soup))
        if not description:
            for p in (soup.find("main") or soup).find_all("p"):
                txt = clean_text(p)
                if len(txt) >= 70:
                    description = txt[:1500]
                    break
    except Exception as exc:
        print(f"[warn] detalle no disponible {url}: {exc}", file=sys.stderr)
    return {"title": title or fallback_title or url, "url": url, "section": section,
            "description": description, "published": published or first_seen, "first_seen": first_seen}


def collect_bilatzailea(state):
    pages = [f"{BASE}/bilatzailea"] + [f"{BASE}/bilatzailea/page/{n}" for n in range(2, SEARCH_PAGES + 1)]
    candidates = {}
    for page_url in pages:
        print(f"[info] leyendo {page_url}")
        try:
            soup = BeautifulSoup(fetch(page_url), "html.parser")
        except Exception as exc:
            print(f"[warn] {page_url}: {exc}", file=sys.stderr)
            continue
        for a in (soup.find("main") or soup).find_all("a", href=True):
            url = canonicalize(a.get("href"), page_url)
            if not url:
                continue
            path = urlparse(url).path
            if not any(path.startswith(p) for p in ARTICLE_PREFIXES):
                continue
            title = clean_text(a)
            if len(title) < 8:
                continue
            candidates.setdefault(url, (title, section_from_path(path)))
    for url, (title, section) in candidates.items():
        if url not in state["items"]:
            item = article_details(url, title, section)
            state["items"][url] = item
            print(f"[new] {section}: {item['title']}")


def collect_telebista(state):
    page_url = f"{BASE}/telebista"
    print(f"[info] leyendo {page_url}")
    try:
        soup = BeautifulSoup(fetch(page_url), "html.parser")
    except Exception as exc:
        print(f"[warn] Telebista: {exc}", file=sys.stderr)
        return
    for a in (soup.find("main") or soup).find_all("a", href=True):
        url = canonicalize(a.get("href"), page_url)
        if not url or not urlparse(url).path.startswith("/telebista/"):
            continue
        title = clean_text(a)
        if len(title) < 5 or url in state["items"]:
            continue
        nearby, parent = "", a.parent
        for _ in range(4):
            if parent is None:
                break
            nearby = clean_text(parent)
            if DATE_RE.search(nearby):
                break
            parent = parent.parent
        item = article_details(url, title, "GEDAR TB")
        date = parse_date(nearby)
        if date:
            item["published"] = date
        state["items"][url] = item
        print(f"[new] GEDAR TB: {item['title']}")


def collect_agenda(state):
    page_url = f"{BASE}/agenda"
    print(f"[info] leyendo {page_url}")
    try:
        soup = BeautifulSoup(fetch(page_url), "html.parser")
    except Exception as exc:
        print(f"[warn] Agenda: {exc}", file=sys.stderr)
        return
    for a in (soup.find("main") or soup).find_all("a", href=True):
        url = canonicalize(a.get("href"), page_url)
        if not url or not urlparse(url).path.startswith("/agenda/"):
            continue
        text = clean_text(a)
        if len(text) < 8 or url in state["items"]:
            continue
        item = article_details(url, text, "AGENDA")
        date = parse_date(text)
        if date:
            item["published"] = date
        state["items"][url] = item
        print(f"[new] AGENDA: {item['title']}")


def item_datetime(item):
    raw = item.get("published") or item.get("first_seen") or now_iso()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def add_text(parent, name, value):
    el = ET.SubElement(parent, name)
    el.text = value or ""


def build_feed(state):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    add_text(channel, "title", "Gedar — RSS completo no oficial")
    add_text(channel, "link", BASE)
    add_text(channel, "description", "RSS no oficial de Gedar: Bilatzailea + Telebista + Agenda.")
    add_text(channel, "language", "eu")
    add_text(channel, "lastBuildDate", format_datetime(datetime.now(timezone.utc)))
    items = sorted(state["items"].values(), key=item_datetime, reverse=True)[:MAX_FEED_ITEMS]
    for item in items:
        node = ET.SubElement(channel, "item")
        add_text(node, "title", f"[{item['section']}] {item['title']}")
        add_text(node, "link", item["url"])
        add_text(node, "guid", item["url"])
        add_text(node, "pubDate", format_datetime(item_datetime(item)))
        add_text(node, "category", item["section"])
        if item.get("description"):
            add_text(node, "description", html.escape(item["description"]))
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    tree.write(FEED_FILE, encoding="utf-8", xml_declaration=True)


def main():
    state = load_state()
    collect_bilatzailea(state)
    collect_telebista(state)
    collect_agenda(state)
    save_state(state)
    build_feed(state)
    print(f"[ok] {FEED_FILE} generado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
