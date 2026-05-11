#!/usr/bin/env python3
"""
KnowledgeHub Helper — Shared utilities for Tom's knowledge vault tooling.

This module provides functions used across the RSS feed sync, webcrawl,
and future vault management scripts. It's the shared utility layer that
sits beneath the individual pipeline scripts.

Current capabilities:
- Image/asset localization: download embedded images from articles and
  rewrite markdown links to point to local copies.

Future candidates:
- Vault-wide dedup helpers
- Knowledge agent integration
- TOC/index generation helpers

Location: dev/Claude-Projects/RSS feed sync/KnowledgeHub_Helper.py
(shared code directory — all pipeline scripts import from here)
"""

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VAULT_ROOT = Path(
    "/Users/thomasmartens/Library/CloudStorage/"
    "OneDrive-tommartens/TomsVault"
)

REQUESTS_TIMEOUT = 30  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Delay between image downloads (seconds) — be polite
DELAY_BETWEEN_DOWNLOADS = 0.5

# Maximum image file size to download (10 MB)
MAX_IMAGE_SIZE = 10 * 1024 * 1024

# Image extensions we'll download
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}

# Regex to find markdown image references, including clickable images.
#
# Pattern 1 (clickable): [![alt](img_url)](link_url)
# Pattern 2 (plain):     ![alt](img_url)
#
# We match clickable images first (greedy outer brackets), then plain.
# Group 1: full match for replacement
# Group 2: alt text
# Group 3: image URL
# Group 4: link URL (only for clickable images)

# Clickable image: [![alt](img_url)](link_url)
CLICKABLE_IMAGE_RE = re.compile(
    r"\[!\[([^\]]*)\]\(([^)\s]+(?:\s+\"[^\"]*\")?)\)\]\(([^)]+)\)"
)

# Plain image: ![alt](img_url)
PLAIN_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+(?:\s+\"[^\"]*\")?)\)"
)


# ---------------------------------------------------------------------------
# Asset folder configuration
# ---------------------------------------------------------------------------

# Maps blog source tags to their articleassets folder configuration.
# Only blogs listed here will have their images downloaded locally.
# The key is the source_tag used in frontmatter.
# vault_folder: where the articles live
# assets_subfolder: name of the articleassets folder (sibling to articles)

ASSET_CONFIG = {
    "ChrisWebbBlog": {
        "vault_folder": (
            VAULT_ROOT / "About Data" / "Chris Webb - crossjoin_co_uk"
        ),
        "assets_subfolder": "Chris Webb - crossjoin_co_uk - articleassets",
    },
    "MicrosoftFabricBlog": {
        "vault_folder": (
            VAULT_ROOT / "Microsoft Fabric" / "Microsoft Fabric blog"
        ),
        "assets_subfolder": "Microsoft Fabric blog - articleassets",
    },
    "PowerBIBlog": {
        "vault_folder": (
            VAULT_ROOT
            / "Microsoft Fabric"
            / "Microsoft Fabric - Power BI"
            / "Power BI blog"
        ),
        "assets_subfolder": "Power BI blog - articleassets",
    },
    "DataMozartBlog": {
        "vault_folder": (
            VAULT_ROOT
            / "About Data"
            / "Nikola Ilic - datamozart"
        ),
        "assets_subfolder": "Nikola Ilic - datamozart - articleassets",
    },
    "FabricGuruBlog": {
        "vault_folder": (
            VAULT_ROOT
            / "About Data"
            / "Sandeep Pawar - fabricguru"
        ),
        "assets_subfolder": "Sandeep Pawar - fabricguru - articleassets",
    },
    "TabularEditorBlog": {
        "vault_folder": (
            VAULT_ROOT
            / "About Data"
            / "Tabular Editor - tabulareditor blog"
        ),
        "assets_subfolder": "Tabular Editor - tabulareditor blog - articleassets",
    },
    "TabularEditorDocs": {
        "vault_folder": (
            VAULT_ROOT
            / "About Data"
            / "Tabular Editor - tabulareditor docs"
        ),
        "assets_subfolder": "Tabular Editor - tabulareditor docs - articleassets",
    },
    "MincedDataBlog": {
        "vault_folder": (
            VAULT_ROOT
            / "About Data"
            / "Tom Martens - minceddatadotinfo"
        ),
        "assets_subfolder": "Tom Martens - minceddatadotinfo - articleassets",
    },
    "RakiRahmanBlog": {
        "vault_folder": (
            VAULT_ROOT
            / "About Data"
            / "Raki Rahman - RakiRahmanMe"
        ),
        "assets_subfolder": "Raki Rahman - RakiRahmanMe - articleassets",
    },
    "FourMooBlog": {
        "vault_folder": (
            VAULT_ROOT
            / "About Data"
            / "Gilbert Quevauvilliers - fourmoo"
        ),
        "assets_subfolder": "Gilbert Quevauvilliers - fourmoo - articleassets",
    },
}


def should_download_assets(source_tag: str) -> bool:
    """Check if a blog/source is configured for local asset download."""
    return source_tag in ASSET_CONFIG


def get_assets_folder(source_tag: str, article_title: str) -> Path | None:
    """
    Get the full path to the articleassets folder for a given article.

    Returns:
        Path like: .../Chris Webb - crossjoin_co_uk/
                   Chris Webb - crossjoin_co_uk - articleassets/
                   <article_title>/
        Or None if this source isn't configured for asset download.
    """
    config = ASSET_CONFIG.get(source_tag)
    if not config:
        return None

    assets_dir = (
        config["vault_folder"]
        / config["assets_subfolder"]
        / article_title
    )
    return assets_dir


# ---------------------------------------------------------------------------
# Image download and localization
# ---------------------------------------------------------------------------

def _extract_image_filename(url: str) -> str:
    """
    Extract a clean filename from an image URL.

    Handles WordPress-style URLs like:
        https://i0.wp.com/blog.crossjoin.co.uk/wp-content/uploads/2025/04/image.png?resize=832%2C326&ssl=1
    → image.png

    Also handles URLs with no extension by generating a hash-based name.
    """
    # Strip query parameters and fragments
    clean_url = url.split("?")[0].split("#")[0]

    # Get the path component
    parsed = urlparse(clean_url)
    path = unquote(parsed.path)

    # Get the filename from the path
    filename = Path(path).name

    if not filename or filename == "/":
        # Generate a hash-based name if we can't extract one
        url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
        filename = f"image_{url_hash}.png"

    # Ensure it has an image extension
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        # Try to infer from URL or default to .png
        filename = filename + ".png"

    # Truncate long filenames (macOS limit: 255 bytes)
    # Keep the extension, truncate the stem
    stem = Path(filename).stem
    ext = Path(filename).suffix
    max_stem_bytes = 200 - len(ext.encode("utf-8"))  # leave room for extension + safety margin
    while len(stem.encode("utf-8")) > max_stem_bytes:
        stem = stem[:len(stem) - 10]  # chop 10 chars at a time
    filename = stem + ext

    return filename


def _make_filename_unique(target_dir: Path, filename: str) -> str:
    """
    If a file with this name already exists in target_dir, append a number.
    Returns the (potentially modified) filename.
    """
    candidate = target_dir / filename
    if not candidate.exists():
        return filename

    stem = Path(filename).stem
    ext = Path(filename).suffix
    counter = 1
    while True:
        new_name = f"{stem}_{counter}{ext}"
        if not (target_dir / new_name).exists():
            return new_name
        counter += 1


def _download_image(url: str, target_path: Path) -> bool:
    """
    Download a single image to target_path.
    Returns True on success, False on failure.
    """
    try:
        resp = requests.get(
            url,
            timeout=REQUESTS_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        )
        resp.raise_for_status()

        # Check content length if available
        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_IMAGE_SIZE:
            print(f"      ⚠️  Image too large ({int(content_length)} bytes), skipping")
            return False

        # Download with size limit
        data = b""
        for chunk in resp.iter_content(chunk_size=8192):
            data += chunk
            if len(data) > MAX_IMAGE_SIZE:
                print(f"      ⚠️  Image exceeded {MAX_IMAGE_SIZE} bytes, skipping")
                return False

        if not data:
            print(f"      ⚠️  Empty response, skipping")
            return False

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(data)
        return True

    except requests.RequestException as e:
        print(f"      ⚠️  Failed to download image: {e}")
        return False


def _compute_relative_path(article_note_path: Path, image_path: Path) -> str:
    """
    Compute the relative path from the article note to the image file.

    This produces a standard markdown-compatible relative path that works
    in both Obsidian and VS Code.

    Example:
        article: .../Chris Webb - crossjoin_co_uk/Some Article.md
        image:   .../Chris Webb - crossjoin_co_uk/
                     Chris Webb - crossjoin_co_uk - articleassets/
                     Some Article/image.png
        result:  Chris Webb - crossjoin_co_uk - articleassets/Some Article/image.png
    """
    try:
        # Both paths share the same parent (the blog vault folder)
        # The article is in the blog folder root
        # The image is in blog_folder/articleassets_subfolder/article_title/
        rel = image_path.relative_to(article_note_path.parent)
        return str(rel)
    except ValueError:
        # Fallback: compute relative path manually
        # This shouldn't happen with our folder structure, but just in case
        return str(image_path)


def _format_path_for_markdown(path_str: str) -> str:
    """
    Format a relative path for use in markdown image syntax.

    Uses angle-bracket syntax: ![alt](<path with spaces>)
    This is the standard CommonMark way to handle paths containing
    spaces and special characters. Works in both Obsidian and VS Code.

    Returns the path wrapped in angle brackets if it contains spaces
    or parentheses, plain otherwise.
    """
    needs_brackets = " " in path_str or "(" in path_str or ")" in path_str
    if needs_brackets:
        return f"<{path_str}>"
    return path_str


def _collect_image_refs(markdown: str) -> list[dict]:
    """
    Find all image references in markdown, handling both clickable and
    plain images. Returns a list of dicts with keys:
        full_match: the entire matched text to be replaced
        alt_text: alt text from ![alt](...)
        image_url: the image source URL
        is_clickable: True if wrapped in a link [![](img)](link)

    Clickable images are matched first to avoid the inner ![](url) being
    matched again by the plain pattern.
    """
    refs = []
    # Track character positions already matched (to avoid double-matching)
    matched_spans: list[tuple[int, int]] = []

    # Pass 1: Clickable images  [![alt](img_url)](link_url)
    for m in CLICKABLE_IMAGE_RE.finditer(markdown):
        refs.append({
            "full_match": m.group(0),
            "alt_text": m.group(1),
            "image_url": m.group(2).strip(),
            "is_clickable": True,
            "start": m.start(),
        })
        matched_spans.append((m.start(), m.end()))

    # Pass 2: Plain images ![alt](img_url) — skip those inside clickable matches
    for m in PLAIN_IMAGE_RE.finditer(markdown):
        pos = m.start()
        already_matched = any(s <= pos < e for s, e in matched_spans)
        if not already_matched:
            refs.append({
                "full_match": m.group(0),
                "alt_text": m.group(1),
                "image_url": m.group(2).strip(),
                "is_clickable": False,
                "start": m.start(),
            })

    # Sort by position so replacements are in order
    refs.sort(key=lambda r: r["start"])
    return refs


def download_and_localize_images(
    markdown: str,
    article_title: str,
    source_tag: str,
    article_note_path: Path,
    dry_run: bool = False,
) -> str:
    """
    Download all images in the markdown and rewrite links to local paths.

    This is the main entry point for image localization. Call it after
    creating the markdown content but before writing the note to disk.

    Handles two markdown image patterns:
    - Plain:     ![alt](url)           → ![alt](local_path)
    - Clickable: [![alt](url)](link)   → ![alt](local_path)
      The outer link wrapper is removed because the linked URL typically
      points to the same image at full resolution, which is now local.

    Args:
        markdown: The article markdown content (with external image URLs).
        article_title: The sanitized article title (used for the asset subfolder).
        source_tag: The blog source tag (e.g., "ChrisWebbBlog").
        article_note_path: Full path where the .md note will be written.
        dry_run: If True, log what would be done but don't download.

    Returns:
        The markdown with image URLs rewritten to relative local paths.
        Images that failed to download keep their original external URL.
    """
    if not should_download_assets(source_tag):
        return markdown

    assets_dir = get_assets_folder(source_tag, article_title)
    if not assets_dir:
        return markdown

    # Find all image references (clickable + plain)
    refs = _collect_image_refs(markdown)
    if not refs:
        return markdown

    print(f"    🖼️  Found {len(refs)} image(s) to localize")

    # Track URL → local filename mapping to handle duplicate URLs
    url_to_local: dict[str, str] = {}
    replacements: list[tuple[str, str]] = []  # (old_text, new_text)
    download_count = 0

    for i, ref in enumerate(refs):
        full_match = ref["full_match"]
        alt_text = ref["alt_text"]
        image_url = ref["image_url"]

        # Strip optional title from URL: "url \"title\""
        if ' "' in image_url:
            image_url = image_url.split(' "')[0]

        # Skip data URIs
        if image_url.startswith("data:"):
            continue

        # Skip already-relative paths (already localized)
        if not image_url.startswith("http"):
            continue

        if dry_run:
            filename = _extract_image_filename(image_url)
            print(f"      [{i+1}/{len(refs)}] Would download: {filename}")
            continue

        # Check if we already downloaded this URL in this article
        if image_url in url_to_local:
            local_filename = url_to_local[image_url]
        else:
            # Download the image
            filename = _extract_image_filename(image_url)
            filename = _make_filename_unique(assets_dir, filename)
            target_path = assets_dir / filename

            print(f"      [{i+1}/{len(refs)}] Downloading: {filename}")
            success = _download_image(image_url, target_path)

            if success:
                url_to_local[image_url] = filename
                local_filename = filename
                download_count += 1
                # Small delay between downloads
                if i < len(refs) - 1:
                    time.sleep(DELAY_BETWEEN_DOWNLOADS)
            else:
                # Keep original URL on failure
                continue

        # Build the new relative path
        local_path = assets_dir / local_filename
        rel_path = _compute_relative_path(article_note_path, local_path)
        formatted_path = _format_path_for_markdown(rel_path)

        # Build the new markdown image reference.
        # For both clickable and plain images, produce a plain image link.
        # The clickable wrapper (linking to full-res version on the web)
        # is no longer needed since we have the image locally.
        new_image_md = f"![{alt_text}]({formatted_path})"
        replacements.append((full_match, new_image_md))

    # Apply all replacements
    result = markdown
    for old_text, new_text in replacements:
        result = result.replace(old_text, new_text, 1)

    if download_count > 0 and not dry_run:
        print(f"    ✅ {download_count} image(s) downloaded to: "
              f"{assets_dir.relative_to(VAULT_ROOT)}")

    return result
