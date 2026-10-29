"""Optional daily operations: persistent history, review summaries and safe recovery."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid
from urllib.parse import urlsplit
import serp_monitor as m

EMPTY = 'Empty organic array requires review; no ranking observation accepted.'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def event(name, **fields):
    print(json.dumps({'event': name, 'at': m.utc_now(), **fields}), flush=True)


Busy = m.Busy
lock_database = m.database_lock


class MeasuredClient(m.ApiClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_attempts = {'POST': 0, 'GET': 0}
        self.http_statuses = []

    def request(self, method, path, body=None, timeout=None):
        self.http_attempts[method] = self.http_attempts.get(method, 0) + 1
        status, payload = super().request(method, path, body, timeout)
        self.http_statuses.append(status)
        return status, payload


def summarize(rows, run, state_counts, evidence_mode, report, started, finished):
    review = []
    for row in rows:
        reasons = []
        status = row['status']
        if status in {'down', 'lost', 'still_absent', 'baseline_absent'}:
            reasons.append('rank_declined' if status == 'down' else 'target_not_found_in_returned_results')
        if row['previous_url'] and row['current_url'] and row['previous_url'] != row['current_url']:
            reasons.append('landing_page_changed')
        if row['previous_organic_count'] != '' and row['organic_count'] != '' and row['previous_organic_count'] != row['organic_count']:
            reasons.append('returned_coverage_changed')
        if status in {'error', 'pending', 'empty_unverified', 'not_comparable'}:
            reasons = ['empty_response_needs_confirmation' if status == 'empty_unverified' else 'collection_order_uncertain' if status == 'not_comparable' else 'collection_incomplete']
        if reasons:
            review.append({**row, 'review_reasons': ';'.join(reasons)})
    gap = []
    for row in rows:
        if row['captured_at'] and row['previous_at']:
            gap.append(round((datetime.fromisoformat(row['captured_at']) - datetime.fromisoformat(row['previous_at'])).total_seconds() / 3600, 6))
    return {'run_id': run, 'started_at': started, 'finished_at': finished,
            'evidence_mode': evidence_mode, 'status_counts': dict(state_counts),
            'row_status_counts': dict(Counter(row['status'] for row in rows)),
            'review_item_count': len(review), 'report': str(report),
            'receipt_gap_hours': gap, 'planned_interval_hours': 24,
            'scope': 'Desktop, first returned organic page; no guaranteed result count.',
            'items': review}


def csv_write(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        w = csv.DictWriter(stream, fieldnames=fields); w.writeheader()
        w.writerows({k: m.csv_cell(row.get(k, '')) for k in fields} for row in rows)


def operation(args):
    dbpath = args.db.resolve(); artifacts = args.artifacts.resolve()
    with lock_database(dbpath):
        if args.command == 'backup':
            if not dbpath.is_file():
                raise m.MonitorError('Backup source database does not exist.')
            if args.destination is not None and args.destination.resolve() == dbpath:
                raise m.MonitorError('Backup destination must differ from the source database.')
            if args.destination is not None and args.destination.exists():
                raise m.MonitorError('Backup destination already exists; choose a new file.')
            if args.destination is None:
                raise m.MonitorError('Backup requires --destination.')
            args.destination.resolve().parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(dbpath)) as source, sqlite3.connect(str(args.destination.resolve())) as dest:
                source.backup(dest)
            event('backup_complete'); return 0
        if args.command == 'resume' and not args.run_id:
            raise m.MonitorError('Resume requires --run-id.')
        config = None
        if args.command != 'resume':
            raw = json.loads(args.config.resolve().read_text())
            if not all(k in raw for k in ('gl', 'hl', 'google_domain', 'location')):
                raise m.MonitorError('Daily workflow requires explicit gl, hl, google_domain and location.')
            config = m.load_config(args.config.resolve())
        if args.controlled_url:
            url = urlsplit(args.controlled_url)
            if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path or url.query or url.fragment or url.username:
                raise m.MonitorError('Controlled endpoint must be an explicit loopback HTTP origin.')
            m.API_BASE = args.controlled_url
            key = 'CONTROLLED_TEST_ONLY'
            mode = 'demo'
            evidence = 'controlled_http_fixture'
        else:
            key = m.load_key(args.env_file.resolve())
            mode = 'live'; evidence = 'live_scrapeless'
        db = m.connect(dbpath)
        try:
            if args.command == 'tick':
                now = datetime.now(timezone.utc)
                hour, minute = map(int, args.at_utc.split(':'))
                if not (0 <= hour < 24 and 0 <= minute < 60):raise m.MonitorError('Invalid UTC schedule.')
                if (now.hour, now.minute) < (hour, minute):
                    event('not_due'); return 0
                db.execute('CREATE TABLE IF NOT EXISTS workflow_slots (slot TEXT PRIMARY KEY, claimed_at TEXT NOT NULL)')
                slot = now.date().isoformat() + ':' + args.at_utc
                with db:
                    claimed = db.execute('INSERT OR IGNORE INTO workflow_slots VALUES (?, ?)', (slot, m.utc_now())).rowcount
                if not claimed:
                    event('already_attempted'); return 0
            if args.command == 'resume':
                run = db.execute('SELECT * FROM runs WHERE id = ?', (args.run_id,)).fetchone()
                if not run or run['mode'] != mode:raise m.MonitorError('Resume requires the same evidence mode and an existing run.')
                run_id = args.run_id
            else:
                run_id = m.create_run(db, config, mode)
            attempt = uuid.uuid4().hex[:10]; start = m.utc_now()
            event('started', run_id=run_id, attempt=attempt, evidence_mode=evidence)
            atomic_json(artifacts / 'latest-attempt.json', {'run_id': run_id, 'attempt': attempt, 'complete': False, 'state': 'running', 'started_at': start, 'evidence_mode': evidence})
            client = MeasuredClient(key, timeout=args.timeout, poll_timeout=args.poll_timeout, poll_interval=args.poll_interval)
            m.run_collection(db, run_id, client, lambda _: None)
            rows = m.report_rows(db, run_id)
            counts = Counter(r[0] for r in db.execute('SELECT status FROM snapshots WHERE run_id=?', (run_id,)))
            success = all(k == 'success' for k in counts)
            folder = artifacts / 'runs' / run_id
            report = folder / (attempt + '.csv')
            csv_write(report, rows, m.FIELDS)
            summary = summarize(rows, run_id, counts, evidence, report.relative_to(artifacts), start, m.utc_now())
            summary['complete'] = success
            summary['planned_interval_hours'] = 24 if args.command == 'tick' else None
            summary['http_attempts'] = client.http_attempts
            summary['http_statuses'] = client.http_statuses
            summary['google_redirect_lookups'] = client.google_redirect_lookups
            summary['diagnostics'] = [{'query': row['keyword'], **json.loads(row['diagnostic_json'] or '{}')} for row in db.execute('SELECT keyword,diagnostic_json FROM snapshots WHERE run_id=?', (run_id,))]
            summary['search_settings'] = {k: rows[0][k] for k in ('gl', 'hl', 'google_domain', 'location')}
            summary['device'] = 'desktop'
            summary['start'] = 0
            summary['report_version'] = rows[0]['report_version']
            summary['report_state'] = rows[0]['report_state']
            summary['readiness'] = 'Controlled regression verified; consecutive live scheduling not established.'
            summary_path = folder / (attempt + '.summary.json')
            atomic_json(summary_path, summary)
            csv_write(folder / (attempt + '.review.csv'), summary['items'], m.FIELDS + ['review_reasons'])
            note = ['# Operator summary', '', 'Evidence mode: ' + evidence, 'Run: ' + run_id,
                    'Collection complete: ' + str(success), 'Snapshot states: ' + json.dumps(dict(counts)),
                    'Items needing review: ' + str(len(summary['items'])), 'Report: ' + str(report.relative_to(artifacts)),
                    '', 'Read the current-attempt status before interpreting an older successful report.',
                    'Comparison order uses request intervals; unknown or overlapping intervals are not compared.']
            for item in summary['items']:
                note.append('- ' + item['keyword'] + ': ' + item['review_reasons'])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (attempt + '.summary.md')).write_text('\n'.join(note) + '\n')
            pointer = {'run_id': run_id, 'attempt': attempt, 'complete': success, 'state': 'complete' if success else 'incomplete', 'summary': str(summary_path.relative_to(artifacts)), 'report': str(report.relative_to(artifacts))}
            atomic_json(artifacts / 'latest-attempt.json', pointer)
            if success:atomic_json(artifacts / 'latest-success.json', pointer)
            event('finished', run_id=run_id, attempt=attempt, complete=success, state_counts=dict(counts), review_items=len(summary['items']), report=str(report.relative_to(artifacts)))
            return 0 if success else 1
        finally:
            db.close()


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['run', 'resume', 'tick', 'backup'])
    p.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    p.add_argument('--env-file', type=Path, default=Path(__file__).with_name('.env'))
    p.add_argument('--db', type=Path, required=True)
    p.add_argument('--artifacts', type=Path, required=True)
    p.add_argument('--run-id')
    p.add_argument('--destination', type=Path)
    p.add_argument('--controlled-url')
    p.add_argument('--at-utc', default='09:00')
    p.add_argument('--timeout', type=m.positive_number, default=60)
    p.add_argument('--poll-timeout', type=m.positive_number, default=120)
    p.add_argument('--poll-interval', type=m.positive_number, default=2)
    return p


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        return operation(args)
    except Busy:
        event('busy', complete=False); return 75
    except (m.MonitorError, OSError, ValueError, sqlite3.Error) as exc:
        atomic_json(args.artifacts.resolve() / 'latest-attempt.json', {'state': 'preflight_or_workflow_error', 'complete': False, 'at': m.utc_now(), 'error_type': type(exc).__name__})
        event('workflow_error', complete=False, error_type=type(exc).__name__)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
