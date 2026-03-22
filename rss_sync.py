#!/usr/bin/env python3
"""
RSS Feed Sync — Layer 1
Fetches Microsoft Fabric Blog and Power BI Blog RSS feeds,
deduplicates by URL, clips full articles as markdown notes
into Tom's Obsidian vault.

Usage:
    python rss_sync.py                  # sync both feeds
    python rss_sync.py --dry-run        # preview without writing files
    python rss_sync.py --max-articles 3 # limit to 3 articles per feed (for testing)
"""

import argparse
import re
import html
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

import json

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT_ROOT = Path(
    "PathtoyourVault/YourVault"
)

# Feed config is loaded from feeds.json in the GitHub repo (single source of
# truth). The FEEDS list below is built from that file. If the file is missing,
# we fall back to hardcoded defaults so the script still works standalone.

FEEDS_CONFIG_FILE = (
    Path.home() / "Documents" / "GitHub" / "RSS-feed-sync" / "feeds.json"
)

_DEFAULT_FEEDS = [
    {
        "name": "Microsoft Fabric Blog",
        "source_tag": "MicrosoftFabricBlog",
        "rss_url": "https://blog.fabric.microsoft.com/en-us/blog/feed/",
        "vault_folder": VAULT_ROOT / "Microsoft Fabric" / "Microsoft Fabric blog",
    },
    {
        "name": "Power BI Blog",
        "source_tag": "PowerBIBlog",
        "rss_url": "https://powerbi.microsoft.com/en-us/blog/feed/",
        "vault_folder": VAULT_ROOT
        / "Microsoft Fabric"
        / "Microsoft Fabric - Power BI"
        / "Power BI blog",
    },
]


def _load_feeds() -> list[dict]:
    """Load feed definitions from feeds.json, or fall back to defaults."""
    if FEEDS_CONFIG_FILE.exists():
        with open(FEEDS_CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        feeds = []
        for fc in config["feeds"]:
            feeds.append({
                "name": fc["name"],
                "source_tag": fc["source"],
                "rss_url": fc["rss_url"],
                "vault_folder": VAULT_ROOT / fc["vault_folder"],
            })
        return feeds
    return _DEFAULT_FEEDS


FEEDS = _load_feeds()

SYNC_LOG = VAULT_ROOT / "Microsoft Fabric" / "_rss-sync-log.md"

REQUESTS_TIMEOUT = 30  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Pattern to match blog category links in markdown output
# e.g. "- [Real-Time Intelligence](/en-us/blog/category/real-time-intelligence)"
CATEGORY_LINK_RE = re.compile(
    r"^- \[.*?\]\(/[\w-]+/blog/category/([\w-]+)\)\s*$", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Helpers — file naming
# ---------------------------------------------------------------------------

# Characters that are illegal or problematic in filenames
UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:|?*"<>]')


def sanitize_title(raw_title: str) -> str:
    """
    Turn an RSS entry title into a safe, Unicode-clean filename (no extension).

    - Decodes HTML entities to proper Unicode (& → &, ' → ', – → –)
    - Strips characters unsafe for filesystems
    - Collapses multiple spaces / leading-trailing whitespace
    """
    # Decode HTML entities → proper Unicode
    clean = html.unescape(raw_title)
    # Replace unsafe filesystem characters with a dash
    clean = UNSAFE_FILENAME_CHARS.sub("–", clean)
    # Collapse whitespace
    clean = re.sub(r"\s+", " ", clean).strip()
    # Truncate to a reasonable filename length
    if len(clean) > 200:
        clean = clean[:200].rsplit(" ", 1)[0]
    return clean


# ---------------------------------------------------------------------------
# Helpers — deduplication
# ---------------------------------------------------------------------------

def get_existing_urls(folder: Path) -> set[str]:
    """
    Scan all .md files in folder and extract the link: value from YAML
    frontmatter. Returns a set of URLs (normalized to lowercase).
    """
    urls = set()
    if not folder.exists():
        return urls

    for md_file in folder.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Quick frontmatter extraction — look for link: between --- fences
        if not text.startswith("---"):
            continue
        end = text.find("---", 3)
        if end == -1:
            continue
        frontmatter = text[3:end]

        for line in frontmatter.splitlines():
            if line.strip().lower().startswith("link:"):
                url = line.split(":", 1)[1].strip().strip('"').strip("'")
                if url:
                    urls.add(url.lower().rstrip("/"))
                break

    return urls


# ---------------------------------------------------------------------------
# Helpers — article fetching & conversion
# ---------------------------------------------------------------------------

def fix_protocol_relative_urls(soup: BeautifulSoup) -> None:
    """
    Fix protocol-relative URLs (//example.com/...) in src and href attributes.
    Microsoft blog pages use these; Obsidian needs full https:// URLs.
    """
    for tag in soup.find_all(src=True):
        if tag["src"].startswith("//"):
            tag["src"] = "https:" + tag["src"]
    for tag in soup.find_all(href=True):
        if tag["href"].startswith("//"):
            tag["href"] = "https:" + tag["href"]


def fetch_article_html(url: str) -> str | None:
    """
    Fetch the full article page and return the article body HTML.
    Returns None if the fetch fails.
    """
    try:
        resp = requests.get(
            url,
            timeout=REQUESTS_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ⚠ Failed to fetch {url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Fix protocol-relative URLs before extracting content
    fix_protocol_relative_urls(soup)

    # Microsoft blog pages typically wrap the article in <article> or
    # a div with class containing "post-content" / "entry-content"
    article = (
        soup.find("article")
        or soup.find("div", class_=re.compile(r"(post|entry)[-_]content"))
        or soup.find("div", class_="blog-post-content")
    )

    if article:
        # Remove nav, sidebar, related posts, share buttons, etc.
        for tag in article.find_all(
            ["nav", "aside", "footer", "script", "style", "noscript"]
        ):
            tag.decompose()
        for tag in article.find_all(
            class_=re.compile(
                r"(share|social|related|sidebar|comment|newsletter|author-bio)",
                re.IGNORECASE,
            )
        ):
            tag.decompose()
        return str(article)

    # Fallback: return the whole body (stripped of obvious junk)
    body = soup.find("body")
    if body:
        for tag in body.find_all(
            ["nav", "header", "footer", "aside", "script", "style", "noscript"]
        ):
            tag.decompose()
        return str(body)

    return None


def extract_categories_from_markdown(markdown: str) -> tuple[list[str], str]:
    """
    Extract blog category tags from markdown and remove them from content.

    Microsoft blog articles contain category links like:
        - [Real-Time Intelligence](/en-us/blog/category/real-time-intelligence)

    These are navigation breadcrumbs, not article content. We extract the
    category slugs as tags and strip the lines from the markdown.

    Returns (tags, cleaned_markdown).
    """
    tags = CATEGORY_LINK_RE.findall(markdown)
    cleaned = CATEGORY_LINK_RE.sub("", markdown)
    # Clean up any blank lines left behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return tags, cleaned


def html_to_markdown(html_content: str) -> str:
    """Convert HTML to clean markdown, keeping images as external links."""
    markdown = md(
        html_content,
        heading_style="ATX",
        bullets="-",
    )
    # Clean up excessive blank lines
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


# ---------------------------------------------------------------------------
# Helpers — note writing
# ---------------------------------------------------------------------------

def _format_iso8601(dt: datetime) -> str:
    """Format a datetime as ISO-8601 with colon in timezone offset.

    Python's strftime %z produces '+0000' (no colon). Dataview needs
    '+00:00' to auto-parse the date. This helper inserts the colon.
    """
    raw = dt.strftime("%Y-%m-%dT%H:%M:%S%z")       # e.g. 2026-03-20T12:00:00+0000
    return raw[:-2] + ":" + raw[-2:]                 # e.g. 2026-03-20T12:00:00+00:00


def format_published_date(entry) -> str:
    """
    Extract a clean published date from a feed entry.
    Prefers the parsed date struct, falls back to the raw string.
    """
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        from time import mktime
        dt = datetime.fromtimestamp(mktime(entry.published_parsed), tz=timezone.utc)
        return _format_iso8601(dt)

    # Fallback: raw string from feed
    return getattr(entry, "published", "")


def extract_author(entry) -> str:
    """Extract author name from feed entry."""
    if hasattr(entry, "author") and entry.author:
        return entry.author
    if hasattr(entry, "authors") and entry.authors:
        return entry.authors[0].get("name", "")
    return ""


def build_note(
    entry,
    article_markdown: str,
    source_tag: str,
    clipped_timestamp: str,
    categories: list[str] | None = None,
) -> str:
    """Assemble the complete markdown note with YAML frontmatter."""
    link = entry.link
    author = extract_author(entry)
    published = format_published_date(entry)

    # Build tags in Obsidian's preferred YAML list format:
    #   tags:
    #     - "tag-one"
    #     - "tag-two"
    tags = categories if categories else []
    if tags:
        tags_lines = "tags:\n" + "\n".join(f'  - "{t}"' for t in tags)
    else:
        tags_lines = "tags:"

    frontmatter = (
        f"---\n"
        f"link: {link}\n"
        f"author: {author}\n"
        f"published: {published}\n"
        f"clipped: {clipped_timestamp}\n"
        f"source: {source_tag}\n"
        f"{tags_lines}\n"
        f"---\n"
    )

    return frontmatter + "\n# Content\n\n" + article_markdown + "\n"


# ---------------------------------------------------------------------------
# Helpers — sync log
# ---------------------------------------------------------------------------

def write_sync_log(results: list[dict], trigger: str) -> None:
    """Append a sync run entry to the sync log."""
    timestamp = _format_iso8601(datetime.now(timezone.utc))

    lines = [f"\n## {timestamp}\n"]
    for r in results:
        lines.append(f"- Feed: {r['name']} — {r['new_count']} new articles")

    # List all new articles
    all_new = []
    for r in results:
        for title, rel_path in r["new_articles"]:
            all_new.append(f"  - [[{rel_path}]]")

    if all_new:
        lines.append("- New articles:")
        lines.extend(all_new)

    lines.append(f"- Trigger: {trigger}")
    lines.append("")

    log_text = "\n".join(lines)

    # Create log file if it doesn't exist
    if not SYNC_LOG.exists():
        SYNC_LOG.write_text(
            "# RSS Sync Log\n\n"
            "This log is automatically maintained by the RSS feed sync script.\n",
            encoding="utf-8",
        )

    with open(SYNC_LOG, "a", encoding="utf-8") as f:
        f.write(log_text)

    print(f"\n📝 Sync log updated: {SYNC_LOG.name}")


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def sync_feed(
    feed_config: dict, dry_run: bool, clipped_ts: str, max_articles: int | None
) -> dict:
    """
    Sync a single RSS feed. Returns a result dict with counts and article list.
    """
    name = feed_config["name"]
    rss_url = feed_config["rss_url"]
    vault_folder = feed_config["vault_folder"]
    source_tag = feed_config["source_tag"]

    print(f"\n{'='*60}")
    print(f"📡 Fetching: {name}")
    print(f"   URL: {rss_url}")
    print(f"   Target: {vault_folder.relative_to(VAULT_ROOT)}")
    print(f"{'='*60}")

    # Step 1 — Fetch RSS feed
    feed = feedparser.parse(rss_url)
    if feed.bozo and not feed.entries:
        print(f"  ❌ Feed parse error: {feed.bozo_exception}")
        return {"name": name, "new_count": 0, "new_articles": []}

    total_entries = len(feed.entries)
    print(f"  📄 Found {total_entries} entries in feed")

    # Step 2 — Deduplicate by URL
    existing_urls = get_existing_urls(vault_folder)
    print(f"  🗂  Found {len(existing_urls)} existing articles in vault folder")

    new_entries = []
    for entry in feed.entries:
        url = getattr(entry, "link", "")
        if url and url.lower().rstrip("/") not in existing_urls:
            new_entries.append(entry)

    print(f"  🆕 {len(new_entries)} new articles to process")

    # Apply max_articles limit
    if max_articles is not None and len(new_entries) > max_articles:
        print(f"  🔒 Limiting to {max_articles} articles (--max-articles)")
        new_entries = new_entries[:max_articles]

    if not new_entries:
        return {"name": name, "new_count": 0, "new_articles": []}

    # Step 3 — Fetch and clip each new article
    new_articles = []
    for i, entry in enumerate(new_entries, 1):
        title = html.unescape(getattr(entry, "title", "Untitled"))
        url = entry.link
        print(f"\n  [{i}/{len(new_entries)}] {title}")
        print(f"    🔗 {url}")

        if dry_run:
            safe_name = sanitize_title(title)
            rel_path = f"{vault_folder.relative_to(VAULT_ROOT)}/{safe_name}"
            new_articles.append((title, rel_path))
            print(f"    ✅ Would create: {safe_name}.md")
            continue

        # Fetch full article
        article_html = fetch_article_html(url)
        if article_html:
            article_md = html_to_markdown(article_html)
            print(f"    📥 Fetched full article ({len(article_md)} chars)")
        else:
            # Fallback: use RSS entry content (like SimpleRSS did)
            raw_content = getattr(entry, "content", [{}])[0].get("value", "")
            if not raw_content:
                raw_content = getattr(entry, "summary", "")
            article_md = html_to_markdown(raw_content)
            print(f"    ⚠ Using RSS excerpt as fallback ({len(article_md)} chars)")

        # Extract category tags and clean them from the article body
        categories, article_md = extract_categories_from_markdown(article_md)
        if categories:
            print(f"    🏷  Tags: {', '.join(categories)}")

        # Build note
        note_text = build_note(
            entry, article_md, source_tag, clipped_ts, categories
        )

        # Write to vault
        safe_name = sanitize_title(title)
        note_path = vault_folder / f"{safe_name}.md"

        # Handle filename collision (unlikely but possible)
        if note_path.exists():
            note_path = vault_folder / f"{safe_name} (2).md"

        vault_folder.mkdir(parents=True, exist_ok=True)
        note_path.write_text(note_text, encoding="utf-8")

        rel_path = str(note_path.relative_to(VAULT_ROOT)).replace(".md", "")
        new_articles.append((title, rel_path))
        print(f"    ✅ Created: {note_path.name}")

    return {
        "name": name,
        "new_count": len(new_articles),
        "new_articles": new_articles,
    }


def main():
    parser = argparse.ArgumentParser(description="RSS Feed Sync for Obsidian vault")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be synced without writing files",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="Limit number of articles per feed (useful for testing)",
    )
    parser.add_argument(
        "--trigger",
        default="manual",
        choices=["manual", "scheduled"],
        help="Log whether this was a manual or scheduled run",
    )
    args = parser.parse_args()

    mode_label = "DRY RUN" if args.dry_run else "LIVE"
    if args.max_articles:
        mode_label += f" (max {args.max_articles} per feed)"

    print("🚀 RSS Feed Sync — Layer 1")
    print(f"   Mode: {mode_label}")
    print(f"   Trigger: {args.trigger}")
    print(f"   Vault: {VAULT_ROOT}")

    clipped_ts = _format_iso8601(datetime.now(timezone.utc))

    results = []
    for feed_config in FEEDS:
        result = sync_feed(feed_config, args.dry_run, clipped_ts, args.max_articles)
        results.append(result)

    # Summary
    total_new = sum(r["new_count"] for r in results)
    action = "found" if args.dry_run else "synced"
    print(f"\n{'='*60}")
    print(f"✅ Done! {total_new} new articles {action}.")
    for r in results:
        print(f"   {r['name']}: {r['new_count']} new")
    print(f"{'='*60}")

    # Write sync log
    if not args.dry_run and total_new > 0:
        write_sync_log(results, args.trigger)


if __name__ == "__main__":
    main()
