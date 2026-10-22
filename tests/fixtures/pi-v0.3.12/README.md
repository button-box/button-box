Historical reader and controller fixtures from v0.3.12, commit `3fb1c2a3a4e25fccee6a2454f02f0675579a11dc`.

The four complete modules are exact source bytes. `finish_previews.py.txt`,
`runtime_state.py.txt`, `run.py.txt`, and `poll_once.py.txt` are the exact
`_finish_previews`, `_load`, `run`, and `poll_once` methods, with class indentation
removed. These runtime methods are also unchanged in step 1 (`04d53cd`).
Tests use these files without Git history or network access. Do not update them
when current code changes.
