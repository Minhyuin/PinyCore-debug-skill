#!/usr/bin/env python
"""Sanity-check the workflow files without a YAML parser.

PyYAML is not a dependency of this repository (nothing here is), and a workflow that fails to
parse costs a confusing red X on the very first push. This does what a parser would do for the
parts that actually break: every workflow must be present, name its triggers, declare jobs, and
pin actions to a version rather than a moving branch.

    python -X utf8 tools/check_workflows.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github' / 'workflows'

# Every workflow this repository is supposed to have. PinyCore's set has no analogue for `cd.yml`
# (a firmware build) or `request.yml`/`auto_assign.yml` (reviewer rotation across a team), so the
# expectation is written down here rather than inferred from whatever is on disk.
EXPECTED = ('ci.yml', 'labeler.yml', 'release.yml')


def main():
    failures = []
    names = sorted(path.name for path in WORKFLOWS.glob('*.yml'))
    for expected in EXPECTED:
        if expected not in names:
            failures.append(f'.github/workflows/{expected} is missing')
    print('workflows present: ' + ', '.join(names))

    for path in sorted(WORKFLOWS.glob('*.yml')):
        text = path.read_text(encoding='utf-8')
        for key in ('name:', 'on:', 'jobs:'):
            if not re.search(r'^' + re.escape(key), text, re.MULTILINE):
                failures.append(f'{path.name}: no top-level {key!r}')
        if 'permissions:' not in text:
            failures.append(f'{path.name}: declares no permissions block')
        for match in re.finditer(r'uses:\s*(\S+)', text):
            action = match.group(1)
            if '@' not in action:
                failures.append(f'{path.name}: action {action!r} is not pinned to a version')
            elif re.search(r'@(main|master|HEAD)$', action):
                failures.append(f'{path.name}: action {action!r} tracks a moving branch')
        if 'pull_request_target' in text:
            # Nothing here needs the privileged trigger, and it is the classic way a public
            # repository leaks a token to a fork.
            failures.append(f'{path.name}: uses pull_request_target, which this repo does not need')
    print(f'{len(names)} workflow file(s) checked')

    for path in sorted((ROOT / '.github').glob('*.yml')):
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            failures.append(f'{path.name}: empty')
        print('config present: ' + path.name)

    for failure in failures:
        print('FAIL ' + failure)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
