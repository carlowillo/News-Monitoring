# Christian Concern News Monitor

Two files. No dependencies. No config files. Free to run.

## Files

- `monitor.py` — the whole system (feeds and keywords are inside it, near the top)
- `.github/workflows/monitor.yml` — tells GitHub to run it hourly, free

## Setup

1. In your GitHub repo, **delete any old files** from previous attempts:
   `requirements.txt`, `feeds.yaml`, `issues.yaml`, `seen_articles.json`.
   They are no longer used and will cause confusion if left behind.
2. Upload `monitor.py` to the root of the repo (replacing the existing one).
3. Upload `.github/workflows/monitor.yml` (replacing the existing one).
   If drag-and-drop won't include the `.github` folder, use
   "Add file" → "Create new file" and type the path
   `.github/workflows/monitor.yml` manually — GitHub creates the folders for you.
4. Go to the **Actions** tab → **News Monitor** → **Run workflow**.

That's it. Nothing else is needed.

## Where results appear

`digest_log.md` appears in your repo after the first run that finds something.
It's a running log, newest at the top, so nothing is lost between checks.
Bookmark:

```
https://github.com/YOUR-USERNAME/YOUR-REPO/blob/main/digest_log.md
```

## Changing what it monitors

Open `monitor.py` on GitHub, click the pencil icon, and edit:

- `FEEDS` — the list of news sources (add any RSS URL)
- `ISSUES` — your issue names and the keywords that flag them

Commit, and the next run uses the new settings.

### Adding a paywalled title

Titles like The Times don't publish full RSS, but Google News indexes their
headlines. Add a line to `FEEDS` like:

```python
("Google News: Times abortion",
 "https://news.google.com/rss/search?q=abortion+site:thetimes.co.uk&hl=en-GB&gl=GB&ceid=GB:en"),
```

You get the headline and a link (not the full text, which stays behind the
paywall) — enough to spot relevant coverage and click through.

## Optional: Slack alerts

Create an Incoming Webhook in Slack, then in your repo go to
Settings → Secrets and variables → Actions → New repository secret,
name it `SLACK_WEBHOOK_URL`, and paste the webhook URL.
Leave it unset and the script simply skips Slack.

## What this doesn't do

- Full text of paywalled articles (headlines and links only, by design)
- Social media monitoring (X, Facebook) — separate problem, has real costs
- Instant alerts — it checks hourly
