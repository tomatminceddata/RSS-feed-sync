#!/usr/bin/env python3
"""
RSS Pull & Sync — Local script
Pulls pending articles from the GitHub repo, processes them using
rss_sync.py's article fetching logic, creates Obsidian notes,
marks articles as synced, and pushes the updated JSON back.

Usage:
    python rss_pull_and_sync.py              # process all pending articles
    python rss_pull_and_sync.py --dry-run    # preview without writing files or pushing
    python rss_pull_and_sync.py --no-git     # skip git pull/push (for testing)
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# This script lives in dev/Claude-Projects/RSS feed sync/
# The GitHub repo is at Documents/GitHub/RSS-feed-sync/
SCRIPT_DIR = Path(__file__).parent
REPO_DIR = Path.home() / "Documents" / "GitHub" / "RSS-feed-sync"
PENDING_FILE = REPO_DIR / "pending_articles.json"

# Import helpers from rss_sync.py (same directory)
sys.path.insert(0, str(SCRIPT_DIR))
from rss_sync import (
    VAULT_ROOT,
    FEEDS,
    SYNC_LOG,
    sanitize_title,
    get_existing_urls,
    fetch_article_html,
    html_to_markdown,
    extract_categories_from_markdown,
    build_note,
    write_sync_log,
    _format_iso8601,
)


# ---------------------------------------------------------------------------
# Feed config lookup
# ---------------------------------------------------------------------------

FEED_LOOKUP = {f["source_tag"]: f for f in FEEDS}


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_pull():
    """Pull latest from the GitHub repo."""
    print(f"📥 git pull in {REPO_DIR}")
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ⚠ git pull failed: {result.stderr.strip()}")
        print("  Continuing with local state...")
    else:
        print(f"  {result.stdout.strip()}")


def git_push():
    """Commit and push the updated pending_articles.json."""
    print(f"\n📤 Committing and pushing changes...")
    subprocess.run(
        ["git", "add", "pending_articles.json"],
        cwd=REPO_DIR,
        check=True,
    )

    # Check if there are staged changes
    result = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        cwd=REPO_DIR,
    )
    if result.returncode == 0:
        print("  No changes to commit.")
        return

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run(
        ["git", "commit", "-m", f"✅ {now}: articles synced to vault"],
        cwd=REPO_DIR,
        check=True,
    )
    subprocess.run(
        ["git", "push"],
        cwd=REPO_DIR,
        check=True,
    )
    print("  Pushed successfully.")


# ---------------------------------------------------------------------------
# Pending articles helpers
# ---------------------------------------------------------------------------

def load_pending() -> dict:
    """Load the pending articles JSON."""
    if not PENDING_FILE.exists():
        print(f"❌ {PENDING_FILE} not found. Have you cloned the repo?")
        sys.exit(1)

    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_pending(data: dict) -> None:
    """Write the pending articles JSON."""
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def process_pending(data: dict, dry_run: bool) -> int:
    """Process all pending articles. Returns count of newly synced articles."""
    clipped_ts = _format_iso8601(datetime.now(timezone.utc))
    synced_count = 0
    sync_results = {}  # feed_name -> list of (title, rel_path)

    pending = [a for a in data["articles"] if a["status"] == "pending"]

    if not pending:
        print("\n✅ No pending articles to process.")
        return 0

    print(f"\n📋 {len(pending)} pending articles to process\n")

    for i, article in enumerate(pending, 1):
        url = article["url"]
        title = article["title"]
        feed_source = article["feed"]

        # Look up the feed configuration
        feed_config = FEED_LOOKUP.get(feed_source)
        if not feed_config:
            print(f"  [{i}/{len(pending)}] ⚠ Unknown feed source: {feed_source}")
            print(f"    Skipping: {title}")
            continue

        vault_folder = feed_config["vault_folder"]
        source_tag = feed_config["source_tag"]

        print(f"  [{i}/{len(pending)}] {title}")
        print(f"    🔗 {url}")
        print(f"    📂 {vault_folder.relative_to(VAULT_ROOT)}")

        # Check if already in vault (dedup against existing notes)
        existing_urls = get_existing_urls(vault_folder)
        if url.lower().rstrip("/") in existing_urls:
            print(f"    ⏭  Already in vault — marking as synced")
            article["status"] = "synced"
            article["synced_at"] = clipped_ts
            synced_count += 1
            continue

        if dry_run:
            safe_name = sanitize_title(title)
            print(f"    ✅ Would create: {safe_name}.md")
            continue

        # Fetch full article
        article_html = fetch_article_html(url)
        if article_html:
            article_md = html_to_markdown(article_html)
            print(f"    📥 Fetched full article ({len(article_md)} chars)")
        else:
            print(f"    ❌ Failed to fetch article — leaving as pending")
            continue

        # Extract category tags
        categories, article_md = extract_categories_from_markdown(article_md)
        if categories:
            print(f"    🏷  Tags: {', '.join(categories)}")

        # Build a minimal entry object that build_note expects
        class EntryStub:
            pass

        entry = EntryStub()
        entry.link = url
        entry.author = ""
        entry.published = article.get("published", "")
        entry.published_parsed = None  # we already have the formatted string

        note_text = build_note(entry, article_md, source_tag, clipped_ts, categories)

        # Write to vault
        safe_name = sanitize_title(title)
        note_path = vault_folder / f"{safe_name}.md"

        if note_path.exists():
            note_path = vault_folder / f"{safe_name} (2).md"

        vault_folder.mkdir(parents=True, exist_ok=True)
        note_path.write_text(note_text, encoding="utf-8")

        rel_path = str(note_path.relative_to(VAULT_ROOT)).replace(".md", "")
        print(f"    ✅ Created: {note_path.name}")

        # Track for sync log
        feed_name = feed_config["name"]
        if feed_name not in sync_results:
            sync_results[feed_name] = []
        sync_results[feed_name].append((title, rel_path))

        # Mark as synced
        article["status"] = "synced"
        article["synced_at"] = clipped_ts
        synced_count += 1

    # Write sync log
    if not dry_run and sync_results:
        log_entries = []
        for feed_name, articles_list in sync_results.items():
            log_entries.append({
                "name": feed_name,
                "new_count": len(articles_list),
                "new_articles": articles_list,
            })
        write_sync_log(log_entries, "pull-and-sync")

    return synced_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pull pending articles from GitHub and sync to Obsidian vault"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be synced without writing files",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Skip git pull/push (for testing)",
    )
    args = parser.parse_args()

    print("🚀 RSS Pull & Sync")
    print(f"   Repo: {REPO_DIR}")
    print(f"   Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"   Git:  {'disabled' if args.no_git else 'auto pull/push'}")

    # Step 1: git pull
    if not args.no_git:
        git_pull()

    # Step 2: Load pending articles
    data = load_pending()
    pending_count = sum(1 for a in data["articles"] if a["status"] == "pending")
    total_count = len(data["articles"])
    print(f"\n📊 {total_count} articles tracked, {pending_count} pending")

    # Step 3: Process pending articles
    synced = process_pending(data, args.dry_run)

    # Step 4: Save updated JSON
    if not args.dry_run:
        save_pending(data)

    # Step 5: git push
    if not args.no_git and not args.dry_run and synced > 0:
        git_push()

    print(f"\n{'='*60}")
    action = "would be synced" if args.dry_run else "synced"
    print(f"✅ Done! {synced} articles {action}.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
