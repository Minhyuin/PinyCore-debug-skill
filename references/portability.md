# Migrating PinyCore-debug to another Windows machine

The package was developed against one rig. This file is what to do on the next one, and what
"portable" does and does not mean here.

## The short version

```powershell
$skill = '<the skill directory on this machine>'
$tool  = Join-Path $skill 'scripts\piny_debug.py'

# 1. What does this machine have? Nothing is written, nothing is connected.
python $tool doctor --portable

# 2. Start a profile from the template.
Copy-Item (Join-Path $skill 'profiles\pinycore.example.json') (Join-Path $skill 'profiles\my-rig.json')
#    ...edit "project", "elf", "jlink_device", "jlink_serial" ...

# 3. If doctor found the tools, write those paths in -- only when you ask for it by name.
python $tool doctor --profile (Join-Path $skill 'profiles\my-rig.json') `
                    --write-profile (Join-Path $skill 'profiles\my-rig.json')
```

`usable: true` and an empty `problems[]` in the doctor output is the goal. Everything below
explains what the report means.

## What is actually machine-bound

Nothing in the scripts. The only absolute path in the host code is

```python
SYSTEMVIEW_INI = Path.home()/'AppData'/'Roaming'/'SEGGER'/'SEGGER SystemView.ini'   # piny_trace.py:261
```

which is correct on any Windows installation because it starts from `Path.home()`. Every other
path is read out of the profile named by `--profile`. So the machine-specific surface is exactly
one file: `profiles/<rig>.json`.

## Discovery order

`doctor` and every session resolve a tool through `piny_tools.find_tool`, which tries, in order:

1. **The profile's own value**, whenever it points at a file that exists. An explicit
   configuration always wins, and discovery never rewrites a working profile behind your back.
2. **PATH** (`shutil.which`), under the usual names: `arm-none-eabi-gdb`, `JLinkGDBServerCL`,
   `JLinkGDBServer`, `SystemView`, `openocd`.
3. **The conventional install directories** under `%ProgramFiles%`, `%ProgramFiles(x86)%`,
   `%ProgramW6432%`, `%ProgramData%`, `%LOCALAPPDATA%\Programs`, `%USERPROFILE%\scoop\apps`,
   `%ChocolateyInstall%` and the literal `C:\Program Files*`, matched against the layouts
   `SEGGER/JLink*/`, `SEGGER/SystemView*/`, `OpenOCD/bin/`, `chocolatey/lib/openocd/...`.
   A version-less directory (`SEGGER/JLink`) is preferred to a release-stamped one because it is
   the name the user controls; stamped directories are the fallback.
4. **The registry** (`HKLM\SOFTWARE\SEGGER\J-Link`, `HKLM\SOFTWARE\WOW6432Node\SEGGER\J-Link`,
   and the matching SystemView keys), read through `winreg` only on Windows.

Every result carries the step that answered it (`source`: `profile`, `PATH: <name>`,
`<install label>`, or `registry: <key>`), so a wrong answer is diagnosable rather than merely
wrong. Duplicate paths are collapsed case-insensitively, because a PATH hit is usually also an
install-directory hit.

## What to change, and what never to copy

`doctor --portable` splits the profile keys in two. This split is the part that matters, because
one half is a migration chore and the other half is a correctness trap.

| kind | keys | meaning |
|---|---|---|
| `machine` | `project`, `gdb`, `openocd`, `systemview`, `jlink_gdb_server` | belong to the computer. A stale value can be relocated by discovery, and copying the wrong one is an obvious failure. |
| `target` | `probe`, `jlink_device`, `jlink_serial`, `jlink_speed_khz`, `expected_device_id`, `idcode_address`, `freertos_tick_symbol`, `cycle_frequency_hz`, `ram_ranges` | belong to the board and the probe. **Copying these to another rig is how a plausible-looking profile produces wrong numbers.** Re-establish them there. |

Two of the target keys deserve their own warning:

- `jlink_speed_khz` is a **measurement**, not a preference. On the development rig, 4000 kHz could
  not drain the 1 KB SysView RTT buffer: 2847 overflow events were dropped, the exported context
  loads summed to 65.4 % instead of 100 %, and Idle was understated as 48.5 %. At 12000 kHz the
  same window gave 1 overflow and a 100.07 % sum; at 24000 kHz, 0 overflows and 99.99 %. The
  quality gate (`context_load_sum` within ±1 of 100, `overflow_events == 0`) is what certifies a
  capture, so **never lower the speed to make a run "work"** and never copy this number to a rig
  whose wiring, probe or event rate differs. Re-measure and check the sum.
- `jlink_serial` identifies one probe. When it is missing, `doctor` reports that and stops there:
  it deliberately does not pick whichever probe answers, because that would silently change which
  board you are talking to. Read the serial from `JLink.exe` or JLinkConfig and set it.

## Missing tools are errors with names

Before, a wrong path surfaced as

```
FileNotFoundError: [WinError 2] 系统找不到指定的文件
```

from inside `subprocess.Popen`, naming neither the profile key nor the tool. Now `tool_path()`
raises:

```
Profile key "gdb" points at E:/gone/gdb.exe, which is not a file on this machine.
Set it to the right path in <profile>. Found one here instead: <path> -- run "doctor" to see the full list.
```

and `doctor` lists every problem as an action under `problems[]`, including a `project` directory
that does not exist (it is also the subprocess working directory, so it was worth checking) and an
ELF that has not been built yet.

## The liveness symbol no longer has to be maintained

`freertos_tick_symbol` is the address of FreeRTOS's `xTickCount`, which is a property of one built
image and moves on every rebuild. It may now be omitted: when it is absent, the address is resolved
out of the ELF named by the profile with `arm-none-eabi-nm` (looked for beside the configured GDB,
then on PATH). A profile value still wins, for the case where you deliberately want to watch a
different image. When it cannot be resolved, the liveness check reports `checked: false` — never a
guess — and `doctor` says so in `tick_note`.

## Strict mode

`load_profile(path, discover_tools=False)` uses the profile exactly as written and performs no
probing. Use it to reproduce a problem, or on a locked-down machine where even the discovery
itself should be off. The CLIs do not expose it because discovery only ever substitutes a tool the
machine actually has; a missing tool is reported, not invented.

## What has and has not been verified

- Verified: the whole test suite (129 tests, `OK (skipped=1)`) runs against temporary directories
  and patched probes, so it exercises the discovery logic without depending on this machine;
  `doctor --portable` on the development rig reports every tool from `profile` with
  `usable: true`; and a deliberately broken profile — `D:/code/PinyCore`, GDB and
  `JLinkGDBServerCL.exe` under `C:/Users/Someone/...` and `D:/Tools/...` — was recovered on this
  machine, with GDB found on PATH, J-Link found through `HKLM\SOFTWARE\SEGGER\J-Link`,
  SystemView and OpenOCD through their install directories, all four written out as
  `suggested_profile_overrides`, and the missing `project` directory reported. Evidence:
  `artifacts/portability-doctor`, `artifacts/portability-foreign`, `artifacts/portability-write`.
- **Not verified: a second real machine.** `INSTALL_BASES` and the registry keys are written from
  ordinary install layouts, not from a survey. On the first new machine, run `doctor --portable`
  and check the `source` of each entry before trusting it.

## One wart, stated so it is not a surprise

Every command, `doctor` included, creates this run's own `artifacts/<timestamp>-<hex>/` directory
under the working directory even when it has nothing to write. That is the same evidence-directory
convention the rest of the tool follows (each run keeps its `result.json` and logs), but it means a
skill installed inside a version-controlled tree leaves an untracked `artifacts/` behind. Ignore it
in the project's `.gitignore`, or pass `--output` somewhere else.
