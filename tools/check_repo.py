#!/usr/bin/env python
"""Repository self-checks: documentation rot, leaked configuration, version drift.

The package is mostly prose -- SKILL.md, STATUS.md, two reference documents -- and prose about a
moving target goes stale silently. Each check here exists because that specific thing HAS gone
stale in this repository:

* the test baseline was quoted in four files and each one was updated at a different time;
* the file map said ``piny_trace.py`` had ``doctor`` and ``capture`` subcommands, which it never
  had (they belong to ``piny_debug.py``), so a reader following the map ran a command that does
  not exist;
* ``profiles/pinycore.json`` holds this machine's absolute paths and this probe's serial number
  and must never be published to a public repository.

Everything is done with the standard library, because the package makes that promise and CI has
no pip step to make it untrue. Run it from the repository root:

    python -X utf8 tools/check_repo.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'

# Every file the package ships. A new module that is not listed here is a module nobody documents,
# so the check is deliberately a whitelist rather than "whatever happens to be on disk".
PACKAGE_FILES = (
    'SKILL.md',
    'STATUS.md',
    'README.md',
    'LICENSE',
    'profiles/pinycore.example.json',
    'scripts/piny_debug.py',
    'scripts/piny_linux.py',
    'scripts/piny_tools.py',
    'scripts/piny_trace.py',
    'scripts/piny_view.py',
    'scripts/piny_view_win32.py',
    'scripts/piny_view_x11.py',
    'tests/test_debug.py',
    'tests/test_linux.py',
    'tests/test_tools.py',
    'tests/test_trace.py',
    'tests/test_view.py',
    'tests/test_view_x11.py',
    'tools/check_repo.py',
    'tools/check_workflows.py',
    'tools/release_notes.py',
    'tools/smoke_doctor.py',
)

# Directories in which every `*.py` has to be declared above. A module that exists but is not in
# PACKAGE_FILES is a module no document names, which is how the file maps went stale before.
SHIPPED_DIRECTORIES = ('scripts', 'tests', 'tools')

# Files that must NOT be published. `profiles/pinycore.json` names machine absolute paths and the
# serial number of one physical probe; a clone is expected to start from the example instead.
MUST_NOT_BE_TRACKED = ('profiles/pinycore.json',)

# Documents whose stated test baseline must agree, and the two spellings they use for it.
TEST_COUNT_FILES = ('SKILL.md', 'STATUS.md', 'README.md', 'references/pinycore.md')
TEST_COUNT_PATTERNS = (re.compile(r'(\d+)\s+tests\b'), re.compile(r'(\d+)\s*个(?:用例|测试)'))
# A count spelled with one of those patterns only counts as a BASELINE if it is in the same
# ballpark as the number the suite actually ran. Without this, a sentence that describes part of
# the suite -- "test_linux.py's 26 cases" -- reads as a second, disagreeing baseline and fails the
# check on correct prose. A stale baseline is always near the real one, so the tolerance is wide
# on purpose: it is there to tell "a count of the whole suite" from "a count of something else",
# not to police a digit.
TEST_COUNT_TOLERANCE = 0.25


def load_module(name, path):
    # `piny_trace` imports `piny_debug` by bare name, and the tests do the same, so the scripts
    # directory has to be importable. Doing it here rather than relying on the caller's cwd keeps
    # `python tools/check_repo.py` working from anywhere.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def subcommands(parser):
    """The subcommand names an argparse parser actually accepts."""
    for action in parser._actions:
        if isinstance(getattr(action, 'choices', None), dict) and action.dest == 'action':
            return sorted(action.choices)
    return []


class Checks:
    def __init__(self):
        self.failures = []
        self.notes = []

    def fail(self, check, message):
        self.failures.append((check, message))

    def ok(self, message):
        self.notes.append(message)


def check_package_files(checks):
    missing = [name for name in PACKAGE_FILES if not (ROOT / name).is_file()]
    for name in missing:
        checks.fail('package files', 'declared in tools/check_repo.py but absent: ' + name)

    # The whitelist only proves the files it names exist. This is the other direction: a module
    # that was added and never declared would sail past a check that only reads the list, and that
    # is exactly how a file map goes stale. Adding a module therefore means declaring it here,
    # which in turn makes the file-map check below demand that a document names it.
    undeclared = []
    for directory in SHIPPED_DIRECTORIES:
        for path in sorted((ROOT / directory).glob('*.py')):
            name = f'{directory}/{path.name}'
            if name not in PACKAGE_FILES:
                undeclared.append(name)
    for name in undeclared:
        checks.fail('package files', f'{name} exists but is not declared in PACKAGE_FILES')

    if not missing and not undeclared:
        checks.ok(f'{len(PACKAGE_FILES)} declared package files present, none undeclared')

    # A file map that names a file which does not exist is worse than no file map, and one that
    # omits a file means a reader never learns it is there.
    documented = '\n'.join((ROOT / name).read_text(encoding='utf-8')
                           for name in ('STATUS.md', 'README.md') if (ROOT / name).is_file())
    for name in PACKAGE_FILES:
        if name.endswith(('.json', 'LICENSE')) or name in ('STATUS.md', 'README.md'):
            continue
        # The full path or the bare filename both count: a file map lists `tests/test_debug.py`,
        # while a sentence about a module says `test_debug.py`, and both tell a reader it exists.
        if name not in documented and Path(name).name not in documented:
            checks.fail('file map', f'{name} is shipped but never named in STATUS.md or README.md')


def check_documented_commands(checks):
    debug = load_module('piny_debug', SCRIPTS / 'piny_debug.py')
    trace = load_module('piny_trace', SCRIPTS / 'piny_trace.py')
    real = {'piny_debug.py': subcommands(debug.build_parser()),
            'piny_trace.py': subcommands(trace.build_parser())}
    checks.ok('subcommands: ' + '; '.join(f'{k}: {", ".join(v)}' for k, v in real.items()))

    # Each document introduces a script's subcommands as a slash-separated list. That list is the
    # claim worth checking: it is what a reader copies, and both halves of this repository's file
    # map have been wrong about it (STATUS.md claimed piny_trace.py had `doctor` and `capture`).
    #
    # A chunk naming BOTH scripts is skipped rather than guessed at: the two lists sit in one
    # sentence ("piny_debug.py is ... (`doctor` / ...), piny_trace.py is ... (`analyze` / ...)"),
    # and attributing the first list to the second script would be a false accusation. What this
    # check does prove is that every single-script list is right.
    list_shape = re.compile(r'`((?:[a-z][a-z0-9-]+`\s*/\s*)+`[a-z][a-z0-9-]+)`')
    for document in ('SKILL.md', 'STATUS.md', 'README.md'):
        path = ROOT / document
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        for script, names in real.items():
            for chunk in re.split(r'[。\n]', text):
                if script not in chunk:
                    continue
                if any(other in chunk for other in real if other != script):
                    continue
                found = list_shape.search(chunk)
                if not found:
                    continue
                claimed = re.findall(r'[a-z][a-z0-9-]+', found.group(1))
                wrong = [name for name in claimed if name not in names]
                if wrong:
                    checks.fail('documented commands',
                                f'{document} says {script} has {", ".join(wrong)}; '
                                f'it has {", ".join(names)}')
                elif claimed == names:
                    checks.ok(f'{document} lists {script} subcommands correctly')

    # The other half of the same claim: a name only belongs in a list because it lives on the
    # other script, and that is only true while it is still there.
    for name in ('doctor', 'capture'):
        if name not in real['piny_debug.py']:
            checks.fail('documented commands',
                        f'docs say {name!r} belongs to piny_debug.py, but it does not')


# Values in the template that are legitimately not "one machine's path": either a name resolved
# inside the checkout, or SEGGER's own default install location, which is the same directory on
# every Windows box and needs no secret to name.
EXAMPLE_ALLOWED_VALUES = ('tools/openocd/dap.cfg', 'target/stm32h7x.cfg',
                          'build/agent-profile/PinyCore.elf',
                          'C:/Program Files/SEGGER/JLink/JLinkGDBServerCL.exe',
                          'C:/Program Files/SEGGER/SystemView/SystemView.exe')


def check_example_profile(checks):
    example = ROOT / 'profiles' / 'pinycore.example.json'
    try:
        payload = json.loads(example.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        checks.fail('example profile', f'not valid JSON: {exc}')
        return

    # Every machine-derived path must be a placeholder. A template that ships one real path is a
    # template that silently points at somebody else's machine. The test is the directory part:
    # "C:/path/to/x.exe" and "C:/Program Files/SEGGER/..." are generic install locations that any
    # reader recognises as "replace me", whereas "E:/Ele/Tool/..." is one person's disk.
    config_keys = {'interface_config', 'target_config', 'elf', 'isr_coverage', 'name'}
    for key, value in payload.items():
        if key.startswith('_') or key in config_keys:
            continue
        if not isinstance(value, str) or '/' not in value and '\\' not in value:
            continue
        if value in EXAMPLE_ALLOWED_VALUES or 'path/to' in value:
            continue
        checks.fail('example profile',
                    f'{key} = {value!r} is neither a "path/to" placeholder nor a '
                    f'project-relative value; a template must not carry one machine\'s path')
    if payload.get('jlink_serial') != 'PUT-YOUR-PROBE-SERIAL-HERE':
        checks.fail('example profile', 'jlink_serial must stay the explicit placeholder; the '
                                       'serial identifies one physical probe and is never guessed')
    if payload.get('freertos_tick_symbol') is not None:
        checks.fail('example profile', 'freertos_tick_symbol must be null in the template: it is '
                                       'an address inside one built image')
    for key in ('project', 'gdb', 'jlink_gdb_server', 'systemview', 'probe', 'jlink_device'):
        if key not in payload:
            checks.fail('example profile', f'required key {key!r} is missing')
    checks.ok('example profile parses and is placeholders only')

    # The template must survive the code path it exists for.
    debug = load_module('piny_debug', SCRIPTS / 'piny_debug.py')
    strict = debug.load_profile(example, discover_tools=False)
    if strict.get('name') != payload['name']:
        checks.fail('example profile', 'load_profile did not return the template unchanged')
    if strict.get('_tools'):
        checks.fail('example profile', 'strict mode discovered tools; it must use the file as written')


def own_git_root():
    """The repository root, but only when it is this directory.

    This package is also installed inside other projects (e.g. `<project>/.dsh/skills/`), and there
    the enclosing repository is somebody else's. Reading its index to answer questions about the
    skill would be both wrong and alarming -- the local profile is *expected* to be untracked here
    and tracked there -- so the git-backed checks stand down instead of guessing.
    """
    try:
        proc = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=ROOT, capture_output=True,
                              text=True, errors='replace', timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return Path(proc.stdout.strip()).resolve() == ROOT.resolve()
    except OSError:
        return False


def check_no_leaked_configuration(checks):
    if not own_git_root():
        checks.ok('leaked-configuration check skipped: this is not a checkout of this repository')
        return
    try:
        tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True,
                                 timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        checks.fail('leaked configuration', f'could not list tracked files: {exc}')
        return
    if tracked.returncode != 0:
        checks.fail('leaked configuration', 'git ls-files failed: ' + tracked.stderr.strip())
        return
    names = {line.strip().replace('\\', '/') for line in tracked.stdout.splitlines() if line.strip()}
    for name in MUST_NOT_BE_TRACKED:
        if name in names:
            checks.fail('leaked configuration',
                        f'{name} is tracked; it names this machine and this probe, and the '
                        f'repository is public. `git rm --cached {name}` and keep it gitignored.')
    if names:
        checks.ok(f'{len(names)} tracked files, no local profile among them')


def check_test_baseline(checks, expected):
    seen = {}
    for document in TEST_COUNT_FILES:
        path = ROOT / document
        if not path.is_file():
            checks.fail('test baseline', f'{document} is missing')
            continue
        text = path.read_text(encoding='utf-8')
        found = {match.group(1) for pattern in TEST_COUNT_PATTERNS
                 for match in pattern.finditer(text)}
        found = {number for number in found
                 if abs(int(number) - expected) <= expected * TEST_COUNT_TOLERANCE}
        if not found:
            checks.fail('test baseline',
                        f'{document} states no test count; add "-> {expected} tests" so the '
                        f'number is checkable instead of remembered')
            continue
        if found != {str(expected)}:
            checks.fail('test baseline',
                        f'{document} says {sorted(found)}, the suite ran {expected}')
        seen[document] = found
    if seen and not checks.failures:
        checks.ok(f'test baseline {expected} agrees across {", ".join(sorted(seen))}')


def check_version(checks, expected):
    README = (ROOT / 'README.md').read_text(encoding='utf-8')
    match = re.search(r'github/v/release/[^)\s]+[^)]*', README)
    if not match:
        checks.fail('version', 'README.md carries no release badge to compare a tag against')
    debug = load_module('piny_debug', SCRIPTS / 'piny_debug.py')
    trace = load_module('piny_trace', SCRIPTS / 'piny_trace.py')
    if getattr(trace, '__version__', None) != getattr(debug, '__version__', None):
        checks.fail('version', 'piny_trace.py and piny_debug.py report different versions')
    if expected:
        if debug.__version__ != expected:
            checks.fail('version',
                        f'tag says {expected}, scripts say {debug.__version__}; bump '
                        f'__version__ in scripts/piny_debug.py in the same commit as the tag')
        else:
            checks.ok(f'version {expected} matches the scripts')


def check_line_endings(checks):
    """Text must enter the repository as LF, which is what `.gitattributes` asks for.

    The released artifact carries SHA256SUMS, so a file whose stored bytes depend on which machine
    committed it would make those checksums unreproducible. `git ls-files --eol` reads what is
    actually in the index rather than what one editor happened to leave in the working tree, and
    `w/crlf` alone is not a failure -- that is just how the file sits on this disk.
    """
    if not own_git_root():
        checks.ok('line-endings check skipped: this is not a checkout of this repository')
        return
    proc = subprocess.run(['git', 'ls-files', '--eol'], cwd=ROOT, capture_output=True, text=True,
                          errors='replace')
    if proc.returncode != 0:
        checks.fail('line endings', 'git ls-files --eol failed: ' + proc.stderr.strip())
        return
    offenders = []
    for line in proc.stdout.splitlines():
        fields = line.split('\t', 1)
        if len(fields) != 2:
            continue
        attributes, path = fields
        match = re.search(r'i/(\S+)', attributes)
        if match and match.group(1) not in ('none', 'lf'):
            offenders.append(f'{path} (i/{match.group(1)})')
    if offenders:
        checks.fail('line endings',
                    'stored with CRLF; run "git add --renormalize ." and commit: '
                    + ', '.join(offenders))
    else:
        checks.ok('every tracked text file is stored as LF')


def read_test_count():
    """Ask unittest how many tests exist, without stating the number anywhere in this file."""
    proc = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'unittest', 'discover',
                           '-s', 'tests', '-t', 'tests', '-v'],
                          cwd=ROOT, capture_output=True, text=True, errors='replace', timeout=600)
    match = re.search(r'^Ran (\d+) tests? in', proc.stderr + proc.stdout, re.MULTILINE)
    if not match:
        return None, (proc.stderr + proc.stdout)[-2000:]
    return int(match.group(1)), ''


def run(tag=None):
    checks = Checks()
    check_package_files(checks)
    check_documented_commands(checks)
    check_example_profile(checks)
    check_line_endings(checks)
    check_no_leaked_configuration(checks)
    check_version(checks, tag)

    count, tail = read_test_count()
    if count is None:
        checks.fail('test baseline', 'could not read the test count from unittest:\n' + tail)
    else:
        check_test_baseline(checks, count)

    for note in checks.notes:
        print('ok   ' + note)
    for check, message in checks.failures:
        print(f'FAIL [{check}] {message}')
    print()
    if checks.failures:
        print(f'{len(checks.failures)} check(s) failed')
        return 1
    print('all repository checks passed')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--tag', default=None,
                    help='release tag being published, e.g. v0.1.0; checks the scripts agree')
    args = ap.parse_args(argv)
    tag = args.tag.lstrip('v') if args.tag else None
    return run(tag)


if __name__ == '__main__':
    sys.exit(main())
