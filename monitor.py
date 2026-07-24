#!/usr/bin/env python3
"""
Christian Concern News Monitor - self-contained edition.
Only reports articles published within the last MAX_AGE_HOURS (default 24).
To change what it monitors, edit the FEEDS and ISSUES lists below.
"""

import os
import re
import json
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# EDIT THIS: how far back to look
# ---------------------------------------------------------------------------

MAX_AGE_HOURS = 24

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

NS_ATOM = {"a": "http://www.w3.org/2005/Atom"}
NS_DC = {"dc": "http://purl.org/dc/elements/1.1/"}


def parse_date(raw):
    """Turn a feed's date string into a timezone-aware UTC datetime."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    iso = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def is_recent(published, cutoff):
    if published is None:
        return False
    return published >= cutoff


def load_seen():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return set()
        return set(json.loads(content))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def save_seen(seen):
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

        raw_date = node.findtext("pubDate")
        if not raw_date:
            raw_date = node.findtext("date")
        if not raw_date:
            dc_node = node.find("dc:date", NS_DC)
            if dc_node is not None:
                raw_date = dc_node.text

        if title and link:
            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": source_name,
                "published": parse_date(raw_date),
            })

    for node in root.findall(".//a:entry", NS_ATOM):
        title = strip_html(node.findtext("a:title", namespaces=NS_ATOM))
        link_el = node.find("a:link", NS_ATOM)
        link = link_el.get("href") if link_el is not None else ""
        summary = strip_html(
            node.findtext("a:summary", namespaces=NS_ATOM)
            or node.findtext("a:content", namespaces=NS_ATOM)
        )

        raw_date = (
            node.findtext("a:published", namespaces=NS_ATOM)
            or node.findtext("a:updated", namespaces=NS_ATOM)
        )

        if title and link:
            items.append({
                "title": title,
                "link": link,
                "summary": summary,
                "source": source_name,
                "published": parse_date(raw_date),
            })

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
    lines = [f"# Digest - {stamp}",
             f"*Articles published in the last {MAX_AGE_HOURS} hours*", ""]
    if not any(by_issue.values()):
        lines.append("No new relevant articles this run.")
        return "\n".join(lines)
    for issue, articles in by_issue.items():
        if not articles:
            continue
        articles.sort(key=lambda a: a["published"], reverse=True)
        lines.append(f"## {issue} ({len(articles)})")
        for a in articles:
            when = a["published"].strftime("%d %b %H:%M")
            lines.append(f"- [{a['title']}]({a['link']}) - *{a['source']}*, {when} UTC")
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
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    print(f"Only reporting articles published after "
          f"{cutoff.strftime('%Y-%m-%d %H:%M UTC')}")

    seen = load_seen()
    print(f"Previously seen: {len(seen)} articles")

    all_items = []
    for name, url in FEEDS:
        try:
            items = parse_feed(fetch(url), name)
            fresh = [i for i in items if is_recent(i["published"], cutoff)]
            undated = sum(1 for i in items if i["published"] is None)
            note = f" ({undated} undated, skipped)" if undated else ""
            print(f"  {name}: {len(items)} fetched, {len(fresh)} recent{note}")
            all_items.extend(fresh)
        except Exception as e:
            print(f"  ! {name} failed: {e}")

    new_items = []
    for it in all_items:
        aid = hashlib.sha256(it["link"].encode()).hexdigest()
        if aid not in seen:
            new_items.append(it)
            seen.add(aid)

    print(f"\n{len(new_items)} new and recent, out of {len(all_items)} recent")

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
