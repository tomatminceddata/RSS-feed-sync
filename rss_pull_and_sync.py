#!/usr/bin/env python3
"""
RSS Pull & Sync — Local script
Pulls pending articles from the GitHub repo, processes them using
rss_sync.py's article fetching logic, creates Obsidian notes,
marks articles as synced, and pushes the updated JSON back.

Each article in pending_articles.json carries `feed` (source_tag) and
`parser` (parser name) — together they identify which feed config to
use. This is needed because the same source_tag can appear under
multiple parsers (e.g. MicrosoftFabricBlog appears under both
'wordpress' and 'khoros' during the parallel-run migration).

Usage:
    python rss_pull_and_sync.py              # process all pending articles
    python rss_pull_and_sync.py --dry-run    # preview without writing files or pushing
    python rss_pull_and_sync.py --no-git     # skip git pull/push (for testing)
    python rss_pull_and_sync.py --feed-kind khoros           # process only Khoros articles
    python rss_pull_and_sync.py --feed-kind khoros --dry-run # preview only Khoros articles
    python rss_pull_and_sync.py --max-articles 3             # process at most 3 pending articles (total)
    python rss_pull_and_sync.py --feed-kind khoros --max-articles 1  # one Khoros article only (surgical testing)
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
    PARSERS,
    SYNC_LOG,
    sanitize_title,
    get_existing_urls,
    fetch_article,
    html_to_markdown,
    extract_categories_from_markdown,
    build_note,
    write_sync_log,
    _format_iso8601,
)
from KnowledgeHub_Helper import download_and_localize_images, should_download_assets


# ---------------------------------------------------------------------------
# Feed config lookup
# ---------------------------------------------------------------------------

# Keyed by (source_tag, parser_name) — the same source_tag can appear
# under multiple parsers during parallel-run migrations (e.g. WordPress
# legacy + Khoros community both producing MicrosoftFabricBlog articles).
FEED_LOOKUP: dict[tuple[str, str], dict] = {
    (f["source_tag"], f.get("parser", "wordpress")): f for f in FEEDS
}


def lookup_feed(source_tag: str, parser_name: str) -> dict | None:
    """
    Find the feed config matching the given source_tag and parser.
    Falls back to the source_tag-only match if the exact pair is not
    found — this handles old pending_articles.json entries that don't
    yet have a `parser` field.
    """
    exact = FEED_LOOKUP.get((source_tag, parser_name))
    if exact is not None:
        return exact
    # Back-compat fallback: old entries without `parser` get matched
    # against any feed with the same source_tag. If multiple feeds share
    # the source_tag (the parallel-run case) we prefer 'wordpress' to
    # match the historical behaviour.
    for (st, pn), cfg in FEED_LOOKUP.items():
        if st == source_tag and pn == "wordpress":
            return cfg
    for (st, pn), cfg in FEED_LOOKUP.items():
        if st == source_tag:
            return cfg
    return None


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

def process_pending(
    data: dict,
    dry_run: bool,
    feed_kind: str | None = None,
    max_articles: int | None = None,
) -> int:
    """Process pending articles. Returns count of newly synced articles.

    If feed_kind is set (e.g. 'khoros'), only articles whose parser matches
    are processed. Useful for draining one feed kind at a time during
    testing or migration windows.

    If max_articles is set, processing stops after that many articles
    (TOTAL across the run, not per-feed). The cap is applied AFTER the
    feed_kind filter, so `--feed-kind khoros --max-articles 1` yields
    one Khoros article — ideal for surgical testing where a bad result
    can be unwound with a single vault deletion and a single
    pending_articles.json edit.
    """
    clipped_ts = _format_iso8601(datetime.now(timezone.utc))
    synced_count = 0
    sync_results = {}  # feed_name -> list of (title, rel_path)

    pending = [a for a in data["articles"] if a["status"] == "pending"]

    # Filter by parser kind if requested. We do this BEFORE counting so
    # the displayed totals reflect what will actually be processed, not
    # what's in the queue overall.
    if feed_kind is not None:
        before = len(pending)
        pending = [
            a for a in pending
            if a.get("parser", "wordpress") == feed_kind
        ]
        skipped = before - len(pending)
        if skipped:
            print(f"\n🎯 Filter --feed-kind {feed_kind}: skipping {skipped} "
                  f"non-matching pending article(s)")

    # Apply the total cap AFTER the feed_kind filter so the two flags
    # compose intuitively.
    if max_articles is not None and len(pending) > max_articles:
        print(f"\n🔒 Limiting to {max_articles} article(s) (--max-articles); "
              f"{len(pending) - max_articles} will remain pending for next run")
        pending = pending[:max_articles]

    if not pending:
        print("\n✅ No pending articles to process.")
        return 0

    print(f"\n📋 {len(pending)} pending articles to process\n")

    for i, article in enumerate(pending, 1):
        url = article["url"]
        title = article["title"]
        feed_source = article["feed"]
        # Default to 'wordpress' for back-compat with entries created
        # before check_feeds.py started writing the parser field.
        parser_name = article.get("parser", "wordpress")

        # Look up the feed configuration
        feed_config = lookup_feed(feed_source, parser_name)
        if not feed_config:
            print(f"  [{i}/{len(pending)}] ⚠ Unknown feed: source={feed_source} parser={parser_name}")
            print(f"    Skipping: {title}")
            continue

        vault_folder = feed_config["vault_folder"]
        source_tag = feed_config["source_tag"]

        print(f"  [{i}/{len(pending)}] {title}")
        print(f"    🔗 {url}")
        print(f"    📂 {vault_folder.relative_to(VAULT_ROOT)}")
        print(f"    ⚙  Parser: {parser_name}")

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

        # Fetch full article via the configured parser
        article_html, parser_tags = fetch_article(url, parser_name)
        if article_html:
            article_md = html_to_markdown(article_html)
            print(f"    📥 Fetched full article ({len(article_md)} chars)")
        else:
            print(f"    ❌ Failed to fetch article — leaving as pending")
            continue

        # Tag extraction: combine parser-supplied tags (Khoros: from page)
        # and markdown-extracted tags (WordPress: category breadcrumbs).
        # De-duplicate while preserving order.
        markdown_tags, article_md = extract_categories_from_markdown(article_md)
        seen = set()
        categories: list[str] = []
        for t in list(parser_tags) + list(markdown_tags):
            key = t.strip().lower()
            if key and key not in seen:
                seen.add(key)
                categories.append(t.strip())
        if categories:
            print(f"    🏷  Tags: {', '.join(categories)}")

        # Build a minimal entry object that build_note expects.
        # We carry author through from the JSON (Khoros feeds populate
        # this; WordPress feeds typically leave it empty).
        class EntryStub:
            pass

        entry = EntryStub()
        entry.link = url
        entry.author = article.get("author", "")
        entry.published = article.get("published", "")
        entry.published_parsed = None  # we already have the formatted string

        note_text = build_note(
            entry, article_md, source_tag, parser_name, clipped_ts, categories
        )

        # Write to vault
        safe_name = sanitize_title(title)
        note_path = vault_folder / f"{safe_name}.md"

        if note_path.exists():
            note_path = vault_folder / f"{safe_name} (2).md"

        # Download images locally if configured for this source
        if should_download_assets(source_tag):
            note_text = download_and_localize_images(
                markdown=note_text,
                article_title=safe_name,
                source_tag=source_tag,
                article_note_path=note_path,
                dry_run=dry_run,
            )

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
    parser.add_argument(
        "--feed-kind",
        choices=sorted(PARSERS.keys()),
        default=None,
        help=(
            "Process only pending articles whose parser matches this kind "
            "(e.g. 'khoros'). Useful for testing one parser in isolation."
        ),
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help=(
            "Limit total number of articles processed in this run (applied "
            "after --feed-kind filter). Useful for surgical testing: a bad "
            "result is one vault deletion away from a retry."
        ),
    )
    args = parser.parse_args()

    print("🚀 RSS Pull & Sync")
    print(f"   Repo: {REPO_DIR}")
    print(f"   Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"   Git:  {'disabled' if args.no_git else 'auto pull/push'}")
    if args.feed_kind:
        print(f"   Filter: --feed-kind {args.feed_kind} (only matching articles)")
    if args.max_articles:
        print(f"   Limit: --max-articles {args.max_articles} (total cap)")

    # Step 1: git pull
    if not args.no_git:
        git_pull()

    # Step 2: Load pending articles
    data = load_pending()
    pending_count = sum(1 for a in data["articles"] if a["status"] == "pending")
    total_count = len(data["articles"])
    print(f"\n📊 {total_count} articles tracked, {pending_count} pending")

    # Step 3: Process pending articles
    synced = process_pending(data, args.dry_run, args.feed_kind, args.max_articles)

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
