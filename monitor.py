#!/usr/bin/env python3
"""
Christian Concern News Monitor
==============================

Runs twice a day (08:55 and 14:00 UK) and can also be run manually at any
time from the GitHub Actions "Run workflow" button.

Structure follows Christian Concern's own 12 issues, grouped into the four
families used on the website (Life, Family, Freedom, Foundations), plus
three extra sections: Politics & government, Christian Concern in the news,
and Worth reading.

Everything you would normally want to change is in the CONFIG block below.
"""

import os
import re
import json
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# ===========================================================================
# CONFIG - edit anything in this block
# ===========================================================================

MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "24"))

BATCH_SIZE = 60          # articles per AI call - raise to cut calls
PARALLEL_BATCHES = 6     # classification batches run at once (0 = sequential)
PARALLEL_FETCHES = 12    # feeds/searches downloaded at once
FETCH_TIMEOUT = 10       # seconds before giving up on a feed
ENABLE_CLUSTERING = False  # the review pass merges duplicates now
MODEL = "gpt-5.5"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Safety cap. With ~95 searches a busy day can return a lot of articles, and
# every one costs money to classify. If we exceed this, we keep the most
# recent and report how many were dropped, so cost can never run away.
MAX_ARTICLES_PER_RUN = int(os.environ.get("MAX_ARTICLES_PER_RUN", "500"))

# Most international coverage is noise for a UK organisation. This is the
# maximum number of foreign stories allowed into a single digest, however
# many the classifier lets through.
MAX_INTERNATIONAL = int(os.environ.get("MAX_INTERNATIONAL", "6"))

# Effort levels. The first pass is only a coarse sorter now - the review
# pass is the quality gate - so it does not need to think hard, and it is
# the one that runs many times.
CLASSIFY_EFFORT = "low"       # many calls, rough bucketing
REVIEW_EFFORT = "medium"      # the quality gate - drop, merge, re-file
RECOMMEND_EFFORT = "medium"   # picks the top five

# How long to wait on a single API call. This was 900s, which meant one
# hung request burned fifteen minutes before the retry even started. A
# request that has not answered in three minutes is not going to.
API_TIMEOUT = 180

# Reasoning tokens count towards this. Too high lets a call think for many
# minutes; too low and it thinks itself out of an answer.
MAX_OUTPUT_TOKENS = 16000

# Stories per review call. One call covering everything was timing out, so
# the review is chunked and the results stitched back together.
REVIEW_CHUNK = 40

# Hard ceiling on the whole review phase. If the API is having a bad day,
# we stop reviewing and show what we have rather than burning the morning.
REVIEW_BUDGET = 360

# --- The 12 issues, grouped as on christianconcern.com/issues -------------
# The description after each name is given to the AI to guide sorting.

ISSUE_FAMILIES = {
    "Life": {
        "Beginning of life":
            "Abortion law and practice, buffer zones, abortion pills, "
            "decriminalisation, embryo research and experimentation, IVF, "
            "prenatal and disability screening, the morning-after pill, "
            "foetal viability, pro-life campaigning and prosecutions.",
        "End of life":
            "Assisted suicide and euthanasia in the UK and abroad, the "
            "Terminally Ill Adults Bill, palliative and hospice care, "
            "withdrawal of treatment, DNR orders, care of the dying and "
            "the elderly, safeguarding of the vulnerable and disabled.",
    },
    "Family": {
        "Marriage":
            "Marriage law and reform, wedding law and ceremonies, divorce "
            "including no-fault divorce, cohabitation, family breakdown, "
            "family policy, forced marriage, and policies that weaken or "
            "redefine marriage.",
        "Gender":
            "Transgender ideology, the reality of biological sex, the "
            "Supreme Court judgment on the meaning of sex and its "
            "implementation, single-sex spaces, prisons, sport, puberty "
            "blockers and youth gender medicine, the Cass Review, self-ID, "
            "gender in the workplace and public bodies.",
        "Sexuality":
            "Sexual ethics and the sexual revolution, conversion therapy "
            "or conversion practices bans, pornography, sexual content in "
            "schools, LGBT activism and Pride, and the consequences of "
            "sexual autonomy.",
        "Education":
            "Schools and curriculum, relationships and sex education, "
            "religious education, parental rights and consent, faith "
            "schools, independent and private schools, home education, "
            "universities and academic freedom, and Christian education.",
    },
    "Freedom": {
        "Christian freedoms":
            "Christians penalised, disciplined, sacked, arrested or "
            "prosecuted for their beliefs; employment tribunals on religion "
            "or belief; street preachers; chaplains; foster carers and "
            "adoption; medical and professional conscience; regulators and "
            "professional bodies acting against Christians.",
        "Freedom of speech":
            "Censorship and deplatforming, non-crime hate incidents, "
            "policing of speech, hate speech and public order law, protest "
            "and prayer bans, broadcasting regulation, online safety rules, "
            "and academic or workplace speech restrictions.",
    },
    "Foundations": {
        "Christian society":
            "Britain's Christian heritage and its erosion, "
            "dechristianisation, the monarchy and establishment, national "
            "identity and cohesion, church-state relations, and the public "
            "benefit of Christian foundations.",
        "Secularism":
            "The squeezing out of religion from public life, equality law "
            "used against belief, charity regulation and the Charity "
            "Commission, the EHRC, humanist and secularist campaigning, "
            "and official guidance that marginalises faith.",
        "Islam":
            "Islamisation of Britain, sharia, grooming gangs, blasphemy "
            "codes and suppression of criticism, definitions of "
            "Islamophobia or anti-Muslim hostility, mosques and planning, "
            "Islamist extremism and terrorism, and Christian-Muslim "
            "relations.",
        "Church":
            "The Church of England and other denominations, doctrine and "
            "compromise, safeguarding, church leaders and Christian public "
            "figures, church closures and growth, persecution of the "
            "global church, and significant deaths or appointments.",
    },
}

# Flattened list of the 12 issue names, in display order.
ISSUE_NAMES = [name for fam in ISSUE_FAMILIES.values() for name in fam]

# --- Three extra sections, outside the 12 issues --------------------------

EXTRA_SECTIONS = {
    "Politics & government":
        "Government and Parliament as they touch our issues: ministerial "
        "and cabinet appointments and the appointees' records on our "
        "issues, peerages, advisers and strategists, bills and votes, "
        "party positions, select committees, elections, and statements by "
        "senior politicians. Include a political story when knowing it "
        "would change how we engage - for example a new health secretary's "
        "position on assisted suicide, or an education secretary's record "
        "on faith and private schools.",
    "Christian Concern in the news":
        "THE HIGHEST PRIORITY SECTION. Anything involving Christian Concern "
        "(the UK organisation), the Christian Legal Centre, our staff, or "
        "our clients and cases - our own published articles, our people "
        "quoted, interviewed, cited or criticised anywhere, and any coverage "
        "of the cases we run, INCLUDING coverage by foreign outlets. If an "
        "article names one of our clients, it belongs here even if a US or "
        "other overseas outlet published it. Also include significant output "
        "from allied organisations we work alongside. "
        "CRITICAL: we are Christian Concern, a UK organisation. We are NOT "
        "'International Christian Concern', a separate American persecution "
        "watchdog that publishes at persecution.org. Articles by or about "
        "International Christian Concern are NOT about us - do not put them "
        "in this section.",
    "Worth reading":
        "A LAST RESORT, and usually empty. Use it ONLY when a piece cannot "
        "be assigned to any of the twelve issues at all. "
        "If you can name ANY issue the piece is about, put it under that "
        "issue with \"comment\": true and list the others in \"also\". "
        "Touching several issues is NOT a reason to use this section - "
        "that is exactly what \"also\" is for. "
        "Worked examples of what does NOT belong here: an essay arguing "
        "Christianity can fill the void left by multiculturalism goes "
        "under Christian society with also=[Islam]; a piece on the "
        "dissolution of private education goes under Education; a peer "
        "explaining why the assisted dying bill failed goes under End of "
        "life. All three are commentary, and all three belong under their "
        "issue where the team will actually look for them. "
        "Only include commentary where the writer, subject or outlet "
        "carries real weight - a known commentator, a public figure, a "
        "peer or MP, or a serious outlet. Not blog posts by unknown "
        "authors or churnalism.",
}

ALL_SECTIONS = ISSUE_NAMES + list(EXTRA_SECTIONS)

# --- Who and what to watch for the "Christian Concern in the news" section -

OUR_PEOPLE = [
    "Andrea Williams", "Andrea Minichiello Williams", "Tim Dieppe",
    "Carys Moseley", "Paul Huxley", "Holly Baines", "Emily Bourne",
    "Darius Sandhu", "Rebecca Hunt", "Roger Kiska",
]

# Christian Legal Centre clients and cases. THIS IS A PRIORITY LIST - each
# name gets its own search, and any article mentioning one goes straight to
# the top section regardless of which outlet ran it or which country it is
# in. Add new clients here as cases begin.
OUR_CASES = [
    "Bernard Randall", "Kristie Higgs", "Jennifer Melle",
    "Darlington Nurses", "Felix Ngole", "Aaron Edwards",
    "Matthew Grech", "Victoria Culf", "Luke Salmons",
    "Bread of Life Community Church", "Steve Maile",
    "Bethany Hutchison", "Trent College chaplain",
]

ALLIED_ORGS = [
    "Christian Institute", "CARE", "Evangelical Alliance",
    "Free Speech Union", "Care Not Killing", "SPUC", "Right To Life UK",
    "Christian Legal Centre",
]

# --- Search terms. These drive COVERAGE. Add freely. ----------------------

SEARCH_TERMS = [
    # Beginning of life
    "abortion law UK", "abortion buffer zones", "abortion decriminalisation",
    "pro-life UK", "abortion clinic protest UK", "abortion pills post UK",
    "embryo research UK", "IVF regulation UK", "morning-after pill UK",
    # Personal testimony often carries further than policy coverage - the
    # Tanni Grey-Thompson interview is exactly this kind of story.
    "abortion disability UK", "Down syndrome abortion UK",
    "abortion regret testimony", "pregnancy pressure abortion",
    # End of life
    "assisted dying bill UK", "assisted suicide UK",
    "Terminally Ill Adults Bill", "euthanasia UK law",
    "palliative care policy UK", "withdrawal of treatment court UK",
    # Marriage
    "marriage law reform UK", "wedding law reform UK", "no-fault divorce UK",
    "family breakdown UK", "forced marriage UK", "cohabitation law UK",
    # Gender
    "gender identity schools UK", "puberty blockers UK", "Cass Review",
    "single sex spaces ruling UK", "transgender prisoners UK",
    "Supreme Court sex judgment", "trans guidance NHS UK",
    "gender critical tribunal UK", "transgender sport UK",
    # Sexuality
    "conversion therapy ban UK", "conversion practices bill UK",
    "sex education schools UK", "pornography regulation UK", "Pride UK",
    # Education
    "faith schools UK", "private schools policy UK",
    "religious education curriculum UK", "parental rights schools UK",
    "home education UK", "universities free speech UK",
    "RSE curriculum UK", "Christian education UK", "school curriculum reform UK",
    # Christian freedoms
    "Christian sacked belief UK", "employment tribunal religion belief UK",
    "street preacher arrested UK", "chaplain sacked UK",
    "foster carers religion UK", "nurse religious belief UK",
    "doctor conscience UK", "Christian discrimination tribunal",
    # Freedom of speech
    "free speech arrest UK", "non-crime hate incident",
    "hate speech law UK", "police speech investigation UK",
    "censorship UK broadcasting", "Online Safety Act speech",
    "protest ban UK", "deplatforming UK",
    # Christian society
    "Christian heritage Britain", "church attendance UK",
    "Christianity decline Britain", "King Charles faith",
    "establishment Church England", "blasphemy Britain",
    # Integration, cohesion and national identity - the Trevor Phillips
    # piece had no term that could have found it.
    "integration cohesion Britain", "British values debate",
    "multiculturalism Britain", "national identity Britain",
    "two-tier policing", "free speech Britain culture",
    # Secularism
    "Charity Commission investigation", "EHRC code of practice",
    "equality law religion UK", "secularism UK public life",
    "humanists UK campaign",
    # Islam
    "sharia UK", "Islamism UK", "grooming gangs",
    "Islamophobia definition UK", "mosque planning UK",
    "Islamist extremism UK", "terrorism UK arrest",
    # Islam in institutions - the Ramadan police story fell through the gap
    # between "extremism" and "planning", which is where most of this sits.
    "Ramadan workplace UK", "Islam police UK", "halal UK institutions",
    "Muslim staff network UK", "prayer rooms UK workplace",
    "Islam schools UK", "Muslim Council of Britain",
    # Church
    "Church of England", "Archbishop of Canterbury",
    "church safeguarding UK", "Christian persecution global",
    "bishops House of Lords", "denomination doctrine UK",
    # Politics & government
    "cabinet minister assisted dying", "MPs abortion vote",
    "government faith policy UK", "new cabinet appointment UK",
    "minister record social issues UK",
]

# Standing RSS feeds (in addition to the searches above).
FEEDS = [
    ("BBC News (UK)", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
    ("BBC News (Politics)", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
    ("Sky News (UK)", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    ("The Guardian (UK)", "https://www.theguardian.com/uk/rss"),
    ("Christian Today", "https://www.christiantoday.com/rss-feeds"),
    ("Church Times", "https://www.churchtimes.co.uk/rss/news"),
    ("Premier Christian News", "https://premierchristian.news/rss"),
    # Several feeds for our own site. WordPress often keeps campaign and
    # action pages out of the main feed, which is why "Six Weeks to Save
    # Their Lives" and the cohabitation consultation were missed. Failures
    # are ignored, so listing extras costs nothing.
    ("Christian Concern", "https://christianconcern.com/feed/"),
    ("Christian Concern (news)", "https://christianconcern.com/news/feed/"),
    ("Christian Concern (comment)", "https://christianconcern.com/comment/feed/"),
    ("Christian Concern (action)", "https://christianconcern.com/action/feed/"),
    ("Christian Legal Centre",
     "https://christianconcern.com/ccpressreleases/feed/"),
]

# ===========================================================================
# END OF CONFIG
# ===========================================================================

STATE_FILE = "seen_articles.json"
LOG_FILE = "digest_log.md"
MANUAL_FILE = "manual_digest.md"
MAX_LOG_ENTRIES = 150
USER_AGENT = "Mozilla/5.0 (compatible; NewsMonitor/1.0)"

NS_ATOM = {"a": "http://www.w3.org/2005/Atom"}
NS_DC = {"dc": "http://purl.org/dc/elements/1.1/"}


def _or_query(names, per_query=4):
    """
    Group names into OR queries so we make a handful of searches instead of
    one per name. Google News supports OR between quoted phrases.
    """
    out = []
    for i in range(0, len(names), per_query):
        chunk = names[i:i + per_query]
        out.append(" OR ".join(f'"{n}"' for n in chunk))
    return out


# Staff who appear in the media often enough to deserve their own search.
# Bundling them into an OR query dilutes the results and was why Tim being
# quoted on a smaller site was missed.
KEY_PEOPLE = ["Tim Dieppe", "Andrea Williams", "Paul Huxley",
              "Carys Moseley", "Roger Kiska"]


def our_search_terms():
    """Extra searches for the 'Christian Concern in the news' section."""
    terms = ['"Christian Concern"', '"Christian Legal Centre"']
    terms += [f'"{p}"' for p in KEY_PEOPLE]
    terms += _or_query([p for p in OUR_PEOPLE if p not in KEY_PEOPLE])
    terms += _or_query(OUR_CASES)
    terms += _or_query(ALLIED_ORGS)
    return terms


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def google_news_url(term):
    """Google News RSS search. We ask for 2 days and trim locally."""
    q = urllib.parse.quote_plus(f"{term} when:2d")
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
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
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
    return published is not None and published >= cutoff


def fetch(url, timeout=FETCH_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text):
    """
    Strip tags AND collapse all whitespace to single spaces.

    Collapsing matters more than it looks: article titles and summaries are
    fed to the AI as a numbered list, one article per line. A summary
    containing a newline splits one article across several lines, so the
    AI's "item 3" stops matching our item 3 and every classification after
    it lands on the wrong article.
    """
    text = re.sub("<[^<]+?>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def clean_google_title(title):
    """Google News appends ' - Outlet'."""
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
            title, found = clean_google_title(title)
            if found:
                outlet = found
            summary = summary[:300]
        if title and link:
            items.append({"title": title, "link": link, "summary": summary,
                          "source": outlet, "published": parse_date(raw_date)})

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
            items.append({"title": title, "link": link, "summary": summary,
                          "source": source_name,
                          "published": parse_date(raw_date)})
    return items


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def normalise_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query)
        keep = [(k, v) for k, v in query
                if not k.lower().startswith(("utm_", "fbclid", "gclid", "ito",
                                             "ref", "share", "cmp", "at_"))]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc.lower().replace("www.", ""),
             parts.path.rstrip("/"), urllib.parse.urlencode(keep), "")) or url
    except Exception:
        return url


STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "as", "at", "by", "from", "is", "are", "was", "were", "be",
    "been", "it", "its", "this", "that", "these", "those", "over", "after",
    "says", "say", "said", "new", "uk", "britain", "british", "amid", "could",
}


def title_tokens(title):
    text = re.sub(r"[^a-z0-9\s]", " ", title.lower())
    return {w for w in text.split() if w not in STOPWORDS and len(w) > 2}


def titles_match(a, b, threshold=0.62):
    if not a or not b:
        return False
    overlap = len(a & b)
    union = len(a | b)
    if union == 0:
        return False
    smaller = min(len(a), len(b))
    containment = overlap / smaller if smaller else 0
    return (overlap / union) >= threshold or containment >= 0.85


def deduplicate(items):
    by_url = {}
    for it in items:
        key = normalise_url(it["link"])
        if key not in by_url:
            by_url[key] = it
    kept = []
    for it in by_url.values():
        tokens = title_tokens(it["title"])
        match = None
        for existing in kept:
            if titles_match(tokens, existing["_tokens"]):
                match = existing
                break
        if match:
            others = match.setdefault("also_in", [])
            if it["source"] != match["source"] and it["source"] not in others:
                others.append(it["source"])
            if it["published"] and match["published"] and \
               it["published"] < match["published"]:
                match["published"] = it["published"]
        else:
            it["_tokens"] = tokens
            it.setdefault("also_in", [])
            kept.append(it)
    for it in kept:
        it.pop("_tokens", None)
    return kept


# ---------------------------------------------------------------------------
# Story clustering (AI) - collapses the same story across many outlets
# ---------------------------------------------------------------------------

# Outlets we would rather quote as the "main" source for a story.
PREFERRED_SOURCES = [
    "BBC", "The Times", "The Telegraph", "The Guardian", "Sky News",
    "The Independent", "Daily Mail", "Christian Today",
    "Premier Christian News", "Church Times", "Financial Times",
    "The Spectator", "GB News", "Daily Express", "The i Paper",
]


def source_rank(name):
    low = (name or "").lower()
    for i, pref in enumerate(PREFERRED_SOURCES):
        if pref.lower() in low:
            return i
    return len(PREFERRED_SOURCES)




def ai_cluster_chunk(api_key, chunk):
    """Returns list of groups, or None if the call failed."""
    listing = "\n".join(
        f"{i}. {one_line(a['title'])}  [{one_line(a['source'])}]"
        for i, a in enumerate(chunk))
    prompt = f"""Below are news headlines collected today. The same event is
usually reported by many outlets, each writing its own headline. Your job is
to find those duplicates.

Group headlines that report the SAME underlying event.

They ARE the same story if they describe the same event, decision, ruling,
announcement, incident, report or set of figures - even when the wording,
emphasis, framing, or the exact numbers quoted differ completely.

Worked example. All six of these are ONE story and belong in one group:
  "The morning-after pill is now free on the NHS"
  "Pharmacies supply more than 300,000 doses of the morning-after pill"
  "Hundreds of thousands of women get morning-after pill via free service"
  "More than 300,000 women get free morning-after pill under new scheme"
  "NHS Pharmacies Dispense 305,000 Morning-After Pills in Five Months"
  "Morning-After Pill: 305,000 Free NHS Doses Given"
Note that they share almost no wording, and quote 300,000 and 305,000. They
are still the same story.

They are NOT the same story if they describe different events, different
stages of a process (a bill passing one house versus being defeated in
another), different countries, or different people - even when the subject
matter is closely related.

Be thorough. A big story may be carried by fifteen outlets. Look for every
duplicate, not just the obvious ones.

Headlines:
{listing}

Reply with ONLY a JSON array of groups, each group an array of index numbers
reporting the same story. Include ONLY groups with two or more members.
If genuinely nothing is duplicated, reply [].

Example: [[0, 4, 9, 22], [2, 7]]"""

    reply = call_openai(api_key, prompt, effort="medium")
    if reply is None:
        return None
    parsed = extract_json(reply)
    if not isinstance(parsed, list):
        return None
    groups = []
    for g in parsed:
        if not isinstance(g, list):
            continue
        idxs = []
        for v in g:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(chunk) and n not in idxs:
                idxs.append(n)
        if len(idxs) > 1:
            groups.append(idxs)
    return groups


def collapse_group(members):
    """Pick one article to represent a group; record the other outlets."""
    members.sort(key=lambda a: (source_rank(a["source"]),
                                a["published"] or datetime.max.replace(
                                    tzinfo=timezone.utc)))
    lead = members[0]
    others = lead.setdefault("also_in", [])
    for m in members[1:]:
        if m["source"] != lead["source"] and m["source"] not in others:
            others.append(m["source"])
        if m["published"] and lead["published"] and \
           m["published"] < lead["published"]:
            lead["published"] = m["published"]
    return lead


def cluster_stories(api_key, items, chunk_size=800):
    """
    Collapse the same story reported by many outlets down to one entry.

    All headlines go in a SINGLE call wherever possible. An earlier version
    chunked them after sorting on the rarest words in each headline, which
    did not work: paraphrases of one story produce completely different sort
    keys, so the duplicates ended up in different chunks and were never
    compared. If we do have to chunk, we sort by publication time instead,
    since outlets pick up the same story within a few hours of each other.
    """
    stats = {"merged": 0, "calls": 0, "failed": 0}
    if not api_key or len(items) < 2:
        return items, stats

    if len(items) <= chunk_size:
        chunks = [list(items)]
    else:
        ordered = sorted(items, key=lambda a: a["published"] or datetime.min
                         .replace(tzinfo=timezone.utc))
        chunks = [ordered[i:i + chunk_size]
                  for i in range(0, len(ordered), chunk_size)]
        print(f"  {len(items)} headlines - splitting into {len(chunks)} calls")

    kept = []
    for n, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"  clustering chunk {n}/{len(chunks)} ({len(chunk)})...")
        else:
            print(f"  clustering {len(chunk)} headlines in one call...")
        stats["calls"] += 1
        groups = ai_cluster_chunk(api_key, chunk)
        if groups is None:
            stats["failed"] += 1
            print("    ! clustering FAILED for this batch - "
                  "duplicates will get through")
            kept += chunk
            continue
        grouped_idx = set()
        for g in groups:
            grouped_idx.update(g)
        for g in groups:
            members = [chunk[i] for i in g]
            kept.append(collapse_group(members))
            stats["merged"] += len(members) - 1
        kept += [a for i, a in enumerate(chunk) if i not in grouped_idx]

    return kept, stats


# ---------------------------------------------------------------------------
# Perspective brief
# ---------------------------------------------------------------------------

CC_PERSPECTIVE = """Christian Concern is a conservative evangelical advocacy
organisation founded by Andrea Minichiello Williams, working closely with the
Christian Legal Centre, which litigates on behalf of Christians. It campaigns
to see the UK return to its Christian foundations, and generally OPPOSES the
secular direction of law and culture.

Its settled positions:
- Life is sacred from conception to natural death. Pro-life on abortion;
  opposed to assisted suicide and euthanasia as a "culture of death", warning
  of pressure on the vulnerable, disabled and elderly, and arguing for better
  palliative care instead.
- Marriage is a lifelong union of one man and one woman, and is the foundation
  of family and society.
- Biological sex is real and immutable. Supports the Supreme Court judgment on
  the meaning of sex and scrutinises whether public bodies are complying.
  Opposes gender self-ID, youth gender medicine and breaches of single-sex
  spaces.
- Opposes conversion therapy bans on the grounds that they criminalise
  consensual conversation, prayer and ordinary pastoral care.
- Parents, not the state, have primary responsibility for children's
  education and moral formation.
- Defends Christians penalised for their beliefs, and free speech generally,
  including for those it disagrees with.
- Concerned about Islamisation, sharia, grooming gangs, blasphemy codes, and
  definitions of anti-Muslim hostility that suppress legitimate criticism.
- Critical of secularism, of equality law used against believers, and of
  regulators acting against Christians.
- Critical of the Church of England where it compromises on doctrine or fails
  on safeguarding, and wants a confident, public-facing Church.
- Concerned about dechristianisation and defends Britain's Christian heritage.
- Sceptical of the ECHR and Human Rights Act where they obstruct these aims.

IMPORTANT ON POLITICS: treat the government, Prime Minister, cabinet and
Parliament as a live and changing landscape. Do not assume any particular
party or person is in office. What matters is what the current holders of
office do and have said about our issues."""


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _openai_request(api_key, prompt, token_param, max_tokens, effort):
    payload = {"model": MODEL,
               "messages": [{"role": "user", "content": prompt}]}
    if token_param:
        payload[token_param] = max_tokens
    if effort:
        payload["reasoning_effort"] = effort
    req = urllib.request.Request(
        OPENAI_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return None, "no choices in response"
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            finish = choices[0].get("finish_reason", "")
            detail = data.get("usage", {}).get("completion_tokens_details", {})
            return None, (f"empty content (finish_reason={finish}, "
                          f"reasoning_tokens={detail.get('reasoning_tokens','?')})"
                          f" - raise MAX_OUTPUT_TOKENS or lower effort")
        return content, None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        return None, f"HTTP {e.code}: {detail or e.reason}"
    except Exception as e:
        return None, str(e)


def _param_unknown(err, names):
    if not err:
        return False
    low = err.lower()
    unknown = ("unrecognized request argument" in low
               or "unknown parameter" in low
               or "unsupported parameter" in low
               or "is not supported with this model" in low)
    return unknown and any(n.lower() in low for n in names)


def _value_rejected(err, names):
    if not err:
        return False
    low = err.lower()
    bad = ("unsupported value" in low or "invalid value" in low
           or "does not support" in low)
    return bad and any(n.lower() in low for n in names)


def call_openai(api_key, prompt, effort=None, max_tokens=None, retries=3):
    max_tokens = max_tokens or MAX_OUTPUT_TOKENS
    ladder = ["xhigh", "high", "medium", "low", "none"]
    if effort in ladder:
        effort_options = ladder[ladder.index(effort):] + [None]
    elif effort:
        effort_options = [effort, "high", "medium", None]
    else:
        effort_options = [None]
    token_options = ["max_completion_tokens", "max_tokens", None]
    effort_names = ["reasoning_effort", "effort"]
    token_names = ["max_tokens", "max_completion_tokens"]
    err = None

    for attempt in range(retries):
        idx = 0
        while idx < len(effort_options):
            eff = effort_options[idx]
            for token_param in token_options:
                text, err = _openai_request(api_key, prompt, token_param,
                                            max_tokens, eff)
                if text is not None:
                    return text
                if _param_unknown(err, token_names) or \
                   _value_rejected(err, token_names):
                    continue
                break
            if _param_unknown(err, effort_names):
                print("    (model does not accept reasoning_effort, omitting)")
                effort_options = [None]
                idx = 0
                continue
            if _value_rejected(err, effort_names):
                print(f"    (reasoning_effort '{eff}' rejected, stepping down)")
                idx += 1
                continue
            break
        wait = 2 ** attempt
        print(f"    ! OpenAI call failed ({err}), retry in {wait}s")
        time.sleep(wait)
    return None


def extract_json(text):
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

KEYWORD_FALLBACK = {
    "Beginning of life": ["abortion", "pro-life", "unborn", "embryo",
                          "buffer zone", "morning-after pill"],
    "End of life": ["assisted dying", "assisted suicide", "euthanasia",
                    "right to die", "palliative", "hospice"],
    "Marriage": ["marriage", "wedding", "divorce", "cohabit"],
    "Gender": ["transgender", "trans women", "trans men", "puberty blocker",
               "gender identity", "cass review", "single sex", "biological sex"],
    "Sexuality": ["conversion therapy", "conversion practices", "pride",
                  "pornography", "sex education"],
    "Education": ["school", "curriculum", "university", "pupils", "teacher",
                  "faith school", "home education"],
    "Christian freedoms": ["christian sacked", "street preacher", "chaplain",
                           "religious discrimination", "belief tribunal"],
    "Freedom of speech": ["free speech", "hate speech", "non-crime hate",
                          "censorship", "deplatform"],
    "Christian society": ["christian heritage", "dechristian", "church attendance"],
    "Secularism": ["secular", "charity commission", "ehrc", "humanist"],
    "Islam": ["islam", "muslim", "sharia", "mosque", "grooming gang",
              "islamist", "blasphemy"],
    "Church": ["church of england", "archbishop", "bishop", "diocese",
               "cathedral", "vicar", "pastor"],
    "Politics & government": ["minister", "cabinet", "mps", "parliament",
                              "prime minister", "peerage", "secretary of state"],
    "Christian Concern in the news": ["christian concern", "christian legal centre"],
}


# Outlets that are NOT us, despite the name. International Christian Concern
# is a separate American persecution watchdog; its articles kept landing in
# our own section.
NOT_US_SOURCES = ["international christian concern", "persecution.org",
                  "persecution.com"]

CC_SECTION = "Christian Concern in the news"
WORTH_READING = "Worth reading"


def is_actually_us(item):
    """Guard against International Christian Concern being mistaken for us."""
    src = (item.get("source") or "").lower()
    if any(bad in src for bad in NOT_US_SOURCES):
        return False
    text = f"{item.get('title','')} {item.get('summary','')}".lower()
    # "International Christian Concern said..." is about them, not us
    if "international christian concern" in text and "christian legal" not in text:
        return False
    return True


def keyword_classify(item):
    text = f"{item['title']} {item['summary']}".lower()
    return [name for name, kws in KEYWORD_FALLBACK.items()
            if any(k in text for k in kws)]


def build_section_brief():
    lines = []
    for family, issues in ISSUE_FAMILIES.items():
        lines.append(f"\n{family.upper()}")
        for name, desc in issues.items():
            lines.append(f"- {name}: {desc}")
    lines.append("\nADDITIONAL SECTIONS")
    for name, desc in EXTRA_SECTIONS.items():
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def one_line(text, limit=None):
    """Force text onto a single line. Guards the numbered-list format."""
    out = re.sub(r"\s+", " ", str(text or "")).strip()
    return out[:limit] if limit else out


def ai_classify_batch(api_key, batch):
    listing = "\n".join(
        f"{i}. {one_line(a['title'])}"
        + (f" [{one_line(a['source'])}]" if a['source'] else "")
        + (f" — {one_line(a['summary'], 200)}" if a['summary'] else "")
        for i, a in enumerate(batch))

    our_names = ", ".join(OUR_PEOPLE + OUR_CASES)
    allies = ", ".join(ALLIED_ORGS)

    prompt = f"""You are a media monitor for Christian Concern, a UK Christian public policy organisation.

{CC_PERSPECTIVE}

Sort each headline into one or more of these sections:
{build_section_brief()}

Our people and cases (for the "Christian Concern in the news" section):
{our_names}
Allied organisations: {allies}

THREE TESTS - an article must pass all three.

TEST 1 - GEOGRAPHY. The default is UK ONLY.
Include UK stories: UK law, courts, Parliament, government, institutions,
public bodies, or events and people in the UK.

International stories are the EXCEPTION, not the rule. Include one ONLY if a
British Christian reading it would be genuinely shocked, moved, or alarmed
about where things are heading. It must clear a high bar:
  - a national-level law, ruling or policy change of real consequence
    (a country legalising euthanasia for minors, a supreme court overturning
    a major precedent, a national ban on Christian practice)
  - a shocking or heartbreaking human story that crystallises an issue
    (a teenager euthanised, a pastor's family murdered for their faith)
  - a development that will visibly shape the UK debate

WOULD PASS: "Dutch psychiatrists investigate euthanasia of 17-year-old";
"US Supreme Court strikes down conversion therapy ban"; "Islamist attack at
Berlin Pride"; "Nigerian pastor's family murdered by extremists".

WOULD FAIL - do not include these: a US school district dispute; a state
legislature's routine business; a foreign council or planning decision; a
foreign politician's cabinet reshuffle; a bill in another country's
parliament that does not affect us; a hospital, university or charity
initiative abroad; local news from any other country.

If you are unsure whether an international story clears the bar, EXCLUDE it.

TEST 2 - IS IT SUBSTANTIAL?
Include reporting of events: rulings, votes, bills, cases, arrests, official
figures, investigations, appointments, resignations, campaigns launched, and
significant statements by people who matter.
Commentary and interviews ARE allowed, under the issue they concern, with
"comment": true - but only where the writer, subject or outlet carries real
weight. A peer explaining why a bill failed, a well-known commentator on
national identity, or a public figure's personal testimony about abortion
all qualify. An unknown blogger does not.
EXCLUDE: conference and event announcements, service or facility launches,
fundraising appeals, awareness days, charity press releases, trade-magazine
features, sponsored content, listicles and letters pages. If the headline
could have run in any week of any year, exclude it - UNLESS it is a
substantial piece by or about someone whose view carries weight on one of
our issues.

TEST 3 - DOES IT MATTER TO US?
It must have real public-policy, legal or cultural significance for our work -
something that changes the picture, sets a precedent, moves a debate, reveals
where a person in power stands, or that our supporters would want to know.

EXCLUDE local and routine material even when the topic sounds relevant:
  - an individual school's admissions, buildings, transport, uniform, prizes,
    sports results, leavers' events, or Ofsted visit
  - a parish or diocese's building works, fetes, appointments or arts
    competitions
  - school heating, air conditioning, holidays, or wellbeing features
  - general party politics with no bearing on our issues (the economy,
    transport, housing, defence, foreign affairs)
  - human interest and lifestyle pieces

Then mark what survives:
  "high"   - directly on one of our issues, or a Christian penalised for their
             beliefs, or something we would very likely comment on.
  "medium" - genuinely significant and connected, worth knowing about.
Do not include anything weaker. If in doubt, leave it out.

Articles:
{listing}

Reply with ONLY a JSON array, no other text:
[{{"i": 0,
   "primary": "Gender",
   "also": ["Freedom of speech", "Secularism"],
   "relevance": "high",
   "scope": "uk",
   "comment": false}}]

Set "comment": true for opinion pieces, essays, interviews and personal
testimony - anything that is a viewpoint rather than a report of events.
These still go under the issue they concern; the flag simply marks them so
the team can tell at a glance what is comment and what is news.

"primary" is the ONE section the article most belongs in - its core subject,
the reason it matters to us. Every article gets exactly one.
"also" lists any other sections it genuinely touches, or [] if none. Do not
stretch: only list a section if the article really bears on it.
If an article involves Christian Concern, the Christian Legal Centre or one
of our clients, "primary" must be "Christian Concern in the news", with the
issues it concerns listed in "also".

"scope" must be "uk" or "international". Mark it "international" if the story
is principally about events outside the UK. One exception: an article about
Christian Concern, the Christian Legal Centre or one of our clients is ALWAYS
"uk", whichever outlet published it - a US paper covering one of our cases is
still our news.
Use section names EXACTLY as written above.
If nothing qualifies, reply []."""

    reply = call_openai(api_key, prompt, effort=CLASSIFY_EFFORT)
    if reply is None:
        return None
    parsed = extract_json(reply)
    return parsed if isinstance(parsed, list) else None


def classify_all(items, api_key):
    by_section = {name: [] for name in ALL_SECTIONS}
    urgent = []
    stats = {"ai_batches": 0, "fallback_batches": 0,
             "uk": 0, "international": 0}

    if not api_key:
        print("No OPENAI_API_KEY set - using keyword matching")
        for it in items:
            for sec in keyword_classify(it):
                if sec in by_section:
                    by_section[sec].append(it)
        return by_section, urgent, stats

    total = (len(items) + BATCH_SIZE - 1) // BATCH_SIZE
    batches = [items[b * BATCH_SIZE:(b + 1) * BATCH_SIZE] for b in range(total)]

    # Batches are independent, and each call spends nearly all its time
    # waiting on the network, so running several at once cuts the wall clock
    # roughly by the number of workers. Results are collected by batch index
    # so the outcome does not depend on which call finishes first.
    results_by_batch = {}
    if PARALLEL_BATCHES and PARALLEL_BATCHES > 1 and total > 1:
        from concurrent.futures import ThreadPoolExecutor
        workers = min(PARALLEL_BATCHES, total)
        print(f"  classifying {total} batches, {workers} at a time...")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(ai_classify_batch, api_key, b): i
                       for i, b in enumerate(batches)}
            done = 0
            for fut, i in futures.items():
                try:
                    results_by_batch[i] = fut.result()
                except Exception as e:
                    print(f"    ! batch {i + 1} raised {e}")
                    results_by_batch[i] = None
                done += 1
                if done % 5 == 0 or done == total:
                    print(f"    {done}/{total} batches done")
    else:
        for i, b in enumerate(batches):
            print(f"  AI batch {i + 1}/{total} ({len(b)} articles)...")
            results_by_batch[i] = ai_classify_batch(api_key, b)

    for b in range(total):
        batch = batches[b]
        results = results_by_batch.get(b)
        if results is None:
            stats["fallback_batches"] += 1
            print(f"    ! batch {b + 1} FAILED - falling back to keywords "
                  f"(no relevance or scope filtering will apply)")
            for it in batch:
                for sec in keyword_classify(it):
                    if sec in by_section:
                        by_section[sec].append(it)
            continue
        stats["ai_batches"] += 1
        for r in results:
            try:
                idx = int(r.get("i", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= idx < len(batch):
                continue
            rel = str(r.get("relevance", "medium")).strip().lower()
            if rel == "low":
                continue
            if rel not in ("high", "medium"):
                rel = "medium"
            scope = str(r.get("scope", "uk")).strip().lower()
            if scope not in ("uk", "international"):
                scope = "uk"
            # An international story has to be genuinely important to earn a
            # place, so anything less than "high" is dropped outright.
            if scope == "international" and rel != "high":
                continue
            item = dict(batch[idx])
            item["relevance"] = rel
            item["scope"] = scope

            # One primary section per article, plus a note of what else it
            # touches. Previously an article on several issues was printed
            # once per issue, which made the digest repetitive.
            primary = str(r.get("primary", "")).strip()
            also = [x for x in (r.get("also") or [])
                    if x in by_section and x != primary]

            # Tolerate the older "sections" format, first entry as primary.
            if primary not in by_section:
                legacy = [x for x in (r.get("sections") or [])
                          if x in by_section]
                if not legacy:
                    continue
                primary, also = legacy[0], legacy[1:]

            # Anything involving us belongs in our section, whichever issue
            # it also concerns.
            if CC_SECTION in [primary] + also and is_actually_us(item):
                also = [x for x in [primary] + also if x != CC_SECTION]
                primary = CC_SECTION
            elif primary == CC_SECTION and not is_actually_us(item):
                if not also:
                    continue      # only reason to keep it was a false match
                primary, also = also[0], also[1:]

            # Worth reading is a last resort. If the AI put something there
            # but also named a real issue, move it to that issue - a comment
            # piece on assisted dying belongs under End of life, where the
            # team will look for it, not in a separate commentary bucket.
            if primary == WORTH_READING and also:
                primary, also = also[0], also[1:]
                item_moved = True
            else:
                item_moved = False

            item["is_comment"] = bool(r.get("comment")) or item_moved
            item["also_sections"] = also
            by_section[primary].append(item)
            stats[scope] += 1

    by_section = cap_international(by_section)
    return by_section, urgent, stats


def cap_international(by_section, limit=None):
    """
    Keep only the most significant international stories. Even with a strict
    prompt, foreign coverage is voluminous and can crowd out UK news, so this
    is a structural backstop rather than a matter of trusting the wording.
    """
    limit = limit if limit is not None else MAX_INTERNATIONAL
    # Our own coverage is exempt: a US outlet reporting one of our cases is
    # still our news, and must never be squeezed out by the foreign cap.
    ours = {a["link"] for a in by_section.get(CC_SECTION, [])}
    intl = {}
    for section, articles in by_section.items():
        if section == CC_SECTION:
            continue
        for a in articles:
            if a.get("scope") == "international" and a["link"] not in ours:
                intl[a["link"]] = a
    if len(intl) <= limit:
        return by_section

    # Rank by how widely carried, then how recent - a story a dozen outlets
    # ran is more likely to matter than one that appeared once.
    ranked = sorted(intl.values(),
                    key=lambda a: (-len(a.get("also_in", [])),
                                   -a["published"].timestamp()))
    keep = {a["link"] for a in ranked[:limit]}
    dropped = len(intl) - len(keep)
    for name, articles in by_section.items():
        if name == CC_SECTION:
            continue
        by_section[name] = [a for a in articles
                            if a.get("scope") != "international"
                            or a["link"] in keep or a["link"] in ours]
    print(f"  capped international stories: kept {len(keep)}, "
          f"dropped {dropped}")
    return by_section


# ---------------------------------------------------------------------------
# Second pass - review the assembled list as a whole
# ---------------------------------------------------------------------------

def _review_chunk(api_key, entries, offset, deadline=None):
    """
    Review one slice of the digest. Returns (drop, moves, merges, scopes)
    using LOCAL indices, or None if the call failed.

    The reply lists only EXCEPTIONS - what to drop, move or merge - rather
    than restating every surviving story. That keeps the output small, which
    is what makes this call reliable; asking for the full list back was
    producing replies so large the connection timed out.
    """
    listing = "\n".join(
        f"{i}. [{'/'.join(e['sections'])}] {one_line(e['article']['title'])}"
        f"  - {one_line(e['article']['source'])}"
        for i, e in enumerate(entries))

    prompt = f"""You are the final editor of Christian Concern's daily news digest.

{CC_PERSPECTIVE}

Available sections:
{', '.join(ALL_SECTIONS)}

Below is part of today's list. Clean it up. Be strict - the team reads this
quickly and a weak entry costs them more than a missing one.

DROP anything that does not belong:
- not about the UK, unless genuinely shocking or a major national-level
  development that will shape the UK debate (a teenager euthanised abroad,
  an Islamist attack in Europe, a landmark foreign ruling). Routine foreign
  politics, US school district disputes, other countries' parliamentary
  business and foreign local news are all out.
- sport, celebrity, entertainment, lifestyle, travel, consumer news
- individual schools' buildings, transport, uniforms, prizes, sports,
  leavers' events, advertising complaints, admissions
- parish and diocesan fetes, arts competitions, building projects, routine
  clergy appointments
- letters pages, diary items, light features
- trade-press announcements, service launches, conference notices,
  fundraising appeals, awareness days
- general politics with no bearing on our issues
- anything simply not significant enough to be worth knowing

MERGE duplicates. Several outlets report one event with different headlines
and different numbers. These are ONE story despite sharing almost no wording:
  "The morning-after pill is now free on the NHS"
  "Pharmacies supply more than 300,000 doses of the morning-after pill"
  "NHS Pharmacies Dispense 305,000 Morning-After Pills in Five Months"
Different stages of a process, different countries or different people are
NOT the same story.

MOVE anything filed under the wrong section. Each article appears under ONE
section only - the core issue it is really about. If you move something, name
just that one section.

MARK anything principally about events outside the UK as international.

List:
{listing}

Reply with ONLY a JSON object listing the EXCEPTIONS. Anything you do not
mention is kept as it is.

{{"drop": [1, 4, 7],
  "merge": [[0, 3, 9]],
  "move": [{{"i": 2, "sections": ["Gender"]}}],
  "international": [5, 8]}}

Use [] for any category with nothing in it. In each merge group the first
index is the version that will be shown; prefer a well-known UK outlet.
Use section names exactly as listed above."""

    # Two attempts rather than three: a chunk that has failed twice is
    # unlikely to succeed, and the stories simply pass through unreviewed.
    reply = call_openai(api_key, prompt, effort=REVIEW_EFFORT, retries=2)
    if reply is None:
        return None
    parsed = extract_json_object(reply)
    if not isinstance(parsed, dict):
        return None

    def ints(key):
        out = []
        for v in parsed.get(key, []) or []:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(entries):
                out.append(n + offset)
        return out

    drop = set(ints("drop"))
    intl = set(ints("international"))

    merges = []
    for g in parsed.get("merge", []) or []:
        if not isinstance(g, list):
            continue
        idxs = []
        for v in g:
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(entries) and (n + offset) not in idxs:
                idxs.append(n + offset)
        if len(idxs) > 1:
            merges.append(idxs)

    moves = {}
    for r in parsed.get("move", []) or []:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("i", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= n < len(entries):
            continue
        secs = [x for x in r.get("sections", []) if x in ALL_SECTIONS]
        if secs:
            moves[n + offset] = secs

    return drop, moves, merges, intl


def review_digest(api_key, by_section):
    """
    A second look at the finished list, in chunks.

    The first pass sees articles in batches, so it cannot know that six
    entries scattered across the digest are the same story. This pass sees
    the assembled list and drops, merges, re-files and re-marks it.
    """
    # Order by section so that different reports of one story - which
    # usually land in the same section - stay in the same chunk.
    entries, index_of = [], {}
    for section in ALL_SECTIONS:
        for a in by_section.get(section, []):
            key = a["link"]
            if key not in index_of:
                index_of[key] = len(entries)
                entries.append({"article": a, "sections": [section]})
            else:
                entries[index_of[key]]["sections"].append(section)

    stats = {"reviewed": len(entries), "dropped": 0, "merged": 0,
             "moved": 0, "failed_chunks": 0}
    if not api_key or not entries:
        return by_section, stats

    drop, moves, merges, intl = set(), {}, [], set()
    chunks = [(i, entries[i:i + REVIEW_CHUNK])
              for i in range(0, len(entries), REVIEW_CHUNK)]
    print(f"  reviewing in {len(chunks)} chunk(s) of up to {REVIEW_CHUNK}...")

    deadline = time.time() + REVIEW_BUDGET
    for offset, chunk in chunks:
        if time.time() > deadline:
            remaining = len(chunks) - (offset // REVIEW_CHUNK)
            print(f"    ! review budget of {REVIEW_BUDGET}s spent - "
                  f"{remaining} chunk(s) left unreviewed")
            stats["failed_chunks"] += remaining
            break
        result = _review_chunk(api_key, chunk, offset, deadline)
        if result is None:
            stats["failed_chunks"] += 1
            print(f"    ! chunk at {offset} failed - its stories pass through "
                  f"unreviewed")
            continue
        d, mv, mg, it = result
        drop |= d
        moves.update(mv)
        merges += mg
        intl |= it

    stats["dropped"] = len(drop)
    stats["moved"] = len(moves)

    # Apply merges among the survivors
    merged_away = set()
    for group in merges:
        members = [n for n in group if n not in drop and n not in merged_away]
        if len(members) < 2:
            continue
        members.sort(key=lambda n: source_rank(entries[n]["article"]["source"]))
        lead = entries[members[0]]["article"]
        others = lead.setdefault("also_in", [])
        for n in members[1:]:
            other = entries[n]["article"]
            for name in [other["source"]] + (other.get("also_in") or []):
                if name != lead["source"] and name not in others:
                    others.append(name)
            if other["published"] and lead["published"] and \
               other["published"] < lead["published"]:
                lead["published"] = other["published"]
            merged_away.add(n)
            stats["merged"] += 1

    rebuilt = {name: [] for name in by_section}
    for idx, entry in enumerate(entries):
        if idx in drop or idx in merged_away:
            continue
        article = entry["article"]
        if idx in intl:
            article["scope"] = "international"
        # One section per article. If the review re-filed it, the first
        # section it names becomes primary and the rest become "also
        # touches", so nothing is duplicated across the digest.
        target = moves.get(idx, entry["sections"])
        target = [t for t in target if t in rebuilt]
        if not target:
            continue
        primary, extra = target[0], target[1:]
        if extra:
            existing = article.get("also_sections") or []
            article["also_sections"] = list(dict.fromkeys(existing + extra))
        rebuilt[primary].append(article)

    return rebuilt, stats


def extract_json_object(text):
    """Like extract_json but for a JSON object rather than an array."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Top 5
# ---------------------------------------------------------------------------

def recommend_top5(api_key, by_section):
    pool, seen_links = [], set()
    for section, articles in by_section.items():
        if section == "Worth reading":
            continue  # commentary is for reading, not for us to comment on
        for a in articles:
            if a["link"] in seen_links:
                continue
            seen_links.add(a["link"])
            pool.append((section, a))

    if not pool or not api_key:
        return None

    listing = "\n".join(
        f"{i}. [{sec}] ({a.get('relevance','medium')}) {a['title']}"
        f"  — {a['source']}"
        + (f", also in {', '.join(a['also_in'][:4])}" if a.get("also_in") else "")
        + (f"\n   {a['summary'][:200]}" if a.get("summary") else "")
        for i, (sec, a) in enumerate(pool))

    prompt = f"""You are advising Christian Concern's communications team.

{CC_PERSPECTIVE}

Below are today's stories. Choose the FIVE best for Christian Concern to
comment on publicly, ranked 1-5 (1 = highest priority).

Weigh:
- REACH: how big is the story, how many outlets are carrying it?
- TIMELINESS: is there a narrow window to be part of the conversation?
- DISTINCTIVENESS: can we say something others are not saying?
- PICKUP: how likely are journalists, MPs or sympathetic accounts to quote,
  share or cite us on this?
- STRATEGIC FIT: does it advance a campaign we are running, or connect to a
  Christian Legal Centre case?
- SUPPORTERS: would our audience share it, act on it, or donate?

Spread the picks across different issues where the quality is comparable -
five stories on the same topic is less useful than a balanced set. Favour
"high" relevance items.

Stories:
{listing}

WRITING FOR THE AUDIENCE, NOT FOR US

Our comment goes out as video and written content to ordinary Christians -
parents, churchgoers, people at work, grandparents. They are not policy
specialists and they are deciding within seconds whether this is worth their
time. Write the "affects", "angle" and "hook" fields for THEM, not for a
communications team.

That means:
- Name who is actually affected and how. "Parents choosing a secondary
  school next term", "nurses asked to sign a policy they disagree with",
  "anyone praying near a clinic". Not "the public" or "society".
- Make the consequence concrete and near-term. What will be different, for
  whom, and roughly when.
- Give the viewer something they gain: something they did not know, a way to
  understand what is happening, or something they can actually do - write to
  an MP, ask a question at a school, know their rights at work.
- Cash out any abstraction immediately. "A culture of death" means nothing
  on its own; "doctors being able to raise assisted suicide with your
  grandmother before she has asked about it" does.
- Do NOT overstate. If the practical effect is limited or some way off, say
  so plainly. Audiences punish exaggeration, and being trusted is worth more
  than one good week of views.

Reply with ONLY a JSON array, ranked best first, no other text:
[{{"i": 0,
   "title": "copy the headline exactly as shown, so we can verify the match",
   "rank": 1,
   "what": "1-2 sentences on what actually happened, with specifics.",
   "affects": "1-2 sentences naming who this concretely affects and how -
               the everyday situation a viewer would recognise.",
   "angle": "2-3 sentences: the line we take, AND what the viewer gets from
             hearing it - what they will understand, or be able to do, that
             they could not before.",
   "hook": "one spoken opening line, under 20 words, that makes someone stop
            scrolling. A question or a concrete stake, never a slogan.",
   "headline": "a video title - specific, honest, no clickbait"}}]"""

    reply = call_openai(api_key, prompt, effort=RECOMMEND_EFFORT)
    if reply is None:
        return None
    parsed = extract_json(reply)
    if not isinstance(parsed, list):
        return None

    picks, used = [], set()
    for r in parsed:
        try:
            idx = int(r.get("i", -1))
        except (TypeError, ValueError):
            idx = None
        echoed = str(r.get("title", "")).strip()
        valid = idx is not None and 0 <= idx < len(pool)
        if valid and echoed:
            if not titles_match(title_tokens(echoed),
                                title_tokens(pool[idx][1]["title"]), 0.5):
                valid = False
        if not valid and echoed:
            et = title_tokens(echoed)
            for j, (_, cand) in enumerate(pool):
                if titles_match(et, title_tokens(cand["title"]), 0.5):
                    idx, valid = j, True
                    print(f"    (corrected a mismatched index to #{j})")
                    break
        if not valid or idx in used:
            continue
        used.add(idx)
        section, article = pool[idx]
        picks.append({"article": article, "section": section,
                      "rank": r.get("rank", len(picks) + 1),
                      "what": str(r.get("what", "")).strip(),
                      "affects": str(r.get("affects", "")).strip(),
                      "angle": str(r.get("angle", "")).strip(),
                      "hook": str(r.get("hook", "")).strip(),
                      "headline": str(r.get("headline", "")).strip()})
    picks.sort(key=lambda p: p["rank"] if isinstance(p["rank"], int) else 99)
    return picks[:5] or None


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def build_digest(by_section, urgent, used_ai, top5=None, label=""):
    """
    Layout, top to bottom:
      1. Christian Concern in the news - our own coverage and our clients
      2. The twelve issues, grouped into families (UK stories)
      3. Politics & government
      4. Worth reading
      5. International - everything foreign, gathered in one place
      6. Top 5 to comment on
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "AI-classified" if used_ai else "keyword-matched"
    hrs = int(MAX_AGE_HOURS) if MAX_AGE_HOURS == int(MAX_AGE_HOURS) \
        else round(MAX_AGE_HOURS, 1)
    title = f"# Digest{' - ' + label if label else ''} - {stamp}"
    lines = [title, f"*Last {hrs} hours, {mode}*", ""]

    total = sum(len(v) for v in by_section.values())
    if total == 0:
        lines.append("No new relevant articles this run.")
        return "\n".join(lines)

    RANK = {"high": 0, "medium": 1}
    today = datetime.now(timezone.utc).date()

    def render(a, note=""):
        pub = a["published"]
        when = pub.strftime("%H:%M") if pub.date() == today \
            else pub.strftime("%a %H:%M")
        src = a["source"]
        others = a.get("also_in") or []
        if others:
            named = ", ".join(others[:3])
            extra = f" +{len(others) - 3} more" if len(others) > 3 else ""
            src += f" (also {named}{extra})"
        bits = []
        if a.get("is_comment"):
            bits.append("comment")
        if note:
            bits.append(note)
        touches = a.get("also_sections") or []
        if touches:
            bits.append(f"also touches {', '.join(touches)}")
        tail = "  ·  " + "  ·  ".join(bits) if bits else ""
        return f"- [{a['title']}]({a['link']}) — *{src}*, {when}{tail}"

    def sort_articles(articles):
        # News before comment, then strongest first, then newest.
        articles.sort(key=lambda a: (1 if a.get("is_comment") else 0,
                                     RANK.get(a.get("relevance", "medium"), 1),
                                     -a["published"].timestamp()))
        return articles

    # Our own coverage is never treated as "foreign", even when a US outlet
    # reports one of our cases - it belongs at the top either way.
    ours = {a["link"] for a in by_section.get(CC_SECTION, [])}

    def is_intl(a):
        return a.get("scope") == "international" and a["link"] not in ours

    # --- 1. Christian Concern, first ---------------------------------------
    cc = by_section.get(CC_SECTION, [])
    if cc:
        lines.append(f"## ⭐ CHRISTIAN CONCERN IN THE NEWS ({len(cc)})")
        lines += [render(a) for a in sort_articles(list(cc))]
        lines.append("")

    # --- 2. The twelve issues, UK stories only -----------------------------
    empty = []
    for family, issues in ISSUE_FAMILIES.items():
        present = []
        for name in issues:
            uk_only = [a for a in by_section.get(name, []) if not is_intl(a)]
            if uk_only:
                present.append((name, uk_only))
            elif not by_section.get(name):
                # Only call it empty if there is genuinely nothing. A section
                # whose only stories were foreign has been moved to the
                # international block, not lost.
                empty.append(name)
        if not present:
            continue
        lines.append(f"## {family.upper()}")
        lines.append("")
        for name, articles in present:
            lines.append(f"### {name} ({len(articles)})")
            lines += [render(a) for a in sort_articles(articles)]
            lines.append("")

    # --- 3 & 4. Politics, then commentary ----------------------------------
    for name in ("Politics & government", "Worth reading"):
        arts = [a for a in by_section.get(name, []) if not is_intl(a)]
        if arts:
            lines.append(f"## {name.upper()} ({len(arts)})")
            lines += [render(a) for a in sort_articles(arts)]
            lines.append("")

    # --- 5. Everything international, gathered at the bottom ---------------
    intl, where = {}, {}
    for section, articles in by_section.items():
        if section == CC_SECTION:
            continue
        for a in articles:
            if is_intl(a) and a["link"] not in intl:
                intl[a["link"]] = a
                where[a["link"]] = section
    if intl:
        items = sort_articles(list(intl.values()))
        lines.append(f"## 🌍 INTERNATIONAL ({len(items)})")
        lines += [render(a, where[a["link"]]) for a in items]
        lines.append("")

    # --- 6. Top five -------------------------------------------------------
    if top5:
        lines.append("---")
        lines.append("")
        lines.append("# 🎯 Top 5 to comment on")
        lines.append("")
        for p in top5:
            a = p["article"]
            lines.append(f"### {p['rank']}. [{a['title']}]({a['link']})")
            lines.append(f"*{p['section']} · {a['source']}*")
            lines.append("")
            if p.get("what"):
                lines.append(p["what"])
                lines.append("")
            if p.get("affects"):
                lines.append(f"**Who this hits:** {p['affects']}")
                lines.append("")
            if p.get("angle"):
                lines.append(f"**Angle:** {p['angle']}")
                lines.append("")
            if p.get("hook"):
                lines.append(f"**Opening line:** \"{p['hook']}\"")
                lines.append("")
            if p.get("headline"):
                lines.append(f"**Title:** *{p['headline']}*")
                lines.append("")

    if empty:
        lines.append("---")
        lines.append(f"*Nothing significant today: {', '.join(empty)}*")

    return "\n".join(lines)


def append_log(digest, path=LOG_FILE):
    sep = "\n\n---\n\n"
    old = ""
    try:
        with open(path, encoding="utf-8") as f:
            old = f.read()
    except FileNotFoundError:
        pass
    combined = digest.strip() + (sep + old if old.strip() else "")
    parts = [p for p in combined.split(sep) if p.strip()][:MAX_LOG_ENTRIES]
    with open(path, "w", encoding="utf-8") as f:
        f.write(sep.join(parts) + "\n")


def markdown_to_html(md):
    """
    Turn the digest into readable HTML for email.

    Deliberately small and dependency-free - it only needs to handle the
    handful of constructs the digest actually uses: headings, links,
    bold, italics, bullets and horizontal rules.
    """
    def inline(text):
        text = (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                      r'<a href="\2" style="color:#1a5490;'
                      r'text-decoration:none">\1</a>', text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
        return text

    out, in_list = [], False
    for line in md.split("\n"):
        stripped = line.strip()

        if stripped.startswith("- "):
            if not in_list:
                out.append('<ul style="padding-left:18px;margin:6px 0">')
                in_list = True
            out.append(f'<li style="margin:6px 0;line-height:1.45">'
                       f'{inline(stripped[2:])}</li>')
            continue
        if in_list:
            out.append("</ul>")
            in_list = False

        if not stripped:
            continue
        if stripped == "---":
            out.append('<hr style="border:0;border-top:1px solid #ddd;'
                       'margin:22px 0">')
        elif stripped.startswith("### "):
            out.append(f'<h3 style="margin:18px 0 6px;font-size:15px;'
                       f'color:#222">{inline(stripped[4:])}</h3>')
        elif stripped.startswith("## "):
            out.append(f'<h2 style="margin:24px 0 8px;font-size:17px;'
                       f'color:#1a5490;border-bottom:2px solid #e8e8e8;'
                       f'padding-bottom:4px">{inline(stripped[3:])}</h2>')
        elif stripped.startswith("# "):
            out.append(f'<h1 style="margin:0 0 10px;font-size:20px;'
                       f'color:#111">{inline(stripped[2:])}</h1>')
        else:
            out.append(f'<p style="margin:8px 0;line-height:1.5">'
                       f'{inline(stripped)}</p>')
    if in_list:
        out.append("</ul>")

    body = "\n".join(out)
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f6f6f6">
<div style="max-width:720px;margin:0 auto;padding:24px;background:#ffffff;
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,
     Arial,sans-serif;font-size:14px;color:#333">
{body}
<p style="margin-top:28px;padding-top:12px;border-top:1px solid #eee;
   font-size:11px;color:#999">
Generated automatically by the Christian Concern news monitor.</p>
</div></body></html>"""


def send_email(digest, subject_suffix=""):
    """
    Email the digest. Silently does nothing unless SMTP settings are set,
    so the monitor still runs fine without email configured.
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    # Google shows app passwords as "abcd efgh ijkl mnop". Pasting that
    # verbatim fails authentication, so strip any spaces.
    password = os.environ.get("SMTP_PASS", "").replace(" ", "").strip()
    recipients_raw = os.environ.get("EMAIL_TO", "").strip()

    missing = [name for name, val in (("SMTP_HOST", host),
                                      ("SMTP_USER", user),
                                      ("SMTP_PASS", password),
                                      ("EMAIL_TO", recipients_raw))
               if not val]
    if missing:
        print(f"  ! email NOT sent - these secrets are missing or empty: "
              f"{', '.join(missing)}")
        return False

    # Accept commas, semicolons or newlines between addresses
    # Tolerate the ways people naturally paste address lists: surrounding
    # quotes, angle brackets, and stray whitespace.
    recipients = []
    for part in re.split(r"[,;\n]+", recipients_raw):
        addr = part.strip().strip("'\"").strip()
        m_ = re.search(r"<([^>]+)>", addr)      # "Name <a@b.com>"
        if m_:
            addr = m_.group(1).strip()
        if "@" in addr:
            recipients.append(addr)
    if not recipients:
        return False

    port = int(os.environ.get("SMTP_PORT", "465"))
    sender = os.environ.get("EMAIL_FROM", "").strip() or user

    # Gmail will not let you send as an arbitrary address - it rewrites the
    # From header to the account address unless the alias is configured under
    # "Send mail as". Warn rather than let it silently look wrong.
    if "gmail" in host.lower() and sender.lower() != user.lower():
        print(f"  note: sending via Gmail as {user}; the From address "
              f"{sender} will be ignored unless it is set up as an alias")

    stamp = uk_now().strftime("%a %d %b")
    subject = f"Christian Concern news digest - {stamp}{subject_suffix}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(digest, "plain", "utf-8"))
    msg.attach(MIMEText(markdown_to_html(digest), "html", "utf-8"))

    try:
        if port == 587:
            server = smtplib.SMTP(host, port, timeout=60)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=60)
        with server:
            server.login(user, password)
            server.sendmail(sender, recipients, msg.as_string())
        print(f"  emailed to {len(recipients)} recipient(s): "
              f"{', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"  ! email failed: {e}")
        return False


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
# State
# ---------------------------------------------------------------------------

def load_seen():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        return set(json.loads(content)) if content else set()
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen)[-8000:], f)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def uk_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/London"))
    except Exception:
        return datetime.now(timezone.utc)


RUNS_FILE = "last_runs.json"


def load_last_runs():
    try:
        with open(RUNS_FILE, encoding="utf-8") as f:
            data = json.loads(f.read().strip() or "{}")
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


def record_run(slot_name, day, emailed=True):
    """
    Record that a slot has run. 'emailed' records whether the digest actually
    reached anyone - a run that produced a digest nobody received is not
    really done, and should be retried by the next cron.
    """
    runs = load_last_runs()
    runs[slot_name] = {"day": day, "emailed": bool(emailed)}
    with open(RUNS_FILE, "w", encoding="utf-8") as f:
        json.dump(runs, f)


def slot_done_today(runs, slot_name, today):
    """
    True only if the slot ran today AND the digest was delivered. Older
    versions stored a bare date string, so handle that too.
    """
    entry = runs.get(slot_name)
    if entry is None:
        return False
    if isinstance(entry, str):          # old format: assume delivered
        return entry == today
    if not isinstance(entry, dict):
        return False
    if entry.get("day") != today:
        return False
    if entry.get("emailed"):
        return True
    # It ran but the email did not get out. Allow one retry, then stop so a
    # permanently broken mailbox cannot trigger a run on every cron.
    attempts = entry.get("attempts", 1)
    return attempts >= 2


# --- Schedule ---------------------------------------------------------------
#
# Windows are UK local time and deliberately wide. GitHub's scheduler is
# unreliable: in practice the first cron of each pair is often dropped and
# the second fires up to an hour late. A wide window means a late start
# still produces the digest; the once-per-day guard stops duplicates.
#
# Weekends are off. Monday morning covers the whole weekend in one digest.

WEEKEND_OFF = True          # no Saturday or Sunday digests
MONDAY_HOURS = 48.0         # Monday morning lookback (see SETUP notes)
WEEKDAY_MORNING_HOURS = 24.0
AFTERNOON_HOURS = 6.0

SLOTS = [
    {"name": "morning",   "from": 7 * 60 + 45,  "to": 11 * 60},
    {"name": "afternoon", "from": 13 * 60 + 30, "to": 16 * 60},
]


def lookback_for(slot_name, when):
    """
    How far back a given run should look.

    Monday morning is the weekend catch-up, so it reaches back further than
    a normal weekday morning.
    """
    if slot_name == "afternoon":
        return AFTERNOON_HOURS
    if when.weekday() == 0:          # Monday
        return MONDAY_HOURS
    return WEEKDAY_MORNING_HOURS


def should_run_now():
    """
    Return (lookback_hours, slot_name) if this run should proceed,
    otherwise (None, None). Each slot runs at most once per day.
    """
    now = uk_now()

    if WEEKEND_OFF and now.weekday() >= 5:
        day = "Saturday" if now.weekday() == 5 else "Sunday"
        print(f"  {day} - no digest at weekends. Monday morning will cover "
              f"the whole weekend.")
        return None, None

    minutes = now.hour * 60 + now.minute
    today = now.strftime("%Y-%m-%d")
    runs = load_last_runs()

    for slot in SLOTS:
        if not (slot["from"] <= minutes <= slot["to"]):
            continue
        if slot_done_today(runs, slot["name"], today):
            print(f"  the {slot['name']} digest already ran today "
                  f"({today}) - skipping this duplicate")
            return None, None
        prev = runs.get(slot["name"])
        if isinstance(prev, dict) and prev.get("day") == today \
                and not prev.get("emailed"):
            print(f"  the {slot['name']} digest ran today but the email "
                  f"failed - retrying")
        return lookback_for(slot["name"], now), slot["name"]
    return None, None


def is_manual_run():
    """
    Work out whether this is a manual run.

    Checked in order of reliability:
      1. GITHUB_EVENT_NAME - GitHub sets this automatically on every run, so
         this works even if the workflow file was never updated. Pressing
         "Run workflow" gives 'workflow_dispatch'.
      2. FORCE_RUN=1 - explicit override, useful for local testing or if the
         workflow sets it directly.

    Returns (is_manual, reason) so the reason can be printed for diagnosis.
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "").strip().lower()
    if event == "workflow_dispatch":
        return True, "GITHUB_EVENT_NAME=workflow_dispatch"
    if os.environ.get("FORCE_RUN", "").strip() == "1":
        return True, "FORCE_RUN=1"
    if event == "schedule":
        return False, "GITHUB_EVENT_NAME=schedule"
    if event:
        return False, f"GITHUB_EVENT_NAME={event}"
    return False, "no GitHub event set (running locally?)"


def main():
    global MAX_AGE_HOURS

    manual, why = is_manual_run()

    # TRIGGER_MODE changes what a hand-pressed or externally triggered run
    # counts as:
    #   auto      one-off. Emails, but records nothing, so the next
    #             scheduled run is unaffected. Good for testing.
    #   scheduled behaves exactly like a cron run - windows and weekends
    #             respected. This is what an external scheduler passes.
    #   catchup   run the morning digest NOW, whatever the time or day, and
    #             record it - so the next scheduled run will not repeat
    #             these stories.
    trigger_mode = os.environ.get("TRIGGER_MODE", "").strip().lower()
    catchup = manual and trigger_mode == "catchup"
    if manual and trigger_mode == "scheduled":
        manual = False
        why = "external trigger, treated as scheduled"
    elif catchup:
        manual = False
        why = "manual catch-up, will be recorded as today's digest"

    print(f"Run type: {'MANUAL' if manual else 'scheduled'}  ({why})")

    if manual:
        # A manual run is "show me what is happening now". It deliberately
        # IGNORES what has already been reported, and deliberately does NOT
        # record what it finds - otherwise the next scheduled digest would
        # skip those stories.
        MAX_AGE_HOURS = float(os.environ.get("MANUAL_HOURS", "24"))
        label = "manual run"
        print(f"MANUAL RUN - {MAX_AGE_HOURS}h window, ignoring previous runs")
    else:
        if catchup:
            # Explicitly asked for, so ignore the time window and the
            # weekend rule - but still record it, which is the point.
            slot_name = "morning"
            window = lookback_for("morning", uk_now())
            print(f"  catch-up: running the morning digest now "
                  f"({window:g}h) and recording it")
        else:
            window, slot_name = should_run_now()
        if window is None:
            print(f"UK time {uk_now().strftime('%H:%M')} is outside both "
                  f"digest windows (08:20-11:00 and 13:55-16:00), or that "
                  f"slot has already run today. Skipping.")
            return
        MAX_AGE_HOURS = window
        today_uk = uk_now().strftime("%Y-%m-%d")
        is_monday_catchup = (slot_name == "morning"
                             and uk_now().weekday() == 0
                             and MAX_AGE_HOURS > WEEKDAY_MORNING_HOURS)
        label = "weekend catch-up" if is_monday_catchup else ""
        print(f"UK time {uk_now().strftime('%H:%M %Z')} - {slot_name} digest, "
              f"{MAX_AGE_HOURS}h window"
              + ("  (Monday weekend catch-up)" if is_monday_catchup else ""))

    t_start = time.time()
    timings = {}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    print(f"Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"AI: {MODEL if api_key else 'OFF (keyword fallback)'}")

    # The morning run is the daily catch-up: it should show EVERYTHING from
    # the last 24 hours, even items that appeared in yesterday afternoon's
    # digest. Only the afternoon top-up filters against what has already
    # been reported, so it shows just what has broken since the morning.
    full_sweep = manual or MAX_AGE_HOURS >= WEEKDAY_MORNING_HOURS
    seen = set() if full_sweep else load_seen()
    if full_sweep and not manual:
        print("Full sweep: showing everything in the window, "
              "ignoring what earlier runs reported")
    elif not manual:
        print(f"Previously seen: {len(seen)} (top-up run)")

    t0 = time.time()
    all_items = []
    searches = SEARCH_TERMS + our_search_terms()

    # Every source is an independent download that spends its time waiting on
    # the network, so these run concurrently. Sequentially this was the single
    # slowest phase of the run - over a hundred requests, one after another,
    # with a slow feed blocking everything behind it.
    jobs = [(term, google_news_url(term), "Google News", True)
            for term in searches]
    jobs += [(name, url, name, False) for name, url in FEEDS]

    print(f"\nFetching {len(jobs)} sources ({PARALLEL_FETCHES} at a time)...")

    def grab(job):
        label, url, source, is_google = job
        try:
            items = parse_feed(fetch(url), source, is_google=is_google)
            return label, [i for i in items if is_recent(i["published"], cutoff)], None
        except Exception as e:
            return label, [], str(e)

    failures = 0
    if PARALLEL_FETCHES and PARALLEL_FETCHES > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=PARALLEL_FETCHES) as pool:
            results = list(pool.map(grab, jobs))
    else:
        results = [grab(j) for j in jobs]

    for label, items, err in results:
        if err:
            failures += 1
        else:
            all_items.extend(items)
    print(f"  {len(all_items)} articles in window, {failures} sources failed")

    timings["fetching"] = time.time() - t0
    deduped = deduplicate(all_items)
    merged = sum(len(i.get("also_in", [])) for i in deduped)
    print(f"\n{len(all_items)} fetched -> {len(deduped)} unique "
          f"({merged} duplicates merged)")

    new_items = []
    for it in deduped:
        key = hashlib.sha256(normalise_url(it["link"]).encode()).hexdigest()
        if key not in seen:
            new_items.append(it)
            seen.add(key)

    if len(new_items) > MAX_ARTICLES_PER_RUN:
        new_items.sort(key=lambda a: a["published"], reverse=True)
        dropped = len(new_items) - MAX_ARTICLES_PER_RUN
        new_items = new_items[:MAX_ARTICLES_PER_RUN]
        print(f"! capped at {MAX_ARTICLES_PER_RUN} articles "
              f"({dropped} oldest dropped to control cost)")

    # Word matching cannot tell "morning-after pill free on NHS" and
    # "Pharmacies supply 300,000 doses" apart from two unrelated stories, so
    # the AI does the clustering. Done before classification, it also cuts
    # the number of articles we pay to classify.
    cluster_stats = {"merged": 0, "calls": 0, "failed": 0}
    if new_items and api_key and ENABLE_CLUSTERING:
        print(f"\nClustering {len(new_items)} headlines...")
        before = len(new_items)
        new_items, cluster_stats = cluster_stories(api_key, new_items)
        print(f"  {before} -> {len(new_items)} distinct stories "
              f"({cluster_stats['merged']} duplicates merged)")

    print(f"\n{len(new_items)} to classify\n")

    t0 = time.time()
    if new_items:
        by_section, urgent, cls_stats = classify_all(new_items, api_key)
    else:
        by_section, urgent = {n: [] for n in ALL_SECTIONS}, []
        cls_stats = {"ai_batches": 0, "fallback_batches": 0,
                     "uk": 0, "international": 0}

    timings["classify"] = time.time() - t0

    t0 = time.time()
    review_stats = {"reviewed": 0, "dropped": 0, "merged": 0, "moved": 0}
    top5 = None
    if api_key and any(by_section.values()):
        total_now = len({a["link"] for v in by_section.values() for a in v})
        print(f"\nReviewing the assembled list ({total_now} stories, "
              f"effort={REVIEW_EFFORT})...")
        by_section, review_stats = review_digest(api_key, by_section)
        print(f"  dropped {review_stats['dropped']}, "
              f"merged {review_stats['merged']}, "
              f"moved {review_stats['moved']}")
        by_section = cap_international(by_section)

    timings["review"] = time.time() - t0

    t0 = time.time()
    # The review pass normally supplies the top five in the same call. This
    # only runs if it did not, so it costs nothing on a healthy run.
    if api_key and any(by_section.values()):
        print(f"\nPicking top 5 (effort={RECOMMEND_EFFORT})...")
        top5 = recommend_top5(api_key, by_section)
        if top5:
            print(f"  selected {len(top5)}")

    timings["top5"] = time.time() - t0

    digest = build_digest(by_section, urgent, bool(api_key), top5, label)
    print("\n" + digest)

    with open("latest_digest.md", "w", encoding="utf-8") as f:
        f.write(digest)

    if manual:
        # Always write, even when empty, so a manual run always produces
        # visible output. Kept separate so it never pollutes the twice-daily
        # log, and seen_articles.json is deliberately NOT updated.
        append_log(digest, MANUAL_FILE)
        print(f"\nWritten to {MANUAL_FILE} (scheduled log and state untouched)")
        # Manual runs email by default now. Getting a test email out was
        # taking too many attempts, and untick-to-skip is easier to reason
        # about than tick-to-send.
        want_email = os.environ.get("EMAIL_ON_MANUAL")
        if want_email is None or want_email == "":
            # The variable is not reaching us at all, which means the
            # workflow file has not been updated. Say so explicitly rather
            # than leaving it ambiguous.
            print("  note: EMAIL_ON_MANUAL was not passed in - your workflow "
                  "file is out of date. Emailing anyway.")
            send_email(digest, " (manual run)")
        elif want_email.strip().lower() in ("1", "true", "yes", "on"):
            send_email(digest, " (manual run)")
        else:
            print("  not emailed - the 'Also email this run' box was unticked")
    else:
        if any(by_section.values()):
            append_log(digest, LOG_FILE)
            post_slack(digest)
            if full_sweep:
                suffix = " - weekend catch-up" if label else ""
            else:
                suffix = " (afternoon update)"
            emailed = send_email(digest, suffix)
        else:
            # Still send on a full sweep so the team knows it ran and there
            # genuinely was nothing, rather than wondering if it broke.
            emailed = send_email(digest) if full_sweep else True

        # If email is not configured at all, there is nothing to retry, so
        # treat the slot as done rather than re-running on every cron.
        email_configured = bool(os.environ.get("EMAIL_TO", "").strip()
                                and os.environ.get("SMTP_HOST", "").strip())
        if not email_configured:
            emailed = True

        # A full sweep starts from an empty set, so merge with what is
        # already on file rather than throwing the history away - otherwise
        # the afternoon top-up would have nothing to filter against.
        if full_sweep:
            seen |= load_seen()
        save_seen(seen)

        # Only count the slot as finished if the digest actually reached
        # someone. If the email failed, leave it open so the next cron in
        # the window tries again rather than silently skipping.
        prev = load_last_runs().get(slot_name)
        attempts = 1
        if isinstance(prev, dict) and prev.get("day") == today_uk:
            attempts = prev.get("attempts", 1) + 1
        runs = load_last_runs()
        runs[slot_name] = {"day": today_uk, "emailed": bool(emailed),
                           "attempts": attempts}
        with open(RUNS_FILE, "w", encoding="utf-8") as f:
            json.dump(runs, f)
        if not emailed:
            print("  ! THE DIGEST WAS NOT EMAILED - this slot stays open so "
                  "the next scheduled run will try again")

    # Run summary - makes it obvious from the log whether the AI actually
    # did its job, rather than having to infer it from the digest.
    print("\n" + "=" * 52)
    print("RUN SUMMARY")
    print(f"  clustering calls   : {cluster_stats['calls']} "
          f"({cluster_stats['failed']} failed)")
    print(f"  duplicates merged  : {cluster_stats['merged']}")
    print(f"  classify batches   : {cls_stats['ai_batches']} by AI, "
          f"{cls_stats['fallback_batches']} fell back to keywords")
    print(f"  first-pass kept    : {cls_stats['uk']} UK, "
          f"{cls_stats['international']} international")
    print(f"  review pass        : saw {review_stats['reviewed']}, "
          f"dropped {review_stats['dropped']}, "
          f"merged {review_stats['merged']}, "
          f"moved {review_stats['moved']}"
          + (f", {review_stats['failed_chunks']} CHUNKS FAILED"
             if review_stats.get("failed_chunks") else ""))
    print("  time spent:")
    for phase in ("fetching", "classify", "review", "top5"):
        if phase in timings:
            print(f"    {phase:14} {timings[phase]:6.0f}s")
    print(f"    {'TOTAL':14} {time.time() - t_start:6.0f}s")
    if cluster_stats["failed"]:
        print("  !! clustering failed - THAT is why duplicates got through")
    if cls_stats["fallback_batches"]:
        print("  !! some batches fell back to keywords - THAT is why weak or "
              "foreign stories got through")
    print("=" * 52)

    if api_key and new_items:
        batches = (len(new_items) + BATCH_SIZE - 1) // BATCH_SIZE
        calls = batches + (1 if top5 else 0)
        per = {"xhigh": 0.30, "high": 0.15, "medium": 0.07,
               "low": 0.04, "none": 0.02}
        est = (batches * per.get(CLASSIFY_EFFORT, 0.15)
               + (per.get(RECOMMEND_EFFORT, 0.30) if top5 else 0))
        print(f"\n(~{calls} AI calls, very roughly {est:.2f} USD this run)")
    print("Done.")


if __name__ == "__main__":
    main()
