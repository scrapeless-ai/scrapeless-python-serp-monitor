"""Real subprocesses and loopback HTTP; no external API and no credentials."""
from pathlib import Path
import csv
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

SOURCE = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
TEMP = Path(tempfile.mkdtemp(prefix='scrapeless-workflow-proof-'))
for name in ('serp_monitor.py', 'daily_workflow.py'):
    shutil.copy2(SOURCE / name, TEMP / name)
sys.path.insert(0, str(TEMP))
import serp_monitor as m
results = []
calls = []
state = {'phase': 0, 'errors': {}, 'empty': set(), 'async': set(), 'ready': False, 'block': set()}
release = threading.Event(); release.set()
tasks = {}


def payload(q):
    phase = state['phase']
    rank = {'alpha': (3, 5), 'beta': (2, 2), 'gamma': (1, None)}.get(q, (2, 2))[min(phase, 1)]
    count = 4 if q == 'beta' and phase else 5
    if q in state['empty']:
        return {'organic_results': []}
    return {'organic_results': [
        {'position': i, 'title': 'Controlled result ' + str(i),
         'link': ('https://example.com/' + ('new' if phase and q == 'beta' else 'old')) if i == rank else 'https://example.org/page/' + str(i)}
        for i in range(1, count + 1)]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, status, body):
        raw = json.dumps(body).encode()
        try:
            self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        q = body['input']['q']; calls.append({'method': 'POST', 'query': q, 'phase': state['phase']})
        if q in state['block']: release.wait(15)
        if q in state['errors']: self.send(state['errors'][q], {'error': 'Controlled failure'}); return
        if q in state['async']:
            task = 'controlled-' + uuid.uuid4().hex[:8]; tasks[task] = q; self.send(201, {'taskId': task})
        else: self.send(200, payload(q))
    def do_GET(self):
        task = self.path.rsplit('/', 1)[-1]; calls.append({'method': 'GET', 'task_id': task})
        self.send(200, payload(tasks[task])) if state['ready'] else self.send(201, {'message': 'Controlled pending'})


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
url = 'http://127.0.0.1:' + str(server.server_port)
ENV = dict(os.environ); ENV.pop('SCRAPELESS_API_KEY', None); ENV['PYTHONDONTWRITEBYTECODE'] = '1'


def conf(folder, keys=('alpha', 'beta', 'gamma'), location='Austin, Texas, United States'):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / 'config.json'; p.write_text(json.dumps({'keywords': list(keys), 'target_domains': ['example.com'], 'gl': 'us', 'hl': 'en', 'google_domain': 'google.com', 'location': location})); return p


def cmd(folder, config, action='run', run_id=None):
    argv = [sys.executable, str(TEMP / 'daily_workflow.py'), action, '--config', str(config), '--db', str(folder / 'rankings.sqlite'), '--artifacts', str(folder / 'artifacts'), '--controlled-url', url, '--poll-interval', '0.01', '--poll-timeout', '0.12', '--timeout', '3']
    if run_id: argv += ['--run-id', run_id]
    return argv


def run(folder, config, action='run', run_id=None, cwd='/'):
    p = subprocess.run(cmd(folder, config, action, run_id), cwd=cwd, env=ENV, capture_output=True, text=True, timeout=10)
    (folder / (uuid.uuid4().hex[:6] + '.process.log')).write_text(p.stdout + p.stderr)
    return p


def latest(folder):
    a = folder / 'artifacts'; pointer = json.loads((a / 'latest-attempt.json').read_text()); summary = json.loads((a / pointer['summary']).read_text()); rows = list(csv.DictReader((a / pointer['report']).open(encoding='utf-8-sig'))); return pointer, summary, rows


def count_post(start): return sum(x['method'] == 'POST' for x in calls[start:])

def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(0.05)
    raise AssertionError('Timed out waiting for controlled condition')


def record(name, **fields):
    results.append({'case': name, 'passed': True, **fields}); print(json.dumps(results[-1]), flush=True)


try:
    folder = TEMP / 'persistence'; cfg = conf(folder)
    assert run(folder, cfg).returncode == 0
    baseline, _, _ = latest(folder)
    state['phase'] = 1
    assert run(folder, cfg, cwd=str(TEMP)).returncode == 0
    pointer, summary, rows = latest(folder)
    assert {x['status'] for x in rows} == {'down', 'unchanged', 'lost'}
    assert any('landing_page_changed' in x['review_reasons'] for x in summary['items'])
    assert any('returned_coverage_changed' in x['review_reasons'] for x in summary['items'])
    assert all(x['previous_at'] for x in rows)
    with sqlite3.connect(folder / 'rankings.sqlite') as db: assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 6
    record('separate_processes_same_database_different_working_directories', snapshots=6, review_items=summary['review_item_count'], actual_row_statuses=[x['status'] for x in rows])
    shutil.copytree(folder / 'artifacts', OUT / 'controlled-outputs')
    original = {x.name: x.read_bytes() for x in (folder / 'artifacts/runs' / pointer['run_id']).glob('*.csv')}
    for status in (401, 402, 429):
        state['errors'] = {'alpha': status}; start = len(calls); previous_success = (folder / 'artifacts/latest-success.json').read_bytes()
        assert run(folder, cfg).returncode == 1
        _, sm, rr = latest(folder)
        assert count_post(start) == 1
        assert all(x['status'] not in {'down', 'lost', 'new'} for x in rr)
        assert (folder / 'artifacts/latest-success.json').read_bytes() == previous_success
        record('controlled_http_' + str(status), submitted_queries=1, failure_exit=1, previous_success_pointer_unchanged=True)
    state['errors'] = {}
    state['empty'] = {'alpha'}
    assert run(folder, cfg).returncode == 1
    _, sm, rr = latest(folder)
    assert next(x for x in rr if x['keyword'] == 'alpha')['status'] == 'empty_unverified'
    assert not any(x['keyword'] == 'alpha' and any(r in x['review_reasons'] for r in ['rank_declined', 'target_not_found']) for x in sm['items'])
    state['empty'] = set()
    record('core_quarantines_empty_array', business_loss_alert=False)
    cfg2 = conf(folder, location='New York, New York, United States')
    assert run(folder, cfg2).returncode == 0
    _, _, rr = latest(folder); assert all(x['previous_rank'] == '' for x in rr)
    record('changed_location_starts_separate_series')
    assert sm['http_attempts']['POST'] == 3
    record('http_submission_and_poll_counts_reported_separately')
    backup = folder / 'backup.sqlite'
    p = subprocess.run([sys.executable, str(TEMP / 'daily_workflow.py'), 'backup', '--db', str(folder / 'rankings.sqlite'), '--artifacts', str(folder / 'artifacts'), '--destination', str(backup)], env=ENV, capture_output=True, text=True)
    assert p.returncode == 0
    with sqlite3.connect(backup) as db, sqlite3.connect(folder / 'rankings.sqlite') as original_db: assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == original_db.execute('SELECT count(*) FROM snapshots').fetchone()[0]
    record('sqlite_backup_preserves_history')
    for name, data in original.items(): assert (folder / 'artifacts/runs' / pointer['run_id'] / name).read_bytes() == data
    record('completed_output_files_not_overwritten_by_later_attempts')

    folder = TEMP / 'async'; cfg = conf(folder, ('async',)); state['async'] = {'async'}; state['ready'] = False; start = len(calls)
    assert run(folder, cfg).returncode == 1
    pending, _, rows = latest(folder); assert rows[0]['status'] == 'pending'
    posts = count_post(start); state['ready'] = True
    assert run(folder, cfg, 'resume', pending['run_id']).returncode == 0
    assert count_post(start) == posts == 1
    record('saved_async_task_resumed_with_get_only', total_post_submissions=1)

    folder = TEMP / 'concurrency'; cfg = conf(folder, ('alpha',)); state['async'] = set(); state['block'] = {'alpha'}; release.clear()
    db = m.connect(folder / 'rankings.sqlite'); rid = m.create_run(db, m.load_config(cfg), 'demo'); db.close(); start = len(calls)
    a = subprocess.Popen(cmd(folder, cfg, 'resume', rid), env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_for(lambda: count_post(start) == 1)
    b = run(folder, cfg, 'resume', rid); assert b.returncode == 75 and 'busy' in b.stdout
    release.set(); a.communicate(timeout=10); assert a.returncode == 0 and count_post(start) == 1
    state['block'] = set(); record('two_real_wrapper_cli_processes_same_pending_run', total_post_submissions=1, second_exit=75, note='Shared core lock also covers the original CLI; tested separately.')

    folder = TEMP / 'kill-uncertain'; cfg = conf(folder, ('alpha',)); state['block'] = {'alpha'}; release.clear(); start = len(calls)
    a = subprocess.Popen(cmd(folder, cfg), env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_for(lambda: count_post(start) == 1)
    a.kill(); a.communicate(timeout=5); release.set(); state['block'] = set()
    assert json.loads((folder / 'artifacts/latest-attempt.json').read_text())['complete'] is False
    with sqlite3.connect(folder / 'rankings.sqlite') as db: rid = db.execute('SELECT id FROM runs').fetchone()[0]
    b = run(folder, cfg, 'resume', rid)
    assert b.returncode == 1 and count_post(start) == 1 and 'busy' not in b.stdout
    record('killed_post_releases_lock_without_blind_resubmission', total_post_submissions=1, manual_investigation_required=True)

    folder = TEMP / 'kill-pending'; cfg = conf(folder, ('async',)); state['async'] = {'async'}; state['ready'] = False; start = len(calls)
    a = subprocess.Popen(cmd(folder, cfg) + ['--poll-timeout', '8'], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_for(lambda: any(x['method'] == 'GET' for x in calls[start:])); a.kill(); a.communicate(timeout=5)
    with sqlite3.connect(folder / 'rankings.sqlite') as db: rid = db.execute('SELECT id FROM runs').fetchone()[0]
    state['ready'] = True; assert run(folder, cfg, 'resume', rid).returncode == 0; assert count_post(start) == 1
    record('killed_poll_resumes_saved_task', total_post_submissions=1)
    state['async'] = set()

    folder = TEMP / 'utc-slot'; cfg = conf(folder, ('scheduled',)); start = len(calls)
    argv = cmd(folder, cfg, 'tick') + ['--at-utc', '00:00']
    first = subprocess.run(argv, cwd='/', env=ENV, capture_output=True, text=True, timeout=10)
    second = subprocess.run(argv, cwd='/tmp', env=ENV, capture_output=True, text=True, timeout=10)
    assert first.returncode == second.returncode == 0 and 'already_attempted' in second.stdout
    assert count_post(start) == 1
    record('utc_daily_slot_prevents_duplicate_timer_ticks', submissions=1, time_basis='UTC; configured due time 00:00')

    # Invoke the production parser/main in separate interpreters. Only the test
    # launcher swaps API_BASE to the loopback fixture; no public endpoint override.
    def core_cmd(folder, rid):
        code = 'import sys; import serp_monitor as m; m.API_BASE=sys.argv.pop(1); raise SystemExit(m.main(sys.argv[1:]))'
        return [sys.executable, '-c', code, url, 'collect', '--resume', rid,
                '--db', str(folder / 'rankings.sqlite'), '--output', str(folder / 'core.csv'),
                '--poll-interval', '0.01', '--poll-timeout', '8', '--timeout', '3']
    core_env = dict(ENV, SCRAPELESS_API_KEY='EXAMPLE_CONTROLLED_TEST_ONLY', PYTHONPATH=str(TEMP))
    folder = TEMP / 'core-concurrency'; cfg = conf(folder, ('alpha',))
    db = m.connect(folder / 'rankings.sqlite'); rid = m.create_run(db, m.load_config(cfg), 'live'); db.close()
    state['block'] = {'alpha'}; release.clear(); start = len(calls)
    a = subprocess.Popen(core_cmd(folder, rid), env=core_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_for(lambda: count_post(start) == 1)
    b = subprocess.run(core_cmd(folder, rid), env=core_env, capture_output=True, text=True, timeout=5)
    assert b.returncode == 75 and 'Busy' in b.stderr
    cross = run(folder, cfg, 'resume', rid)
    assert cross.returncode == 75
    release.set(); a.communicate(timeout=10)
    assert a.returncode == 0 and count_post(start) == 1
    again = subprocess.run(core_cmd(folder, rid), env=core_env, capture_output=True, text=True, timeout=5)
    assert again.returncode == 0 and count_post(start) == 1
    record('two_original_cli_processes_and_workflow_share_lock', total_post_submissions=1, second_exit=75, workflow_exit=75, completed_item_skipped=True)
    state['block'] = set()

    folder = TEMP / 'core-kill-post'; cfg = conf(folder, ('alpha',))
    db = m.connect(folder / 'rankings.sqlite'); rid = m.create_run(db, m.load_config(cfg), 'live'); db.close()
    state['block'] = {'alpha'}; release.clear(); start = len(calls)
    a = subprocess.Popen(core_cmd(folder, rid), env=core_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_for(lambda: count_post(start) == 1); a.kill(); a.communicate(timeout=5)
    release.set(); state['block'] = set()
    b = subprocess.run(core_cmd(folder, rid), env=core_env, capture_output=True, text=True, timeout=5)
    assert b.returncode == 1 and count_post(start) == 1
    record('original_cli_killed_post_lock_released_no_repost', total_post_submissions=1, resumed_exit=1)

    folder = TEMP / 'core-kill-poll'; cfg = conf(folder, ('async',))
    db = m.connect(folder / 'rankings.sqlite'); rid = m.create_run(db, m.load_config(cfg), 'live'); db.close()
    state['async'] = {'async'}; state['ready'] = False; start = len(calls)
    a = subprocess.Popen(core_cmd(folder, rid), env=core_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait_for(lambda: any(x['method'] == 'GET' for x in calls[start:])); a.kill(); a.communicate(timeout=5)
    state['ready'] = True
    b = subprocess.run(core_cmd(folder, rid), env=core_env, capture_output=True, text=True, timeout=5)
    assert b.returncode == 0 and count_post(start) == 1
    record('original_cli_killed_poll_resumes_get_only', total_post_submissions=1, resumed_exit=0)
    state['async'] = set()

    # Genuine OS timer, isolated loopback data, then unload and reload from another cwd.
    folder = TEMP / 'scheduler'; cfg = conf(folder, ('scheduled',)); label = 'com.scrapeless.serp.proof.' + uuid.uuid4().hex[:10]
    domain = 'gui/' + str(os.getuid()); plist = TEMP / 'proof.plist'; log = folder / 'launchd.log'
    job = {'Label': label, 'ProgramArguments': cmd(folder, cfg), 'WorkingDirectory': '/tmp', 'RunAtLoad': True, 'StartInterval': 10, 'StandardOutPath': str(log), 'StandardErrorPath': str(folder / 'launchd.err'), 'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'}}
    def finished():
        if not log.exists(): return 0
        return sum('"event": "finished"' in line for line in log.read_text().splitlines())
    bootstrapped = False
    try:
        plist.write_bytes(plistlib.dumps(job))
        p = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist)], capture_output=True, text=True)
        if p.returncode != 0:
            results.append({'case': 'actual_launchd_timer', 'passed': False, 'state': 'blocked', 'exit_code': p.returncode}); print(json.dumps(results[-1]), flush=True)
        else:
            bootstrapped = True; wait_for(lambda: finished() >= 2, timeout=35)
            subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True); bootstrapped = False
            count_before = finished(); job['WorkingDirectory'] = '/'; plist.write_bytes(plistlib.dumps(job))
            p = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist)], capture_output=True, text=True); assert p.returncode == 0; bootstrapped = True
            wait_for(lambda: finished() > count_before, timeout=15)
            subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True); bootstrapped = False
            with sqlite3.connect(folder / 'rankings.sqlite') as db:
                snapshots = db.execute('SELECT count(*) FROM snapshots').fetchone()[0]
            assert snapshots >= 3
            shutil.copytree(folder / 'artifacts', OUT / 'scheduler-outputs'); shutil.copy2(log, OUT / 'scheduler-process.log')
            record('actual_launchd_timer', successful_executions=finished(), persisted_snapshots=snapshots, reloaded_with_different_working_directory=True, interval_seconds=10, job_unloaded=True, evidence_mode='controlled_short_interval')
    finally:
        if bootstrapped:subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True)
finally:
    release.set(); server.shutdown(); server.server_close()
    (OUT / 'controlled-results.json').write_text(json.dumps({'evidence_mode': 'controlled_loopback_HTTP_and_real_processes', 'new_external_api_calls': 0, 'cases': results}, indent=2) + '\n')
    (OUT / 'controlled-http-calls.json').write_text(json.dumps(calls, indent=2) + '\n')
    # Private absolute-path manifests are not copied into public evidence.
    print(json.dumps({'completed_cases': len(results), 'failed_cases': sum(not x['passed'] for x in results)}), flush=True)
