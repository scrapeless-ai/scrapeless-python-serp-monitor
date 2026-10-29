"""Track first-page Google organic rankings with Scrapeless and SQLite.

Python 3.9+, standard library only. Run `python serp_monitor.py --help`.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager, ExitStack
import threading
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urljoin, parse_qs
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_BASE = "https://api.scrapeless.com"
ACTOR = "scraper.google.search"
FIELDS = [
    "run_id", "captured_at", "keyword", "target_domain", "status",
    "current_rank", "previous_rank", "rank_change", "current_url",
    "previous_url", "organic_count", "previous_organic_count", "previous_at",
    "gl", "hl", "google_domain", "location", "error",
    "snapshot_id", "baseline_snapshot_id", "submitted_at", "received_at",
    "time_basis", "comparison_state", "comparison_note", "report_version", "report_state",
]


class MonitorError(Exception):
    """Expected user-facing failure; never include credentials."""


class Busy(MonitorError):
    """Another local process owns the database; exit code 75."""


class EmptyUnverified(MonitorError):
    """An empty organic array is not a confirmed absence observation."""


class HttpFailure(MonitorError):
    def __init__(self, status: int, hint: str):
        self.status = status
        super().__init__("HTTP " + str(status) + ": " + hint)


class PendingTask(MonitorError):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__("Polling timed out; resume this run to query the existing task.")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalized_domain(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MonitorError("Each target domain must be a nonempty hostname.")
    value = value.strip().lower().rstrip(".")
    if any(c in value for c in "/:@?# \\"):
        raise MonitorError("Use hostnames such as scrapeless.com, without a URL or path.")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise MonitorError("Invalid target domain.") from None
    if len(value) > 253 or "." not in value or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
        for part in value.split(".")
    ):
        raise MonitorError("Invalid target domain.")
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitorError("Cannot read configuration JSON: " + str(path)) from exc
    if not isinstance(config, dict):
        raise MonitorError("Configuration must be a JSON object.")
    allowed = {"keywords", "target_domains", "gl", "hl", "google_domain", "location"}
    if set(config) - allowed:
        raise MonitorError("Unknown configuration fields: " + ", ".join(sorted(set(config) - allowed)))
    for field in ("keywords", "target_domains"):
        values = config.get(field)
        if not isinstance(values, list) or not values or not all(
            isinstance(v, str) and v.strip() for v in values
        ):
            raise MonitorError(field + " must be a nonempty list of strings.")
        config[field] = [v.strip() for v in values]
    config["target_domains"] = [normalized_domain(v) for v in config["target_domains"]]
    for field in ("keywords", "target_domains"):
        if len(set(config[field])) != len(config[field]):
            raise MonitorError(field + " contains duplicates.")
    for field, default in (("gl", "us"), ("hl", "en"), ("google_domain", "google.com")):
        value = config.setdefault(field, default)
        if not isinstance(value, str) or not value.strip():
            raise MonitorError(field + " must be a nonempty string.")
        config[field] = value.strip().lower()
    if not re.fullmatch(r"[a-z]{2}", config["gl"]):
        raise MonitorError("gl must be a two-letter country code.")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", config["hl"]):
        raise MonitorError("Invalid hl language code.")
    config["google_domain"] = normalized_domain(config["google_domain"])
    if "location" in config:
        if not isinstance(config["location"], str) or not config["location"].strip():
            raise MonitorError("location must be a nonempty string, or omitted.")
        config["location"] = config["location"].strip()
    return config


def load_key(env_file: Path) -> str:
    # Environment wins over .env; never execute or source the file.
    key = os.environ.get("SCRAPELESS_API_KEY", "").strip()
    if not key and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"\s*(?:export\s+)?SCRAPELESS_API_KEY\s*=\s*(.*?)\s*", line)
            if match:
                key = match.group(1)
                if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                    key = key[1:-1]
    if not key or key in {"YOUR_SCRAPELESS_API_KEY", "YOUR_API_KEY"}:
        raise MonitorError("Set SCRAPELESS_API_KEY in the environment or your local .env file.")
    if any(c.isspace() for c in key):
        raise MonitorError("SCRAPELESS_API_KEY must not contain whitespace.")
    return key


def search_input(config: dict[str, Any], keyword: str) -> dict[str, Any]:
    data = {k: config[k] for k in ("gl", "hl", "google_domain")}
    data.update(q=keyword, start=0, device="desktop")
    if config.get("location"):
        data["location"] = config["location"]
    # Do not set num: the API reference recommends omitting it unless needed.
    return data


def scope_key(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parse_organic(payload: Any, resolve_link=None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise MonitorError("Expected a JSON object from the Google Search API.")
    if payload.get("error") or payload.get("success") is False or (
        "code" in payload and payload["code"] not in (0, 200, "0", "200")
    ) or str(payload.get("status", "")).lower() in {"failed", "error"}:
        raise MonitorError("API returned an error object instead of search results.")
    organic = payload.get("organic_results")
    if not isinstance(organic, list):
        raise MonitorError("Missing organic_results array; response is not a usable ranking snapshot.")
    if not organic:
        raise EmptyUnverified("Empty organic array requires confirmation; excluded from ranking baselines.")
    result = []
    positions = set()
    for item in organic:
        if not isinstance(item, dict):
            raise MonitorError("Invalid organic result object.")
        position, link = item.get("position"), item.get("link")
        original_link = link
        if resolve_link is not None and isinstance(link, str):
            link = resolve_link(link)
        if type(position) is not int or position < 1 or position in positions:
            raise MonitorError("Organic positions must be unique positive integers.")
        try:
            parsed = urlsplit(link) if isinstance(link, str) else None
            valid_url = parsed and parsed.scheme in {"http", "https"} and parsed.hostname
            if valid_url:
                normalized_domain(parsed.hostname)
        except (ValueError, MonitorError):
            valid_url = False
        if not valid_url:
            raise MonitorError("Organic result has no valid absolute HTTP(S) destination URL.")
        positions.add(position)
        normalized = {"position": position, "link": link, "title": str(item.get("title", ""))}
        if link != original_link:
            normalized.update(source_link=original_link, link_source="google_redirect_location")
        result.append(normalized)
    return sorted(result, key=lambda row: row["position"])


def best_match(organic: list[dict[str, Any]], domain: str) -> Optional[dict[str, Any]]:
    for item in sorted(organic, key=lambda row: row["position"]):
        host = normalized_domain(urlsplit(item["link"]).hostname or "")
        if host == domain or host.endswith("." + domain):
            return item
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API token to a redirected host.
        return None


class ApiClient:
    def __init__(self, key: str, timeout: float = 60, poll_timeout: float = 120,
                 poll_interval: float = 2):
        self.key = key
        self.timeout = timeout
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval
        self.opener = build_opener(NoRedirect())
        self.redirect_opener = build_opener(NoRedirect())
        self.google_redirect_lookups = 0
        self._redirect_cache = {}
        self._google_domain = "google.com"

    def request(self, method: str, path: str, body: Any = None,
                timeout: Optional[float] = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = Request(API_BASE + path, data=data, method=method, headers={
            "x-api-token": self.key, "Content-Type": "application/json",
            "User-Agent": "scrapeless-python-serp-monitor/0.1.0",
        })
        try:
            response = self.opener.open(req, timeout=timeout or self.timeout)
        except HTTPError as exc:
            # Error body may echo secrets. Expose only a fixed hint and HTTP code.
            status = exc.code
            exc.close()
            hints = {400: "Check search parameters.", 401: "Check your API key.",
                     402: "Check account balance.", 403: "Check token permissions.",
                     429: "Rate limited; reduce request frequency.",
                     500: "Server error; wait before trying again."}
            return status, {"_http_error": hints.get(status, "API request failed.")}
        except (URLError, TimeoutError, OSError, HTTPException):
            raise MonitorError("Network request failed or timed out; POST was not automatically retried.") from None
        with response:
            try:
                return response.status, json.loads(response.read())
            except (ValueError, UnicodeError):
                raise MonitorError("API returned non-JSON content.") from None
            except (OSError, HTTPException):
                raise MonitorError("Reading the API response failed; the request was not automatically retried.") from None

    def resolve_link(self, link: str) -> str:
        """Resolve only recognized Google wrappers, without following destination sites."""
        try:
            parsed = urlsplit(link)
        except ValueError:
            return link  # The normal URL validator reports the malformed item.
        domain = self._google_domain.lower().removeprefix("www.")
        if not re.fullmatch(r"google\.(?:com|[a-z]{2}|(?:co|com)\.[a-z]{2})", domain):
            domain = "google.com"
        trusted = {domain, "www." + domain, "google.com", "www.google.com"}
        relative = not parsed.scheme and not parsed.netloc and link.startswith("/")
        trusted_absolute = parsed.scheme == "https" and parsed.hostname in trusted and not parsed.username and not parsed.password
        if parsed.path not in {"/goto", "/url"} or not (relative or trusted_absolute):
            return link
        if link in self._redirect_cache:
            return self._redirect_cache[link]
        params = parse_qs(parsed.query)
        targets = params.get("url", []) + params.get("q", [])
        # Plain-text destinations can be decoded locally. Opaque tokens require Google's Location header.
        if len(targets) == 1 and self._absolute_destination(targets[0]):
            self._redirect_cache[link] = targets[0]
            return targets[0]
        current = urljoin("https://www." + domain, link)
        for _ in range(3):
            current_url = urlsplit(current)
            if current_url.scheme != "https" or current_url.hostname not in trusted or current_url.path not in {"/goto", "/url"}:
                break
            self.google_redirect_lookups += 1
            # Separate opener and explicit headers: no Scrapeless key or cookie is sent to Google.
            request = Request(current, method="GET", headers={"User-Agent": "scrapeless-python-serp-monitor/0.1.0"})
            try:
                response = self.redirect_opener.open(request, timeout=min(10, self.timeout))
            except HTTPError as exc:
                status, location = exc.code, exc.headers.get("Location")
                exc.close()
                if status not in {301, 302, 303, 307, 308} or not location:
                    raise MonitorError("Google result redirect could not be resolved; no destination inferred.") from None
            except (URLError, TimeoutError, OSError, HTTPException):
                raise MonitorError("Google result redirect lookup failed; no new search was submitted.") from None
            else:
                response.close()
                raise MonitorError("Google result redirect did not return a Location header; no destination inferred.")
            destination = urljoin(current, location)
            if not self._absolute_destination(destination):
                raise MonitorError("Google result redirect returned an invalid destination.")
            final = urlsplit(destination)
            if final.hostname in trusted and final.path in {"/goto", "/url"}:
                current = destination
                continue
            self._redirect_cache[link] = destination
            return destination  # Never request the third-party destination.
        raise MonitorError("Google result redirect exceeded the bounded resolution limit.")

    @staticmethod
    def _absolute_destination(link):
        try:
            parsed = urlsplit(link)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                return False
            parsed.port  # Reject malformed ports.
            normalized_domain(parsed.hostname)
            return True
        except (ValueError, MonitorError):
            return False

    def search(self, data: dict[str, Any], on_task: Callable[[str], None],
               task_id: Optional[str] = None) -> dict[str, Any]:
        self._google_domain = data.get("google_domain", "google.com")
        if not task_id:
            status, payload = self.request("POST", "/api/v1/scraper/request", {
                "actor": ACTOR, "input": data,
            })
            if status == 200:
                return payload
            if status != 201:
                self.raise_http(status, payload)
            task_id = payload.get("taskId") if isinstance(payload, dict) else None
            if not isinstance(task_id, str) or not task_id.strip():
                raise MonitorError("HTTP 201 response did not contain a taskId.")
            on_task(task_id)
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            status, payload = self.request(
                "GET", "/api/v1/scraper/result/" + quote(task_id, safe=""),
                timeout=min(self.timeout, remaining),
            )
            if status == 200:
                return payload
            if status == 201:
                continue
            if status in {429, 500, 502, 503, 504}:
                # Back off on GET errors. Docs recommend a minute after HTTP 500.
                delay = 60 if status == 500 else max(5, self.poll_interval)
                time.sleep(min(delay, max(0, deadline - time.monotonic())))
                continue  # Only result GETs are repeated, always for the same task.
            self.raise_http(status, payload)
        raise PendingTask(task_id)

    @staticmethod
    def raise_http(status: int, payload: Any) -> None:
        hint = payload.get("_http_error", "Unexpected API response.") if isinstance(payload, dict) else "Unexpected API response."
        raise HttpFailure(status, hint)


_held_locks = threading.local()


@contextmanager
def database_lock(path: Path):
    """Nonblocking, reentrant per thread. All entry points share the same lock."""
    if str(path) == ":memory:":
        yield
        return
    try:
        import fcntl
    except ImportError:
        raise MonitorError("Collection requires POSIX file locks (macOS or Linux).") from None
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    held = getattr(_held_locks, "paths", {})
    if key in held:
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("Database is busy; another collection or migration is running.") from None
        held[key] = handle
        _held_locks.paths = held
        try:
            yield
        finally:
            del held[key]
            fcntl.flock(handle, fcntl.LOCK_UN)
    # Keep the sidecar file: unlinking it can create competing lock inodes.


def database_path(db):
    name = db.execute("PRAGMA database_list").fetchone()[2]
    return Path(name) if name else Path(":memory:")


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path) if str(path) == ":memory:" else Path(path).resolve()
    with database_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            db.close()
            raise MonitorError("Database schema is newer than this program.")
        existing = db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone()
        if existing and version < 2 and str(path) != ":memory:":
            backup_path = path.with_name(path.name + ".pre-v2.sqlite")
            if backup_path.exists():
                db.close()
                raise MonitorError("Migration backup already exists; preserve it and move it aside before retrying migration.")
            backup = sqlite3.connect(str(backup_path))
            try:
                db.backup(backup)
            finally:
                backup.close()
            backup_path.chmod(0o600)
        try:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    mode TEXT NOT NULL, config_json TEXT NOT NULL, status TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    keyword TEXT NOT NULL, scope_key TEXT NOT NULL, input_json TEXT NOT NULL,
                    status TEXT NOT NULL, task_id TEXT, captured_at TEXT, raw_json TEXT,
                    organic_json TEXT, error TEXT, UNIQUE(run_id, keyword))""")
                additions = {
                    "submitted_at": "TEXT", "received_at": "TEXT", "time_basis": "TEXT",
                    "baseline_id": "INTEGER REFERENCES snapshots(id)",
                    "comparison_state": "TEXT", "comparison_locked": "INTEGER NOT NULL DEFAULT 0",
                    "diagnostic_json": "TEXT"}
                columns = {r[1] for r in db.execute("PRAGMA table_info(snapshots)")}
                for name, declaration in additions.items():
                    if name not in columns:
                        db.execute("ALTER TABLE snapshots ADD COLUMN " + name + " " + declaration)
                if "report_version" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}:
                    db.execute("ALTER TABLE runs ADD COLUMN report_version INTEGER NOT NULL DEFAULT 0")
                db.execute("""CREATE TABLE IF NOT EXISTS report_versions (
                    run_id TEXT NOT NULL REFERENCES runs(id), version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, state TEXT NOT NULL, rows_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, version))""")
                db.execute("CREATE INDEX IF NOT EXISTS snapshot_scope ON snapshots(scope_key, status, id)")
                if version < 2 and existing:
                    db.execute("""UPDATE snapshots SET received_at=captured_at, time_basis='legacy_unknown',
                        comparison_state='legacy_unknown', comparison_locked=1 WHERE status='success'""")
                    for row in db.execute("SELECT id,organic_json FROM snapshots WHERE status='success'").fetchall():
                        if json.loads(row['organic_json'] or 'null') == []:
                            db.execute("""UPDATE snapshots SET status='empty_unverified',
                                error='Legacy empty array requires confirmation; excluded from baselines.' WHERE id=?""", (row['id'],))
                    db.execute("""UPDATE runs SET status='partial' WHERE EXISTS
                        (SELECT 1 FROM snapshots WHERE run_id=runs.id AND status!='success')""")
                db.execute("PRAGMA user_version = 2")
        except Exception:
            db.close()
            raise
        return db


def create_run(db: sqlite3.Connection, config: dict[str, Any], mode: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    with db:
        db.execute("""INSERT INTO runs (id,started_at,mode,config_json,status)
            VALUES (?, ?, ?, ?, 'running')""", (run_id, utc_now(), mode, json.dumps(config, ensure_ascii=False)))
        for keyword in config["keywords"]:
            data = search_input(config, keyword)
            db.execute("""INSERT INTO snapshots
                (run_id, keyword, scope_key, input_json, status) VALUES (?, ?, ?, ?, 'queued')""",
                       (run_id, keyword, scope_key(data), json.dumps(data, ensure_ascii=False)))
    return run_id


def parse_time(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else None
    except ValueError:
        return None


def freeze_comparison(db, snapshot_id):
    current = db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if current['comparison_locked']:
        return
    start, end = parse_time(current['submitted_at']), parse_time(current['received_at'])
    baseline, state = None, 'baseline'
    if start is None or end is None or start > end:
        state = 'not_comparable'
    else:
        mode = db.execute("SELECT mode FROM runs WHERE id=?", (current['run_id'],)).fetchone()[0]
        candidates = db.execute("""SELECT s.* FROM snapshots s JOIN runs r ON r.id=s.run_id
            WHERE s.scope_key=? AND s.id!=? AND r.mode=?""", (current['scope_key'], snapshot_id, mode)).fetchall()
        earlier, overlap = [], False
        for other in candidates:
            received = parse_time(other['received_at'])
            submitted = parse_time(other['submitted_at'])
            if other['status'] == 'success' and received is not None and received <= start:
                earlier.append((received, other['id']))
            elif other['status'] in {'success', 'pending', 'requesting'}:
                # The collection instant is not known inside overlapping request intervals.
                # Include an unfinished saved task: later retrieval must not rewrite this report.
                if (submitted is None or submitted < end) and (received is None or received > start):
                    overlap = True
        if overlap:
            state = 'not_comparable'
        elif earlier:
            baseline = max(earlier)[1]
            state = 'comparable'
    db.execute("""UPDATE snapshots SET baseline_id=?, comparison_state=?, comparison_locked=1
        WHERE id=? AND comparison_locked=0""", (baseline, state, snapshot_id))


def diagnostics(payload, normalized=None):
    organic = payload.get('organic_results') if isinstance(payload, dict) else None
    invalid = []
    resolved = {row["position"] for row in (normalized or []) if row.get("link_source")}
    resolved_indexes = []
    if isinstance(organic, list):
        for index, item in enumerate(organic):
            if isinstance(item, dict) and item.get("position") in resolved:
                resolved_indexes.append(index)
                continue
            try:
                parse_organic({'organic_results': [item]})
            except MonitorError:
                invalid.append(index)
    # No response values, titles, URLs or credentials in diagnostic summaries.
    info = {'organic_array_length': len(organic) if isinstance(organic, list) else None, 'invalid_item_indexes': invalid}
    if resolved_indexes:
        info['resolved_google_redirect_indexes'] = resolved_indexes
    return json.dumps(info)


def run_collection(db: sqlite3.Connection, run_id: str, client: Any,
                   progress: Callable[[str], None] = print) -> bool:
    with database_lock(database_path(db)):
        return _run_collection(db, run_id, client, progress)


def _run_collection(db, run_id, client, progress):
    rows = db.execute("SELECT * FROM snapshots WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    if not rows:
        raise MonitorError("Unknown or empty run: " + run_id)
    changed = False
    for row in rows:
        if row['status'] == 'success':
            continue
        if row['status'] == 'empty_unverified' or (row['status'] != 'queued' and not row['task_id']):
            progress(row['keyword'] + ': not resubmitted; inspect this incomplete observation.')
            continue
        snapshot_id = row['id']
        with db:
            claimed = db.execute("""UPDATE snapshots SET status='requesting',error=NULL,
                submitted_at=CASE WHEN task_id IS NULL THEN ? ELSE submitted_at END,
                time_basis=CASE WHEN task_id IS NULL THEN 'request_interval' ELSE time_basis END
                WHERE id=? AND status=? AND task_id IS ?""", (utc_now(), snapshot_id, row['status'], row['task_id'])).rowcount
        if not claimed:
            continue
        changed = True
        def on_task(task_id):
            with db:
                db.execute("UPDATE snapshots SET task_id=?,status='pending' WHERE id=?", (task_id, snapshot_id))
        payload, received = None, None
        try:
            payload = client.search(json.loads(row['input_json']), on_task, row['task_id'])
            received = utc_now()
            organic = parse_organic(payload, resolve_link=getattr(client, "resolve_link", None))
            with db:
                db.execute("""UPDATE snapshots SET status='success',captured_at=?,received_at=?,
                    raw_json=?,organic_json=?,error=NULL,diagnostic_json=? WHERE id=?""",
                    (received, received, json.dumps(payload, ensure_ascii=False), json.dumps(organic, ensure_ascii=False), diagnostics(payload, organic), snapshot_id))
                freeze_comparison(db, snapshot_id)
            progress(row['keyword'] + ': ' + str(len(organic)) + ' organic results saved')
        except MonitorError as exc:
            state = 'empty_unverified' if isinstance(exc, EmptyUnverified) else 'pending' if isinstance(exc, PendingTask) else 'error'
            with db:
                db.execute("""UPDATE snapshots SET status=?,error=?,received_at=?,diagnostic_json=? WHERE id=?""",
                           (state, str(exc), received, diagnostics(payload), snapshot_id))
            progress(row['keyword'] + ': ' + str(exc))
            if isinstance(exc, HttpFailure) and exc.status in {401,402,403,429}:
                progress('Stopped this batch; remaining keywords were not submitted.')
                break
    failures = db.execute("SELECT COUNT(*) FROM snapshots WHERE run_id=? AND status!='success'", (run_id,)).fetchone()[0]
    if changed:
        with db:
            db.execute("UPDATE runs SET status=?,finished_at=? WHERE id=?", ('partial' if failures else 'success', utc_now(), run_id))
            publish_report(db, run_id)
    return failures == 0


def build_rows(db, run_id, version, report_state):
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    config = json.loads(run['config_json'])
    rows = []
    for current in db.execute("SELECT * FROM snapshots WHERE run_id=? ORDER BY id", (run_id,)):
        previous = db.execute("SELECT * FROM snapshots WHERE id=?", (current['baseline_id'],)).fetchone() if current['baseline_id'] else None
        data = json.loads(current['input_json'])
        for domain in config['target_domains']:
            row = dict.fromkeys(FIELDS, '')
            row.update(run_id=run_id, captured_at=current['received_at'] or '', snapshot_id=current['id'],
                       keyword=current['keyword'], target_domain=domain, baseline_snapshot_id=current['baseline_id'] or '',
                       submitted_at=current['submitted_at'] or '', received_at=current['received_at'] or '',
                       time_basis=current['time_basis'] or '', comparison_state=current['comparison_state'] or '',
                       report_version=version, report_state=report_state,
                       gl=data['gl'], hl=data['hl'], google_domain=data['google_domain'], location=data.get('location',''))
            if current['status'] != 'success':
                row.update(status=current['status'] if current['status'] in {'pending','empty_unverified'} else 'error',
                           error=current['error'] or 'Snapshot incomplete; no ranking accepted.')
                rows.append(row)
                continue
            organic = json.loads(current['organic_json'])
            match = best_match(organic, domain)
            rank = match['position'] if match else None
            row.update(current_rank=rank if rank is not None else '', current_url=match['link'] if match else '', organic_count=len(organic))
            if current['comparison_state'] in {'not_comparable','legacy_unknown'}:
                row.update(status='not_comparable', comparison_note='Collection order is not established; no ranking change calculated.')
            elif previous is None:
                row['status'] = 'baseline' if rank is not None else 'baseline_absent'
            else:
                old_organic = json.loads(previous['organic_json'])
                old_match = best_match(old_organic, domain)
                old_rank = old_match['position'] if old_match else None
                row.update(previous_rank=old_rank if old_rank is not None else '', previous_url=old_match['link'] if old_match else '',
                           previous_organic_count=len(old_organic), previous_at=previous['received_at'] or '')
                if old_rank is None:
                    row['status'] = 'new' if rank is not None else 'still_absent'
                elif rank is None:
                    row['status'] = 'lost'
                else:
                    delta = old_rank - rank
                    row.update(rank_change=delta,status='up' if delta > 0 else 'down' if delta < 0 else 'unchanged')
            rows.append(row)
    return rows


def publish_report(db, run_id):
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise MonitorError('Unknown run: ' + run_id)
    version = run['report_version'] + 1
    states = [r[0] for r in db.execute('SELECT status FROM snapshots WHERE run_id=?', (run_id,))]
    state = 'complete' if states and all(s == 'success' for s in states) else 'partial'
    rows = build_rows(db, run_id, version, state)
    db.execute('INSERT INTO report_versions VALUES (?,?,?,?,?)', (run_id,version,utc_now(),state,json.dumps(rows,ensure_ascii=False)))
    db.execute('UPDATE runs SET report_version=? WHERE id=?', (version,run_id))
    return rows


def report_rows(db: sqlite3.Connection, run_id: str, version=None) -> list[dict[str, Any]]:
    with database_lock(database_path(db)):
        run = db.execute('SELECT report_version FROM runs WHERE id=?', (run_id,)).fetchone()
        if run is None:
            raise MonitorError('Unknown run: ' + run_id)
        if version is None and not run['report_version']:
            with db:
                publish_report(db, run_id)
            run = db.execute('SELECT report_version FROM runs WHERE id=?', (run_id,)).fetchone()
        record = db.execute('SELECT rows_json FROM report_versions WHERE run_id=? AND version=?',
                            (run_id, version if version is not None else run['report_version'])).fetchone()
        if record is None:
            raise MonitorError('Unknown report version for this run.')
        return json.loads(record[0])


def csv_cell(value: Any) -> Any:
    # Protect spreadsheet users from formulas in queries, titles, or URLs.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_report(db: sqlite3.Connection, run_id: str, output: Path, version=None) -> list[dict[str, Any]]:
    rows = report_rows(db, run_id, version)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({key: csv_cell(value) for key, value in row.items()} for row in rows)
    os.replace(temporary, output)
    return rows


class DemoClient:
    """Synthetic data, deliberately separate from live snapshots."""
    def __init__(self, round_number: int):
        self.round_number = round_number

    def search(self, data, on_task, task_id=None):
        keywords = {"scrapeless": (3, 1), "web scraping api": (2, 4), "google search api": (None, 2)}
        position = keywords[data["q"]][self.round_number]
        return {"organic_results": [
            {"position": i, "title": "Synthetic example " + str(i),
             "link": "https://www.scrapeless.com/en" if i == position else "https://example.org/page/" + str(i)}
            for i in range(1, 6)
        ], "demo": True}


def positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a positive number.") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Expected a finite positive number.")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Make live API calls and export a CSV report")
    collect.add_argument("--config", type=Path, default=Path("config.json"))
    collect.add_argument("--env-file", type=Path, default=Path(".env"))
    collect.add_argument("--db", type=Path, default=Path("data/rankings.sqlite"))
    collect.add_argument("--output", type=Path, default=None, help="CSV path; default output/<run-id>.csv")
    collect.add_argument("--resume", metavar="RUN_ID", help="Resume saved tasks; never repost an uncertain request")
    collect.add_argument("--timeout", type=positive_number, default=60)
    collect.add_argument("--poll-timeout", type=positive_number, default=120)
    collect.add_argument("--poll-interval", type=positive_number, default=2)
    export = sub.add_parser("export", help="Re-export a saved run; no API calls")
    export.add_argument("--db", type=Path, default=Path("data/rankings.sqlite"))
    export.add_argument("--run-id", help="Default: latest saved run")
    export.add_argument("--output", type=Path, default=None)
    export.add_argument("--version", type=int, help="Read an immutable saved report version")
    history = sub.add_parser("history", help="List saved runs; no API calls")
    history.add_argument("--db", type=Path, default=Path("data/rankings.sqlite"))
    demo = sub.add_parser("demo", help="Run two synthetic snapshots, without an API key")
    demo.add_argument("--output-dir", type=Path, default=Path("output/demo"))
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    db = None
    resources = ExitStack()
    try:
        if args.command == "demo":
            db = connect(Path(":memory:"))
            config = {"keywords": ["scrapeless", "web scraping api", "google search api"],
                      "target_domains": ["scrapeless.com"], "gl": "us", "hl": "en",
                      "google_domain": "google.com", "location": "Austin, Texas, United States"}
            for number in range(2):
                run_id = create_run(db, config, "demo")
                run_collection(db, run_id, DemoClient(number))
                rows = export_report(db, run_id, args.output_dir / ("round-" + str(number + 1) + ".csv"))
            print("SYNTHETIC DEMO ONLY: no live API calls, no live database modified.")
            for row in rows:
                print("{}: {} -> {} ({}, change={})".format(
                    row["keyword"], row["previous_rank"], row["current_rank"], row["status"], row["rank_change"]))
            print("CSV reports: " + str(args.output_dir))
            return 0
        if args.command == "collect":
            key = load_key(args.env_file)
            config = None if args.resume else load_config(args.config)
            resources.enter_context(database_lock(args.db))
            db = connect(args.db)
            if args.resume:
                run = db.execute("SELECT * FROM runs WHERE id = ?", (args.resume,)).fetchone()
                if not run or run["mode"] != "live":
                    raise MonitorError("Resume requires an existing live run in this database.")
                run_id = args.resume
            else:
                run_id = create_run(db, config, "live")
            print("Run: " + run_id, flush=True)
            success = run_collection(db, run_id, ApiClient(key, args.timeout, args.poll_timeout, args.poll_interval),
                                     lambda text: print(text, flush=True))
            revision = report_rows(db, run_id)[0]["report_version"]
            output = args.output or Path("output") / (run_id + "-v" + str(revision) + ".csv")
            export_report(db, run_id, output)
            print("CSV: " + str(output))
            if not success:
                print("Run incomplete. Inspect the CSV and use --resume " + run_id + " for saved task IDs.", file=sys.stderr)
                return 1
            return 0
        if not args.db.is_file():
            raise MonitorError("Database does not exist. Run collect first.")
        db = connect(args.db)
        if args.command == "history":
            for row in db.execute("SELECT id, started_at, mode, status FROM runs ORDER BY started_at DESC"):
                print("\t".join(str(value) for value in row))
            return 0
        latest = db.execute("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        run_id = args.run_id or (latest[0] if latest else None)
        if run_id is None:
            raise MonitorError("Database contains no runs.")
        revision = report_rows(db, run_id, args.version)[0]["report_version"]
        output = args.output or Path("output") / (run_id + "-v" + str(revision) + ".csv")
        export_report(db, run_id, output, revision)
        print("CSV: " + str(output))
        return 0
    except Busy as exc:
        print("Busy: " + str(exc), file=sys.stderr)
        return 75
    except (MonitorError, OSError, sqlite3.Error) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Use history and collect --resume RUN_ID to query saved task IDs.", file=sys.stderr)
        return 130
    finally:
        if db is not None:
            db.close()
        resources.close()


if __name__ == "__main__":
    raise SystemExit(main())
