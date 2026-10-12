#!/usr/bin/env python
"""Release notes for a tag, written from the repository's own facts.

PinyCore generates its notes from commit types with `github-tag-action`. This repository is one
package rather than a firmware tree, so what a reader of a release wants is shorter and more
specific: what this version promises, what changed since the previous tag, and how to install it.
The one thing that must never be guessed is the test count, so it is taken from the same four
documents that CI checks rather than restated here.

    python -X utf8 tools/release_notes.py v0.1.0
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_COUNT_FILES = ('SKILL.md', 'STATUS.md', 'references/pinycore.md')
TEST_COUNT_PATTERNS = (re.compile(r'(\d+)\s+tests\b'), re.compile(r'(\d+)\s*个(?:用例|测试)'))

# Commit types, spelled the way the log spells them, mapped to a heading. This mirrors the shape
# of PinyCore's `.github/release.yml` categories by label.
SECTIONS = (('feat', '新增 / Features'), ('fix', '修复 / Fixes'),
            ('perf', '性能 / Performance'), ('refactor', '重构 / Refactoring'),
            ('doc', '文档 / Documentation'), ('test', '测试 / Tests'),
            ('build', '构建 / Build'), ('ci', 'CI/CD'), ('chore', '杂项 / Chores'))


def git(*args):
    proc = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True,
                          errors='replace', timeout=60)
    return proc.stdout.strip() if proc.returncode == 0 else ''


def previous_tag(tag):
    for candidate in git('tag', '--sort=-creatordate').splitlines():
        if candidate.strip() and candidate.strip() != tag:
            return candidate.strip()
    return ''


def commits(tag):
    previous = previous_tag(tag)
    span = f'{previous}..{tag}' if previous else tag
    lines = git('log', '--pretty=%s', span).splitlines()
    if not lines and not previous:
        lines = git('log', '--pretty=%s', '-n', '50').splitlines()
    return previous, [line for line in lines if line.strip()]


def test_count():
    counts = set()
    for document in TEST_COUNT_FILES:
        path = ROOT / document
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        counts |= {match.group(1) for pattern in TEST_COUNT_PATTERNS
                   for match in pattern.finditer(text)}
    return sorted(counts)[0] if len(counts) == 1 else '?'


def main(argv):
    tag = (argv[0] if argv else '').lstrip('v')
    if not tag:
        print('usage: release_notes.py <tag>', file=sys.stderr)
        return 2
    version = tag
    previous, messages = commits('v' + version if not version.startswith('v') else version)

    grouped = {}
    other = []
    for message in messages:
        head = re.match(r'^([a-z]+)(\([^)]*\))?!?:', message)
        if head and head.group(1) in dict(SECTIONS):
            grouped.setdefault(head.group(1), []).append(message)
        else:
            other.append(message)

    print(f'# PinyCore-debug-skill {version}')
    print()
    print('Agent-facing skill package for debugging and profiling a PinyCore board over a SEGGER')
    print('J-Link: register access through GDB, CPU-load measurement through SEGGER SystemView.')
    print()
    print(f'- Skills version: `{version}` (`scripts/piny_debug.py --version`)')
    print(f'- Tests: {test_count()} (`python -X utf8 -m unittest discover -s tests -t tests`)')
    print('- Python 3.10+ on Windows and Linux; standard library only, no pip install')
    print()
    if previous:
        print(f'## Changes since {previous}')
    else:
        print('## Changes')
    print()
    for key, title in SECTIONS:
        entries = grouped.get(key)
        if not entries:
            continue
        print(f'### {title}')
        print()
        for entry in entries:
            print(f'- {entry}')
        print()
    if other:
        print('### 其他 / Other')
        print()
        for entry in other:
            print(f'- {entry}')
        print()
    print('## Install')
    print()
    print('```powershell')
    print('git clone https://github.com/Minhyuin/PinyCore-debug-skill.git')
    print('Copy-Item -Recurse PinyCore-debug-skill "$env:USERPROFILE\\.dsh\\skills\\PinyCore-debug"')
    print('# then, in your PinyCore checkout:')
    print('Copy-Item profiles\\pinycore.example.json profiles\\my-rig.json   # fill it in')
    print('python scripts\\piny_debug.py doctor --portable                   # what to fix')
    print('python scripts\\piny_debug.py doctor --write-profile profiles\\my-rig.json')
    print('```')
    print()
    print('The archive attached to this release is the same package; it deliberately does not')
    print('contain `profiles/pinycore.json`, which names one machine and one probe.')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
