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
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# ===========================================================================
# CONFIG - edit anything in this block
# ===========================================================================

MAX_AGE_HOURS = float(os.environ.get("MAX_AGE_HOURS", "24"))

BATCH_SIZE = 30          # articles per AI call - raise to cut cost
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

CLASSIFY_EFFORT = "high"
RECOMMEND_EFFORT = "xhigh"
MAX_OUTPUT_TOKENS = 32000

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
        "Anything involving Christian Concern itself, the Christian Legal "
        "Centre, our staff, or our cases - including our own published "
        "articles and news, our people quoted, interviewed, cited or "
        "criticised elsewhere, and coverage of the cases we run. Also "
        "include significant output from allied organisations we work "
        "alongside.",
    "Worth reading":
        "Significant commentary and analysis on our issues: opinion "
        "pieces, essays and interventions that are worth our team reading "
        "even though they are not news reports. ONLY include these when "
        "the writer or outlet carries real weight - a well-known "
        "commentator, a public figure, or a serious outlet. Do not include "
        "routine columns, blog posts by unknown authors, or churnalism.",
}

ALL_SECTIONS = ISSUE_NAMES + list(EXTRA_SECTIONS)

# --- Who and what to watch for the "Christian Concern in the news" section -

OUR_PEOPLE = [
    "Andrea Williams", "Andrea Minichiello Williams", "Tim Dieppe",
    "Carys Moseley", "Paul Huxley", "Holly Baines", "Emily Bourne",
    "Darius Sandhu", "Rebecca Hunt", "Roger Kiska",
]

OUR_CASES = [
    "Darlington Nurses", "Jennifer Melle", "Steve Maile",
    "Bethany Hutchison",
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
    # Secularism
    "Charity Commission investigation", "EHRC code of practice",
    "equality law religion UK", "secularism UK public life",
    "humanists UK campaign",
    # Islam
    "sharia UK", "Islamism UK", "grooming gangs",
    "Islamophobia definition UK", "mosque planning UK",
    "Islamist extremism UK", "terrorism UK arrest",
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
    ("Christian Concern", "https://christianconcern.com/feed/"),
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


def our_search_terms():
    """Extra searches for the 'Christian Concern in the news' section."""
    terms = ['"Christian Concern"', '"Christian Legal Centre"']
    terms += _or_query(OUR_PEOPLE)
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


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def strip_html(text):
    return re.sub("<[^<]+?>", "", text or "").strip()


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
        f"{i}. {a['title']}  [{a['source']}]" for i, a in enumerate(chunk))
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
        with urllib.request.urlopen(req, timeout=900) as resp:
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


def ai_classify_batch(api_key, batch):
    listing = "\n".join(
        f"{i}. {a['title']}"
        + (f" [{a['source']}]" if a['source'] else "")
        + (f" — {a['summary'][:200]}" if a['summary'] else "")
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
Commentary is allowed ONLY in the "Worth reading" section, and only where the
writer or outlet carries real weight.
EXCLUDE: conference and event announcements, service or facility launches,
fundraising appeals, awareness days, charity press releases, trade-magazine
features, sponsored content, listicles, letters pages, and light features. If
the headline could have run in any week of any year, exclude it.

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
   "sections": ["Gender"],
   "relevance": "high",
   "scope": "uk",
   "urgent": false}}]

"scope" must be "uk" or "international". Mark it "international" if the story
is principally about events outside the UK.
An article may belong to more than one section - list all that apply, but do
not stretch. Use section names EXACTLY as written above.
Set "urgent" true only if it needs a response today - a vote or ruling
imminent, a campaigner changing position, a story going viral, or a Christian
publicly attacked for their faith.
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
    for b in range(total):
        batch = items[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        print(f"  AI batch {b + 1}/{total} ({len(batch)} articles)...")
        results = ai_classify_batch(api_key, batch)
        if results is None:
            stats["fallback_batches"] += 1
            print("    ! AI FAILED for this batch - falling back to keywords "
                  "(no relevance or scope filtering will apply)")
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
            stats[scope] += 1
            matched = False
            for sec in r.get("sections", []):
                if sec in by_section:
                    by_section[sec].append(item)
                    matched = True
            if matched and r.get("urgent"):
                if not any(u["link"] == item["link"] for u in urgent):
                    urgent.append(item)

    by_section = cap_international(by_section)
    return by_section, urgent, stats


def cap_international(by_section, limit=None):
    """
    Keep only the most significant international stories. Even with a strict
    prompt, foreign coverage is voluminous and can crowd out UK news, so this
    is a structural backstop rather than a matter of trusting the wording.
    """
    limit = limit if limit is not None else MAX_INTERNATIONAL
    intl = {}
    for articles in by_section.values():
        for a in articles:
            if a.get("scope") == "international":
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
        by_section[name] = [a for a in articles
                            if a.get("scope") != "international"
                            or a["link"] in keep]
    print(f"  capped international stories: kept {len(keep)}, "
          f"dropped {dropped}")
    return by_section


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

Reply with ONLY a JSON array, ranked best first, no other text:
[{{"i": 0,
   "title": "copy the headline exactly as shown, so we can verify the match",
   "rank": 1,
   "what": "1-2 sentences on what actually happened, with specifics.",
   "angle": "2-3 sentences: the specific line we could take, and why it works
             for us - what makes it distinctive, timely or winnable.",
   "headline": "a punchy suggested headline for our comment piece"}}]"""

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
                      "angle": str(r.get("angle", "")).strip(),
                      "headline": str(r.get("headline", "")).strip()})
    picks.sort(key=lambda p: p["rank"] if isinstance(p["rank"], int) else 99)
    return picks[:5] or None


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def build_digest(by_section, urgent, used_ai, top5=None, label=""):
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

    def render(a):
        pub = a["published"]
        when = pub.strftime("%H:%M") if pub.date() == today \
            else pub.strftime("%a %H:%M")
        src = a["source"]
        others = a.get("also_in") or []
        if others:
            named = ", ".join(others[:3])
            extra = f" +{len(others) - 3} more" if len(others) > 3 else ""
            src += f" (also {named}{extra})"
        flag = " 🌍" if a.get("scope") == "international" else ""
        return f"- [{a['title']}]({a['link']}){flag} — *{src}*, {when}"

    def section_block(name, articles, level="###"):
        articles.sort(key=lambda a: (RANK.get(a.get("relevance", "medium"), 1),
                                     -a["published"].timestamp()))
        out = [f"{level} {name} ({len(articles)})"]
        out += [render(a) for a in articles]
        out.append("")
        return out

    if urgent:
        lines.append("## ⚡ RESPOND NOW")
        lines += [render(a) for a in urgent]
        lines.append("")

    empty = []
    for family, issues in ISSUE_FAMILIES.items():
        present = [(n, by_section[n]) for n in issues if by_section[n]]
        empty += [n for n in issues if not by_section[n]]
        if not present:
            continue
        lines.append(f"## {family.upper()}")
        lines.append("")
        for name, articles in present:
            lines += section_block(name, articles)

    for name in EXTRA_SECTIONS:
        if by_section[name]:
            lines += section_block(name.upper(), by_section[name], level="##")

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
            if p.get("angle"):
                lines.append(f"**Angle:** {p['angle']}")
                lines.append("")
            if p.get("headline"):
                lines.append(f"**Possible headline:** *{p['headline']}*")
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


def should_run_now():
    """Return the lookback window in hours, or None to skip."""
    now = uk_now()
    minutes = now.hour * 60 + now.minute
    slots = [
        (8 * 60 + 55, 24.0),   # 08:55 UK -> last 24h
        (14 * 60 + 0, 5.5),    # 14:00 UK -> since the morning run
    ]
    for target, window in slots:
        if abs(minutes - target) <= 20:
            return window
    return None


# ---------------------------------------------------------------------------

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
        window = should_run_now()
        if window is None:
            print(f"UK time {uk_now().strftime('%H:%M')} is not one of the "
                  f"scheduled slots (08:55 or 14:00), so this run is skipping.")
            print("If you expected a manual run, this means GitHub did not "
                  "report it as 'workflow_dispatch'. Press the 'Run workflow' "
                  "button on the Actions tab, or set FORCE_RUN=1.")
            return
        MAX_AGE_HOURS = window
        label = ""
        print(f"UK time {uk_now().strftime('%H:%M %Z')} - "
              f"{MAX_AGE_HOURS}h window")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    print(f"Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"AI: {MODEL if api_key else 'OFF (keyword fallback)'}")

    seen = set() if manual else load_seen()
    if not manual:
        print(f"Previously seen: {len(seen)}")

    all_items = []
    searches = SEARCH_TERMS + our_search_terms()
    print(f"\nRunning {len(searches)} searches...")
    for term in searches:
        try:
            items = parse_feed(fetch(google_news_url(term)),
                               "Google News", is_google=True)
            fresh = [i for i in items if is_recent(i["published"], cutoff)]
            if fresh:
                print(f"  '{term}': {len(fresh)}")
            all_items.extend(fresh)
        except Exception as e:
            print(f"  ! '{term}' failed: {e}")

    print(f"\nReading {len(FEEDS)} standing feeds...")
    for name, url in FEEDS:
        try:
            items = parse_feed(fetch(url), name)
            fresh = [i for i in items if is_recent(i["published"], cutoff)]
            print(f"  {name}: {len(fresh)}")
            all_items.extend(fresh)
        except Exception as e:
            print(f"  ! {name} failed: {e}")

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
    if new_items and api_key:
        print(f"\nClustering {len(new_items)} headlines...")
        before = len(new_items)
        new_items, cluster_stats = cluster_stories(api_key, new_items)
        print(f"  {before} -> {len(new_items)} distinct stories "
              f"({cluster_stats['merged']} duplicates merged)")

    print(f"\n{len(new_items)} to classify\n")

    if new_items:
        by_section, urgent, cls_stats = classify_all(new_items, api_key)
    else:
        by_section, urgent = {n: [] for n in ALL_SECTIONS}, []
        cls_stats = {"ai_batches": 0, "fallback_batches": 0,
                     "uk": 0, "international": 0}

    top5 = None
    if api_key and any(by_section.values()):
        print(f"\nPicking top 5 (effort={RECOMMEND_EFFORT}, may take "
              f"a few minutes)...")
        top5 = recommend_top5(api_key, by_section)
        if top5:
            print(f"  selected {len(top5)}")

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
    else:
        if any(by_section.values()):
            append_log(digest, LOG_FILE)
            post_slack(digest)
        save_seen(seen)

    # Run summary - makes it obvious from the log whether the AI actually
    # did its job, rather than having to infer it from the digest.
    print("\n" + "=" * 52)
    print("RUN SUMMARY")
    print(f"  clustering calls   : {cluster_stats['calls']} "
          f"({cluster_stats['failed']} failed)")
    print(f"  duplicates merged  : {cluster_stats['merged']}")
    print(f"  classify batches   : {cls_stats['ai_batches']} by AI, "
          f"{cls_stats['fallback_batches']} fell back to keywords")
    print(f"  stories kept       : {cls_stats['uk']} UK, "
          f"{cls_stats['international']} international")
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
