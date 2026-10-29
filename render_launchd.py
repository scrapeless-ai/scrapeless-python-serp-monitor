"""Render an example only; never install or activate a live schedule."""
import argparse
from pathlib import Path
import plistlib

p = argparse.ArgumentParser(description=__doc__)
for name in ('python', 'project', 'config', 'env-file', 'db', 'artifacts', 'output'):
    p.add_argument('--' + name, type=Path, required=True)
p.add_argument('--at-utc', default='09:00')
a = p.parse_args()
args = [str(a.python.resolve()), str(a.project.resolve() / 'daily_workflow.py'), 'tick',
        '--config', str(a.config.resolve()), '--env-file', str(a.env_file.resolve()),
        '--db', str(a.db.resolve()), '--artifacts', str(a.artifacts.resolve()), '--at-utc', a.at_utc]
a.artifacts.resolve().mkdir(parents=True, exist_ok=True)
job = {'Label': 'com.scrapeless.serp.daily.example', 'ProgramArguments': args,
       'WorkingDirectory': str(a.project.resolve()), 'StartInterval': 60, 'RunAtLoad': True,
       'StandardOutPath': str(a.artifacts.resolve() / 'scheduler.log'),
       'StandardErrorPath': str(a.artifacts.resolve() / 'scheduler.err'),
       'EnvironmentVariables': {'PYTHONDONTWRITEBYTECODE': '1'}}
a.output.resolve().parent.mkdir(parents=True, exist_ok=True)
a.output.write_bytes(plistlib.dumps(job))
print('Example rendered. No scheduler was installed or activated.')
