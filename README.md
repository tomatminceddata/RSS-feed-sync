# RSS Feed Sync

Automated RSS feed monitoring for Microsoft Fabric Blog and Power BI Blog.

## How It Works

**GitHub Actions** checks both RSS feeds every 4 hours (or on manual trigger). When new articles are found, their URLs are stored in `pending_articles.json` and committed to this repo.

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

## Files

| File | Where | Purpose |
|------|-------|---------|
| `check_feeds.py` | This repo | Lightweight feed checker (runs in GitHub Actions) |
| `pending_articles.json` | This repo | Shared state — discovered articles and their status |
| `.github/workflows/check_feeds.yml` | This repo | Cron schedule + workflow definition |
| `rss_pull_and_sync.py` | `dev/Claude-Projects/RSS feed sync/` | Local heavy-lifting script |
| `rss_sync.py` | `dev/Claude-Projects/RSS feed sync/` | Core sync logic (imported by pull_and_sync) |
