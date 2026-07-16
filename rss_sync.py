#!/usr/bin/env python3
"""
RSS Feed Sync — Layer 1
Fetches RSS feeds (Microsoft Fabric Blog, Power BI Blog, MacRumors, etc.),
deduplicates by URL, clips full articles as markdown notes into Tom's
Obsidian vault.

Architecture:
    Each feed declares a `parser` in feeds.json. The parser drives:
      - how the article body is extracted from the page
      - how tags are extracted (sometimes from the page, sometimes from
        markdown after extraction)
    Supported parsers:
      - "wordpress" : the legacy blog.fabric.microsoft.com WordPress structure
      - "khoros"    : the new community.fabric.microsoft.com Khoros/Lithium
                      structure (replaces wordpress for Microsoft blogs)
      - "default"   : generic fallback (used by MacRumors, SonyAlphaRumors)

Usage:
    python rss_sync.py                  # sync all feeds
    python rss_sync.py --dry-run        # preview without writing files
    python rss_sync.py --max-articles 3 # limit to 3 articles per feed (testing)
"""

import argparse
import os
import re
import html
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

import json

from KnowledgeHub_Helper import download_and_localize_images, should_download_assets

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Vault root is read from the TOMSVAULT_ROOT environment variable so the
# same scripts run on multiple devices (MBP → .../TomsVault,
# MBA → .../TomsVaultMBA). Set in ~/.zshrc per machine. The fallback below
# is the MBP path — if the env var is missing for any reason, scripts
# continue working on the MBP without change.
VAULT_ROOT = Path(
    os.environ.get(
        "TOMSVAULT_ROOT",
        "/Users/thomasmartens/Library/CloudStorage/"
        "OneDrive-tommartens/TomsVault"
    )
)

# Feed config is loaded from feeds.json in the GitHub repo (single source of
# truth). The FEEDS list below is built from that file. If the file is missing,
# we fall back to hardcoded defaults so the script still works standalone.

FEEDS_CONFIG_FILE = (
    Path.home() / "Documents" / "GitHub" / "RSS-feed-sync" / "feeds.json"
)

_DEFAULT_FEEDS = [
    {
        "name": "Microsoft Fabric Blog (Khoros community)",
        "source_tag": "MicrosoftFabricBlog",
        "parser": "khoros",
        "rss_url": (
            "https://community.fabric.microsoft.com/oxcrx34285/rss/board"
            "?board.id=fbc_fabricupdatesblogs"
        ),
        "vault_folder": VAULT_ROOT / "Microsoft Fabric" / "Microsoft Fabric blog",
    },
    {
        "name": "Power BI Blog (Khoros community)",
        "source_tag": "PowerBIBlog",
        "parser": "khoros",
        "rss_url": (
            "https://community.fabric.microsoft.com/oxcrx34285/rss/board"
            "?board.id=fbc_pbiupdatesblog"
        ),
        "vault_folder": VAULT_ROOT
        / "Microsoft Fabric"
        / "Microsoft Fabric - Power BI"
        / "Power BI blog",
    },
]


def _load_feeds() -> list[dict]:
    """Load feed definitions from feeds.json, or fall back to defaults.

    The `parser` field is read from the JSON; if missing it defaults to
    'wordpress' (the original behaviour, for back-compat with old configs).
    """
    if FEEDS_CONFIG_FILE.exists():
        with open(FEEDS_CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        feeds = []
        for fc in config["feeds"]:
            feeds.append({
                "name": fc["name"],
                "source_tag": fc["source"],
                "parser": fc.get("parser", "wordpress"),
                "rss_url": fc["rss_url"],
                "vault_folder": VAULT_ROOT / fc["vault_folder"],
            })
        return feeds
    return _DEFAULT_FEEDS


FEEDS = _load_feeds()

SYNC_LOG = VAULT_ROOT / "Microsoft Fabric" / "_rss-sync-log.md"

# Pending-stub manifest. Lives next to this script in dev/ (NOT the vault —
# these are knowledge-hub articles and the vault stays clean), and NOT keyed
# to VAULT_ROOT because it's machine-independent pipeline state, not vault
# content. It is the durable work-list of articles the guard skipped: each
# entry is a Khoros URL whose real body is still behind Cloudflare, waiting
# for a clean-IP (Azure) page-fetch. See update_pending_stubs() for lifecycle.
PENDING_STUBS_FILE = Path(__file__).resolve().parent / "pending_stubs.json"

REQUESTS_TIMEOUT = 30  # seconds

# Minimum length (chars, post-markdown) for a feed-body fallback to be
# accepted. Below this we treat the entry as a stub and skip it rather than
# freeze a teaser in the vault (see sync_feed's fallback branch and RSS
# handoff #10, the feed-freeze finding). Khoros stubs run ~40–520 chars;
# full-text feed entries run 6–12 K, so the gap is wide and this threshold
# sits safely inside it. Only affects the fallback path — pages we actually
# fetch are never length-checked.
MIN_FALLBACK_CHARS = 1500

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Pattern to match WordPress blog category links in markdown output
# e.g. "- [Real-Time Intelligence](/en-us/blog/category/real-time-intelligence)"
CATEGORY_LINK_RE = re.compile(
    r"^- \[.*?\]\(/[\w-]+/blog/category/([\w-]+)\)\s*$", re.MULTILINE
)

# Pattern to match empty heading lines (e.g. "# " on a line by itself).
# Khoros pages occasionally render as `<h1></h1>` followed by a real heading,
# producing `# \n# Real Title` after markdownify. We strip the empty ones.
EMPTY_HEADING_RE = re.compile(r"^#+\s*$\n?", re.MULTILINE)


# ---------------------------------------------------------------------------
# Helpers — file naming
# ---------------------------------------------------------------------------

# Characters that are illegal or problematic in filenames
UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:|?*"<>]')


def sanitize_title(raw_title: str) -> str:
    """
    Turn an RSS entry title into a safe, Unicode-clean filename (no extension).

    - Fixes malformed HTML entities (e.g. &8211; → &#8211;) before decoding
    - Decodes HTML entities to proper Unicode (& → &, ' → ', – → –)
    - Strips characters unsafe for filesystems
    - Collapses multiple spaces / leading-trailing whitespace
    """
    # Fix malformed numeric entities: &8211; → &#8211; (missing #)
    # Also handles hex entities: &x2F; → &#x2F;
    clean = re.sub(r"&(x?[0-9A-Fa-f]+;)", r"&#\1", raw_title)
    # Decode HTML entities → proper Unicode
    clean = html.unescape(clean)
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
# Helpers — HTTP and HTML cleanup
# ---------------------------------------------------------------------------

def fix_protocol_relative_urls(soup: BeautifulSoup) -> None:
    """
    Fix protocol-relative URLs (//example.com/...) in src and href attributes.
    Some Microsoft pages use these; Obsidian needs full https:// URLs.
    """
    for tag in soup.find_all(src=True):
        if tag["src"].startswith("//"):
            tag["src"] = "https:" + tag["src"]
    for tag in soup.find_all(href=True):
        if tag["href"].startswith("//"):
            tag["href"] = "https:" + tag["href"]


def http_get(url: str, allow_redirects: bool = True) -> requests.Response | None:
    """Plain HTTP GET with the configured user agent and timeout."""
    try:
        resp = requests.get(
            url,
            timeout=REQUESTS_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=allow_redirects,
        )
        resp.raise_for_status()
        return resp
    except requests.HTTPError as e:
        print(f"  ⚠ HTTP error fetching {url}: {e}")
        return None
    except requests.RequestException as e:
        print(f"  ⚠ Failed to fetch {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Parser — wordpress (legacy blog.fabric.microsoft.com / powerbi.microsoft.com)
# ---------------------------------------------------------------------------

def fetch_article_html_wordpress(url: str) -> tuple[str | None, list[str]]:
    """
    Fetch a WordPress-hosted blog article. Returns (article_html, tags).
    For WordPress, tags are NOT extracted here — they live in the body
    markdown as category breadcrumbs and are extracted later via
    extract_categories_from_markdown(). So this returns ([]) for tags.

    Handles the blog.fabric.microsoft.com → community.fabric.microsoft.com
    redirect issue: some articles redirect to a community URL that returns
    404 because Microsoft's redirect mapping is incomplete. In that case,
    we detect the broken redirect and report it clearly rather than
    silently failing.
    """
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(
            url,
            timeout=REQUESTS_TIMEOUT,
            headers=headers,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Check if this is a 404 caused by a bad redirect
        if (
            e.response is not None
            and e.response.status_code == 404
            and "community.fabric.microsoft.com" in e.response.url
            and "blog.fabric.microsoft.com" in url
        ):
            print(f"  ⚠ Redirect to community domain returned 404, retrying on original domain...")
            # Retry: don't follow the redirect, fetch from the original domain directly
            try:
                resp2 = requests.get(
                    url,
                    timeout=REQUESTS_TIMEOUT,
                    headers=headers,
                    allow_redirects=False,
                )
                # If we get a 3xx, the content isn't served from the original domain either
                if resp2.status_code in (301, 302, 303, 307, 308):
                    redirect_target = resp2.headers.get("Location", "unknown")
                    print(f"  ⚠ Article redirects to {redirect_target} (which 404s)")
                    print(f"    Microsoft redirect mapping issue — article not yet available at the new URL.")
                    return None, []
                resp2.raise_for_status()
                resp = resp2  # use the non-redirected response
            except requests.RequestException as e2:
                print(f"  ⚠ Retry also failed: {e2}")
                return None, []
        else:
            print(f"  ⚠ Failed to fetch {url}: {e}")
            return None, []
    except requests.RequestException as e:
        print(f"  ⚠ Failed to fetch {url}: {e}")
        return None, []

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
        return str(article), []

    # Fallback: return the whole body (stripped of obvious junk)
    body = soup.find("body")
    if body:
        for tag in body.find_all(
            ["nav", "header", "footer", "aside", "script", "style", "noscript"]
        ):
            tag.decompose()
        return str(body), []

    return None, []


# ---------------------------------------------------------------------------
# Parser — khoros (new community.fabric.microsoft.com)
# ---------------------------------------------------------------------------

def fetch_article_html_khoros(url: str) -> tuple[str | None, list[str]]:
    """
    Fetch a Khoros community article. Returns (article_html, tags).

    Khoros pages have a clean structure:
      - Article body: <div class="lia-message-body"> (id="bodyDisplay")
      - Tags ("Labels:" block): <div class="LabelsForArticle"> containing
        <a class="label-link"> per label
    Tags live OUTSIDE the article body, so we extract them from the full
    page soup before narrowing down to the body.
    """
    resp = http_get(url)
    if resp is None:
        return None, []

    soup = BeautifulSoup(resp.text, "html.parser")
    fix_protocol_relative_urls(soup)

    # Tags first — they're in the full-page soup, not the article body.
    tags: list[str] = []
    labels_container = soup.find("div", class_="LabelsForArticle")
    if labels_container:
        for a in labels_container.find_all("a", class_=re.compile(r"label-link")):
            text = a.get_text(strip=True)
            if text:
                tags.append(text)

    # Article body
    article = soup.find("div", class_="lia-message-body")
    if article is None:
        # Defensive fallback — Khoros HTML may evolve. Caller will fall
        # back to the RSS summary if we return None.
        print(f"  ⚠ Khoros parser: <div class='lia-message-body'> not found")
        return None, tags

    # Strip anything we don't want in the article body. Khoros bodies
    # tend to be clean already, but let's be defensive against future
    # additions of share/related widgets.
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

    return str(article), tags


# ---------------------------------------------------------------------------
# Parser — default (generic feeds: MacRumors, SonyAlphaRumors)
# ---------------------------------------------------------------------------

def fetch_article_html_default(url: str) -> tuple[str | None, list[str]]:
    """
    Generic fallback parser for feeds without a specific implementation.
    Tries common article containers; if none match, returns the cleaned body.
    Tags are not extracted here — third-party feeds typically expose tags
    via the RSS entry (entry.tags) and our caller can read them directly.
    """
    resp = http_get(url)
    if resp is None:
        return None, []

    soup = BeautifulSoup(resp.text, "html.parser")
    fix_protocol_relative_urls(soup)

    article = (
        soup.find("article")
        or soup.find("div", class_=re.compile(r"(post|entry|article)[-_]content"))
        or soup.find("main")
    )

    if article:
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
        return str(article), []

    body = soup.find("body")
    if body:
        for tag in body.find_all(
            ["nav", "header", "footer", "aside", "script", "style", "noscript"]
        ):
            tag.decompose()
        return str(body), []

    return None, []


# ---------------------------------------------------------------------------
# Parser registry — dispatcher
# ---------------------------------------------------------------------------

PARSERS = {
    "wordpress": fetch_article_html_wordpress,
    "khoros": fetch_article_html_khoros,
    "default": fetch_article_html_default,
}


def fetch_article(url: str, parser_name: str) -> tuple[str | None, list[str]]:
    """Dispatch to the named parser. Falls back to 'default' if unknown."""
    parser_fn = PARSERS.get(parser_name, fetch_article_html_default)
    return parser_fn(url)


# Backwards-compat shim: rss_backfill.py imports `fetch_article_html` from
# this module. The new dispatcher splits that name into parser-specific
# variants. Keep the old name pointing at the WordPress parser since that's
# what backfill was originally written against.
def fetch_article_html(url: str) -> str | None:
    html, _tags = fetch_article_html_wordpress(url)
    return html


# ---------------------------------------------------------------------------
# Helpers — markdown post-processing
# ---------------------------------------------------------------------------

def extract_categories_from_markdown(markdown: str) -> tuple[list[str], str]:
    """
    Extract WordPress blog category tags from markdown and remove them
    from content.

    Microsoft WordPress blog articles contain category links like:
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


def strip_empty_headings(markdown: str) -> str:
    """Remove empty heading lines (e.g. '# ' alone on a line).

    Khoros pages occasionally produce empty H1s right before real ones,
    yielding `# \\n# Real Heading` in markdownify output. Strip them.
    """
    cleaned = EMPTY_HEADING_RE.sub("", markdown)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def html_to_markdown(html_content: str) -> str:
    """Convert HTML to clean markdown, keeping images as external links."""
    markdown = md(
        html_content,
        heading_style="ATX",
        bullets="-",
    )
    # Clean up excessive blank lines
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    # Drop empty heading lines (Khoros artefact, harmless elsewhere)
    markdown = strip_empty_headings(markdown)
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
    """Extract author name from feed entry.

    Khoros feeds populate dc:creator → entry.author cleanly (e.g. 'TwinkleCyril').
    WordPress feeds usually leave it empty.
    """
    if hasattr(entry, "author") and entry.author:
        return entry.author
    if hasattr(entry, "authors") and entry.authors:
        return entry.authors[0].get("name", "")
    return ""


def build_note(
    entry,
    article_markdown: str,
    source_tag: str,
    feed_kind: str,
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
        f"feed_kind: {feed_kind}\n"
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
# Helpers — pending-stub manifest
# ---------------------------------------------------------------------------

def _load_pending_stubs() -> dict[str, dict]:
    """Load the current manifest as {normalized_url: entry}, or {} if absent."""
    if PENDING_STUBS_FILE.exists():
        try:
            with open(PENDING_STUBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                e["url"].lower().rstrip("/"): e
                for e in data.get("stubs", [])
                if e.get("url")
            }
        except (OSError, json.JSONDecodeError):
            print("  ⚠ pending_stubs.json unreadable — starting a fresh manifest")
            return {}
    return {}


def update_pending_stubs(results: list[dict]) -> None:
    """Merge this run's skipped stubs into pending_stubs.json.

    Lifecycle of an entry:
      - New stub this run → added, first_seen = last_seen = now.
      - Seen again        → first_seen preserved; last_seen + body_chars refreshed.
      - Captured          → dropped, because a real vault note now exists for
                            the URL (once Azure page-fetches the full body, the
                            stub is no longer pending).

    Removal is capture-based, not observation-based, so an entry persists even
    after it ages out of the 20-entry feed window — the manifest stays a
    complete work-list until each URL is actually captured. A stub we stopped
    seeing but never captured would otherwise be lost entirely, since the guard
    deliberately wrote no note for it.
    """
    now = _format_iso8601(datetime.now(timezone.utc))
    manifest = _load_pending_stubs()

    # 1) Fold in this run's stubs (preserve first_seen for known URLs)
    for r in results:
        for s in r.get("stubs", []):
            key = s["url"].lower().rstrip("/")
            if key in manifest:
                manifest[key]["last_seen"] = now
                manifest[key]["body_chars"] = s["body_chars"]
            else:
                s["first_seen"] = now
                s["last_seen"] = now
                manifest[key] = s

    # 2) Prune entries since captured (a real note now exists on disk).
    #    Cache existing-URL scans per folder so each folder is read once.
    folder_cache: dict[str, set[str]] = {}
    for key, entry in list(manifest.items()):
        rel = entry.get("vault_folder", "")
        if rel not in folder_cache:
            folder_cache[rel] = get_existing_urls(VAULT_ROOT / rel)
        if key in folder_cache[rel]:
            del manifest[key]

    # 3) Write current state (sorted oldest-first by first_seen)
    out = {
        "generated": now,
        "count": len(manifest),
        "stubs": sorted(manifest.values(), key=lambda e: e.get("first_seen", "")),
    }
    with open(PENDING_STUBS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n📋 Pending stubs tracked: {len(manifest)} → {PENDING_STUBS_FILE.name}")


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
    parser_name = feed_config.get("parser", "wordpress")

    print(f"\n{'='*60}")
    print(f"📡 Fetching: {name}")
    print(f"   URL: {rss_url}")
    print(f"   Target: {vault_folder.relative_to(VAULT_ROOT)}")
    print(f"   Parser: {parser_name}")
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
    stubs: list[dict] = []
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

        # Fetch full article via the configured parser
        article_html, parser_tags = fetch_article(url, parser_name)
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

            # Stub guard: a short feed body means the real article is still
            # behind the Cloudflare wall. Writing the stub would freeze a
            # teaser in the vault that URL-dedup then skips on every future
            # run — permanently losing the real body. Instead, skip it and
            # leave the URL un-synced so it's retried each run and captured
            # properly once a clean IP (Azure) can page-fetch it.
            if len(article_md) < MIN_FALLBACK_CHARS:
                print(
                    f"    ⏭  Feed body below {MIN_FALLBACK_CHARS} chars — "
                    f"likely a stub; leaving unsynced for later page-fetch. "
                    f"Skipping."
                )
                stubs.append({
                    "url": url,
                    "title": title,
                    "source": source_tag,
                    "parser": parser_name,
                    "published": format_published_date(entry),
                    "vault_folder": str(vault_folder.relative_to(VAULT_ROOT)),
                    "body_chars": len(article_md),
                })
                continue

        # Tag extraction: combine parser-supplied tags (from page) and
        # markdown-extracted tags (WordPress category breadcrumbs).
        # Both can be empty; we de-duplicate while preserving order.
        markdown_tags, article_md = extract_categories_from_markdown(article_md)
        seen = set()
        categories = []
        for t in list(parser_tags) + list(markdown_tags):
            key = t.strip().lower()
            if key and key not in seen:
                seen.add(key)
                categories.append(t.strip())
        if categories:
            print(f"    🏷  Tags: {', '.join(categories)}")

        # Build note
        note_text = build_note(
            entry, article_md, source_tag, parser_name, clipped_ts, categories
        )

        # Write to vault
        safe_name = sanitize_title(title)
        note_path = vault_folder / f"{safe_name}.md"

        # Handle filename collision (unlikely but possible)
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
        new_articles.append((title, rel_path))
        print(f"    ✅ Created: {note_path.name}")

    return {
        "name": name,
        "new_count": len(new_articles),
        "new_articles": new_articles,
        "stubs": stubs,
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

    # Update the pending-stub manifest (skipped Khoros stubs awaiting Azure).
    # Live runs only — dry-run never reaches the guard, so it has nothing to
    # record and must not write. Runs even when total_new == 0 so that stubs
    # since captured still get pruned.
    if not args.dry_run:
        update_pending_stubs(results)


if __name__ == "__main__":
    main()
