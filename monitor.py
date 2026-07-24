#!/usr/bin/env python3
"""
Christian Concern News Monitor - self-contained edition.

Everything lives in this one file: feeds, keywords, and logic.
No config files, no pip installs, no dependencies beyond Python itself.

To change what it monitors, edit the FEEDS and ISSUES lists below.
"""

import os
import re
import json
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# EDIT THIS: news sources
# ---------------------------------------------------------------------------

FEEDS = [
    ("BBC News (UK)", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
    ("BBC News (Health)", "https://feeds.bbci.co.uk/news/health/rss.xml"),
    ("BBC News (Politics)", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
    ("Sky News (UK)", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    ("The Independent", "https://www.independent.co.uk/news/uk/rss"),
    ("The Telegraph", "https://www.telegraph.co.uk/rss.xml"),
    ("Daily Mail", "https://www.dailymail.co.uk/news/index.rss"),
    ("The Mirror", "https://www.mirror.co.uk/news/?service=rss"),
    ("The Guardian (UK)", "https://www.theguardian.com/uk/rss"),
    ("Christian Today", "https://www.christiantoday.com/rss-feeds"),
    ("Church Times", "https://www.churchtimes.co.uk/rss/news"),
    # Google News searches - these reach paywalled titles' headlines too.
    ("Google News: assisted dying",
     "https://news.google.com/rss/search?q=assisted+dying+UK&hl=en-GB&gl=GB&ceid=GB:en"),
    ("Google News: abortion UK",
     "https://news.google.com/rss/search?q=abortion+UK&hl=en-GB&gl=GB&ceid=GB:en"),
]

# ---------------------------------------------------------------------------
# EDIT THIS: your issues and the words that flag them
# ---------------------------------------------------------------------------

ISSUES = {
    "Assisted Suicide / Assisted Dying": [
        "assisted dying", "assisted suicide", "right to die", "euthanasia",
        "terminally ill adults bill", "end of life bill",
    ],
    "Abortion": [
        "abortion", "buffer zone", "pro-life", "pro-choice", "unborn",
    ],
    "Islam": [
        "islam", "islamist", "sharia", "muslim", "mosque", "grooming gang",
    ],
    "Religious Liberty": [
        "religious freedom", "religious liberty", "free speech",
        "christian persecution", "conversion therapy", "street preacher",
    ],
    "Marriage & Family": [
        "same-sex marriage", "gender ideology", "puberty blocker",
        "cass review", "surrogacy",
    ],
}

# ---------------------------------------------------------------------------

STATE_FILE = "seen_articles.json"
LOG_FILE = "digest_log.md"
MAX_LOG_ENTRIES = 150
USER_AGENT = "Mozilla/5.0 (compatible; NewsMonitor/1.0)"


def load_seen():
    """Load previously-seen article IDs. Survives a missing, empty or corrupt file."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return set()
        return set(json.loads(content))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def save_seen(seen):
    # Keep only the most recent 5000 so the file doesn't grow forever.
    trimmed = sorted(seen)[-5000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text):
    return re.sub("<[^<]+?>", "", text or "").strip()


def parse_feed(xml_bytes, source_name):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items

    for node in root.findall(".//item"):
        title = strip_html(node.findtext("title"))
        link = (node.findtext("link") or "").strip()
        summary = strip_html(node.findtext("description"))
        if title and link:
            items.append({"title": title, "link": link,
                          "summary": summary, "source": source_name})

    ns = {"a": "http://www.w3.org/2005/Atom"}
    for node in root.findall(".//a:entry", ns):
        title = strip_html(node.findtext("a:title", namespaces=ns))
        link_el = node.find("a:link", ns)
        link = link_el.get("href") if link_el is not None else ""
        summary = strip_html(
            node.findtext("a:summary", namespaces=ns)
            or node.findtext("a:content", namespaces=ns)
        )
        if title and link:
            items.append({"title": title, "link": link,
                          "summary": summary, "source": source_name})

    return items


def classify(item):
    text = f"{item['title']} {item['summary']}".lower()
    hits = []
    for issue, keywords in ISSUES.items():
        if any(kw.lower() in text for kw in keywords):
            hits.append(issue)
    return hits


def build_digest(by_issue):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Digest - {stamp}", ""]
    if not any(by_issue.values()):
        lines.append("No new relevant articles this run.")
        return "\n".join(lines)
    for issue, articles in by_issue.items():
        if not articles:
            continue
        lines.append(f"## {issue} ({len(articles)})")
        for a in articles:
            lines.append(f"- [{a['title']}]({a['link']}) - *{a['source']}*")
        lines.append("")
    return "\n".join(lines)


def append_log(digest):
    sep = "\n\n---\n\n"
    old = ""
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            old = f.read()
    except FileNotFoundError:
        pass
    combined = digest.strip() + (sep + old if old.strip() else "")
    parts = [p for p in combined.split(sep) if p.strip()][:MAX_LOG_ENTRIES]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(sep.join(parts) + "\n")


def post_slack(text):
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return
    data = json.dumps({"text": text[:3000]}).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  ! Slack failed: {e}")


def main():
    seen = load_seen()
    print(f"Previously seen: {len(seen)} articles")

    all_items = []
    for name, url in FEEDS:
        try:
            items = parse_feed(fetch(url), name)
            print(f"  {name}: {len(items)}")
            all_items.extend(items)
        except Exception as e:
            print(f"  ! {name} failed: {e}")

    new_items = []
    for it in all_items:
        aid = hashlib.sha256(it["link"].encode()).hexdigest()
        if aid not in seen:
            new_items.append(it)
            seen.add(aid)

    print(f"\n{len(new_items)} new of {len(all_items)} fetched")

    by_issue = {name: [] for name in ISSUES}
    for it in new_items:
        for issue in classify(it):
            by_issue[issue].append(it)

    digest = build_digest(by_issue)
    print("\n" + digest)

    if any(by_issue.values()):
        append_log(digest)
        post_slack(digest)

    save_seen(seen)
    print("Done.")


if __name__ == "__main__":
    main()
