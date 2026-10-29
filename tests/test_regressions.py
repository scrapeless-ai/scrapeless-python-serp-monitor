"""Regression acceptance for frozen reports, data quality and legacy migration."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import serp_monitor as m
from test_monitor import config, result, FixedClient


class PendingClient:
    def search(self, data, on_task, task_id=None):
        on_task('controlled-saved-task')
        raise m.PendingTask('controlled-saved-task')


class TestComparisonRegressions(unittest.TestCase):
    def setUp(self):
        self.db = m.connect(Path(':memory:'))

    def tearDown(self):
        self.db.close()

    def collect(self, rank, conf=None):
        run = m.create_run(self.db, conf or config(), 'live')
        m.run_collection(self.db, run, FixedClient(result(rank)), lambda _: None)
        return run

    def test_old_queued_resume_cannot_flip_completed_report(self):
        self.collect(5)
        old = m.create_run(self.db, config(keywords=['auth-check', 'test']), 'live')
        m.run_collection(self.db, old, FixedClient(m.HttpFailure(401, 'Controlled')), lambda _: None)
        newer = self.collect(2)
        before = m.report_rows(self.db, newer)
        self.assertEqual((before[0]['previous_rank'], before[0]['rank_change']), (5, 3))
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/'before.csv', Path(td)/'after.csv'
            m.export_report(self.db, newer, a)
            m.run_collection(self.db, old, FixedClient(result(1)), lambda _: None)
            late = m.report_rows(self.db, old)[1]
            self.assertEqual((late["previous_rank"], late["rank_change"]), (2, 1))
            self.assertEqual(late["baseline_snapshot_id"], before[0]["snapshot_id"])
            m.export_report(self.db, newer, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(m.report_rows(self.db, newer), before)
        self.assertEqual(before[0]['status'], 'up')
        self.assertLessEqual(before[0]['previous_at'], before[0]['submitted_at'])

    def test_null_baseline_is_frozen_after_late_old_run(self):
        old = m.create_run(self.db, config(), 'live')
        newer = self.collect(2)
        before = m.report_rows(self.db, newer)
        self.assertEqual(before[0]['baseline_snapshot_id'], '')
        m.run_collection(self.db, old, FixedClient(result(1)), lambda _: None)
        self.assertEqual(m.report_rows(self.db, newer), before)
        self.assertEqual(before[0]['status'], 'baseline')

    def test_async_overlap_is_not_ordered_by_receipt_time(self):
        self.collect(5)
        pending = m.create_run(self.db, config(), 'live')
        m.run_collection(self.db, pending, PendingClient(), lambda _: None)
        newer = self.collect(2)
        before = m.report_rows(self.db, newer)
        self.assertEqual(before[0]['status'], 'not_comparable')
        self.assertEqual(before[0]['rank_change'], '')
        m.run_collection(self.db, pending, FixedClient(result(1)), lambda _: None)
        resumed = m.report_rows(self.db, pending)[0]
        self.assertEqual(resumed['status'], 'not_comparable')
        self.assertEqual(m.report_rows(self.db, newer), before)

    def test_partial_resume_creates_new_version_and_retains_old(self):
        run = m.create_run(self.db, config(), 'live')
        m.run_collection(self.db, run, PendingClient(), lambda _: None)
        v1 = m.report_rows(self.db, run)
        self.assertEqual((v1[0]['report_version'], v1[0]['report_state']), (1, 'partial'))
        m.run_collection(self.db, run, FixedClient(result(2)), lambda _: None)
        v2 = m.report_rows(self.db, run)
        self.assertEqual((v2[0]['report_version'], v2[0]['report_state']), (2, 'complete'))
        self.assertEqual(m.report_rows(self.db, run, 1), v1)
        m.run_collection(self.db, run, FixedClient(result(7)), lambda _: None)
        self.assertEqual(m.report_rows(self.db, run), v2)

    def test_empty_sequence_does_not_create_lost_or_new(self):
        self.collect(3)
        empty = m.create_run(self.db, config(), 'live')
        self.assertFalse(m.run_collection(self.db, empty, FixedClient({'organic_results': []}), lambda _: None))
        middle = m.report_rows(self.db, empty)[0]
        self.assertEqual(middle['status'], 'empty_unverified')
        self.assertEqual(middle['current_rank'], '')
        final = m.report_rows(self.db, self.collect(3))[0]
        self.assertEqual((final['status'], final['previous_rank'], final['rank_change']), ('unchanged', 3, 0))

    def test_empty_unmatched_and_error_are_distinct(self):
        statuses = []
        for payload in ({'organic_results': []}, result(None), {'error': 'Controlled'}):
            run = m.create_run(self.db, config(), 'live')
            m.run_collection(self.db, run, FixedClient(payload), lambda _: None)
            statuses.append(m.report_rows(self.db, run)[0]['status'])
        self.assertEqual(statuses, ['empty_unverified', 'baseline_absent', 'error'])

    def test_empty_resume_never_blindly_reposts(self):
        run = m.create_run(self.db, config(), 'live')
        m.run_collection(self.db, run, FixedClient({'organic_results': []}), lambda _: None)
        client = FixedClient(result(3))
        with patch.object(client, 'search') as search:
            self.assertFalse(m.run_collection(self.db, run, client, lambda _: None))
        search.assert_not_called()

    def test_received_at_is_not_claimed_as_provider_collection_time(self):
        run = self.collect(2)
        row = m.report_rows(self.db, run)[0]
        self.assertTrue(row['submitted_at'])
        self.assertGreaterEqual(row['received_at'], row['submitted_at'])
        self.assertEqual(row['captured_at'], row['received_at'])
        self.assertEqual(row['time_basis'], 'request_interval')

    def test_failed_validation_keeps_minimized_diagnostics(self):
        run = m.create_run(self.db, config(), 'live')
        payload = {'organic_results': [{'position': 1, 'link': '/not-an-absolute-url', 'title': 'PRIVATE_TEST_VALUE'}]}
        m.run_collection(self.db, run, FixedClient(payload), lambda _: None)
        row = self.db.execute('SELECT * FROM snapshots WHERE run_id=?', (run,)).fetchone()
        diagnostic = json.loads(row['diagnostic_json'])
        self.assertEqual(diagnostic, {'organic_array_length': 1, 'invalid_item_indexes': [0]})
        self.assertNotIn('PRIVATE_TEST_VALUE', row['diagnostic_json'])
        self.assertIsNone(row['raw_json'])

    def test_unknown_report_version_fails(self):
        run = self.collect(2)
        with self.assertRaises(m.MonitorError):
            m.report_rows(self.db, run, 999)


class TestMigration(unittest.TestCase):
    def legacy(self, path, empty=False, pending=False):
        db = sqlite3.connect(path)
        db.executescript('''
            CREATE TABLE runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                mode TEXT NOT NULL, config_json TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE snapshots (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                keyword TEXT NOT NULL, scope_key TEXT NOT NULL, input_json TEXT NOT NULL, status TEXT NOT NULL,
                task_id TEXT, captured_at TEXT, raw_json TEXT, organic_json TEXT, error TEXT, UNIQUE(run_id,keyword));
        ''')
        cfg=config(); data=m.search_input(cfg,'test'); payload={'organic_results': []} if empty else result(3)
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?)',('legacy','2020-01-01T00:00:00+00:00','2020-01-01T00:01:00+00:00','live',json.dumps(cfg),'partial' if pending else 'success'))
        db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)', (1,'legacy','test',m.scope_key(data),json.dumps(data),'pending' if pending else 'success','saved-legacy' if pending else None,None if pending else '2020-01-01T00:01:00+00:00',json.dumps(payload),json.dumps(payload['organic_results']),None))
        db.commit(); db.close()

    def test_old_database_backup_preservation_and_idempotence(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'old.sqlite'; self.legacy(path)
            original=path.read_bytes(); db=m.connect(path)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],1)
            self.assertEqual(m.report_rows(db,'legacy')[0]['status'],'not_comparable')
            backup=path.with_name(path.name+'.pre-v2.sqlite')
            with sqlite3.connect(backup) as old:
                self.assertEqual(old.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],1)
                self.assertNotIn('submitted_at',{r[1] for r in old.execute('PRAGMA table_info(snapshots)')})
            backup_bytes=backup.read_bytes(); db.close(); again=m.connect(path); again.close()
            self.assertEqual(backup.read_bytes(),backup_bytes)

    def test_legacy_empty_is_quarantined_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'old.sqlite'; self.legacy(path,empty=True); db=m.connect(path)
            self.assertEqual(m.report_rows(db,'legacy')[0]['status'],'empty_unverified')
            self.assertEqual(db.execute('SELECT organic_json FROM snapshots').fetchone()[0],'[]')
            db.close()

    def test_legacy_saved_task_can_resume_but_order_remains_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'old.sqlite'; self.legacy(path,pending=True); db=m.connect(path)
            m.run_collection(db,'legacy',FixedClient(result(2)),lambda _:None)
            row=m.report_rows(db,'legacy')[0]
            self.assertEqual(row['status'],'not_comparable')
            self.assertEqual(row['submitted_at'],'')
            db.close()

    def test_new_observation_can_use_legacy_received_upper_bound(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'old.sqlite'; self.legacy(path); db=m.connect(path)
            run=m.create_run(db,config(),'live');m.run_collection(db,run,FixedClient(result(2)),lambda _:None)
            row=m.report_rows(db,run)[0]
            self.assertEqual((row['status'],row['previous_rank'],row['rank_change']),('up',3,1))
            db.close()

    def test_future_schema_is_rejected_without_downgrade(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'newer.sqlite'
            with sqlite3.connect(path) as db: db.execute('PRAGMA user_version=99')
            with self.assertRaises(m.MonitorError): m.connect(path)
            with sqlite3.connect(path) as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],99)

if __name__ == '__main__': unittest.main()
