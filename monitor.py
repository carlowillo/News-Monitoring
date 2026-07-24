#!/usr/bin/env python3
"""
Christian Concern News Monitor - AI edition.

Two big changes from the basic version:

1. COVERAGE: instead of only reading a fixed list of newspaper feeds, it
   runs Google News searches for each of your search terms. Google indexes
   nearly every outlet (including paywalled ones like The Times), so this
   catches far more than RSS alone.

2. RELEVANCE: instead of simple keyword matching, it asks GPT-5.5 to judge
   each article - so it catches stories that are clearly relevant without
   containing an obvious keyword, and explains why each one matters.

Needs one GitHub secret: OPENAI_API_KEY
Without it, the script still runs and falls back to keyword matching.
"""

import os
import re
import json
import time
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
# EDIT THIS: search terms. Each one becomes a Google News search.
# This is what drives COVERAGE - add terms generously, the AI filters later.
# ---------------------------------------------------------------------------

SEARCH_TERMS = [
    # Assisted dying
    "assisted dying UK",
    "assisted suicide UK",
    "Terminally Ill Adults Bill",
    "euthanasia UK",
    # Abortion
    "abortion UK",
    "abortion buffer zones",
    "abortion decriminalisation UK",
    "pro-life UK",
    # Islam
    "sharia UK",
    "Islamism UK",
    "grooming gangs",
    "blasphemy UK",
    # Religious liberty
    "Christian freedom of speech UK",
    "religious discrimination tribunal UK",
    "conversion therapy ban UK",
    "street preacher arrested",
    "Christian sacked belief",
    "prayer vigil arrest",
    # Marriage, family, gender
    "gender identity schools UK",
    "puberty blockers UK",
    "Cass Review",
    "surrogacy UK",
    # Church and public life
    "Church of England safeguarding",
    "faith schools UK",
]

# ---------------------------------------------------------------------------
# EDIT THIS: standing newspaper feeds (still useful for general sweep)
# ---------------------------------------------------------------------------

FEEDS = [
    ("BBC News (UK)", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
    ("BBC News (Politics)", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
    ("Sky News (UK)", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    ("The Guardian (UK)", "https://www.theguardian.com/uk/rss"),
    ("Christian Today", "https://www.christiantoday.com/rss-feeds"),
    ("Church Times", "https://www.churchtimes.co.uk/rss/news"),
]

# ---------------------------------------------------------------------------
# EDIT THIS: your issues (used by the AI, and by keyword fallback)
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
BATCH_SIZE = 20          # articles per AI call
MODEL = "gpt-5.5"        # or "gpt-5.5-mini" / "gpt-5.4-mini" to cut cost
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

NS_ATOM = {"a": "http://www.w3.org/2005/Atom"}
NS_DC = {"dc": "http://purl.org/dc/elements/1.1/"}


def google_news_url(term):
    """Build a Google News RSS search URL, limited to the last day."""
    q = urllib.parse.quote_plus(f"{term} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"


def parse_date(raw):
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
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-8000:], f)


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text):
    return re.sub("<[^<]+?>", "", text or "").strip()


def clean_google_title(title):
    """Google News appends ' - Outlet'. Split it into title and outlet."""
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if head and len(tail) < 60:
            return head.strip(), tail.strip()
    return title, None


def parse_feed(xml_bytes, source_name, is_google=False):
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return items

    for node in root.findall(".//item"):
        title = strip_html(node.findtext("title"))
        link = (node.findtext("link") or "").strip()
        summary = strip_html(node.findtext("description"))
        raw_date = node.findtext("pubDate") or node.findtext("date")
        if not raw_date:
            dc = node.find("dc:date", NS_DC)
            if dc is not None:
                raw_date = dc.text

        outlet = source_name
        if is_google:
            title, found_outlet = clean_google_title(title)
            if found_outlet:
                outlet = found_outlet
            # Google's description is mostly markup noise
            summary = summary[:300]

        if title and link:
            items.append({
                "title": title, "link": link, "summary": summary,
                "source": outlet, "published": parse_date(raw_date),
            })

    for node in root.findall(".//a:entry", NS_ATOM):
        title = strip_html(node.findtext("a:title", namespaces=NS_ATOM))
        link_el = node.find("a:link", NS_ATOM)
        link = link_el.get("href") if link_el is not None else ""
        summary = strip_html(
            node.findtext("a:summary", namespaces=NS_ATOM)
            or node.findtext("a:content", namespaces=NS_ATOM))
        raw_date = (node.findtext("a:published", namespaces=NS_ATOM)
                    or node.findtext("a:updated", namespaces=NS_ATOM))
        if title and link:
            items.append({
                "title": title, "link": link, "summary": summary,
                "source": source_name, "published": parse_date(raw_date),
            })

    return items


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def keyword_classify(item):
    text = f"{item['title']} {item['summary']}".lower()
    return [issue for issue, kws in ISSUES.items()
            if any(k.lower() in text for k in kws)]


def extract_json(text):
    """Pull a JSON array out of the AI's reply, tolerating markdown fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


def _openai_request(api_key, prompt, token_param, max_tokens):
    """Build and send one OpenAI request. Returns (text, error_message)."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    if token_param:
        payload[token_param] = max_tokens

    req = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return None, "no choices in response"
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            # Reasoning models can spend the whole budget on reasoning
            finish = choices[0].get("finish_reason", "")
            return None, f"empty content (finish_reason={finish})"
        return content, None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        return None, f"HTTP {e.code}: {detail or e.reason}"
    except Exception as e:
        return None, str(e)


def call_openai(api_key, prompt, max_tokens=8000, retries=3):
    """
    Call OpenAI, coping with the fact that newer reasoning models require
    'max_completion_tokens' while older ones use 'max_tokens'.
    Tries each in turn, then no token limit at all, before giving up.
    """
    param_options = ["max_completion_tokens", "max_tokens", None]

    for attempt in range(retries):
        for token_param in param_options:
            text, err = _openai_request(api_key, prompt, token_param, max_tokens)
            if text is not None:
                return text
            # If the parameter itself was rejected, try the next form quietly
            if err and ("max_tokens" in err or "max_completion_tokens" in err
                        or "Unsupported parameter" in err
                        or "unsupported_parameter" in err):
                continue
            # Any other error: stop trying parameter variants, back off instead
            break

        wait = 2 ** attempt
        print(f"    ! OpenAI call failed ({err}), retry in {wait}s")
        time.sleep(wait)

    return None


def ai_classify_batch(api_key, batch, issue_names):
    """Ask the AI to judge a batch of articles. Returns list of dicts or None."""
    listing = "\n".join(
        f"{i}. {a['title']}"
        + (f" [{a['source']}]" if a['source'] else "")
        + (f" — {a['summary'][:150]}" if a['summary'] else "")
        for i, a in enumerate(batch)
    )
    prompt = f"""You are a media monitor for Christian Concern, a UK Christian public policy organisation.

Our issues:
{chr(10).join('- ' + n for n in issue_names)}

Judge each headline below. Include an article if it is genuinely relevant to our work, even if it does not use obvious keywords — for example an employment tribunal about a Christian's beliefs is Religious Liberty; a story about a hospice funding crisis may be relevant to Assisted Dying.

Exclude routine news with no bearing on our issues.

Articles:
{listing}

Reply with ONLY a JSON array, no other text. One object per RELEVANT article:
[{{"i": 0, "issues": ["Religious Liberty"], "why": "one short sentence on why this matters to us", "urgent": false}}]

Set "urgent" true only if it needs a response today — a campaigner changing position, a vote or ruling imminent, a story going viral, or a Christian publicly attacked for their faith.
Use only issue names from the list above. If nothing is relevant, reply []."""

    reply = call_openai(api_key, prompt)
    if reply is None:
        return None
    parsed = extract_json(reply)
    if not isinstance(parsed, list):
        print("    ! Could not parse AI reply as JSON")
        return None
    return parsed


def classify_all(items, api_key, issue_names):
    """Returns (by_issue dict, urgent list). Falls back to keywords if needed."""
    by_issue = {n: [] for n in issue_names}
    urgent = []

    if not api_key:
        print("No OPENAI_API_KEY set - using keyword matching")
        for it in items:
            for iss in keyword_classify(it):
                by_issue[iss].append(it)
        return by_issue, urgent

    total_batches = (len(items) + BATCH_SIZE - 1) // BATCH_SIZE
    for b in range(total_batches):
        batch = items[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        print(f"  AI batch {b + 1}/{total_batches} ({len(batch)} articles)...")
        results = ai_classify_batch(api_key, batch, issue_names)

        if results is None:  # this batch failed - keyword fallback for it
            print("    falling back to keywords for this batch")
            for it in batch:
                for iss in keyword_classify(it):
                    by_issue[iss].append(it)
            continue

        for r in results:
            try:
                idx = int(r.get("i", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(batch):
                continue
            item = dict(batch[idx])
            item["why"] = str(r.get("why", "")).strip()
            matched = False
            for iss in r.get("issues", []):
                if iss in by_issue:
                    by_issue[iss].append(item)
                    matched = True
            if matched and r.get("urgent"):
                urgent.append(item)

    return by_issue, urgent


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_digest(by_issue, urgent, used_ai):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "AI-classified" if used_ai else "keyword-matched"
    lines = [f"# Digest - {stamp}",
             f"*Last {MAX_AGE_HOURS} hours, {mode}*", ""]

    if not any(by_issue.values()):
        lines.append("No new relevant articles this run.")
        return "\n".join(lines)

    if urgent:
        lines.append("## RESPOND NOW")
        for a in urgent:
            lines.append(f"- **[{a['title']}]({a['link']})** - *{a['source']}*")
            if a.get("why"):
                lines.append(f"  {a['why']}")
        lines.append("")

    for issue, articles in by_issue.items():
        if not articles:
            continue
        articles.sort(key=lambda a: a["published"], reverse=True)
        lines.append(f"## {issue} ({len(articles)})")
        for a in articles:
            when = a["published"].strftime("%d %b %H:%M")
            lines.append(f"- [{a['title']}]({a['link']}) - *{a['source']}*, {when} UTC")
            if a.get("why"):
                lines.append(f"  {a['why']}")
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


# ---------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()

    print(f"Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"AI classification: {'ON' if api_key else 'OFF (keyword fallback)'}")

    seen = load_seen()
    print(f"Previously seen: {len(seen)}\n")

    all_items = []

    print(f"Running {len(SEARCH_TERMS)} Google News searches...")
    for term in SEARCH_TERMS:
        try:
            items = parse_feed(fetch(google_news_url(term)),
                               "Google News", is_google=True)
            fresh = [i for i in items if is_recent(i["published"], cutoff)]
            print(f"  '{term}': {len(fresh)} recent")
            all_items.extend(fresh)
        except Exception as e:
            print(f"  ! '{term}' failed: {e}")

    print(f"\nReading {len(FEEDS)} standing feeds...")
    for name, url in FEEDS:
        try:
            items = parse_feed(fetch(url), name)
            fresh = [i for i in items if is_recent(i["published"], cutoff)]
            print(f"  {name}: {len(fresh)} recent")
            all_items.extend(fresh)
        except Exception as e:
            print(f"  ! {name} failed: {e}")

    # Deduplicate within this run, then against previous runs
    by_link = {}
    for it in all_items:
        key = hashlib.sha256(it["link"].encode()).hexdigest()
        if key not in by_link:
            by_link[key] = it

    new_items = []
    for key, it in by_link.items():
        if key not in seen:
            new_items.append(it)
            seen.add(key)

    print(f"\n{len(all_items)} fetched -> {len(by_link)} unique -> "
          f"{len(new_items)} new\n")

    if new_items:
        by_issue, urgent = classify_all(new_items, api_key, list(ISSUES))
    else:
        by_issue, urgent = {n: [] for n in ISSUES}, []

    digest = build_digest(by_issue, urgent, bool(api_key))
    print("\n" + digest)

    if any(by_issue.values()):
        append_log(digest)
        post_slack(digest)

    save_seen(seen)

    if api_key and new_items:
        batches = (len(new_items) + BATCH_SIZE - 1) // BATCH_SIZE
        # GPT-5.5 is ~$5/M input, ~$30/M output; reasoning tokens bill as output.
        print(f"\n(~{batches} AI calls this run, very roughly "
              f"{batches * 0.07:.2f} USD at gpt-5.5 rates)")
    print("Done.")


if __name__ == "__main__":
    main()
