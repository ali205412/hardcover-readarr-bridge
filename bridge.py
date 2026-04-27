"""Hardcover -> Readarr Bridge

Syncs books from your Hardcover shelves to Readarr.
Supports both periodic polling and Hardcover webhooks.
"""

import hmac
import os
import time
import json
import logging
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hardcover-readarr")


def _env_int(key, default):
    val = os.getenv(key, str(default))
    try:
        return int(val)
    except ValueError:
        log.error("Invalid integer for %s: %r, using default %d", key, val, default)
        return default


# Config from environment
HARDCOVER_TOKEN = os.getenv("HARDCOVER_TOKEN", "")
READARR_URL = os.getenv("READARR_URL", "http://readarr:8787")
READARR_API_KEY = os.getenv("READARR_API_KEY", "")
POLL_INTERVAL = _env_int("POLL_INTERVAL", 3600)
SHELF_IDS = os.getenv("SHELF_IDS", "1")
ROOT_FOLDER = os.getenv("ROOT_FOLDER", "/books")
QUALITY_PROFILE_ID = _env_int("QUALITY_PROFILE_ID", 2)
METADATA_PROFILE_ID = _env_int("METADATA_PROFILE_ID", 1)
MONITOR_TYPE = os.getenv("MONITOR_TYPE", "specificBook")
WEBHOOK_PORT = _env_int("WEBHOOK_PORT", 9876)
WEBHOOK_ENABLED = os.getenv("WEBHOOK_ENABLED", "true").lower() == "true"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
SEARCH_ON_ADD = os.getenv("SEARCH_ON_ADD", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
STATE_FILE = os.getenv("STATE_FILE", "/data/synced_books.json")
BIND_ADDRESS = os.getenv("BIND_ADDRESS", "0.0.0.0")
MAX_WEBHOOK_BODY = 1024 * 1024  # 1 MB

HARDCOVER_API = "https://api.hardcover.app/v1/graphql"

_sync_lock = Lock()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"synced": {}}


def save_state(state):
    dirname = os.path.dirname(STATE_FILE)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def hardcover_query(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = Request(
        HARDCOVER_API,
        data=body,
        headers={
            "Authorization": f"Bearer {HARDCOVER_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urlopen(req, timeout=30)
        data = json.loads(resp.read())
        if "errors" in data:
            log.error("Hardcover GraphQL error: %s", data["errors"])
            return None
        return data.get("data")
    except (HTTPError, URLError):
        log.exception("Hardcover API error")
        return None


def readarr_api(path, method="GET", data=None):
    url = f"{READARR_URL}/api/v1{path}"
    body = json.dumps(data).encode() if data else None
    req = Request(
        url,
        data=body,
        headers={
            "X-Api-Key": READARR_API_KEY,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        resp = urlopen(req, timeout=30)
        resp_body = resp.read()
        return json.loads(resp_body) if resp_body else None
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        log.error("Readarr API error %s %s: %s %s", method, path, e.code, error_body[:200])
        return None
    except URLError:
        log.exception("Readarr connection error for %s", path)
        return None


def readarr_get(path):
    url = f"{READARR_URL}/api/v1{path}"
    req = Request(url, headers={"X-Api-Key": READARR_API_KEY})
    try:
        resp = urlopen(req, timeout=30)
        resp_body = resp.read()
        return json.loads(resp_body) if resp_body else None
    except (HTTPError, URLError):
        log.exception("Readarr GET error %s", path)
        return None


def get_hardcover_books(status_ids):
    all_books = []
    for status_id in status_ids:
        query = """
        query GetBooks($statusId: Int!) {
            me {
                user_books(where: {status_id: {_eq: $statusId}}) {
                    id
                    rating
                    date_added
                    book_id
                    book {
                        id
                        title
                        slug
                        pages
                        contributions {
                            author { name id }
                        }
                        editions(limit: 5, order_by: {users_count: desc}) {
                            isbn_13
                            isbn_10
                            asin
                            title
                        }
                    }
                }
            }
        }
        """
        data = hardcover_query(query, {"statusId": status_id})
        if data and data.get("me"):
            books = data["me"].get("user_books", [])
            log.info("Hardcover shelf %d: %d books", status_id, len(books))
            all_books.extend(books)
    return all_books


def search_readarr(title, author=None):
    term = f"{title} {author}" if author else title
    results = readarr_get(f"/search?term={quote(term)}")
    if not results:
        return None
    for r in results:
        r_title = r.get("title", "").lower()
        if title.lower() in r_title or r_title in title.lower():
            return r
    return results[0] if results else None


def get_existing_authors():
    authors = readarr_get("/author")
    if not authors:
        return {}
    return {a.get("authorName", "").lower(): a for a in authors}


def get_existing_books():
    books = readarr_get("/book")
    if not books:
        return {}
    return {b.get("title", "").lower(): b for b in books}


def add_book_to_readarr(hardcover_book, existing_authors, existing_books):
    book = hardcover_book.get("book", {})
    title = book.get("title", "Unknown")
    authors = book.get("contributions", [])
    author_name = authors[0]["author"]["name"] if authors else "Unknown"

    if title.lower() in existing_books:
        log.debug("Already in Readarr: %s", title)
        return True

    search_result = search_readarr(title, author_name)
    if not search_result:
        log.warning("Not found in Readarr search: %s by %s", title, author_name)
        return False

    author_data = search_result.get("author", {})
    author_name_lower = author_data.get("authorName", "").lower()

    if DRY_RUN:
        log.info("[DRY RUN] Would add: %s by %s", title, author_name)
        return True

    foreign_author_id = author_data.get("foreignAuthorId")
    if not foreign_author_id:
        log.warning("No foreignAuthorId for %s", author_name)
        return False

    if author_name_lower not in existing_authors:
        add_data = {
            "authorName": author_data.get("authorName"),
            "foreignAuthorId": foreign_author_id,
            "qualityProfileId": QUALITY_PROFILE_ID,
            "metadataProfileId": METADATA_PROFILE_ID,
            "rootFolderPath": ROOT_FOLDER,
            "monitored": True,
            "monitorNewItems": MONITOR_TYPE,
            "addOptions": {
                "monitor": MONITOR_TYPE,
                "searchForMissingBooks": SEARCH_ON_ADD,
            },
        }
        result = readarr_api("/author", method="POST", data=add_data)
        if not result:
            log.error("Failed to add author: %s", author_name)
            return False
        log.info("Added author to Readarr: %s", author_name)
        existing_authors[author_name_lower] = result
    else:
        log.debug("Author already exists: %s", author_name)

    log.info("Synced: %s by %s", title, author_name)
    return True


def sync():
    if not _sync_lock.acquire(blocking=False):
        log.info("Sync already in progress, skipping")
        return
    try:
        _sync_inner()
    except Exception:
        log.exception("Sync failed")
    finally:
        _sync_lock.release()


def _sync_inner():
    if not HARDCOVER_TOKEN or not READARR_API_KEY:
        log.error("Missing HARDCOVER_TOKEN or READARR_API_KEY")
        return

    state = load_state()

    try:
        shelf_ids = [int(s.strip()) for s in SHELF_IDS.split(",") if s.strip()]
    except ValueError:
        log.error("Invalid SHELF_IDS: %r", SHELF_IDS)
        return

    log.info("Syncing Hardcover shelves %s to Readarr", shelf_ids)
    books = get_hardcover_books(shelf_ids)
    log.info("Total books from Hardcover: %d", len(books))

    existing_authors = get_existing_authors()
    existing_books = get_existing_books()
    log.info("Readarr has %d authors, %d books", len(existing_authors), len(existing_books))

    added = 0
    skipped = 0
    failed = 0

    for hb in books:
        book_id = str(hb.get("book_id", hb.get("id", "")))
        book_title = hb.get("book", {}).get("title", "?")

        if book_id in state["synced"]:
            skipped += 1
            continue

        success = add_book_to_readarr(hb, existing_authors, existing_books)
        if success:
            state["synced"][book_id] = {
                "title": book_title,
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            added += 1
        else:
            failed += 1

        time.sleep(3)

    save_state(state)
    log.info("Sync complete: added=%d skipped=%d failed=%d", added, skipped, failed)


def _verify_webhook_signature(body, signature):
    if not WEBHOOK_SECRET:
        return True
    if not signature:
        return False
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, "sha256").hexdigest()
    return hmac.compare_digest(expected, signature)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_WEBHOOK_BODY:
            log.warning("Webhook body too large: %d bytes", content_length)
            self.send_response(413)
            self.end_headers()
            return

        body = self.rfile.read(content_length)

        signature = self.headers.get("X-Hardcover-Signature", "")
        if not _verify_webhook_signature(body, signature):
            log.warning("Webhook signature verification failed")
            self.send_response(403)
            self.end_headers()
            return

        try:
            payload = json.loads(body)
            log.info("Webhook received: %s", json.dumps(payload)[:200])

            event = payload.get("event", "")
            if event in ("book.status_changed", "book.added", "user_book.created"):
                log.info("Triggering sync from webhook event: %s", event)
                Thread(target=sync, daemon=True).start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        except json.JSONDecodeError:
            log.warning("Webhook received invalid JSON")
            self.send_response(400)
            self.end_headers()
        except Exception:
            log.exception("Webhook handler error")
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            state = load_state()
            self.wfile.write(json.dumps({
                "status": "healthy",
                "synced_count": len(state.get("synced", {})),
            }).encode())
            return

        if self.path == "/sync":
            log.info("Manual sync triggered via API")
            Thread(target=sync, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "sync_started"}).encode())
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        log.debug("HTTP %s", format % args)


def polling_loop():
    while True:
        try:
            sync()
        except Exception:
            log.exception("Polling sync error")
        log.info("Next sync in %d seconds", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


def main():
    log.info("Hardcover -> Readarr Bridge starting")
    log.info("  Readarr: %s", READARR_URL)
    log.info("  Shelves: %s", SHELF_IDS)
    log.info("  Poll interval: %ds", POLL_INTERVAL)
    log.info("  Webhook: %s (port %d)", WEBHOOK_ENABLED, WEBHOOK_PORT)
    log.info("  Webhook auth: %s", "enabled" if WEBHOOK_SECRET else "disabled")
    log.info("  Dry run: %s", DRY_RUN)

    if not HARDCOVER_TOKEN:
        log.error("HARDCOVER_TOKEN is required. Get it from https://hardcover.app/account/api")
        sys.exit(1)
    if not READARR_API_KEY:
        log.error("READARR_API_KEY is required")
        sys.exit(1)

    sync()

    poll_thread = Thread(target=polling_loop, daemon=True)
    poll_thread.start()

    if WEBHOOK_ENABLED:
        server = HTTPServer((BIND_ADDRESS, WEBHOOK_PORT), WebhookHandler)
        log.info("Webhook server listening on %s:%d", BIND_ADDRESS, WEBHOOK_PORT)
        log.info("  POST /webhook  - Hardcover webhook endpoint")
        log.info("  GET  /health   - Health check")
        log.info("  GET  /sync     - Trigger manual sync")
        server.serve_forever()
    else:
        poll_thread.join()


if __name__ == "__main__":
    main()
