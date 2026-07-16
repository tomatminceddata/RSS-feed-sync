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

### The Cloudflare wall, the stub guard, and the pending-stub manifest

Since mid-2026, Microsoft has fronted `community.fabric.microsoft.com` with a Cloudflare *managed challenge*. Article pages (`/t5/.../ba-p/<id>`) return `403 Forbidden` to any plain HTTP fetch — `curl`, `requests`, even a real Chrome TLS fingerprint — and only a full browser that executes the challenge JavaScript clears them. The RSS feed *endpoint* has drifted in and out of the same gate; as of 2026-07 the feed path reads normally from a residential IP while the article pages stay walled.

Because the pages are unreachable, `rss_sync.py` falls back to the article body carried in the feed's `<description>` (`entry.summary`). That works well for the roughly half of posts that ship full-text in the feed. The other half arrive as **stubs** — a one- or two-sentence teaser — with the real article living only on the walled page.

Two facts make those stubs dangerous to write blindly:

1. **URL-dedup is permanent.** Once a note exists for a URL, every future run skips that URL. Writing a ~190-character teaser as if it were the article freezes that teaser in the vault forever.
2. **The feed excerpt is a publish-time snapshot.** When an author later expands a page, the feed's `<description>` stays frozen at the original stub — so a feed-only reader never catches up, no matter how many times it runs. (Observed directly: the Rayfin AMA post grew a full body on its page while its feed description stayed a 48-character one-liner.)

So `rss_sync.py` applies a **stub guard**: if a feed-body fallback is shorter than `MIN_FALLBACK_CHARS` (1500), the note is *not* written. The URL is left un-synced so it is naturally retried on every future run — and captured properly once a clean IP can page-fetch it. Full-text fallbacks write normally; only stubs are held back. The guard sits in the fallback branch only — genuinely short but *complete* articles fetched directly from their own pages (e.g. brief SonyAlphaRumors posts) are never length-checked.

Every skipped stub is recorded in **`pending_stubs.json`** — the durable work-list of articles still awaiting a real page-fetch. It lives next to the script in `dev/` (not the vault, so the knowledge base stays clean; not this repo, because it is machine-local pipeline state). Each entry carries the URL, title, source, published date, target vault folder, the stub's length, and first/last-seen timestamps. Removal is **capture-based, not window-based**: an entry persists even after it ages out of the 20-entry feed window, and is dropped only when a real note for its URL finally exists on disk. That makes the manifest a complete to-do list for the planned migration to a non-challenged egress IP (Azure Functions), where the walled pages can be fetched directly.

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
