import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import serp_monitor as monitor


def config(**updates):
    data = {"keywords": ["test"], "target_domains": ["example.com"],
            "gl": "us", "hl": "en", "google_domain": "google.com"}
    data.update(updates)
    return data


def result(rank):
    return {"organic_results": [{"position": 1, "link": "https://other.example/page"}] if rank is None else [
        {"position": rank, "link": "https://www.example.com/product", "title": "Example"}
    ]}


class FixedClient:
    def __init__(self, payload):
        self.payload = payload

    def search(self, data, on_task, task_id=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class TestApi(unittest.TestCase):
    def test_sync_does_not_poll(self):
        client = monitor.ApiClient("test-key")
        with patch.object(client, "request", return_value=(200, result(1))) as req:
            self.assertEqual(client.search({"q": "test"}, lambda _: None), result(1))
        self.assertEqual(req.call_count, 1)
        self.assertEqual(req.call_args.args[0], "POST")

    def test_async_keeps_task_id_and_only_gets_existing_task(self):
        client = monitor.ApiClient("test-key", poll_interval=0.001)
        saved = []
        with patch.object(client, "request", side_effect=[
            (201, {"taskId": "saved-id"}), (201, {"message": "working"}), (200, result(2))
        ]) as req:
            self.assertEqual(client.search({"q": "test"}, saved.append), result(2))
        self.assertEqual(saved, ["saved-id"])
        self.assertEqual([c.args[0] for c in req.call_args_list], ["POST", "GET", "GET"])
        self.assertEqual(req.call_args.args[1], "/api/v1/scraper/result/saved-id")

    def test_resume_never_posts_and_encodes_task_id(self):
        client = monitor.ApiClient("test-key", poll_interval=0.001)
        with patch.object(client, "request", return_value=(200, result(1))) as req:
            client.search({}, lambda _: None, "saved/id")
        self.assertEqual(req.call_args.args[:2], ("GET", "/api/v1/scraper/result/saved%2Fid"))

    def test_post_errors_are_not_retried(self):
        for status in (400, 401, 402, 403, 429, 500):
            client = monitor.ApiClient("secret")
            with patch.object(client, "request", return_value=(status, {"error": "secret"})) as req:
                with self.assertRaises(monitor.MonitorError) as caught:
                    client.search({}, lambda _: None)
            self.assertEqual(req.call_count, 1)
            self.assertNotIn("secret", str(caught.exception))

    def test_pending_timeout_preserves_id(self):
        client = monitor.ApiClient("key", poll_timeout=0.002, poll_interval=0.001)
        with patch.object(client, "request", return_value=(201, {"taskId": "pending-id"})):
            with self.assertRaises(monitor.PendingTask) as caught:
                client.search({}, lambda _: None)
        self.assertEqual(caught.exception.task_id, "pending-id")

    def test_missing_task_id_is_failure(self):
        client = monitor.ApiClient("key")
        with patch.object(client, "request", return_value=(201, {})):
            with self.assertRaises(monitor.MonitorError):
                client.search({}, lambda _: None)

    def test_get_server_error_backs_off_and_keeps_existing_task(self):
        client = monitor.ApiClient("key", poll_interval=0.001)
        with patch.object(client, "request", side_effect=[(500, {}), (200, result(1))]) as req:
            with patch.object(monitor.time, "sleep") as sleep:
                client.search({}, lambda _: None, "existing-task")
        self.assertEqual([c.args[0] for c in req.call_args_list], ["GET", "GET"])
        self.assertTrue(any(c.args[0] > 59 for c in sleep.call_args_list))

    def test_redirects_never_forward_token(self):
        self.assertIsNone(monitor.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_request_rejects_non_json_and_redacts_http_body(self):
        from urllib.error import HTTPError
        client = monitor.ApiClient("secret")
        error = HTTPError("https://api.scrapeless.com", 401, "Unauthorized", {}, io.BytesIO(b"secret"))
        with patch.object(client.opener, "open", side_effect=error):
            status, payload = client.request("GET", "/api/v1/me")
        self.assertEqual(status, 401)
        self.assertNotIn("secret", str(payload))
        response = io.BytesIO(b"<html>unusable</html>")
        response.status = 200
        with patch.object(client.opener, "open", return_value=response):
            with self.assertRaises(monitor.MonitorError):
                client.request("POST", "/api/v1/scraper/request", {})

        class BrokenResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                raise ConnectionResetError("interrupted")

        with patch.object(client.opener, "open", return_value=BrokenResponse()) as req:
            with self.assertRaises(monitor.MonitorError):
                client.request("POST", "/api/v1/scraper/request", {})
            self.assertEqual(req.call_count, 1)


class TestParsing(unittest.TestCase):
    def test_malformed_or_error_payload_is_not_absent(self):
        invalid = [None, [], {}, {"error": "bad"}, {"code": 20500},
                   {"success": False, "organic_results": []},
                   {"organic_results": [{"position": True, "link": "https://example.com"}]},
                   {"organic_results": [{"position": 1, "link": "/relative"}]},
                   {"organic_results": [{"position": 1, "link": "https://[bad"}]}]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(monitor.MonitorError):
                    monitor.parse_organic(payload)

    def test_empty_results_require_confirmation(self):
        with self.assertRaises(monitor.EmptyUnverified):
            monitor.parse_organic({"organic_results": []})

    def test_domain_boundary_and_best_rank(self):
        organic = monitor.parse_organic({"organic_results": [
            {"position": 1, "link": "https://notexample.com"},
            {"position": 4, "link": "https://example.com.evil.org"},
            {"position": 3, "link": "https://docs.example.com/path"},
            {"position": 2, "link": "https://example.com/other"},
        ]})
        self.assertEqual(monitor.best_match(organic, "example.com")["position"], 2)
        self.assertIsNone(monitor.best_match(organic, "another.com"))

    def test_duplicate_position_rejected(self):
        payload = result(1)
        payload["organic_results"] *= 2
        with self.assertRaises(monitor.MonitorError):
            monitor.parse_organic(payload)

    def test_scope_changes_with_search_settings(self):
        original = monitor.search_input(config(), "test")
        self.assertNotIn("num", original)
        self.assertNotIn("tbm", original)
        for field, value in [("gl", "uk"), ("hl", "fr"), ("location", "Austin"),
                             ("google_domain", "google.co.uk")]:
            other = dict(original, **{field: value})
            self.assertNotEqual(monitor.scope_key(original), monitor.scope_key(other))


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.db = monitor.connect(Path(":memory:"))

    def tearDown(self):
        self.db.close()

    def collect(self, rank, conf=None, mode="live"):
        run = monitor.create_run(self.db, conf or config(), mode)
        monitor.run_collection(self.db, run, FixedClient(result(rank)), lambda _: None)
        return run, monitor.report_rows(self.db, run)[0]

    def test_movement_and_absence_states(self):
        cases = [(5, "baseline", ""), (2, "up", 3), (4, "down", -2),
                 (4, "unchanged", 0), (None, "lost", ""),
                 (None, "still_absent", ""), (1, "new", "")]
        for rank, status, delta in cases:
            _, row = self.collect(rank)
            self.assertEqual(row["status"], status)
            self.assertEqual(row["rank_change"], delta)

    def test_initial_absence_has_no_fake_rank(self):
        _, row = self.collect(None)
        self.assertEqual(row["status"], "baseline_absent")
        self.assertEqual(row["current_rank"], "")

    def test_failure_is_not_a_ranking_drop_and_next_run_uses_last_success(self):
        self.collect(3)
        failed = monitor.create_run(self.db, config(), "live")
        good = monitor.run_collection(self.db, failed, FixedClient({"code": 20500}), lambda _: None)
        self.assertFalse(good)
        row = monitor.report_rows(self.db, failed)[0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["current_rank"], "")
        _, row = self.collect(1)
        self.assertEqual((row["previous_rank"], row["rank_change"]), (3, 2))

    def test_changed_region_and_demo_are_not_previous_live_snapshot(self):
        self.collect(7, mode="demo")
        _, row = self.collect(2)
        self.assertEqual(row["status"], "baseline")
        _, row = self.collect(1, conf=config(gl="uk"))
        self.assertEqual(row["status"], "baseline")

    def test_export_old_run_ignores_future_snapshots(self):
        old, _ = self.collect(3)
        self.collect(1)
        self.assertEqual(monitor.report_rows(self.db, old)[0]["status"], "baseline")

    def test_resume_saved_task_and_does_not_repeat_success(self):
        class PendingClient:
            def search(self, data, on_task, task_id=None):
                on_task("saved-task")
                raise monitor.PendingTask("saved-task")
        run = monitor.create_run(self.db, config(), "live")
        self.assertFalse(monitor.run_collection(self.db, run, PendingClient(), lambda _: None))
        self.assertEqual(monitor.report_rows(self.db, run)[0]["status"], "pending")
        client = FixedClient(result(2))
        with patch.object(client, "search", wraps=client.search) as search:
            self.assertTrue(monitor.run_collection(self.db, run, client, lambda _: None))
            self.assertEqual(search.call_args.args[2], "saved-task")
            monitor.run_collection(self.db, run, client, lambda _: None)
            self.assertEqual(search.call_count, 1)

    def test_uncertain_request_without_id_is_not_reposted(self):
        run = monitor.create_run(self.db, config(), "live")
        client = FixedClient(monitor.MonitorError("network failure"))
        monitor.run_collection(self.db, run, client, lambda _: None)
        with patch.object(client, "search") as search:
            self.assertFalse(monitor.run_collection(self.db, run, client, lambda _: None))
            search.assert_not_called()

    def test_partial_run_keeps_success_and_error_rows(self):
        conf = config(keywords=["good", "bad"])
        class MixedClient:
            def search(self, data, on_task, task_id=None):
                if data["q"] == "bad":
                    raise monitor.MonitorError("failed")
                return result(2)
        run = monitor.create_run(self.db, conf, "live")
        self.assertFalse(monitor.run_collection(self.db, run, MixedClient(), lambda _: None))
        rows = monitor.report_rows(self.db, run)
        self.assertEqual([r["status"] for r in rows], ["baseline", "error"])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/"report.csv"
            monitor.export_report(self.db, run, output)
            with output.open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual(len(list(csv.DictReader(f))), 2)

    def test_auth_balance_and_rate_limit_stop_remaining_submissions(self):
        for status in (401, 402, 403, 429):
            run = monitor.create_run(self.db, config(keywords=["one", "two"]), "live")
            client = FixedClient(monitor.HttpFailure(status, "Stop"))
            with patch.object(client, "search", wraps=client.search) as search:
                self.assertFalse(monitor.run_collection(self.db, run, client, lambda _: None))
                self.assertEqual(search.call_count, 1)
            states = [r[0] for r in self.db.execute("SELECT status FROM snapshots WHERE run_id = ? ORDER BY id", (run,))]
            self.assertEqual(states, ["error", "queued"])

    def test_multiple_domains_have_independent_rows(self):
        run, _ = self.collect(1, config(target_domains=["example.com", "other.com"]))
        self.assertEqual([r["status"] for r in monitor.report_rows(self.db, run)], ["baseline", "baseline_absent"])


class TestConfigAndCli(unittest.TestCase):
    def test_config_rejects_unknown_fields_and_invalid_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"config.json"
            for data in [config(num=100), config(target_domains=["https://example.com"]),
                         config(keywords=["test", "test"]), config(gl="usa"), config(location="")]:
                path.write_text(json.dumps(data))
                with self.assertRaises(monitor.MonitorError):
                    monitor.load_config(path)
            path.write_text(json.dumps(config()))
            self.assertEqual(monitor.load_config(path)["gl"], "us")

    def test_environment_wins_and_dotenv_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/".env"
            path.write_text("export SCRAPELESS_API_KEY='file-token'\n")
            with patch.dict(os.environ, {"SCRAPELESS_API_KEY": "env-token"}):
                self.assertEqual(monitor.load_key(path), "env-token")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(monitor.load_key(path), "file-token")
                path.write_text("SCRAPELESS_API_KEY=YOUR_SCRAPELESS_API_KEY")
                with self.assertRaises(monitor.MonitorError):
                    monitor.load_key(path)

    def test_csv_formula_escape_preserves_numeric_delta(self):
        for value in ["=1+1", "+cmd", "-cmd", "@SUM(A1)", "\t=1"]:
            self.assertTrue(monitor.csv_cell(value).startswith("'"))
        self.assertEqual(monitor.csv_cell(-3), -3)

    def test_invalid_timeout(self):
        for value in ["0", "-1", "nan", "inf"]:
            with self.assertRaises(Exception):
                monitor.positive_number(value)

    def test_demo_runs_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(monitor.main(["demo", "--output-dir", tmp]), 0)
            self.assertIn("SYNTHETIC DEMO ONLY", out.getvalue())
            with (Path(tmp)/"round-2.csv").open(encoding="utf-8-sig") as f:
                self.assertEqual([r["status"] for r in csv.DictReader(f)], ["up", "down", "new"])

    def test_missing_key_exits_one_before_creating_database(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            db_path = Path(tmp)/"data.sqlite"
            with patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(monitor.main(["collect", "--env-file", str(Path(tmp)/"missing"),
                                              "--db", str(db_path)]), 1)
            self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()
