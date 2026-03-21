# RSS Feed Sync

Automated RSS feed monitoring for Microsoft Fabric Blog and Power BI Blog.

## How It Works

**GitHub Actions** checks all configured RSS feeds every 4 hours (or on manual trigger). When new articles are found, their URLs are stored in `pending_articles.json` and committed to this repo. Synced articles older than `keep_synced_days` are automatically pruned.

**Locally**, `rss_pull_and_sync.py` pulls the latest JSON, fetches full article content, creates Obsidian-compatible markdown notes in the vault, marks articles as synced, and pushes the updated JSON back.

## Usage

### Manual trigger (GitHub UI)
Go to Actions → "Check RSS Feeds" → Run workflow

### Local sync (when your Mac is on)
```bash
conda activate rss-sync
cd ~/Library/CloudStorage/OneDrive-tommartens/dev/Claude-Projects/RSS\ feed\ sync/
python rss_pull_and_sync.py
```

### Options
```bash
python rss_pull_and_sync.py --dry-run    # preview without writing
python rss_pull_and_sync.py --no-git     # skip git pull/push
```

## Adding a New Feed

Edit `feeds.json` in this repo. Add a new entry to the `feeds` array:

```json
{
  "name": "My New Blog",
  "source": "MyNewBlog",
  "rss_url": "https://example.com/blog/feed/",
  "vault_folder": "Path/relative/to/vault/root"
}
```

The fields:
- **name**: Display name (shown in logs)
- **source**: Short identifier (stored in frontmatter, no spaces)
- **rss_url**: The RSS feed URL
- **vault_folder**: Path relative to the vault root where articles will be saved

Commit and push. The GitHub Action will start checking the new feed on the next run. The local sync script reads the same `feeds.json`, so new feeds work in both places automatically.

Make sure the target folder exists in the vault before the first sync, or the script will create it.

## Configuration

All configuration lives in `feeds.json`:

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

| File | Where | Purpose |
|------|-------|---------|
| `feeds.json` | This repo | Single source of truth for feed definitions and settings |
| `check_feeds.py` | This repo | Lightweight feed checker (runs in GitHub Actions) |
| `pending_articles.json` | This repo | Shared state — discovered articles and their status |
| `.github/workflows/check_feeds.yml` | This repo | Cron schedule + workflow definition |
| `rss_pull_and_sync.py` | `dev/Claude-Projects/RSS feed sync/` | Local heavy-lifting script |
| `rss_sync.py` | `dev/Claude-Projects/RSS feed sync/` | Core sync logic (imported by pull_and_sync) |
