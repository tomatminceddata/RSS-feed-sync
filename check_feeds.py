#!/usr/bin/env python3
"""
RSS Feed Checker — GitHub Actions version
Lightweight: only checks RSS feeds for new article URLs.
Does NOT fetch full articles or create Obsidian notes.

Reads pending_articles.json, adds any new URLs it finds in the feeds,
writes updated JSON back. GitHub Actions handles the commit.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import mktime

import feedparser

# ---------------------------------------------------------------------------
# Configuration — loaded from feeds.json
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
FEEDS_CONFIG_FILE = SCRIPT_DIR / "feeds.json"
PENDING_FILE = SCRIPT_DIR / "pending_articles.json"


def load_feeds_config() -> dict:
    """Load feed definitions and settings from feeds.json."""
    with open(FEEDS_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_iso8601(dt: datetime) -> str:
    """Format datetime as ISO-8601 with colon in timezone offset."""
    raw = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return raw[:-2] + ":" + raw[-2:]


def load_pending() -> dict:
    """Load the pending articles JSON. Returns empty structure if missing."""
    if PENDING_FILE.exists():
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"articles": [], "last_checked": None}


def save_pending(data: dict) -> None:
    """Write the pending articles JSON."""
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get_known_urls(data: dict) -> set:
    """Get all URLs already tracked (pending or synced)."""
    return {a["url"].lower() for a in data["articles"]}


def parse_published(entry) -> str:
    """Extract published date from feed entry as ISO-8601 string."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        dt = datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc)
        return format_iso8601(dt)
    return getattr(entry, "published", "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prune_synced(data: dict, keep_days: int) -> int:
    """Remove synced articles older than keep_days. Returns count removed."""
    if keep_days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    cutoff_str = format_iso8601(cutoff)

    before = len(data["articles"])
    data["articles"] = [
        a for a in data["articles"]
        if not (a["status"] == "synced" and a.get("synced_at", "") < cutoff_str)
    ]
    removed = before - len(data["articles"])
    if removed:
        print(f"Pruned {removed} synced articles older than {keep_days} days")
    return removed


def main():
    config = load_feeds_config()
    feeds = config["feeds"]
    keep_days = config.get("settings", {}).get("keep_synced_days", 90)

    data = load_pending()
    known_urls = get_known_urls(data)
    now = format_iso8601(datetime.now(timezone.utc))

    # Prune old synced articles
    prune_synced(data, keep_days)

    total_new = 0

    for feed_config in feeds:
        name = feed_config["name"]
        rss_url = feed_config["rss_url"]
        source = feed_config["source"]

        print(f"Checking: {name}")
        feed = feedparser.parse(rss_url)

        if feed.bozo and not feed.entries:
            print(f"  ERROR: Feed parse failed: {feed.bozo_exception}")
            continue

        print(f"  Found {len(feed.entries)} entries in feed")

        new_count = 0
        for entry in feed.entries:
            url = getattr(entry, "link", "")
            if not url or url.lower() in known_urls:
                continue

            title = getattr(entry, "title", "Untitled")
            published = parse_published(entry)

            data["articles"].append({
                "url": url,
                "title": title,
                "published": published,
                "feed": source,
                "discovered": now,
                "status": "pending",
            })

            known_urls.add(url.lower())
            new_count += 1
            print(f"  NEW: {title}")

        print(f"  {new_count} new articles")
        total_new += new_count

    data["last_checked"] = now
    save_pending(data)

    print(f"\nTotal new articles: {total_new}")

    # Exit code signals to the workflow whether a commit is needed
    if total_new > 0:
        print("pending_articles.json updated — commit needed")
    else:
        print("No changes — nothing to commit")

    return total_new


if __name__ == "__main__":
    new_count = main()
    # Write to GitHub Actions output if running in CI
    # (the workflow reads this to decide whether to commit)
    import os
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"new_articles={new_count}\n")
