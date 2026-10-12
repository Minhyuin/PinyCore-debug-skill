#!/usr/bin/env python
"""A runner has no probe, no board and no SEGGER install. That is the point of this check.

It drives the CLI exactly the way the README tells a new user to, with the example profile, and
insists on a structured answer instead of a traceback. A package whose whole value is "the agent
can find out what is wrong" has to be able to say "I cannot find your debugger" on a machine that
has nothing installed.

Nothing here needs PyYAML, a probe or a licence; it is the part of CI that can run anywhere:

    python -X utf8 tools/smoke_doctor.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_placeholders(tmp):
    """The template with every tool pointed at a path that cannot exist.

    Used to demonstrate what discovery does with a profile full of placeholders on a machine that
    *does* have the tools: it should find them and say where. On a machine that has nothing
    installed the same file must instead produce one actionable problem per required tool, but
    that case cannot be staged from inside this process -- discovery legitimately looks at PATH,
    the install directories and the registry -- so it is covered by the unit tests instead
    (`tests/test_tools.py`, "a missing tool says so instead of raising").
    """
    payload = json.loads((ROOT / 'profiles' / 'pinycore.example.json').read_text(encoding='utf-8'))
    payload.update({key: 'C:/path/to/' + Path(key).name for key in
                    ('project', 'gdb', 'jlink_gdb_server', 'systemview', 'openocd')})
    path = Path(tmp) / 'placeholders.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def run_doctor(output_dir, profile, *extra):
    # `--profile` and `--output` are global options and have to precede the subcommand; the README
    # and SKILL.md both spell it that way, and this check is here partly to keep that true.
    cmd = [sys.executable, '-X', 'utf8', str(ROOT / 'scripts' / 'piny_debug.py'),
           '--profile', str(profile), '--output', str(output_dir), 'doctor', *extra]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, errors='replace',
                          timeout=300)
    try:
        return json.loads(proc.stdout), proc
    except ValueError:
        print('doctor did not print JSON\n--- stdout ---\n' + proc.stdout[-4000:] +
              '\n--- stderr ---\n' + proc.stderr[-4000:])
        raise SystemExit(1)


def main():
    failures = []
    example = ROOT / 'profiles' / 'pinycore.example.json'
    with tempfile.TemporaryDirectory() as tmp:
        report, _ = run_doctor(tmp, example)
        if report.get('action') != 'doctor':
            failures.append(f'action is {report.get("action")!r}, not "doctor"')
        for key in ('profile', 'probe', 'tools', 'problems', 'usable'):
            if key not in report:
                failures.append(f'the report has no {key!r} key')
        if report.get('project_exists'):
            failures.append('project_exists is true for a placeholder project path')
        if report.get('usable'):
            failures.append('usable is true although the template points at nothing')
        print('doctor with the example profile: '
              f'usable={report.get("usable")} problems={len(report.get("problems") or [])} '
              f'probe={report.get("probe")!r}')
        for problem in (report.get('problems') or [])[:4]:
            print('  - ' + str(problem))

        # --portable is the migration entry point, so it must also answer on a bare machine.
        portable, _ = run_doctor(tmp, example, '--portable')
        section = portable.get('portable') or {}
        if not section.get('machine_keys') or not section.get('target_keys'):
            failures.append('--portable did not classify the profile keys')
        print('portable classification: '
              f'{len(section.get("machine_keys") or [])} machine keys, '
              f'{len(section.get("target_keys") or [])} target keys')

        # What discovery does with a profile full of placeholders. On a machine that has the tools
        # it must find them and say through which step; on a runner that has none it must produce
        # one actionable problem per required tool and `usable: false`. Both outcomes are correct,
        # so this asserts the contract they share: the answer is structured, and every required
        # tool is either resolved or named as a problem.
        bare, _ = run_doctor(tmp, write_placeholders(tmp))
        problems = ' | '.join(str(item) for item in (bare.get('problems') or []))
        required = ['gdb', 'jlink_gdb_server']
        # `missing_required`, not `usable`: usable is also false when the project checkout is
        # absent, which it always is here, so it cannot distinguish an unreadable rig from an
        # unfindable toolchain.
        if not bare.get('missing_required'):
            resolved = {key: (bare.get('tools') or {}).get(key, {}) for key in required}
            unexplained = [key for key, entry in resolved.items()
                           if not entry.get('exists') and key not in problems]
            if unexplained:
                failures.append('doctor reports no missing tool, but '
                                f'{", ".join(unexplained)} is neither resolved nor named as a '
                                f'problem: {problems!r}')
            print('placeholders with tools present: resolved through ' +
                  ', '.join(f'{key}={entry.get("source")}' for key, entry in resolved.items()))
        else:
            for key in required:
                if key not in problems:
                    failures.append(f'tools are missing, but no problem mentions {key!r}: '
                                    f'{problems!r}')
            if bare.get('usable'):
                failures.append('a required tool is missing, yet usable is true')
            print(f'placeholders with nothing installed: missing_required='
                  f'{bare.get("missing_required")} usable={bare.get("usable")}')

    for failure in failures:
        print('FAIL ' + failure)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
