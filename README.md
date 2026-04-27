# Hardcover -> Readarr Bridge

Syncs books from your [Hardcover](https://hardcover.app) shelves to [Readarr](https://readarr.com) for automatic downloading.

## Features

- **Polling**: Periodically checks Hardcover for new books on your shelves
- **Webhooks**: Instant sync when you add a book on Hardcover
- **Manual trigger**: Hit `/sync` to run a sync on demand
- **State tracking**: Remembers what's already been synced, won't duplicate
- **Configurable shelves**: Sync want-to-read, currently-reading, read, or any combination
- **Dry run mode**: Test without making changes to Readarr
- **Health check**: Built-in `/health` endpoint for monitoring
- **Zero dependencies**: Pure Python stdlib, no pip packages needed

## Quick Start

```bash
# Clone
git clone https://github.com/ali205412/hardcover-readarr-bridge.git
cd hardcover-readarr-bridge

# Configure
cp .env.example .env
# Edit .env with your tokens

# Run
docker compose up -d
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `HARDCOVER_TOKEN` | required | API token from https://hardcover.app/account/api |
| `READARR_URL` | `http://readarr:8787` | Readarr URL |
| `READARR_API_KEY` | required | Readarr API key |
| `POLL_INTERVAL` | `3600` | Seconds between polls (default: 1 hour) |
| `SHELF_IDS` | `1` | Hardcover shelf IDs to sync (comma-separated) |
| `ROOT_FOLDER` | `/books` | Readarr root folder path |
| `QUALITY_PROFILE_ID` | `2` | Readarr quality profile (1=eBook, 2=Spoken) |
| `METADATA_PROFILE_ID` | `1` | Readarr metadata profile |
| `MONITOR_TYPE` | `specificBook` | How Readarr monitors added books |
| `SEARCH_ON_ADD` | `true` | Auto-search for downloads when adding |
| `WEBHOOK_ENABLED` | `true` | Enable webhook server |
| `WEBHOOK_PORT` | `9876` | Webhook listener port |
| `DRY_RUN` | `false` | Log what would happen without making changes |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

### Hardcover Shelf IDs

| ID | Shelf |
|----|-------|
| 1 | Want to Read |
| 2 | Currently Reading |
| 3 | Read |
| 4 | Paused |
| 5 | Did Not Finish |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check + sync count |
| GET | `/sync` | Trigger manual sync |
| POST | `/webhook` | Hardcover webhook receiver |

## Running with your existing stack

Add to your `docker-compose.yml`:

```yaml
hardcover-readarr-bridge:
  image: ghcr.io/ali205412/hardcover-readarr-bridge:latest
  container_name: hardcover-readarr-bridge
  restart: unless-stopped
  environment:
    - HARDCOVER_TOKEN=your_token
    - READARR_URL=http://readarr:8787
    - READARR_API_KEY=your_key
    - SHELF_IDS=1
    - QUALITY_PROFILE_ID=2
  volumes:
    - hardcover_bridge_data:/data
```

## How It Works

1. Polls Hardcover API for books on configured shelves
2. For each new book, searches Readarr by title + author
3. If found in Readarr's database (GoodReads/OpenLibrary), adds the author
4. Readarr then monitors and searches for the audiobook/ebook
5. State is persisted to `/data/synced_books.json` to avoid re-processing

## License

MIT
