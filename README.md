# RSS Feed Sync

Automated RSS feed monitoring for Microsoft Fabric Blog, Power BI Blog, and other technical sources Tom follows. Articles land in his Obsidian vault as full markdown notes — frontmatter, tags, body, and locally-stored images — ready for reading and downstream knowledge tooling.

**Scope note.** This repo holds the RSS-feed-sync half of Tom's knowledge-hub tooling. The webcrawlers that feed the same vault folders from other sources live in separate, private repositories and will not be published. References to webcrawlers in this README and in `KnowledgeHub_Helper.py` exist because the helper is shared infrastructure — they're not an invitation to look for the webcrawler code here.

## How It Works

**GitHub Actions** checks all configured RSS feeds every 4 hours (or on manual trigger). When new articles are found, their URLs are appended to `pending_articles.json` and committed to this repo. Synced articles older than `keep_synced_days` are automatically pruned.

**Locally**, `rss_pull_and_sync.py` pulls the latest JSON, fetches full article content using the per-feed parser (see below), creates Obsidian-compatible markdown notes in the vault with locally-stored images, marks articles as synced, and pushes the updated JSON back.

### Per-feed parser dispatcher

Each feed in `feeds.json` declares a `parser` that controls how article bodies and tags are extracted from the source page. Three parsers ship today:

| Parser | Use case |
|---|---|
| `wordpress` | Legacy `blog.fabric.microsoft.com` / `powerbi.microsoft.com` WordPress structure |
| `khoros` | New `community.fabric.microsoft.com` Khoros/Lithium structure (replaces WordPress for Microsoft blogs since 2026-05) |
| `default` | Generic fallback used by MacRumors, SonyAlphaRumors, and similar third-party feeds |

Adding a new parser means writing a `fetch_article_html_<name>(url) -> (html, tags)` function in `rss_sync.py` and registering it in the `PARSERS` dict. Feeds then opt in by declaring `"parser": "<name>"` in `feeds.json`.

### Image localization

Image-heavy blogs (Microsoft Fabric, Power BI, Chris Webb, Nikola Ilic, Sandeep Pawar, and several others) have their embedded images downloaded into per-blog `articleassets/<article-title>/` folders next to the article notes, with markdown image links rewritten to local relative paths. This is handled by `KnowledgeHub_Helper.py` — the shared utility layer that sits beneath all knowledge-hub pipelines (RSS sync today, others later). A blog opts into image localization by being listed in `ASSET_CONFIG` inside that helper.

**A note on copyright.** Image localization copies images and article text into Tom's private Obsidian vault for personal reading, study, and offline access. Nothing here is republished, redistributed, or made public. All article text and images remain the property of their original authors and publishers — the markdown frontmatter preserves the source URL and author so attribution is never lost, and the publishers' original pages remain the authoritative copies. Anyone reusing this code on their own machine is responsible for staying within the terms of the sites they fetch from.

## Usage

### Manual trigger (GitHub UI)
Go to Actions → "Check RSS Feeds" → Run workflow.

### Local sync (when your Mac is on)
```bash
conda activate rss-sync
cd ~/Library/CloudStorage/OneDrive-tommartens/dev/Claude-Projects/RSS\ feed\ sync/
python rss_pull_and_sync.py
```

### Options
```bash
python rss_pull_and_sync.py --dry-run                       # preview, no writes, no push
python rss_pull_and_sync.py --no-git                        # skip git pull/push (testing)
python rss_pull_and_sync.py --feed-kind khoros              # process only Khoros articles
python rss_pull_and_sync.py --max-articles 3                # cap total processed in this run
python rss_pull_and_sync.py --feed-kind khoros --max-articles 1 --no-git --dry-run
# ↑ the safest possible smoke test: one Khoros article, no fetches, no writes, no git
```

`--feed-kind` and `--max-articles` compose. The cap is applied *after* the kind filter, so the combo means "one Khoros article" not "one of anything." This is the surgical-testing pattern: a bad result is one vault deletion + one `pending_articles.json` revert away from a clean retry.

## Adding a New Feed

Edit `feeds.json` in this repo. Add an entry to the `feeds` array:

```json
{
  "name": "My New Blog",
  "source": "MyNewBlog",
  "parser": "default",
  "rss_url": "https://example.com/blog/feed/",
  "vault_folder": "Path/relative/to/vault/root"
}
```

The fields:
- **name**: Display name (shown in logs)
- **source**: Short identifier stored in frontmatter (`source:`) — no spaces
- **parser**: Which parser to use — `wordpress`, `khoros`, or `default` (see *Per-feed parser dispatcher* above). If omitted, defaults to `wordpress` for back-compat.
- **rss_url**: The RSS feed URL
- **vault_folder**: Path relative to the vault root where articles will be saved

Commit and push. The GitHub Action will start checking the new feed on the next run. The local sync script reads the same `feeds.json`, so new feeds work in both places automatically.

Make sure the target folder exists in the vault before the first sync, or the script will create it.

If the new blog should have its images downloaded locally, also add an entry to `ASSET_CONFIG` in `KnowledgeHub_Helper.py` mapping the `source` tag to its vault folder and an `articleassets` subfolder name. Without that entry, images stay as external CDN links.

## Configuration

All feed configuration lives in `feeds.json`:

```json
{
  "feeds": [ ... ],
  "settings": {
    "keep_synced_days": 90
  }
}
```

- **keep_synced_days**: Synced articles older than this are pruned from the JSON to prevent endless growth. Set to `0` to keep everything. Default: 90 days.

## Files

| File | Purpose |
|------|---------|
| `feeds.json` | Single source of truth for feed definitions and settings |
| `check_feeds.py` | Lightweight feed checker that runs in GitHub Actions |
| `pending_articles.json` | Shared state — discovered articles and their status |
| `.github/workflows/check_feeds.yml` | Cron schedule + workflow definition |
| `rss_sync.py` | Core sync logic — parser dispatcher, RSS fetching, note building |
| `rss_pull_and_sync.py` | Local heavy-lifting script — drains pending queue into vault |
| `KnowledgeHub_Helper.py` | Shared utility layer (image localization, asset config) — used by all knowledge-hub pipelines |

The repo holds the canonical copies of all Python files. The local working copies in `dev/Claude-Projects/RSS feed sync/` are kept in sync with the repo by Tom's normal git workflow.

## Pending Articles JSON Schema

Each entry in `pending_articles.json` looks like:

```json
{
  "url": "https://community.fabric.microsoft.com/...",
  "title": "Article Title",
  "published": "2026-05-05T16:00:00+00:00",
  "feed": "MicrosoftFabricBlog",
  "parser": "khoros",
  "author": "TwinkleCyril",
  "discovered": "2026-05-05T16:12:18+00:00",
  "status": "pending",
  "synced_at": null
}
```

`feed` + `parser` together identify the originating feed (the same `source_tag` can appear under multiple parsers during a parallel-run migration). Old entries written before `check_feeds.py` started capturing `parser` and `author` are still processable — the local script falls back to `wordpress` and an empty author when those fields are missing.

## Authentication

`rss_pull_and_sync.py` finishes by committing and pushing `pending_articles.json` back to this repo. That `git push` relies on a credential stored in the macOS Keychain (an `inet` entry for `github.com`). If that credential is missing, the push fails with `remote: Invalid username or token` even though the local sync itself succeeded — the articles are in the vault, only the push didn't go up.

To recover, re-seed the credential (the next push will prompt, or trigger one manually), then push again. The full mechanism — how the credential is seeded, and the difference between GitHub Desktop's OAuth token and a classic PAT — is documented in the vault at `Getting social/Blog/Way of working/git authentication.md`.
