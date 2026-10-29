"""Exercise the real HTTP transport against a local simulated API, without credits."""
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import serp_monitor as monitor


class TestHttpWorkflow(unittest.TestCase):
    def test_real_transport_sqlite_and_csv_for_sync_and_async(self):
        calls = []
        polls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, self.headers.get("x-api-token"), body))
                if body["input"]["q"] == "async":
                    self.send_json(201, {"taskId": "local-task"})
                else:
                    self.send_json(200, {"organic_results": [
                        {"position": 2, "link": "https://example.com/sync"}
                    ]})

            def do_GET(self):
                polls.append(self.path)
                if len(polls) == 1:
                    self.send_json(201, {"message": "working"})
                else:
                    self.send_json(200, {"organic_results": [
                        {"position": 1, "link": "https://example.com/async"}
                    ]})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                conf = {"keywords": ["sync", "async"], "target_domains": ["example.com"],
                        "gl": "us", "hl": "en", "google_domain": "google.com"}
                db = monitor.connect(Path(tmp)/"rankings.sqlite")
                try:
                    run = monitor.create_run(db, conf, "live")
                    with patch.object(monitor, "API_BASE", "http://127.0.0.1:" + str(server.server_port)):
                        self.assertTrue(monitor.run_collection(
                            db, run, monitor.ApiClient("dummy-key", poll_interval=0.001), lambda _: None))
                    output = Path(tmp)/"report.csv"
                    monitor.export_report(db, run, output)
                    with output.open(encoding="utf-8-sig", newline="") as f:
                        rows = list(csv.DictReader(f))
                    self.assertEqual([r["current_rank"] for r in rows], ["2", "1"])
                    saved = db.execute("SELECT task_id FROM snapshots WHERE keyword = 'async'").fetchone()[0]
                    self.assertEqual(saved, "local-task")
                    raw = db.execute("SELECT raw_json FROM snapshots WHERE keyword = 'sync'").fetchone()[0]
                    self.assertEqual(json.loads(raw)["organic_results"][0]["position"], 2)
                finally:
                    db.close()
            self.assertEqual(len(calls), 2)
            self.assertEqual([c[1] for c in calls], ["dummy-key", "dummy-key"])
            self.assertTrue(all(c[2]["actor"] == monitor.ACTOR for c in calls))
            self.assertEqual(polls, ["/api/v1/scraper/result/local-task"] * 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
