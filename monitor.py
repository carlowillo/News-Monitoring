#!/usr/bin/env python3
"""
x_monitor.py — X / Twitter monitor for Christian Concern.

Sits alongside the existing news monitor. Separate digest, separate email,
same repo. Uses xAI's x_search tool through the Responses API at
api.x.ai/v1/responses, which is first-party sanctioned access to the X
corpus rather than a scrape.

Phases implemented here:
  1. Sphere watch  — what the accounts in our world are posting
     Issue watch   — what is being discussed across X on our twelve issues
  2. Heat layer    — rolling per-issue baselines and spike detection
     Opportunity   — hot story + a Christian Legal Centre case + no CC post

Stdlib only. No third-party dependencies.

Usage:
    python x_monitor.py --verify-handles     # do this FIRST, before going live
    python x_monitor.py --dry-run            # gather, print, send nothing
    python x_monitor.py --hours 12           # lookback window
    python x_monitor.py                      # normal scheduled run

Environment:
    XAI_API_KEY            required
    SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS   required unless --no-email
    X_EMAIL_TO             recipients for this digest (falls back to EMAIL_TO)
    SLACK_WEBHOOK_URL      optional, spike alerts only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:                                    # pragma: no cover
    UK = timezone.utc

import x_config as CFG
import x_metrics as MX

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XAI_URL = "https://api.x.ai/v1/responses"
REQUEST_TIMEOUT = 180          # x_search calls run 30-60s+; 180 is the ceiling
MAX_RETRIES = 3
RETRY_BACKOFF = 5              # seconds, multiplied by attempt number
MAX_CONCURRENCY = 6

REPO = Path(__file__).resolve().parent
HEAT_HISTORY = REPO / "x_heat_history.json"
SEEN_POSTS = REPO / "x_seen_posts.json"
DIGEST_LOG = REPO / "x_digest_log.md"
LAST_RUNS = REPO / "x_last_runs.json"
POST_HISTORY = REPO / "x_post_history.json"

SEEN_RETENTION_DAYS = 30       # GDPR: links + our own summaries only, 30 days
HEAT_RETENTION_DAYS = 90

# Running cost tally for this process
_COST = {"search_calls": 0, "in_tokens": 0, "out_tokens": 0}


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def tier_handles(tier: dict) -> list[str]:
    """Read a tier's handles, accepting either form.

    Old style:  "handles": ["SuellaBraverman", ...]
    New style:  "handles": [{"handle": "SuellaBraverman", "name": "Suella Braverman"}, ...]

    The new form is what discover-handles emits, and keeping the name beside
    the handle is what lets it re-check them later.
    """
    out = []
    for entry in tier.get("handles", []):
        handle = entry.get("handle", "") if isinstance(entry, dict) else entry
        handle = str(handle or "").strip().lstrip("@")
        if handle and not handle.startswith("#"):
            out.append(handle)
    return out


def tier_name_for(tier: dict, handle: str) -> str:
    for entry in tier.get("handles", []):
        if isinstance(entry, dict) and entry.get("handle", "").lstrip("@") == handle:
            return entry.get("name", handle)
    return handle


def collapse_ws(text) -> str:
    """Collapse all whitespace to single spaces.

    Lesson from the news monitor: newlines inside an item silently break any
    downstream numbered list. Collapse at ingest AND at list-build time.
    """
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def uk_stamp(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UK).strftime("%Y-%m-%d %H:%M %Z")


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


def parse_json_block(text: str):
    """Pull a JSON object or array out of a model reply.

    Handles ```json fences, leading prose, and trailing commentary.
    Returns None rather than raising.
    """
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


def normalise_post_url(url: str) -> str:
    """Canonical form of an X post URL, for deduplication."""
    if not url:
        return ""
    url = url.split("?")[0].rstrip("/")
    url = url.replace("//twitter.com/", "//x.com/")
    url = url.replace("//mobile.x.com/", "//x.com/")
    url = url.replace("http://", "https://")
    return url.lower()


# ---------------------------------------------------------------------------
# xAI Responses API
# ---------------------------------------------------------------------------

class GrokError(RuntimeError):
    pass


def _post_json(payload: dict, api_key: str) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        XAI_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_text(data: dict) -> str:
    """Assemble the assistant text from a Responses API payload.

    Do not index by position. Blocks arrive in varying order and reasoning
    blocks are interleaved with message blocks.
    """
    chunks = []
    for item in data.get("output", []) or []:
        if item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") in ("output_text", "text") and part.get("text"):
                    chunks.append(part["text"])
        elif item.get("type") in ("output_text", "text") and item.get("text"):
            chunks.append(item["text"])
    if not chunks and isinstance(data.get("output_text"), str):
        chunks.append(data["output_text"])
    return "\n".join(chunks).strip()


def _extract_citations(data: dict) -> list[str]:
    """Collect source URLs from both channels xAI uses."""
    urls = []
    for c in data.get("citations", []) or []:
        if isinstance(c, str):
            urls.append(c)
        elif isinstance(c, dict) and c.get("url"):
            urls.append(c["url"])
    for item in data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            for ann in part.get("annotations", []) or []:
                if ann.get("type") == "url_citation" and ann.get("url"):
                    urls.append(ann["url"])
    seen, out = set(), []
    for u in urls:
        n = normalise_post_url(u)
        if n and n not in seen:
            seen.add(n)
            out.append(u)
    return out


def call_grok(
    prompt: str,
    api_key: str,
    allowed_handles: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    use_search: bool = True,
) -> dict:
    """One call to the Responses API, with x_search when requested.

    Returns {"text", "citations", "degraded", "degraded_reason"}.

    THE DEGRADED TRAP — read this before changing anything:
    if a narrowing filter is set (allowed_x_handles or a date range) and the
    X index matches nothing, Grok still writes a fluent, confident answer
    from its own training data with zero citations. It is indistinguishable
    from a real result by eye. Any filtered call that returns no citations
    is therefore treated as a hard failure and discarded.
    """
    tool = {"type": "x_search"}
    if allowed_handles:
        tool["allowed_x_handles"] = [h.lstrip("@") for h in allowed_handles]
    if from_date:
        tool["from_date"] = from_date
    if to_date:
        tool["to_date"] = to_date

    payload = {
        "model": model or CFG.GATHER_MODEL,
        "input": [{"role": "user", "content": prompt}],
    }
    if use_search:
        payload["tools"] = [tool]
    if effort:
        payload["reasoning"] = {"effort": effort}

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = _post_json(payload, api_key)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last_err = f"HTTP {exc.code}: {detail}"
            if exc.code in (400, 401, 403, 404):
                raise GrokError(last_err) from exc      # never retry these
        except Exception as exc:                        # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    else:
        raise GrokError(f"all {MAX_RETRIES} attempts failed — {last_err}")

    if use_search:
        _COST["search_calls"] += 1
    usage = data.get("usage", {}) or {}
    _COST["in_tokens"] += int(usage.get("input_tokens") or 0)
    _COST["out_tokens"] += int(usage.get("output_tokens") or 0)

    text = _extract_text(data)
    citations = _extract_citations(data)

    filtered = bool(allowed_handles or from_date or to_date)
    degraded = use_search and filtered and not citations
    reason = None
    if degraded:
        bits = []
        if allowed_handles:
            bits.append(f"allowed_x_handles={len(allowed_handles)}")
        if from_date:
            bits.append(f"from_date={from_date}")
        reason = "no citations returned with " + ", ".join(bits)

    return {
        "text": text,
        "citations": citations,
        "degraded": degraded,
        "degraded_reason": reason,
    }


def run_parallel(jobs: list[tuple], worker, max_workers: int = MAX_CONCURRENCY):
    """Run worker(*job) across jobs, returning (result, error) pairs in order."""
    results: list = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(worker, *job): i for i, job in enumerate(jobs)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = (fut.result(), None)
            except Exception as exc:                    # noqa: BLE001
                results[idx] = (None, f"{type(exc).__name__}: {exc}")
    return results


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def canonical_issue(raw: str, allowed: list[str] | None = None,
                    context: str = "", strict: bool = False) -> str:
    """Force a story onto one of the twelve configured issues.

    Order of attempts: exact name, then keyword match against the issue's
    own vocabulary, then substring, then word overlap. Only after all of
    those fail does it fall back — and in strict mode it returns "" rather
    than guessing.

    The fallback used to be "first item in the pool", which filed every
    assisted-dying story under Beginning of life because "assisted suicide"
    matched nothing and Beginning of life happened to be first in the
    cluster. An arbitrary default is worse than an admitted blank.
    """
    text = collapse_ws(raw).lower()
    if not text:
        return ""
    pool = allowed or CFG.ISSUES

    for issue in list(pool) + CFG.ISSUES:                 # exact
        if text == issue.lower():
            return issue

    haystack = f"{text} {collapse_ws(context).lower()}"    # keywords
    best, best_hits = None, 0
    for issue in list(pool) + CFG.ISSUES:
        hits = sum(1 for kw in CFG.ISSUE_KEYWORDS.get(issue, [])
                   if kw in haystack)
        if hits > best_hits:
            best, best_hits = issue, hits
    if best:
        return best

    for issue in list(pool) + CFG.ISSUES:                  # substring
        low = issue.lower()
        if low in text or text in low:
            return issue

    # Word overlap last, and ignore words shared between issues. "life"
    # appears in both Beginning and End of life, so on its own it decides
    # nothing and must not be allowed to.
    shared = {"life", "christian", "freedom", "of", "the"}
    for issue in list(pool) + CFG.ISSUES:
        words = {w for w in issue.lower().split()
                 if len(w) > 3 and w not in shared}
        if words and words & set(text.split()):
            return issue

    if strict:
        return ""
    return pool[0] if allowed else "Christian society"


STORY_SCHEMA = """
Return ONLY a JSON object, no prose, no markdown fences:

{"stories": [
  {
    "headline": "under 15 words, factual, no editorialising",
    "summary": "2-3 sentences in your own words. Do not quote posts at length.",
    "issue": "exactly one of the issue names given above",
    "also_touches": "another issue name, or empty string",
    "scope": "UK" or "International",
    "accounts": ["handles driving this, without @"],
    "post_urls": ["direct URLs to the most significant posts, most important first"],
    "heat": 1-5,
    "heat_reason": "one sentence: why that number. Cite volume, reach or velocity.",
    "engagement_note": "rough sense of scale if visible, else empty string"
  }
]}

Rules:
- A "story" is a discrete event or controversy, not a topic. "Assisted dying"
  is a topic. "Lords defeat an amendment on assisted dying" is a story.
- Merge posts about the same event into one story.
- If nothing meets the bar, return {"stories": []}. An empty result is a
  valid and useful answer. Do not invent activity to fill the list.
- Heat is your honest estimate of how much activity surrounds the story on X
  right now: 1 = barely discussed, 3 = steady discussion, 5 = dominating.
- Never include a post URL you did not actually retrieve.
"""


def sphere_prompt(tier: dict, hours: int) -> str:
    """Retrieval first, judgement second.

    An earlier version opened with the analytical framing — "identify the
    substantive posts relevant to our issues" — and Grok would reason its
    way out of searching at all, returning nothing with no citations. The
    simple retrieval prompt used by verify-handles never failed. So the
    instruction now orders the search explicitly, then filters what came
    back.
    """
    return collapse_ws(f"""
STEP 1. Search X and retrieve the posts published by the listed accounts in
the last {hours} hours. Do this first, before any analysis. If an account
has posted, you must retrieve those posts.

STEP 2. From what you actually retrieved in step 1, keep the posts that bear
on any of these subjects: {', '.join(CFG.ISSUES)}, or UK politics and
government where it touches them.

Discard personal chat, uncommented reposts, event promotion with no issue
content, and purely devotional posts.

STEP 3. Group what remains into stories and return them.

You are monitoring on behalf of Christian Concern, a UK Christian legal and
advocacy charity. If step 1 returned posts but none survive step 2, return
an empty list — that is a valid answer. Do not skip step 1.
""") + "\n" + STORY_SCHEMA


def issue_prompt(cluster: dict, hours: int) -> str:
    return collapse_ws(f"""
You are monitoring X for Christian Concern, a UK Christian legal and
advocacy charity.

Search X for the last {hours} hours for significant activity relating to:
{cluster['terms']}.

Prioritise a UK context. Include international stories only where they are
being widely discussed by UK accounts or have direct UK relevance.

File each story under exactly one of: {', '.join(cluster['issues'])}.

I care about what is generating real activity — volume of posts, reach of
the accounts involved, speed of pickup — not merely what exists. A single
post from a small account is not a story.
""") + "\n" + STORY_SCHEMA


OPPORTUNITY_PROMPT = """
You are advising Christian Concern on where they have standing to comment.

Below are (A) stories currently active on X, and (B) short notes on live
Christian Legal Centre cases.

Identify only genuine connections: a story where CC represents someone this
happened to, or where a case is closely enough on point that CC can speak
with direct authority rather than as another commentator.

Reply with ONLY a JSON array. Report EXCEPTIONS ONLY — the matches. Do not
restate stories with no match.

[
  {
    "i": <story index>,
    "headline_echo": "first 8 words of that story's headline",
    "case": "the case name from the notes",
    "why": "one sentence on the connection",
    "strength": "direct" or "adjacent"
  }
]

"direct" means CC represents someone in materially the same situation.
"adjacent" means the case is relevant but the link needs explaining.
Be strict. A weak match is worse than no match — it wastes the team's time
and risks a claim CC cannot substantiate. If there are no matches, return [].
"""


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------

def gather(api_key: str, hours: int, verbose: bool = True) -> tuple[list, list]:
    """Run sphere watch and issue watch. Returns (stories, warnings)."""
    since = (now_utc() - timedelta(hours=hours)).strftime("%Y-%m-%d")
    jobs, meta = [], []

    handles_for: dict[str, list] = {}
    for tier in (CFG.HANDLE_TIERS if getattr(CFG, "SPHERE_WATCH_ENABLED", True)
                 else []):
        handles = tier_handles(tier)
        if not handles:
            continue

        if len(handles) > 20:
            raise SystemExit(
                f"Tier '{tier['label']}' has {len(handles)} handles. "
                "xAI caps allowed_x_handles at 20. Split it into another tier."
            )
        # Ask about a few accounts at a time. A single call covering a dozen
        # handles reliably came back empty; the same handles in small batches
        # return posts. Costs a few more searches and is worth it.
        size = max(1, CFG.SPHERE_BATCH_SIZE)
        batches = [handles[i:i + size] for i in range(0, len(handles), size)]
        for n, batch in enumerate(batches, 1):
            label = (tier["label"] if len(batches) == 1
                     else f"{tier['label']} {n}/{len(batches)}")
            jobs.append((sphere_prompt(tier, hours), api_key, batch, since))
            meta.append({"kind": "sphere", "label": label, "vip": tier["vip"],
                         "tier": tier["label"]})
            handles_for[label] = batch

    for cluster in CFG.ISSUE_CLUSTERS:
        jobs.append((issue_prompt(cluster, hours), api_key, None, since))
        meta.append({"kind": "issue", "label": cluster["label"], "vip": False,
                     "issues": cluster["issues"]})

    if len(jobs) > CFG.MAX_SEARCH_CALLS_PER_RUN:
        raise SystemExit(
            f"{len(jobs)} searches exceeds MAX_SEARCH_CALLS_PER_RUN "
            f"({CFG.MAX_SEARCH_CALLS_PER_RUN}). Refusing to run."
        )

    if verbose:
        print(f"[gather] {len(jobs)} searches, since {since}, "
              f"{MAX_CONCURRENCY} concurrent", flush=True)

    def worker(prompt, key, handles, from_date):
        return call_grok(
            prompt, key,
            allowed_handles=handles,
            from_date=from_date,
            model=CFG.GATHER_MODEL,
            effort=CFG.GATHER_EFFORT,
        )

    started = time.time()
    results = run_parallel(jobs, worker)
    if verbose:
        print(f"[gather] done in {time.time() - started:.0f}s", flush=True)

    stories, warnings = [], []
    dropped_urls = 0
    failed_calls = 0
    retries_spent = 0
    for (result, error), m in zip(results, meta):
        tag = f"{m['kind']}/{m['label']}"
        if error:
            failed_calls += 1
            warnings.append(f"{tag}: call failed — {error}")
            continue
        if result["degraded"]:
            # A wrong handle and a genuinely quiet account produce the same
            # empty result on a 12-hour window. Retry once over 7 days to
            # tell them apart, so the warning says something useful.
            verdict = "unknown"
            if m["kind"] == "sphere" and retries_spent < getattr(
                    CFG, "MAX_DEGRADED_RETRIES_PER_RUN", 2):
                retries_spent += 1
                try:
                    wide = call_grok(
                        "List the three most recent posts from each of these "
                        "accounts. Give the date and first few words of each.",
                        api_key,
                        allowed_handles=handles_for.get(m["label"], []),
                        from_date=(now_utc() - timedelta(days=7)).strftime("%Y-%m-%d"),
                        model=CFG.GATHER_MODEL, effort="low")
                    verdict = "quiet" if wide["citations"] else "bad-handles"
                except Exception:  # noqa: BLE001
                    verdict = "unknown"
            if verdict == "quiet":
                warnings.append(
                    f"{tag}: accounts are live but nothing on-topic in the "
                    f"last {hours}h. Handles are fine.")
            elif verdict == "bad-handles":
                warnings.append(
                    f"{tag}: search returned nothing, twice. If these handles "
                    "passed verify-handles then they are fine and the search "
                    "itself is being unreliable — see the note on account "
                    "retrieval in the README.")
            else:
                warnings.append(
                    f"{tag}: discarded as unsourced ({result['degraded_reason']}).")
            continue
        parsed = parse_json_block(result["text"])
        if not isinstance(parsed, dict) or "stories" not in parsed:
            warnings.append(f"{tag}: reply was not parseable JSON, skipped")
            continue

        # Match citations by POST ID, not by URL string. Grok cites a post
        # as x.com/<username>/status/<id> but writes x.com/i/status/<id> in
        # its JSON — the same post by two different paths. String matching
        # therefore never matched anything, which silently emptied every
        # story of its links.
        allowed = set(result["citations"])
        allowed_ids = {MX.extract_id(u) for u in allowed}
        allowed_ids.discard(None)
        by_id = {}
        for u in allowed:
            pid = MX.extract_id(u)
            if pid and pid not in by_id:
                by_id[pid] = u

        for raw in parsed.get("stories") or []:
            if not isinstance(raw, dict) or not raw.get("headline"):
                continue
            urls = [u for u in (raw.get("post_urls") or []) if isinstance(u, str)]
            # Only keep URLs that actually appeared in the citation channel.
            # Anything else is the model reconstructing a plausible link.
            # Only keep URLs that came back through the citation channel
            # AND were named for THIS story. An earlier version fell back to
            # "any citation from this call" when nothing matched, which
            # stapled other stories' posts onto the wrong headline. Showing
            # no link is correct; showing a wrong one is actively misleading.
            verified, uncited = [], 0
            for u in urls:
                pid = MX.extract_id(u)
                if pid and pid in allowed_ids:
                    # Prefer the cited form: it carries the username, which
                    # makes a wrong attribution visible at a glance.
                    verified.append(by_id[pid])
                else:
                    uncited += 1
            dropped_urls += uncited
            try:
                heat = max(1, min(5, int(raw.get("heat") or 1)))
            except (TypeError, ValueError):
                heat = 1
            stories.append({
                "headline": collapse_ws(raw.get("headline"))[:200],
                "summary": collapse_ws(raw.get("summary")),
                "issue": canonical_issue(
                    raw.get("issue"), m.get("issues"),
                    context=f"{raw.get('headline','')} {raw.get('summary','')}"),
                "also_touches": canonical_issue(
                    raw.get("also_touches"), strict=True),
                "scope": "International"
                         if str(raw.get("scope", "")).lower().startswith("int")
                         else "UK",
                "accounts": [collapse_ws(a).lstrip("@")
                             for a in (raw.get("accounts") or [])
                             if isinstance(a, str)][:8],
                "post_urls": verified[:5],
                "heat": heat,
                "heat_reason": collapse_ws(raw.get("heat_reason")),
                "engagement_note": collapse_ws(raw.get("engagement_note")),
                "source_kind": m["kind"],
                "source_label": m["label"],
                "vip": m["vip"],
                "found_at": now_utc().isoformat(),
            })

    # A run where every search errored is broken, not quiet. Reporting
    # "nothing to report" and exiting green is the worst outcome: it looks
    # like a healthy quiet day for as long as it takes someone to notice the
    # digests stopped arriving.
    if jobs and failed_calls == len(jobs):
        raise GrokError(
            f"ALL {failed_calls} searches failed. This is a broken run, not a "
            f"quiet one. First error: {warnings[0] if warnings else 'unknown'}")
    if failed_calls:
        warnings.insert(0, f"{failed_calls} of {len(jobs)} searches failed.")

    linked = sum(1 for s in stories if s["post_urls"])
    if stories and not linked:
        warnings.append(
            "NO story has a verified post link. Every URL the model gave was "
            "absent from its own citations, which usually means the citation "
            "format changed. Engagement cannot be measured without links.")
    elif dropped_urls:
        warnings.append(
            f"{dropped_urls} post URL(s) dropped as uncited "
            f"({linked}/{len(stories)} stories still have links).")

    return stories, warnings


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedupe(stories: list) -> list:
    """Merge stories sharing a post URL.

    Deliberately NOT word-overlap matching. Measured on real headlines,
    Jaccard for the same story across outlets (0.12-0.43) overlaps with
    Jaccard for genuinely different stories (0.38). No threshold separates
    them. Shared post URLs are exact evidence; anything subtler is left to
    the synthesis pass, which sees the whole list at once.
    """
    merged: list = []
    by_url: dict[str, int] = {}

    for story in stories:
        hit = None
        for url in story["post_urls"]:
            key = normalise_post_url(url)
            if key in by_url:
                hit = by_url[key]
                break
        if hit is None:
            idx = len(merged)
            merged.append(story)
            for url in story["post_urls"]:
                by_url[normalise_post_url(url)] = idx
            continue

        target = merged[hit]
        target["heat"] = max(target["heat"], story["heat"])
        target["vip"] = target["vip"] or story["vip"]
        for url in story["post_urls"]:
            key = normalise_post_url(url)
            if key not in by_url:
                by_url[key] = hit
                target["post_urls"].append(url)
        for acct in story["accounts"]:
            if acct not in target["accounts"]:
                target["accounts"].append(acct)
        if story["source_label"] not in target["source_label"]:
            target["source_label"] += f" + {story['source_label']}"

    return merged


def filter_seen(stories: list) -> tuple[list, dict]:
    """Drop stories whose posts we have already reported."""
    seen = load_json(SEEN_POSTS, {})
    cutoff = (now_utc() - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    fresh = []
    for story in stories:
        keys = [normalise_post_url(u) for u in story["post_urls"]]
        keys = [k for k in keys if k]
        if keys and all(k in seen for k in keys):
            continue
        fresh.append(story)
        stamp = now_utc().isoformat()
        for k in keys:
            seen.setdefault(k, stamp)
    return fresh, seen


# ---------------------------------------------------------------------------
# Heat layer
# ---------------------------------------------------------------------------

def update_heat(stories: list) -> tuple[dict, list]:
    """Record this run's per-issue metrics and detect spikes.

    Grok does not return mention counts, so the metric is built here: for
    each issue, how many distinct stories, how many distinct posts, and the
    maximum heat Grok assigned. Over time these become a baseline you own.

    Until MIN_BASELINE_SAMPLES readings exist for an issue, spike detection
    reports "baseline building" rather than a false negative.
    """
    history = load_json(HEAT_HISTORY, {"runs": []})
    stamp = now_utc().isoformat()

    blank = {"stories": 0, "posts": 0, "max_heat": 0,
             "impressions": 0, "engagement": 0, "score": 0}
    current: dict[str, dict] = {i: dict(blank) for i in CFG.ISSUES}
    for story in stories:
        bucket = current.setdefault(story["issue"], dict(blank))
        bucket["stories"] += 1
        bucket["posts"] += len(story["post_urls"])
        bucket["max_heat"] = max(bucket["max_heat"], story["heat"])
        m = story.get("metrics")
        if m:
            bucket["impressions"] += m["impressions"]
            bucket["engagement"] += (m["likes"] + m["reposts"]
                                     + m["replies"] + m["quotes"])
            bucket["score"] += m["score"]

    cutoff = now_utc() - timedelta(days=CFG.BASELINE_DAYS)
    prior = []
    for run in history.get("runs", []):
        try:
            when = datetime.fromisoformat(run["at"])
        except (KeyError, ValueError):
            continue
        if when >= cutoff:
            prior.append(run)

    # Prefer impressions once they exist. Story count is a coarse proxy:
    # three small stories and one story with a million views both read as
    # activity, and only one of them is. Falls back automatically when
    # metrics are unavailable or history predates them.
    metric_key = "impressions"
    if not any(m.get("impressions") for m in current.values()):
        metric_key = "stories"

    spikes = []
    for issue, metrics in current.items():
        series = [r["issues"].get(issue, {}).get(metric_key, 0)
                  for r in prior if "issues" in r]
        series = [v for v in series if isinstance(v, (int, float))]
        if len(series) < CFG.MIN_BASELINE_SAMPLES:
            metrics["status"] = "baseline building"
            metrics["samples"] = len(series)
            continue
        # A history of all zeros is not a baseline. Runs recorded before
        # engagement metrics existed have impressions of 0, so the first
        # measured run reads as an explosion against them. Require some
        # real history in the metric before trusting a comparison.
        if metric_key != "stories" and not any(series):
            metrics["status"] = "baseline building (no measured history)"
            metrics["samples"] = 0
            continue
        mean = statistics.fmean(series)
        sd = statistics.pstdev(series)
        threshold = mean + CFG.SD_MULTIPLIER * sd
        metrics["mean"] = round(mean, 2)
        metrics["threshold"] = round(threshold, 2)
        metrics["samples"] = len(series)
        count = metrics[metric_key]
        floor_ok = (metrics["stories"] >= CFG.MIN_SPIKE_STORIES
                    if metric_key == "stories"
                    else count >= CFG.MIN_SPIKE_IMPRESSIONS)
        if count > threshold and floor_ok and count >= mean + CFG.MIN_SPIKE_ABSOLUTE:
            metrics["status"] = "SPIKE"
            spikes.append({
                "issue": issue,
                "metric": metric_key,
                "count": count,
                "stories": metrics["stories"],
                "mean": round(mean, 2),
                "threshold": round(threshold, 2),
            })
        else:
            metrics["status"] = "normal"

    history.setdefault("runs", []).append({"at": stamp, "issues": current})
    keep = now_utc() - timedelta(days=HEAT_RETENTION_DAYS)
    history["runs"] = [
        r for r in history["runs"]
        if datetime.fromisoformat(r["at"]) >= keep
    ]
    save_json(HEAT_HISTORY, history)
    return current, spikes


# ---------------------------------------------------------------------------
# Opportunity detection
# ---------------------------------------------------------------------------

def load_case_notes() -> dict:
    """Reuse CASE_NOTES from the existing news monitor if it is importable."""
    try:
        sys.path.insert(0, str(REPO))
        from monitor import CASE_NOTES          # type: ignore
        if isinstance(CASE_NOTES, dict) and CASE_NOTES:
            return CASE_NOTES
    except Exception:                            # noqa: BLE001
        pass
    fallback = load_json(REPO / "case_notes.json", {})
    return fallback if isinstance(fallback, dict) else {}


def find_opportunities(stories: list, api_key: str) -> tuple[list, list]:
    """Match hot stories to live CLC cases. Exceptions only."""
    warnings = []
    cases = load_case_notes()
    if not cases:
        warnings.append(
            "No CASE_NOTES found. Put x_monitor.py beside monitor.py, or "
            "drop a case_notes.json next to it. Opportunity flags are off."
        )
        return [], warnings
    if not stories:
        return [], warnings

    lines = []
    for i, s in enumerate(stories):
        lines.append(collapse_ws(
            f"{i}. [{s['issue']}] {s['headline']} — {s['summary']}"))
    case_lines = []
    for name, note in cases.items():
        case_lines.append(collapse_ws(f"- {name}: {note}"))

    prompt = (
        OPPORTUNITY_PROMPT
        + "\n\n(A) STORIES\n" + "\n".join(lines)
        + "\n\n(B) CASES\n" + "\n".join(case_lines)
    )

    try:
        result = call_grok(
            prompt, api_key,
            model=CFG.SYNTHESIS_MODEL,
            effort=CFG.SYNTHESIS_EFFORT,
            use_search=False,
        )
    except GrokError as exc:
        warnings.append(f"opportunity pass failed — {exc}")
        return [], warnings

    parsed = parse_json_block(result["text"])
    if not isinstance(parsed, list):
        warnings.append("opportunity pass returned unparseable JSON, skipped")
        return [], warnings

    matches = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("i")
        if not isinstance(idx, int) or not 0 <= idx < len(stories):
            continue
        # Index echo check. Models drift on numeric indices; verifying the
        # echoed headline against the index catches it. This has fired in
        # real runs on the news monitor.
        echo = collapse_ws(item.get("headline_echo", "")).lower()
        actual = collapse_ws(stories[idx]["headline"]).lower()
        if echo and not actual.startswith(echo[:20]):
            hit = next((j for j, s in enumerate(stories)
                        if collapse_ws(s["headline"]).lower().startswith(echo[:20])),
                       None)
            if hit is None:
                warnings.append(
                    f"opportunity match dropped: index {idx} disagreed with "
                    f"its headline echo")
                continue
            idx = hit
        matches.append({
            "story": idx,
            "case": collapse_ws(item.get("case")),
            "why": collapse_ws(item.get("why")),
            "strength": "direct" if str(item.get("strength", "")).lower()
                        == "direct" else "adjacent",
        })
    return matches, warnings


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def vip_handles() -> set[str]:
    out = set()
    for tier in CFG.HANDLE_TIERS:
        if tier.get("vip"):
            out.update(h.lower() for h in tier_handles(tier))
    return out


def apply_vip_flags(stories: list) -> None:
    """A story is VIP because a VIP account is in it, not because the VIP
    search returned it. Without this, any story a Tier-1 search happens to
    surface gets starred, and the section fills with noise."""
    vips = vip_handles()
    for story in stories:
        accounts = {a.lower() for a in story["accounts"]}
        in_urls = any(
            f"x.com/{v}/" in normalise_post_url(u)
            for u in story["post_urls"] for v in vips
        )
        story["vip"] = bool(accounts & vips) or in_urls


def measure(stories: list) -> list:
    """Attach real engagement numbers to each story, and derive heat from
    them rather than from Grok's estimate.

    Grok's heat is a guess restated differently each run. A measured score
    is stable, comparable between issues, and can be tracked over time.
    Grok's number is kept as `grok_heat` so you can see how far off it was.
    """
    warnings: list[str] = []
    all_urls = [u for s in stories for u in s["post_urls"]]

    # Re-read posts first seen on earlier runs, so growth can be computed.
    tracked, vw = track_velocity(stories)
    warnings.extend(vw)
    all_urls += [f"https://x.com/i/status/{pid}" for pid in tracked]

    metrics, mw = MX.fetch(all_urls, CFG.METRICS_PROVIDER)
    warnings.extend(mw)
    growth = record_posts(metrics) if metrics else {}

    for story in stories:
        found = []
        if story.get("source_kind") == "timeline" and story.get("per_post"):
            for pid, post in story["per_post"].items():
                metrics.setdefault(pid, post)
        for url in story["post_urls"]:
            pid = MX.extract_id(url)
            if pid and pid in metrics:
                found.append(metrics[pid])
        story["grok_heat"] = story["heat"]
        if not found:
            story["metrics"] = None
            story["measured"] = False
            continue
        story["metrics"] = MX.summarise(found)
        story["measured"] = True
        story["per_post"] = {
            pid: metrics[pid] for pid in
            (MX.extract_id(u) for u in story["post_urls"])
            if pid and pid in metrics
        }
        gained = sum(growth.get(pid, {}).get("impressions", 0)
                     for pid in story["per_post"])
        story["growth"] = gained if gained else None
        # Name the real authors. Grok's account list is often approximate.
        real = [m["author"] for m in found if m.get("author")]
        if real:
            story["accounts"] = list(dict.fromkeys(real + story["accounts"]))[:8]

    measured = [s for s in stories if s["measured"]]
    if measured:
        scores = sorted(s["metrics"]["score"] for s in measured)
        for story in measured:
            story["heat"] = score_to_heat(story["metrics"]["score"], scores)
    if len(measured) < len(stories):
        warnings.append(
            f"{len(stories) - len(measured)} of {len(stories)} stories have "
            "no measured engagement; those keep Grok's heat estimate.")
    return warnings


def track_velocity(stories: list) -> tuple[list, list]:
    """Re-read posts seen on earlier runs so a level becomes a rate.

    Measuring once, at discovery, is why a post read 55 impressions: it was
    minutes old. Reading the same post again next run gives the growth since,
    which is what "trending" means. Returns (extra_ids, warnings).
    """
    warnings: list[str] = []
    if not getattr(CFG, "TRACK_POST_VELOCITY", False):
        return [], warnings

    history = load_json(POST_HISTORY, {})
    cutoff = now_utc() - timedelta(hours=CFG.VELOCITY_WINDOW_HOURS)
    live = {}
    for pid, record in history.items():
        try:
            first = datetime.fromisoformat(record["first_seen"])
        except (KeyError, ValueError):
            continue
        if first >= cutoff:
            live[pid] = record

    current = {MX.extract_id(u) for s in stories for u in s["post_urls"]}
    current.discard(None)

    # Skip posts read recently. Impressions move over hours, not minutes, so
    # re-reading a post 90 minutes after the last reading costs money and
    # tells you nothing.
    gap = timedelta(hours=getattr(CFG, "VELOCITY_MIN_HOURS_BETWEEN_READS", 6))
    ready = now_utc() - gap
    extra = []
    for pid, record in live.items():
        if pid in current:
            continue
        try:
            last = datetime.fromisoformat(record.get("last_seen", ""))
        except ValueError:
            extra.append(pid)
            continue
        if last <= ready:
            extra.append(pid)
    if len(extra) > CFG.MAX_TRACKED_POSTS:
        # Newest first — a three-day-old post has stopped moving anyway.
        extra.sort(key=lambda p: live[p].get("first_seen", ""), reverse=True)
        warnings.append(f"velocity: tracking the {CFG.MAX_TRACKED_POSTS} most "
                        f"recent of {len(extra)} eligible posts")
        extra = extra[:CFG.MAX_TRACKED_POSTS]
    return extra, warnings


def record_posts(metrics: dict) -> dict:
    """Store this run's readings and return growth since the last one."""
    history = load_json(POST_HISTORY, {})
    stamp = now_utc().isoformat()
    growth = {}
    for pid, m in metrics.items():
        record = history.get(pid)
        if record and record.get("last_impressions") is not None:
            growth[pid] = {
                "impressions": m["impressions"] - record["last_impressions"],
                "since": record.get("last_seen", ""),
            }
            record["last_impressions"] = m["impressions"]
            record["last_seen"] = stamp
        else:
            history[pid] = {"first_seen": stamp, "last_seen": stamp,
                            "last_impressions": m["impressions"]}
    cutoff = (now_utc() - timedelta(hours=CFG.VELOCITY_WINDOW_HOURS)).isoformat()
    history = {k: v for k, v in history.items()
               if v.get("first_seen", "") >= cutoff}
    save_json(POST_HISTORY, history)
    return growth


GROUNDING_PROMPT = """
Below are stories with the ACTUAL TEXT of the posts they are based on.

Rewrite each summary using only what those posts actually say. Earlier
summaries were written from recollection and contradicted each other on
basic facts — one said a bill was defeated in the Commons, another said the
Lords blocked it. Only one can be true, and the post text is the evidence.

Rules:
- State only what the post text supports. If the posts do not establish a
  detail, leave it out rather than filling it in.
- If the posts disagree with each other, say so plainly in the summary.
- Two or three sentences. Your own words — do not quote at length.
- Correct the headline too if the post text shows it is wrong.

Return ONLY a JSON array:
[{"i": <index>, "headline_echo": "first 8 words of the headline given",
  "headline": "corrected or unchanged", "summary": "rewritten",
  "confidence": "supported"|"partly supported"|"unsupported"}]

Mark "unsupported" if the post text does not back the story at all. That is
a useful answer, not a failure.
"""


def ground_summaries(stories: list, api_key: str) -> list:
    """Rewrite summaries from the real post text fetched via the X API."""
    warnings: list[str] = []
    if not getattr(CFG, "GROUND_SUMMARIES", False):
        return warnings

    usable = [(i, s) for i, s in enumerate(stories)
              if any((s.get("per_post") or {}).get(pid, {}).get("text")
                     for pid in (s.get("per_post") or {}))]
    if not usable:
        warnings.append("grounding skipped: no post text available "
                        "(needs X_BEARER_TOKEN and measured posts)")
        return warnings

    blocks = []
    for i, s in usable:
        lines = [collapse_ws(f"{i}. HEADLINE: {s['headline']}"),
                 collapse_ws(f"   CURRENT SUMMARY: {s['summary']}")]
        for pid, m in (s.get("per_post") or {}).items():
            if m.get("text"):
                lines.append(collapse_ws(
                    f"   POST by @{m.get('author','?')}: {m['text']}"))
        blocks.append("\n".join(lines))

    try:
        result = call_grok(GROUNDING_PROMPT + "\n\n" + "\n\n".join(blocks),
                           api_key, model=CFG.SYNTHESIS_MODEL,
                           effort=CFG.SYNTHESIS_EFFORT, use_search=False)
    except GrokError as exc:
        warnings.append(f"grounding pass failed — {exc}")
        return warnings

    parsed = parse_json_block(result["text"])
    if not isinstance(parsed, list):
        warnings.append("grounding pass returned unparseable JSON, skipped")
        return warnings

    rewritten = 0
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("i")
        if not isinstance(idx, int) or not 0 <= idx < len(stories):
            continue
        echo = collapse_ws(item.get("headline_echo", "")).lower()
        actual = collapse_ws(stories[idx]["headline"]).lower()
        if echo and not actual.startswith(echo[:20]):
            warnings.append(f"grounding: index {idx} disagreed with its "
                            "headline echo, left unchanged")
            continue
        conf = collapse_ws(item.get("confidence")).lower()
        if conf.startswith("unsupported"):
            stories[idx]["unsupported"] = True
            warnings.append(
                f"UNSUPPORTED by post text: {stories[idx]['headline'][:60]}")
            continue
        if collapse_ws(item.get("summary")):
            stories[idx]["summary"] = collapse_ws(item["summary"])
            stories[idx]["grounded"] = True
            rewritten += 1
        if collapse_ws(item.get("headline")):
            stories[idx]["headline"] = collapse_ws(item["headline"])[:200]
        if conf.startswith("partly"):
            stories[idx]["partly_supported"] = True

    if rewritten:
        print(f"[ground] {rewritten}/{len(stories)} summaries rewritten "
              "from real post text")
    return warnings


def timeline_watch(api_key: str, hours: int) -> tuple[list, list]:
    """Read named tiers' timelines straight from the X API, then classify.

    Replaces sphere watch for the accounts that matter. No model decides
    whether to search, so an account that posted cannot be silently missed.
    """
    warnings: list[str] = []
    tiers = getattr(CFG, "TIMELINE_TIERS", []) or []
    if not tiers:
        return [], warnings
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        return [], ["timeline watch needs X_BEARER_TOKEN, skipped"]

    handles, vip_labels = [], {}
    for tier in CFG.HANDLE_TIERS:
        if tier["label"] in tiers:
            for h in tier_handles(tier):
                handles.append(h)
                vip_labels[h.lower()] = tier
    if not handles:
        warnings.append(f"TIMELINE_TIERS names no tier that exists: {tiers}")
        return [], warnings

    since = (now_utc() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    posts, w = MX.fetch_timelines(handles, token, since,
                                  CFG.TIMELINE_MAX_POSTS_PER_ACCOUNT)
    warnings.extend(w)
    if not posts:
        warnings.append(f"timeline watch: no posts from {len(handles)} "
                        f"account(s) in the last {hours}h")
        return [], warnings
    print(f"[timeline] {len(posts)} posts from {len(handles)} accounts")

    listing = []
    for n, post in enumerate(posts):
        listing.append(collapse_ws(
            f"{n}. @{post['author']} ({post['impressions']} impressions): "
            f"{post['text']}"))

    prompt = collapse_ws(f"""
Below are real posts, already retrieved, from accounts connected to
Christian Concern, a UK Christian legal and advocacy charity.

Group them into stories. Keep only posts bearing on any of:
{', '.join(CFG.ISSUES)}, or UK politics and government where it touches them.
Discard personal chat, event promotion with no issue content, and purely
devotional posts.

Every fact in your summary must come from the post text below. Do not add
context you are recalling from elsewhere.
""") + "\n" + STORY_SCHEMA + (
        '\n\nUse the post numbers given. In post_urls, list the NUMBERS of '
        'the posts for each story, as strings, not URLs.\n\n'
        + "\n".join(listing))

    try:
        result = call_grok(prompt, api_key, model=CFG.GATHER_MODEL,
                           effort=CFG.GATHER_EFFORT, use_search=False)
    except GrokError as exc:
        return [], warnings + [f"timeline classification failed — {exc}"]

    parsed = parse_json_block(result["text"])
    if not isinstance(parsed, dict):
        return [], warnings + ["timeline classification returned bad JSON"]

    stories = []
    for raw in parsed.get("stories") or []:
        if not isinstance(raw, dict) or not raw.get("headline"):
            continue
        urls, per_post, accounts = [], {}, []
        for ref in raw.get("post_urls") or []:
            try:
                n = int(str(ref).strip())
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(posts):
                post = posts[n]
                urls.append(post["url"])
                per_post[post["id"]] = post
                if post["author"] not in accounts:
                    accounts.append(post["author"])
        if not urls:
            continue
        tier = vip_labels.get((accounts[0] if accounts else "").lower())
        stories.append({
            "headline": collapse_ws(raw.get("headline"))[:200],
            "summary": collapse_ws(raw.get("summary")),
            "issue": canonical_issue(
                raw.get("issue"),
                context=f"{raw.get('headline','')} {raw.get('summary','')}"),
            "also_touches": canonical_issue(raw.get("also_touches"), strict=True),
            "scope": "International" if str(raw.get("scope", "")).lower()
                     .startswith("int") else "UK",
            "accounts": accounts[:8],
            "post_urls": urls[:5],
            "heat": 1,
            "heat_reason": "",
            "engagement_note": "",
            # Attach the metrics we already have. Timeline posts arrive
            # with their numbers included, so measure() must not pay to
            # fetch them a second time.
            "per_post": per_post,
            "source_kind": "timeline",
            "source_label": "Account timelines",
            "vip": bool(tier and tier.get("vip")),
            "found_at": now_utc().isoformat(),
        })
    return stories, warnings


def score_to_heat(value: int, distribution: list[int]) -> int:
    """Map a weighted engagement score to 1-5 against absolute thresholds.

    Deliberately absolute, not a percentile of this run. A percentile would
    always produce a 5 even on a dead day, which is exactly the false
    signal the heat number exists to avoid.
    """
    for threshold, heat in CFG.HEAT_THRESHOLDS:
        if value >= threshold:
            return heat
    return 1


SECTIONS = [
    ("LIFE", ["Beginning of life", "End of life"]),
    ("FAMILY", ["Marriage", "Gender", "Sexuality", "Education"]),
    ("FREEDOM", ["Christian freedoms", "Freedom of speech"]),
    ("FOUNDATIONS", ["Christian society", "Secularism", "Islam", "Church"]),
]


def rank(stories: list, opportunities: list) -> list:
    """Order stories. Case linkage outranks reach — same rule as the news
    monitor. 'We represent someone this happened to' beats commentary."""
    by_story = {}
    for opp in opportunities:
        cur = by_story.get(opp["story"])
        if cur is None or (cur == "adjacent" and opp["strength"] == "direct"):
            by_story[opp["story"]] = opp["strength"]

    def key(pair):
        i, s = pair
        link = by_story.get(i)
        link_score = 2 if link == "direct" else 1 if link == "adjacent" else 0
        measured = (s.get("metrics") or {}).get("score", 0)
        # Reach outranks the VIP flag. VIP already guarantees a story appears
        # — it has its own section and bypasses the heat threshold — so it
        # does not also need rank priority. Ordering by reach instead means
        # the biggest post leads both the starred section and each issue
        # section, rather than a minor CC tweet burying a national story.
        return (-link_score, -measured, -s["heat"], -int(s["vip"]),
                s["headline"])

    ordered = sorted(enumerate(stories), key=key)
    return [i for i, _ in ordered]


def link_line(story, url: str) -> str:
    """A post link annotated with who actually wrote it.

    Grok attributes posts to stories loosely. Printing the real author and
    reach next to each link makes a bad attribution obvious at a glance
    instead of invisible.
    """
    per = (story.get("per_post") or {}).get(MX.extract_id(url) or "")
    if not per:
        return url
    who = f"@{per['author']}" if per.get("author") else "unknown"
    return f"{url} — {who}, {MX.human(per.get('impressions', 0))} impressions"


def freshness_note(story) -> str:
    """Warn when a story's reach was measured minutes after it was posted.

    Impressions accrue over hours. Measuring at discovery systematically
    under-reports fresh posts: a Right To Life post read 55 impressions on
    one run and 63 three minutes later, because it was brand new. Reporting
    that as low reach is misleading, so the digest says so instead.
    """
    per = story.get("per_post") or {}
    ages = []
    for m in per.values():
        stamp = m.get("created_at")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        ages.append((now_utc() - when).total_seconds() / 3600)
    if not ages:
        return ""
    youngest = min(ages)
    if youngest < 2:
        return (f" · ⏱ posted {int(youngest * 60)} min ago — reach still "
                "climbing, treat as provisional")
    return ""


def metric_line(story) -> str:
    """One line of measured engagement, or an honest note that there is none."""
    m = story.get("metrics")
    if not m:
        return (f"heat {story['heat']}/5 (ESTIMATE ONLY — not comparable with "
                f"measured figures above) · {story['issue']}")
    return (f"**{MX.human(m['impressions'])} impressions** · "
            f"{MX.human(m['likes'])} likes · {MX.human(m['reposts'])} reposts · "
            f"{MX.human(m['replies'])} replies · heat {story['heat']}/5 "
            f"(measured) · {story['issue']}"
            + (f" · 📈 +{MX.human(story['growth'])} since last run"
               if story.get("growth") else "")
            + freshness_note(story))


def render_markdown(stories, order, opportunities, heat, spikes,
                    warnings, hours) -> str:
    opp_by_story: dict[int, list] = {}
    for opp in opportunities:
        opp_by_story.setdefault(opp["story"], []).append(opp)

    total_impressions = sum((s.get("metrics") or {}).get("impressions", 0)
                           for s in stories)
    header = (f"_Lookback {hours}h · {len(stories)} stories · "
              f"{sum(len(s['post_urls']) for s in stories)} posts")
    if total_impressions:
        header += f" · {MX.human(total_impressions)} impressions_"
    else:
        header += "_"
    out = [f"## X monitor — {uk_stamp()}", header, ""]

    if spikes:
        out.append("### 🔥 Spikes")
        for sp in spikes:
            if sp.get("metric") == "impressions":
                out.append(
                    f"- **{sp['issue']}** — {MX.human(sp['count'])} "
                    f"impressions across {sp['stories']} "
                    f"{'story' if sp['stories'] == 1 else 'stories'} "
                    f"(baseline {MX.human(int(sp['mean']))})")
            else:
                out.append(
                    f"- **{sp['issue']}** — {sp['count']} "
                    f"{'story' if sp['count'] == 1 else 'stories'} this run "
                    f"(baseline {sp['mean']}, threshold {sp['threshold']})")
        out.append("")

    direct = [i for i in order if any(o["strength"] == "direct"
                                      for o in opp_by_story.get(i, []))]
    if direct:
        out.append("### 🎯 Where we have standing")
        for i in direct:
            s = stories[i]
            out.append(f"**{s['headline']}**  ")
            out.append(metric_line(s))
            out.append("")
            out.append(s["summary"])
            for opp in opp_by_story[i]:
                out.append(f"- **{opp['case']}** ({opp['strength']}) — {opp['why']}")
            if s["accounts"]:
                out.append("Accounts: "
                           + ", ".join(f"@{a}" for a in s["accounts"][:5]))
            for url in s["post_urls"][:3]:
                out.append(f"- {url}")
            out.append("")

    vips = [i for i in order if stories[i]["vip"]]
    if vips:
        out.append("### ⭐ Our people")
        for i in vips:
            s = stories[i]
            link = s["post_urls"][0] if s["post_urls"] else ""
            who = ", ".join(f"@{a}" for a in s["accounts"][:3])
            bits = [f"- **{s['headline']}**"]
            if who:
                bits.append(f" — {who}")
            if link:
                bits.append(f" · [post]({link})")
            m = s.get("metrics")
            if m and m["impressions"]:
                bits.append(f" · {MX.human(m['impressions'])} impressions")
            out.append("".join(bits))
        out.append("")

    placed = set(direct) | set(vips)
    for section, issues in SECTIONS:
        picks = [i for i in order
                 if stories[i]["issue"] in issues
                 and i not in placed
                 and stories[i]["scope"] == "UK"
                 and (stories[i]["heat"] >= CFG.MIN_HEAT_FOR_DIGEST
                      or i in opp_by_story)]
        if not picks:
            continue
        out.append(f"### {section}")
        for i in picks:
            s = stories[i]
            out.append(f"**{s['headline']}**  ")
            extra = (f" (also {s['also_touches']})"
                     if s["also_touches"] and s["also_touches"] != s["issue"]
                     else "")
            out.append(metric_line(s) + extra)
            out.append("")
            out.append(s["summary"])
            if s.get("partly_supported"):
                out.append("_⚠ only partly supported by the post text — "
                           "check the posts before repeating this._")
            elif not s.get("grounded"):
                out.append("_⚠ summary not verified against post text._")
            if s["heat_reason"] and not s.get("measured"):
                out.append(f"_{s['heat_reason']}_")
            if s["accounts"]:
                out.append("Accounts: " + ", ".join(f"@{a}" for a in s["accounts"][:5]))
            for url in s["post_urls"][:3]:
                out.append(f"- {link_line(s, url)}")
            for opp in opp_by_story.get(i, []):
                out.append(f"- 🎯 {opp['case']} ({opp['strength']}) — {opp['why']}")
            out.append("")
        placed.update(picks)

    intl = [i for i in order if stories[i]["scope"] == "International"
            and i not in placed and stories[i]["heat"] >= CFG.MIN_HEAT_FOR_DIGEST]
    if intl:
        out.append("### 🌍 International")
        for i in intl:
            s = stories[i]
            m = s.get("metrics")
            reach = f" ({MX.human(m['impressions'])} impressions)" if m and m["impressions"] else ""
            out.append(f"- **{s['headline']}**{reach} — {s['summary']}")
        out.append("")

    out.append("### Heat table")
    out.append("| Issue | Stories | Impressions | Engagement | Status |")
    out.append("|---|---|---|---|---|")
    for issue in CFG.ISSUES:
        m = heat.get(issue, {})
        status = m.get("status", "—")
        if status == "baseline building":
            status = (f"baseline building "
                      f"({m.get('samples', 0)}/{CFG.MIN_BASELINE_SAMPLES})")
        out.append(f"| {issue} | {m.get('stories', 0)} "
                   f"| {MX.human(m.get('impressions', 0))} "
                   f"| {MX.human(m.get('engagement', 0))} | {status} |")
    out.append("")

    if warnings:
        out.append("### ⚠️ Warnings")
        for w in warnings:
            out.append(f"- {w}")
        out.append("")

    posts_read = sum(len(s["post_urls"]) for s in stories if s.get("measured"))
    metrics_cost = (posts_read * CFG.PRICE_PER_POST_READ
                    if CFG.METRICS_PROVIDER == "official"
                    else posts_read * CFG.PRICE_PER_POST_READ_THIRDPARTY)
    est = (_COST["search_calls"] * CFG.PRICE_PER_SEARCH_CALL
           + _COST["in_tokens"] / 1e6 * CFG.PRICE_IN_PER_MTOK
           + _COST["out_tokens"] / 1e6 * CFG.PRICE_OUT_PER_MTOK
           + metrics_cost)
    out.append(f"_{_COST['search_calls']} searches · {posts_read} posts measured "
               f"· {_COST['in_tokens']:,} in / {_COST['out_tokens']:,} out tokens "
               f"· approx ${est:.2f}_")
    return "\n".join(out)


HEAT_COLOURS = {5: "#b91c1c", 4: "#c2410c", 3: "#a16207", 2: "#4d7c0f", 1: "#64748b"}
SECTION_COLOURS = {"LIFE": "#b91c1c", "FAMILY": "#7c3aed", "FREEDOM": "#0369a1",
                   "FOUNDATIONS": "#047857"}


def esc(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _stat(value: str, label: str) -> str:
    return (f'<td style="padding:0 14px 0 0;white-space:nowrap">'
            f'<div style="font-size:15px;font-weight:700;color:#0f172a;'
            f'line-height:1.2">{esc(value)}</div>'
            f'<div style="font-size:10px;color:#94a3b8;text-transform:uppercase;'
            f'letter-spacing:.06em">{esc(label)}</div></td>')


def _story_html(story, opps) -> str:
    """One story as a card. Inline styles only — email clients strip
    stylesheets, and many still do not support flexbox, so layout is tables."""
    m = story.get("metrics")
    heat = story.get("heat", 1)
    colour = HEAT_COLOURS.get(heat, "#64748b")

    bits = [f'<div style="border-left:3px solid {colour};padding:2px 0 2px 14px;'
            f'margin:0 0 26px 0">']
    bits.append(f'<div style="font-size:17px;font-weight:700;color:#0f172a;'
                f'line-height:1.35;margin:0 0 8px">{esc(story["headline"])}</div>')

    tags = [f'<span style="background:{colour};color:#fff;font-size:10px;'
            f'font-weight:700;padding:2px 7px;border-radius:3px;'
            f'letter-spacing:.04em">HEAT {heat}/5</span>']
    tags.append(f'<span style="color:#64748b;font-size:11px;padding-left:8px">'
                f'{esc(story["issue"])}</span>')
    if story.get("also_touches") and story["also_touches"] != story["issue"]:
        tags.append(f'<span style="color:#94a3b8;font-size:11px"> · also '
                    f'{esc(story["also_touches"])}</span>')
    if not m:
        tags.append('<span style="color:#b45309;font-size:11px;padding-left:8px">'
                    '· estimate only, not measured</span>')
    bits.append('<div style="margin:0 0 10px">' + "".join(tags) + '</div>')

    if m:
        row = [_stat(MX.human(m["impressions"]), "impressions"),
               _stat(MX.human(m["likes"]), "likes"),
               _stat(MX.human(m["reposts"]), "reposts"),
               _stat(MX.human(m["replies"]), "replies")]
        if story.get("growth"):
            row.append(_stat("+" + MX.human(story["growth"]), "since last run"))
        bits.append('<table cellpadding="0" cellspacing="0" style="margin:0 0 '
                    '10px"><tr>' + "".join(row) + '</tr></table>')

    bits.append(f'<div style="font-size:14px;color:#334155;line-height:1.55;'
                f'margin:0 0 10px">{esc(story["summary"])}</div>')

    note = freshness_note(story).replace(" · ", "")
    if note:
        bits.append(f'<div style="font-size:11px;color:#b45309;margin:0 0 8px">'
                    f'{esc(note)}</div>')
    if story.get("partly_supported"):
        bits.append('<div style="font-size:11px;color:#b45309;margin:0 0 8px">'
                    '⚠ only partly supported by the post text — check before '
                    'repeating</div>')
    elif not story.get("grounded"):
        bits.append('<div style="font-size:11px;color:#94a3b8;margin:0 0 8px">'
                    'summary not verified against post text</div>')

    for opp in opps:
        bits.append(f'<div style="background:#fef3c7;border-radius:4px;'
                    f'padding:8px 10px;margin:0 0 8px;font-size:13px;'
                    f'color:#78350f"><strong>🎯 {esc(opp["case"])}</strong> '
                    f'({esc(opp["strength"])}) — {esc(opp["why"])}</div>')

    links = []
    for url in story["post_urls"][:3]:
        per = (story.get("per_post") or {}).get(MX.extract_id(url) or "", {})
        who = f'@{per["author"]}' if per.get("author") else "post"
        reach = (f' · {MX.human(per["impressions"])}'
                 if per.get("impressions") else "")
        links.append(f'<a href="{esc(url)}" style="color:#2563eb;'
                     f'text-decoration:none;font-size:12px;margin-right:14px">'
                     f'{esc(who)}{esc(reach)} ↗</a>')
    if links:
        bits.append('<div style="margin-top:8px">' + "".join(links) + '</div>')

    bits.append("</div>")
    return "".join(bits)


def render_html(stories, order, opportunities, heat, spikes, warnings,
                hours) -> str:
    """Build the email directly from the data.

    The previous version converted the markdown digest with a handful of
    regexes, which is why the output looked like a log file. Stories are the
    thing being read, so they get the space; diagnostics go to the bottom in
    small grey text."""
    opp_by_story = {}
    for opp in opportunities:
        opp_by_story.setdefault(opp["story"], []).append(opp)

    total_imp = sum((s.get("metrics") or {}).get("impressions", 0)
                    for s in stories)
    measured = sum(1 for s in stories if s.get("measured"))

    h = ['<!DOCTYPE html><html><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         '</head><body style="margin:0;padding:0;background:#f1f5f9">'
         '<div style="max-width:640px;margin:0 auto;padding:22px 18px;'
         'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,'
         'Helvetica,Arial,sans-serif;background:#ffffff">']

    h.append('<div style="border-bottom:2px solid #0f172a;padding-bottom:12px;'
             'margin-bottom:22px">'
             '<div style="font-size:20px;font-weight:800;color:#0f172a;'
             'letter-spacing:-.02em">X monitor</div>'
             f'<div style="font-size:12px;color:#64748b;margin-top:3px">'
             f'{esc(uk_stamp())} · last {hours}h · {len(stories)} '
             f'{"story" if len(stories) == 1 else "stories"}'
             + (f' · {MX.human(total_imp)} impressions' if total_imp else '')
             + '</div></div>')

    if spikes:
        rows = []
        for sp in spikes:
            if sp.get("metric") == "impressions":
                rows.append(f'<div style="font-size:13px;color:#7f1d1d;'
                            f'margin:3px 0"><strong>{esc(sp["issue"])}</strong> '
                            f'— {MX.human(sp["count"])} impressions, baseline '
                            f'{MX.human(int(sp["mean"]))}</div>')
            else:
                rows.append(f'<div style="font-size:13px;color:#7f1d1d;'
                            f'margin:3px 0"><strong>{esc(sp["issue"])}</strong> '
                            f'— {sp["count"]} stories, baseline {sp["mean"]}</div>')
        h.append('<div style="background:#fef2f2;border:1px solid #fecaca;'
                 'border-radius:6px;padding:12px 14px;margin:0 0 22px">'
                 '<div style="font-size:11px;font-weight:700;color:#b91c1c;'
                 'text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">'
                 '🔥 Running above baseline</div>' + "".join(rows) + '</div>')

    placed = set()
    direct = [i for i in order
              if any(o["strength"] == "direct" for o in opp_by_story.get(i, []))]
    if direct:
        h.append('<div style="font-size:12px;font-weight:800;color:#b45309;'
                 'text-transform:uppercase;letter-spacing:.08em;'
                 'margin:0 0 14px">🎯 Where we have standing</div>')
        for i in direct:
            h.append(_story_html(stories[i], opp_by_story.get(i, [])))
        placed.update(direct)

    vips = [i for i in order if stories[i]["vip"] and i not in placed]
    if vips:
        h.append('<div style="font-size:12px;font-weight:800;color:#0f172a;'
                 'text-transform:uppercase;letter-spacing:.08em;'
                 'margin:0 0 14px">⭐ Our people</div>')
        for i in vips:
            h.append(_story_html(stories[i], opp_by_story.get(i, [])))
        placed.update(vips)

    for section, issues in SECTIONS:
        picks = [i for i in order if stories[i]["issue"] in issues
                 and i not in placed and stories[i]["scope"] == "UK"
                 and (stories[i]["heat"] >= CFG.MIN_HEAT_FOR_DIGEST
                      or i in opp_by_story)]
        if not picks:
            continue
        colour = SECTION_COLOURS.get(section, "#0f172a")
        h.append(f'<div style="font-size:12px;font-weight:800;color:{colour};'
                 f'text-transform:uppercase;letter-spacing:.08em;'
                 f'margin:0 0 14px">{esc(section)}</div>')
        for i in picks:
            h.append(_story_html(stories[i], opp_by_story.get(i, [])))
        placed.update(picks)

    intl = [i for i in order if stories[i]["scope"] == "International"
            and i not in placed]
    if intl:
        h.append('<div style="font-size:12px;font-weight:800;color:#475569;'
                 'text-transform:uppercase;letter-spacing:.08em;'
                 'margin:0 0 14px">🌍 International</div>')
        for i in intl:
            h.append(_story_html(stories[i], opp_by_story.get(i, [])))

    # Only issues with activity. Twelve rows of zeroes told nobody anything.
    active = [(k, v) for k, v in heat.items() if v.get("stories")]
    if active:
        cells = []
        for issue, met in sorted(active, key=lambda x: -x[1].get("impressions", 0)):
            cells.append(
                f'<tr><td style="padding:5px 10px 5px 0;font-size:13px;'
                f'color:#334155">{esc(issue)}</td>'
                f'<td style="padding:5px 10px;font-size:13px;color:#0f172a;'
                f'font-weight:600;text-align:right">'
                f'{MX.human(met.get("impressions", 0))}</td>'
                f'<td style="padding:5px 0 5px 10px;font-size:11px;'
                f'color:#94a3b8">{met.get("stories", 0)} '
                f'{"story" if met.get("stories") == 1 else "stories"}</td></tr>')
        h.append('<div style="border-top:1px solid #e2e8f0;margin-top:8px;'
                 'padding-top:16px">'
                 '<div style="font-size:11px;font-weight:700;color:#64748b;'
                 'text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">'
                 'Activity by issue</div>'
                 '<table cellpadding="0" cellspacing="0" style="width:100%">'
                 + "".join(cells) + '</table></div>')

    if warnings:
        items = "".join(f'<div style="margin:2px 0">{esc(w)}</div>'
                        for w in warnings)
        h.append('<div style="border-top:1px solid #e2e8f0;margin-top:20px;'
                 'padding-top:14px;font-size:11px;color:#94a3b8;line-height:1.5">'
                 '<div style="font-weight:700;margin-bottom:5px">Notes</div>'
                 + items + '</div>')

    h.append(f'<div style="border-top:1px solid #e2e8f0;margin-top:18px;'
             f'padding-top:12px;font-size:10px;color:#cbd5e1">'
             f'{measured}/{len(stories)} measured against X\'s API · '
             f'summaries written from retrieved post text · '
             f'verify before publishing</div>')
    h.append("</div></body></html>")
    return "".join(h)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def send_email(subject: str, markdown_text: str, html: str | None = None) -> bool:
    """Send the digest. Returns True only on confirmed delivery.

    App passwords are shown by Google with spaces in them; they must be
    stripped. Changing the account password revokes every app password, so
    a sudden 535 usually means exactly that.
    """
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "").replace(" ", "")
    to = os.environ.get("X_EMAIL_TO") or os.environ.get("EMAIL_TO", "")

    if not all([host, user, password, to]):
        print("[email] SMTP settings incomplete, not sending", file=sys.stderr)
        return False

    recipients = [r.strip() for r in to.split(",") if r.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user          # Gmail rewrites anything else anyway
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(markdown_text, "plain", "utf-8"))
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as s:
                s.login(user, password)
                s.sendmail(user, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=60) as s:
                s.starttls(context=context)
                s.login(user, password)
                s.sendmail(user, recipients, msg.as_string())
    except Exception as exc:                        # noqa: BLE001
        print(f"[email] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    print(f"[email] delivered to {len(recipients)} recipient(s)")
    return True


def post_slack(spikes: list) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url or not spikes:
        return
    def line(s):
        if s.get("metric") == "impressions":
            return (f"• {s['issue']}: {MX.human(s['count'])} impressions "
                    f"across {s['stories']} stories "
                    f"(baseline {MX.human(int(s['mean']))})")
        return f"• {s['issue']}: {s['count']} stories (baseline {s['mean']})"
    text = "*X monitor — spike detected*\n" + "\n".join(line(s) for s in spikes)
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as exc:                        # noqa: BLE001
        print(f"[slack] failed: {exc}", file=sys.stderr)


def append_digest(markdown_text: str) -> None:
    with open(DIGEST_LOG, "a", encoding="utf-8") as fh:
        fh.write("\n\n---\n\n" + markdown_text + "\n")


# ---------------------------------------------------------------------------
# Handle verification
# ---------------------------------------------------------------------------

def discover_handles(api_key: str) -> int:
    """Ask X who these people actually are, instead of guessing.

    The config holds a name against each handle. This searches X for each
    name, reports the handle that actually exists, and prints a ready-to-
    paste replacement block. It removes guesswork from the one part of the
    setup that cannot be checked by reading the code.
    """
    jobs, meta = [], []
    for tier in CFG.HANDLE_TIERS:
        names = []
        for entry in tier.get("handles", []):
            if isinstance(entry, dict):
                names.append((entry.get("name") or entry.get("handle", ""),
                              str(entry.get("handle", "")).lstrip("@")))
            elif entry:
                names.append((str(entry).lstrip("@"), str(entry).lstrip("@")))
        if names:
            jobs.append((tier["label"], names, api_key))
            meta.append(tier["label"])

    print(f"Searching X for the real handles behind {sum(len(j[1]) for j in jobs)} "
          f"names across {len(jobs)} tiers.\n")

    def worker(label, names, key):
        listing = "; ".join(
            f"{n}" + (f" (currently configured as @{h})" if h else "")
            for n, h in names)
        prompt = collapse_ws(f"""
Find the current, active X account for each of these people or
organisations. They are all connected to UK Christian, legal, political or
media life.

{listing}

For each one, search X and report the handle of the account that genuinely
belongs to them. Check the account actually exists and has posted recently.

Return ONLY a JSON array, no prose:
[{{"name": "as given above", "handle": "actual handle without @",
   "confidence": "high"|"low", "note": "why, or why you could not find it"}}]

If you cannot find a real account for someone, set handle to "" and say so
in the note. A wrong handle is far worse than an admitted gap — it fails
silently and contributes nothing.
""")
        return call_grok(prompt, key, model=CFG.SYNTHESIS_MODEL,
                         effort=CFG.SYNTHESIS_EFFORT)

    results = run_parallel(jobs, worker, max_workers=3)
    problems = 0

    for (result, error), label in zip(results, meta):
        print(f"\n{'=' * 62}\n{label}\n{'=' * 62}")
        if error:
            print(f"  search failed — {error}")
            problems += 1
            continue
        parsed = parse_json_block(result["text"])
        if not isinstance(parsed, list):
            print("  reply was not parseable JSON")
            problems += 1
            continue
        good = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = collapse_ws(item.get("name"))
            handle = collapse_ws(item.get("handle")).lstrip("@")
            conf = collapse_ws(item.get("confidence")).lower()
            note = collapse_ws(item.get("note"))
            if not handle:
                print(f"  ✗ {name}: not found — {note}")
                problems += 1
                continue
            mark = "✓" if conf == "high" else "?"
            print(f"  {mark} {name}: @{handle}"
                  + (f" — {note}" if conf != "high" else ""))
            good.append((handle, name))
        if good:
            print("\n  Paste-ready block for x_config.py:\n")
            print('        "handles": [')
            for handle, name in good:
                print(f'            {{"handle": "{handle}", "name": "{name}"}},')
            print("        ],")

    print(f"\n{'=' * 62}")
    print("Replace the 'handles' list in each tier with the block above, then "
          "run verify-handles to confirm.")
    if problems:
        print(f"{problems} entries need attention.")
    return 0


def verify_handles(api_key: str) -> int:
    """Check every configured handle actually resolves and has recent posts.

    Costs roughly one X Search call per handle. Run this before the first
    live run and whenever you edit x_config.py. An unverified handle
    contributes nothing and fails silently.
    """
    jobs, meta = [], []
    for tier in CFG.HANDLE_TIERS:
        for handle in tier_handles(tier):
            jobs.append((handle, api_key))
            meta.append((tier["label"], tier_name_for(tier, handle)))

    print(f"Verifying {len(jobs)} handles "
          f"(~${len(jobs) * CFG.PRICE_PER_SEARCH_CALL:.2f} in search fees)\n")

    def worker(handle, key):
        return call_grok(
            f"List the three most recent posts from @{handle}. "
            "Reply with just the dates and first few words of each.",
            key,
            allowed_handles=[handle],
            model=CFG.GATHER_MODEL,
            effort="low",
        )

    results = run_parallel(jobs, worker)
    good, bad = [], []
    for (result, error), (job, (tier, person)) in zip(results, zip(jobs, meta)):
        handle = job[0]
        if error:
            bad.append((tier, handle, person, f"call failed: {error}"))
        elif result["degraded"]:
            bad.append((tier, handle, person,
                        "no posts found — wrong handle or inactive"))
        else:
            good.append((tier, handle, person, len(result["citations"])))

    print(f"✓ {len(good)} verified\n")
    for tier, handle, person, n in good:
        print(f"    @{handle:<24} {person:<28} {tier}  ({n} posts)")
    if bad:
        print(f"\n✗ {len(bad)} FAILED — fix these in x_config.py before going live\n")
        for tier, handle, person, why in bad:
            print(f"    @{handle:<24} {person:<28} {tier}  — {why}")
        print("\nRun discover-handles to have X searched for the correct "
              "handles automatically.")
    return 1 if bad else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="X monitor for Christian Concern")
    ap.add_argument("--hours", type=int, default=12,
                    help="lookback window in hours (default 12)")
    ap.add_argument("--dry-run", action="store_true",
                    help="gather and print, write and send nothing")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--verify-handles", action="store_true",
                    help="check every configured handle resolves")
    ap.add_argument("--discover-handles", action="store_true",
                    help="search X for the correct handles behind the names")
    ap.add_argument("--slot", choices=["morning", "afternoon", "none"],
                    default="none",
                    help="named slot; runs at most once per day per slot")
    ap.add_argument("--force", action="store_true",
                    help="ignore the slot guard")
    args = ap.parse_args()

    api_key = os.environ.get("XAI_API_KEY", "").strip()
    if not api_key:
        print("XAI_API_KEY is not set", file=sys.stderr)
        return 2

    if args.discover_handles:
        return discover_handles(api_key)

    if args.verify_handles:
        return verify_handles(api_key)

    # Slot guard. GitHub's scheduler drops crons and fires others late, so
    # each slot is scheduled several times across a wide window and this
    # guard makes sure only the first attempt actually runs. Checked before
    # any paid call, so a duplicate attempt costs nothing.
    today = now_utc().astimezone(UK).strftime("%Y-%m-%d")
    runs = load_json(LAST_RUNS, {})
    slot_key = f"{today}:{args.slot}"
    if args.slot != "none" and not args.force:
        if runs.get(slot_key, {}).get("emailed"):
            print(f"[slot] {args.slot} already completed today, exiting")
            return 0

    try:
        stories, warnings = gather(api_key, args.hours)
    except GrokError as exc:
        print(f"\n[FATAL] {exc}\n", file=sys.stderr)
        print("Most likely causes, in order:\n"
              "  1. XAI_API_KEY invalid, expired, or the account is out of "
              "credit — check console.x.ai billing\n"
              "  2. The model name in x_config.py is no longer valid\n"
              "  3. xAI is down\n"
              "The error text above usually says which.", file=sys.stderr)
        return 2
    print(f"[gather] {len(stories)} raw stories")

    stories = dedupe(stories)
    apply_vip_flags(stories)
    print(f"[dedupe] {len(stories)} after merge, "
          f"{sum(1 for s in stories if s['vip'])} involving our people")

    if not args.dry_run:
        stories, seen = filter_seen(stories)
        print(f"[seen] {len(stories)} new")
    else:
        seen = None

    timeline_stories, tw = timeline_watch(api_key, args.hours)
    warnings.extend(tw)
    if timeline_stories:
        stories = dedupe(stories + timeline_stories)
        apply_vip_flags(stories)
        print(f"[timeline] {len(timeline_stories)} stories added, "
              f"{len(stories)} total")

    warnings.extend(measure(stories))
    measured = sum(1 for s in stories if s.get("measured"))
    print(f"[metrics] {measured}/{len(stories)} stories measured "
          f"via {CFG.METRICS_PROVIDER}")

    warnings.extend(ground_summaries(stories, api_key))
    stories = [s for s in stories if not s.get("unsupported")]

    heat, spikes = update_heat(stories)
    if spikes:
        print(f"[heat] spikes: {', '.join(s['issue'] for s in spikes)}")

    opportunities, opp_warnings = find_opportunities(stories, api_key)
    warnings.extend(opp_warnings)
    print(f"[cases] {len(opportunities)} case links")

    if warnings:
        print("\nWarnings from this run:")
        for w in warnings:
            print(f"  - {w}")

    order = rank(stories, opportunities)
    digest = render_markdown(stories, order, opportunities, heat, spikes,
                             warnings, args.hours)
    html = render_html(stories, order, opportunities, heat, spikes,
                       warnings, args.hours)

    if args.dry_run:
        print("\n" + digest)
        try:
            preview = REPO / "x_preview.html"
            preview.write_text(html, encoding="utf-8")
            print(f"\n[preview] email version written to {preview}")
        except OSError:
            pass
        return 0

    if not stories and not spikes:
        # Print the warnings anyway. They live in the digest, so skipping the
        # digest on a quiet run discarded every diagnostic exactly when it
        # was most needed.
        print("[done] no stories to report, no email sent")
        if warnings:
            print("\nWarnings from this run:", file=sys.stderr)
            for w in warnings:
                print(f"  - {w}", file=sys.stderr)
        if seen is not None:
            save_json(SEEN_POSTS, seen)
        return 0

    subject = f"X monitor — {uk_stamp()}"
    if spikes:
        subject = f"🔥 {subject} — {spikes[0]['issue']} spiking"

    delivered = True
    if not args.no_email:
        delivered = send_email(subject, digest, html)

    # Only commit state once delivery is confirmed. A failed email that
    # still marked the run complete means the stories are never retried.
    if delivered:
        append_digest(digest)
        if seen is not None:
            save_json(SEEN_POSTS, seen)
        post_slack(spikes)
        runs[slot_key] = {"stories": len(stories),
                          "at": now_utc().isoformat(),
                          "emailed": True}
        cut = (now_utc() - timedelta(days=14)).strftime("%Y-%m-%d")
        runs = {k: v for k, v in runs.items() if k.split(":")[0] >= cut}
        save_json(LAST_RUNS, runs)
    else:
        print("[done] delivery failed — state not committed, will retry",
              file=sys.stderr)
        return 1

    print(f"[done] {len(stories)} stories delivered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
