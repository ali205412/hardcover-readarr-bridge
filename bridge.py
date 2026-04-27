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
ABS_URL = os.getenv("ABS_URL", "")
ABS_TOKEN = os.getenv("ABS_TOKEN", "")
ABS_SYNC_ENABLED = os.getenv("ABS_SYNC_ENABLED", "false").lower() == "true"
BIND_ADDRESS = os.getenv("BIND_ADDRESS", "0.0.0.0")
MAX_WEBHOOK_BODY = 1024 * 1024  # 1 MB

HARDCOVER_API = "https://api.hardcover.app/v1/graphql"

_sync_lock = Lock()
_last_hc_request = 0.0  # timestamp of last Hardcover API call


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


def hardcover_query(query, variables=None, retries=2):
    global _last_hc_request
    # Enforce 1.1s between requests (max ~54/min, under 60/min limit)
    elapsed = time.time() - _last_hc_request
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_hc_request = time.time()

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
    except HTTPError as e:
        if e.code == 429 and retries > 0:
            log.warning("Rate limited, waiting 60s before retry...")
            time.sleep(60)
            return hardcover_query(query, variables, retries - 1)
        log.exception("Hardcover API error")
        return None
    except URLError:
        log.exception("Hardcover connection error")
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
            me = data["me"]
            if isinstance(me, list):
                me = me[0] if me else {}
            books = me.get("user_books", [])
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
        if ABS_SYNC_ENABLED:
            sync_abs_to_hardcover()
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


def abs_get(path):
    req = Request(
        f"{ABS_URL}{path}",
        headers={"Authorization": f"Bearer {ABS_TOKEN}"},
    )
    try:
        resp = urlopen(req, timeout=30)
        body = resp.read()
        return json.loads(body) if body else None
    except (HTTPError, URLError):
        log.exception("ABS API error %s", path)
        return None


def hardcover_search_book(title, author=None, isbn=None, asin=None):
    # 1. Try ISBN first (most reliable)
    if isbn:
        query = """
        query ByISBN($isbn: String!) {
            editions(where: {isbn_13: {_eq: $isbn}}, limit: 1) {
                book { id title slug }
            }
        }
        """
        data = hardcover_query(query, {"isbn": isbn})
        if data:
            for ed in data.get("editions", []):
                book = ed.get("book", {})
                if book.get("id"):
                    log.debug("Matched by ISBN: %s -> %s", isbn, book.get("title"))
                    return book

    # 2. Try ASIN
    if asin:
        query = """
        query ByASIN($asin: String!) {
            editions(where: {asin: {_eq: $asin}}, limit: 1) {
                book { id title slug }
            }
        }
        """
        data = hardcover_query(query, {"asin": asin})
        if data:
            for ed in data.get("editions", []):
                book = ed.get("book", {})
                if book.get("id"):
                    log.debug("Matched by ASIN: %s -> %s", asin, book.get("title"))
                    return book

    # 3. Fallback: title + author via editions (requires both to prevent mismatches)
    if title and author:
        search_title = title.split(":")[0].strip() if ":" in title else title
        query = """
        query ByTitleAuthor($title: String!) {
            editions(where: {title: {_eq: $title}}, limit: 10) {
                title
                book { id title slug contributions { author { name } } }
            }
        }
        """
        for t in ([search_title, title] if search_title != title else [title]):
            data = hardcover_query(query, {"title": t})
            if data:
                author_lower = author.lower()
                for ed in data.get("editions", []):
                    book = ed.get("book", {})
                    if not book.get("id"):
                        continue
                    # Verify author matches
                    book_authors = [c.get("author", {}).get("name", "").lower()
                                    for c in book.get("contributions", [])]
                    if any(author_lower in a or a in author_lower for a in book_authors):
                        log.debug("Matched by title+author: %s by %s", t, author)
                        return {"id": book["id"], "title": book["title"], "slug": book.get("slug")}

    log.debug("No match for: %s by %s", title, author)
    return None


def hardcover_set_book_status(book_id, status_id, progress=None):
    # 1. Set book status (adds to shelf)
    mutation = """
    mutation SetStatus($bookId: Int!, $statusId: Int!) {
        insert_user_book(object: {book_id: $bookId, status_id: $statusId}) {
            id
        }
    }
    """
    result = hardcover_query(mutation, {"bookId": book_id, "statusId": status_id})
    if not result:
        return None

    # 2. Update progress if provided and book is in-progress
    if progress is not None and 0 < progress < 1.0:
        user_book = result.get("insert_user_book", {})
        user_book_id = user_book.get("id")

        if user_book_id:
            # No existing read, create one with progress
            pct = round(progress * 100)
            insert_read = """
            mutation InsertRead($userBookId: Int!, $read: UserBookReadInput!) {
                insert_user_book_read(user_book_id: $userBookId, user_book_read: $read) { id }
            }
            """
            hardcover_query(insert_read, {"userBookId": user_book_id, "read": {"progress": pct}})
            log.debug("Created read with progress %d%% for user_book %d", pct, user_book_id)

    return result


def sync_abs_to_hardcover():
    if not ABS_URL or not ABS_TOKEN or not HARDCOVER_TOKEN:
        log.debug("ABS sync not configured, skipping")
        return

    log.info("Syncing ABS listening history to Hardcover")

    state = load_state()
    if "abs_synced" not in state:
        state["abs_synced"] = {}

    # Get ABS user progress
    me = abs_get("/api/me")
    if not me:
        log.error("Failed to get ABS user data")
        return

    progress_list = me.get("mediaProgress", [])
    finished = [p for p in progress_list if p.get("isFinished")]
    in_progress = [p for p in progress_list if not p.get("isFinished") and p.get("progress", 0) > 0]

    log.info("ABS: %d finished, %d in progress", len(finished), len(in_progress))

    added = 0
    skipped = 0
    failed = 0

    for progress in finished + in_progress:
        lib_item_id = progress.get("libraryItemId", "")
        state_key = f"abs_{lib_item_id}"

        if state_key in state["abs_synced"]:
            skipped += 1
            continue

        # Get book details from ABS
        item = abs_get(f"/api/items/{lib_item_id}")
        if not item:
            failed += 1
            continue

        meta = item.get("media", {}).get("metadata", {})
        title = meta.get("title", "")
        author = meta.get("authorName", "")
        isbn = meta.get("isbn", "")
        asin = meta.get("asin", "")

        if not title:
            failed += 1
            continue

        # Search Hardcover by ISBN/ASIN first, then title
        hc_book = hardcover_search_book(title, author, isbn=isbn, asin=asin)
        if not hc_book:
            log.warning("Not found on Hardcover: %s by %s", title, author)
            failed += 1
            continue

        hc_book_id = hc_book.get("id")
        is_finished = progress.get("isFinished")
        abs_progress = progress.get("progress", 0)
        status_id = 3 if is_finished else 2  # 3=Read, 2=Currently Reading

        if DRY_RUN:
            status_label = "Read" if status_id == 3 else f"Reading ({abs_progress*100:.0f}%)"
            log.info("[DRY RUN] Would set %s -> %s on Hardcover", title, status_label)
            added += 1
            state["abs_synced"][state_key] = {
                "title": title,
                "hardcover_id": hc_book_id,
                "status": status_id,
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            continue

        result = hardcover_set_book_status(hc_book_id, status_id, progress=abs_progress if not is_finished else None)
        if result:
            status_label = "Read" if status_id == 3 else f"Reading ({abs_progress*100:.0f}%)"
            log.info("Hardcover: %s -> %s", title, status_label)
            state["abs_synced"][state_key] = {
                "title": title,
                "hardcover_id": hc_book_id,
                "status": status_id,
                "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            added += 1
        else:
            log.error("Failed to set status on Hardcover: %s", title)
            failed += 1

        time.sleep(1)  # Hardcover rate limit: 60/min

    save_state(state)
    log.info("ABS -> Hardcover sync: added=%d skipped=%d failed=%d", added, skipped, failed)


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

        if self.path == "/sync-abs":
            log.info("Manual ABS -> Hardcover sync triggered via API")
            Thread(target=sync_abs_to_hardcover, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "abs_sync_started"}).encode())
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
