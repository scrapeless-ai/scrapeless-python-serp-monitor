# Contributing

Keep changes focused on the documented rank-monitoring workflow. Open an issue in the hosting repository with the Python version, sanitized command, expected behavior and observed result. Do not attach API keys, working environment files, private databases.

Before submitting changes:

```bash
python3 -m unittest discover -s tests -v
python3 serp_monitor.py demo
```

Use controlled responses for transport, task recovery and ranking tests. Keep synthetic fixtures separate from real observations. New features should update the README, command help and relevant tests together. Live requests consume credits and are not required for the existing local test suite.

Public documentation and image text must be English. Use placeholders for credentials. Record limitations accurately; do not describe local checks as hosted CI or production acceptance.
