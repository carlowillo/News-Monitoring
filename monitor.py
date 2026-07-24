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
    "assisted dying bill UK",
    "assisted suicide UK",
    "Terminally Ill Adults Bill",
    "euthanasia UK law",
    "right to die case UK",
    # Abortion
    "abortion law UK",
    "abortion buffer zones",
    "abortion decriminalisation",
    "pro-life UK",
    "abortion clinic protest UK",
    "abortion pills post UK",
    # Islam
    "sharia UK",
    "Islamism UK",
    "grooming gangs",
    "blasphemy UK",
    "Islamophobia definition UK",
    # Religious liberty
    "Christian free speech UK",
    "religious discrimination tribunal UK",
    "conversion therapy ban UK",
    "street preacher arrested UK",
    "Christian sacked belief UK",
    "prayer arrest UK",
    "employment tribunal religion belief UK",
    "chaplain sacked UK",
    "foster carers religion UK",
    # Marriage, family, gender
    "gender identity schools UK",
    "puberty blockers UK",
    "Cass Review",
    "single sex spaces ruling UK",
    "transgender prisoners UK",
    "sex education parents UK",
    "no-fault divorce UK",
    "surrogacy law UK",
    # Church and public life
    "Church of England",
    "faith schools UK",
    "bishops House of Lords",
    "religious education UK schools",
    # Law, rights, civil liberties
    "extremism definition UK",
    "Charity Commission Christian",
    "ECHR reform UK",
    "free speech arrest UK",
    "parental rights UK",
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

# How hard the model thinks before answering.
# gpt-5.5 accepts: none, low, medium, high, xhigh  (xhigh = maximum)
# Higher = better judgement, but slower and more expensive, because
# thinking tokens are billed at the output rate.
CLASSIFY_EFFORT = "high"     # sorting articles - a more mechanical task
RECOMMEND_EFFORT = "xhigh"   # picking + justifying the top 5 - real judgement

# Reasoning tokens count towards this, so it must be generous at high effort
# or the model can think until it runs out and return nothing.
MAX_OUTPUT_TOKENS = 32000

NS_ATOM = {"a": "http://www.w3.org/2005/Atom"}
NS_DC = {"dc": "http://purl.org/dc/elements/1.1/"}


def google_news_url(term):
    """
    Build a Google News RSS search URL.
    We ask Google for 2 days and let our own MAX_AGE_HOURS filter do the
    real trimming - 'when:1d' was cutting off stories that our 24-hour
    window would happily accept, because Google's day boundary is its own.
    """
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
# Deduplication
# ---------------------------------------------------------------------------

def normalise_url(url):
    """Strip tracking parameters so the same article isn't counted twice."""
    try:
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query)
        keep = [(k, v) for k, v in query
                if not k.lower().startswith(("utm_", "fbclid", "gclid", "ito",
                                             "ref", "share", "cmp"))]
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
    """Normalise a headline to a set of meaningful words for comparison."""
    text = re.sub(r"[^a-z0-9\s]", " ", title.lower())
    return {w for w in text.split() if w not in STOPWORDS and len(w) > 2}


def titles_match(tokens_a, tokens_b, threshold=0.62):
    """Are these two headlines telling the same story?"""
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    if union == 0:
        return False
    smaller = min(len(tokens_a), len(tokens_b))
    containment = overlap / smaller if smaller else 0
    # Jaccard catches reworded headlines; containment catches one headline
    # being a longer version of another.
    return (overlap / union) >= threshold or containment >= 0.85


def deduplicate(items):
    """
    Two passes: identical URL, then near-identical headline (same story
    carried by several outlets). Records which other outlets ran it, which
    is itself a useful signal of how big a story is.
    """
    by_url = {}
    for it in items:
        key = normalise_url(it["link"])
        if key not in by_url:
            by_url[key] = it
    staged = list(by_url.values())

    kept = []
    for it in staged:
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


def _openai_request(api_key, prompt, token_param, max_tokens, effort):
    """Build and send one OpenAI request. Returns (text, error_message)."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    if token_param:
        payload[token_param] = max_tokens
    if effort:
        payload["reasoning_effort"] = effort

    req = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return None, "no choices in response"
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            finish = choices[0].get("finish_reason", "")
            usage = data.get("usage", {})
            detail = usage.get("completion_tokens_details", {})
            reasoning_used = detail.get("reasoning_tokens", "?")
            return None, (f"empty content (finish_reason={finish}, "
                          f"reasoning_tokens={reasoning_used}) - "
                          f"try raising MAX_OUTPUT_TOKENS or lowering effort")
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
    """The parameter itself isn't recognised - no point trying other values."""
    if not err:
        return False
    low = err.lower()
    unknown = ("unrecognized request argument" in low
               or "unknown parameter" in low
               or "unsupported parameter" in low
               or "is not supported with this model" in low)
    return unknown and any(n.lower() in low for n in names)


def _value_rejected(err, names):
    """The parameter exists but this value isn't allowed - try a lower one."""
    if not err:
        return False
    low = err.lower()
    bad_value = ("unsupported value" in low or "invalid value" in low
                 or "does not support" in low)
    return bad_value and any(n.lower() in low for n in names)


def call_openai(api_key, prompt, effort=None, max_tokens=None, retries=3):
    """
    Call OpenAI, coping with two moving targets:
      - newer reasoning models want 'max_completion_tokens', older 'max_tokens'
      - not every model accepts every reasoning_effort value
    Falls back through both rather than failing outright.
    """
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
                    continue        # try the other token parameter
                break               # not a token-param problem

            if _param_unknown(err, effort_names):
                # Model doesn't know this parameter at all - drop it entirely
                # rather than wasting calls stepping through values.
                print("    (model does not accept reasoning_effort, omitting)")
                effort_options = [None]
                idx = 0
                continue
            if _value_rejected(err, effort_names):
                print(f"    (reasoning_effort '{eff}' rejected, stepping down)")
                idx += 1
                continue
            break                   # not an effort problem either

        wait = 2 ** attempt
        print(f"    ! OpenAI call failed ({err}), retry in {wait}s")
        time.sleep(wait)

    return None


CC_PERSPECTIVE = """Christian Concern is a conservative evangelical advocacy organisation
founded by Andrea Minichiello Williams, working closely with the Christian Legal Centre,
which litigates on behalf of Christians. It campaigns to see the UK return to Christian
foundations, and generally OPPOSES the secular direction of law and culture.

Its settled positions:
- Assisted dying / assisted suicide: strongly opposed. Frames it as a "culture of death",
  warns of pressure on the vulnerable, disabled and elderly, and argues for palliative
  care instead. Interested in hospice funding, safeguarding failures, evidence from
  Canada/Netherlands/Oregon, and campaigners changing their minds.
- Abortion: pro-life. Opposes decriminalisation, buffer zones (as a free-speech issue,
  including silent prayer arrests), late-term abortion, and abortion pills by post.
- Gender and sexuality: upholds biological sex and the Supreme Court judgment on the
  meaning of sex. Opposes gender self-ID, puberty blockers, trans ideology in schools,
  and single-sex space breaches (e.g. the Darlington Nurses case). Supports marriage as
  a lifelong union of one man and one woman; critical of no-fault divorce.
- Conversion therapy bans: opposed, on the grounds that they criminalise consensual
  conversation, prayer and ordinary pastoral care.
- Islam: concerned about Islamisation of Britain, sharia, grooming gangs, blasphemy
  codes, and definitions of "anti-Muslim hostility" that suppress legitimate criticism.
- Religious liberty and free speech: defends Christians disciplined, sacked, arrested or
  prosecuted for their beliefs - street preachers, teachers, nurses, doctors, chaplains,
  foster carers, employees. Very interested in employment tribunals and court rulings.
- Extremism definitions, Charity Commission powers, Prevent, and hate-speech law
  in so far as they stigmatise or threaten Christians and pro-lifers.
- Critical of BBC and mainstream media bias against Christian views.
- Critical of the Church of England for compromise on doctrine and safeguarding failures.
- Concerned about dechristianisation of Britain, and defends Britain's Christian heritage
  against the claim that it is merely ethno-nationalism.
- Sceptical of the ECHR and Human Rights Act where they obstruct these aims."""


def ai_classify_batch(api_key, batch, issue_names):
    """Ask the AI to judge a batch of articles. Returns list of dicts or None."""
    listing = "\n".join(
        f"{i}. {a['title']}"
        + (f" [{a['source']}]" if a['source'] else "")
        + (f" — {a['summary'][:200]}" if a['summary'] else "")
        for i, a in enumerate(batch)
    )
    prompt = f"""You are a media monitor for Christian Concern, a UK Christian public policy organisation.

{CC_PERSPECTIVE}

Our issue categories:
{chr(10).join('- ' + n for n in issue_names)}

TASK: decide which headlines below are worth Christian Concern seeing today.

This list is read quickly by a busy team. A weak item costs them more than a
missed one, so apply all three of these tests. An article must pass ALL THREE.

TEST 1 - IS IT UK?
Include only UK stories: UK law, UK courts, UK politics, UK institutions,
UK people, or something happening in the UK.
The ONLY exception is a major international story with obvious UK relevance -
a landmark foreign ruling or law that will shape the UK debate, or a
significant development in a comparable country (Canada, Ireland, Australia,
US federal). A hospital opening in India, a conference abroad, or routine
foreign news is NOT relevant however well it fits our issues.

TEST 2 - IS IT ACTUALLY NEWS?
Include reporting of events: rulings, votes, bills, cases, arrests, official
figures, investigations, resignations, campaigns launched, significant
statements by public figures.
EXCLUDE: conference and event announcements, service or facility launches,
fundraising appeals, awareness days, charity press releases, trade-magazine
features, sponsored content, listicles, and general opinion columns with no
news peg. If the headline could have run in any week of any year, exclude it.

TEST 3 - DOES IT MATTER TO US?
It must have real public-policy or cultural significance for our issues -
something that changes the picture, sets a precedent, moves a debate, or that
our supporters would want to know about.
EXCLUDE local matters of no national significance, minor administrative news,
and stories that merely touch a topic we care about without adding anything.

Then mark what survives:
  "high"   - directly on one of our issues, or a Christian penalised for
             their beliefs. We would very likely want to comment.
  "medium" - genuinely significant and connected, worth knowing about, but
             not something we would necessarily write about today.

Do not include anything weaker than "medium". If in doubt, leave it out -
a shorter, sharper list is worth far more to us than a long one.

Articles:
{listing}

Reply with ONLY a JSON array, no other text. One object per included article:
[{{"i": 0,
   "issues": ["Religious Liberty"],
   "relevance": "high",
   "urgent": false}}]

Set "urgent" true only if it needs a response today - a vote or ruling
imminent, a campaigner changing position, a story going viral, or a Christian
publicly attacked for their faith.
Use only issue names from the list above. If nothing qualifies, reply []."""

    reply = call_openai(api_key, prompt, effort=CLASSIFY_EFFORT)
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
            rel = str(r.get("relevance", "medium")).strip().lower()
            if rel not in ("high", "medium"):
                # Anything weaker is dropped - the list stays sharp.
                if rel == "low":
                    continue
                rel = "medium"
            item["relevance"] = rel
            matched = False
            for iss in r.get("issues", []):
                if iss in by_issue:
                    by_issue[iss].append(item)
                    matched = True
            if matched and r.get("urgent"):
                urgent.append(item)

    return by_issue, urgent


def recommend_top5(api_key, by_issue):
    """Second AI pass: pick the 5 best articles to comment on, with reasoning."""
    # Collect unique articles across issues
    pool, seen_links = [], set()
    for issue, articles in by_issue.items():
        for a in articles:
            if a["link"] in seen_links:
                continue
            seen_links.add(a["link"])
            pool.append((issue, a))

    if not pool:
        return None
    if not api_key:
        return None

    listing = "\n".join(
        f"{i}. [{issue}] ({a.get('relevance','medium')}) {a['title']}"
        f"  — {a['source']}"
        + (f", also in {', '.join(a['also_in'][:4])}" if a.get("also_in") else "")
        + (f"\n   {a['summary'][:200]}" if a.get("summary") else "")
        for i, (issue, a) in enumerate(pool)
    )

    prompt = f"""You are advising Christian Concern's communications team.

{CC_PERSPECTIVE}

Below are today's stories. Choose the FIVE best for Christian Concern to comment
on publicly, ranked 1-5 (1 = highest priority).

Weigh:
- REACH: how big is the story, how many outlets are carrying it?
- TIMELINESS: is there a narrow window to be part of the conversation?
- DISTINCTIVENESS: can we say something others are not saying? A crowded take
  adds little; an underexplored Christian angle adds a lot.
- PICKUP: how likely are journalists, MPs or sympathetic accounts to quote,
  share or cite us on this?
- STRATEGIC FIT: does it advance a campaign we are running, or connect to a
  Christian Legal Centre case?
- SUPPORTERS: would our audience share it, act on it, or donate?

Favour "high" relevance items. Only pick a "medium" if it is genuinely a better
opportunity - for instance an angle nobody else has spotted.

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

    picks = []
    used = set()
    for r in parsed:
        idx = None
        try:
            idx = int(r.get("i", -1))
        except (TypeError, ValueError):
            idx = None

        echoed = str(r.get("title", "")).strip()

        # Verify the index actually points at the headline the AI described.
        # If not, find the article by title instead - protects against the
        # reasoning being attached to the wrong story.
        valid = idx is not None and 0 <= idx < len(pool)
        if valid and echoed:
            if not titles_match(title_tokens(echoed),
                                title_tokens(pool[idx][1]["title"]),
                                threshold=0.5):
                valid = False

        if not valid and echoed:
            echo_tokens = title_tokens(echoed)
            for j, (_, cand) in enumerate(pool):
                if titles_match(echo_tokens, title_tokens(cand["title"]),
                                threshold=0.5):
                    idx = j
                    valid = True
                    print(f"    (corrected a mismatched index to #{j})")
                    break

        if not valid or idx in used:
            continue
        used.add(idx)

        issue, article = pool[idx]
        picks.append({
            "article": article,
            "issue": issue,
            "rank": r.get("rank", len(picks) + 1),
            "what": str(r.get("what", "")).strip(),
            "angle": str(r.get("angle", "")).strip(),
            "headline": str(r.get("headline", "")).strip(),
        })

    picks.sort(key=lambda p: p["rank"] if isinstance(p["rank"], int) else 99)
    return picks[:5] or None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_digest(by_issue, urgent, used_ai, top5=None):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "AI-classified" if used_ai else "keyword-matched"
    lines = [f"# Digest - {stamp}",
             f"*Last {MAX_AGE_HOURS} hours, {mode}*", ""]

    if not any(by_issue.values()):
        lines.append("No new relevant articles this run.")
        return "\n".join(lines)

    RANK = {"high": 0, "medium": 1}
    today = datetime.now(timezone.utc).date()

    def render(a):
        pub = a["published"]
        # Show the weekday only when it is not today, so a time like
        # "20:15" is never ambiguous across the 24-hour window.
        when = pub.strftime("%H:%M") if pub.date() == today \
            else pub.strftime("%a %H:%M")
        src = a["source"]
        if a.get("also_in"):
            src += f" +{len(a['also_in'])}"
        return f"- [{a['title']}]({a['link']}) — *{src}*, {when}"

    if urgent:
        lines.append("## ⚡ RESPOND NOW")
        for a in urgent:
            lines.append(render(a))
        lines.append("")

    for issue, articles in by_issue.items():
        if not articles:
            continue
        articles.sort(key=lambda a: (RANK.get(a.get("relevance", "medium"), 1),
                                     -a["published"].timestamp()))
        lines.append(f"## {issue} ({len(articles)})")
        for a in articles:
            lines.append(render(a))
        lines.append("")

    if top5:
        lines.append("---")
        lines.append("")
        lines.append("# 🎯 Top 5 to comment on")
        lines.append("")
        for p in top5:
            a = p["article"]
            lines.append(f"### {p['rank']}. [{a['title']}]({a['link']})")
            lines.append(f"*{p['issue']} · {a['source']}*")
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
    if api_key:
        print(f"AI: {MODEL}, classify={CLASSIFY_EFFORT}, "
              f"recommend={RECOMMEND_EFFORT}")
    else:
        print("AI: OFF (keyword fallback)")

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

    # Deduplicate: identical URLs, then same story across different outlets
    deduped = deduplicate(all_items)
    merged = sum(len(i.get("also_in", [])) for i in deduped)
    print(f"\n{len(all_items)} fetched -> {len(deduped)} unique "
          f"({merged} duplicate outlet copies merged)")

    new_items = []
    for it in deduped:
        key = hashlib.sha256(normalise_url(it["link"]).encode()).hexdigest()
        if key not in seen:
            new_items.append(it)
            seen.add(key)

    print(f"{len(new_items)} new since last run\n")

    if new_items:
        by_issue, urgent = classify_all(new_items, api_key, list(ISSUES))
    else:
        by_issue, urgent = {n: [] for n in ISSUES}, []

    top5 = None
    if api_key and any(by_issue.values()):
        print(f"\nPicking top 5 to comment on "
              f"(effort={RECOMMEND_EFFORT}, this can take a few minutes)...")
        top5 = recommend_top5(api_key, by_issue)
        if top5:
            print(f"  selected {len(top5)}")

    digest = build_digest(by_issue, urgent, bool(api_key), top5)
    print("\n" + digest)

    if any(by_issue.values()):
        append_log(digest)
        post_slack(digest)

    save_seen(seen)

    if api_key and new_items:
        batches = (len(new_items) + BATCH_SIZE - 1) // BATCH_SIZE
        calls = batches + (1 if top5 else 0)
        # GPT-5.5 is ~$5/M input, ~$30/M output; reasoning tokens bill as output.
        # Thinking tokens bill at the output rate, so high effort costs more.
        per_call = {"xhigh": 0.30, "high": 0.15, "medium": 0.07,
                    "low": 0.04, "none": 0.02}
        est = (batches * per_call.get(CLASSIFY_EFFORT, 0.15)
               + (per_call.get(RECOMMEND_EFFORT, 0.30) if top5 else 0))
        print(f"\n(~{calls} AI calls, very roughly {est:.2f} USD this run - "
              f"check your OpenAI usage dashboard for the real figure)")
    print("Done.")


if __name__ == "__main__":
    main()
