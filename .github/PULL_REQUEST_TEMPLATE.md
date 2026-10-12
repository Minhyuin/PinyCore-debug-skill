<!--
Thanks for the pull request. This repository ships a debugging skill that other people copy onto
their own machines, so two things matter more here than usual:

* do not commit `profiles/pinycore.json` -- it names one machine's absolute paths and one probe's
  serial number, and CI fails if it is ever tracked;
* if you changed the number of tests, update the count in `SKILL.md`, `STATUS.md` and
  `references/pinycore.md` in the same commit. `tools/check_repo.py` compares all three against
  what the suite actually runs.
-->

## What this changes

<!-- One or two sentences. If it fixes something, say what was observed and on what. -->

## Why

<!-- The reason, not the summary. "doctor told the user to replace a working path with itself"
     is a reason; "fix doctor" is not. -->

## How it was verified

<!--
Be specific, and say which of these you actually did:
  [ ] python -X utf8 -m unittest discover -s tests -t tests
  [ ] python -X utf8 tools/check_repo.py
  [ ] python -X utf8 tools/smoke_doctor.py
  [ ] on a real board: identify / registers / record (say which, and paste the key numbers)
  [ ] not run (say why)
-->

## Affected surface

<!-- Tick what the change touches; this is what the labeler will label anyway. -->
- [ ] probe access (`piny_debug.py`)
- [ ] tracing (`piny_trace.py`, `piny_view.py`)
- [ ] portability (`piny_tools.py`, profile schema)
- [ ] documentation (`SKILL.md`, `STATUS.md`, `README.md`, `references/`)
- [ ] CI/CD (`tools/`, `.github/`)

## Checklist

- [ ] No `profiles/pinycore.json`, no absolute path from my own machine, no probe serial committed
- [ ] Documentation updated where the behaviour it describes changed
- [ ] Test count updated in all three documents if the suite grew or shrank
- [ ] No new third-party dependency (the package promises the standard library)
- [ ] If this touches a probe or a board, the reset/flash rules in `SKILL.md` are still honoured
