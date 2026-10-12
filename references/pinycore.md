# PinyCore profile and handoff

## Local profile (2026-10-09)

- Project: `E:\Ele\Project\rm\PinyCore`.
- Source skill/workbench: `E:\Ele\Project\rm\PinyCore-debug`.
- Installed skill (DSH): `E:\Ele\Project\rm\PinyCore\.dsh\skills\PinyCore-debug`. The DSH project skill root is `<projectRoot>/.dsh/skills`; this install lands inside the PinyCore repo.
- Prior Codex CLI install: `C:\Users\User\.codex\skills\PinyCore-debug`. It still holds pristine copies of the original scripts (`piny_debug.py` 16118 B, `piny_trace.py` 13698 B) and is the provenance record for the recovered files; it predates `piny_view.py`.
- Default configuration: `profiles/pinycore.json`. Paths are intentionally machine-local in this customization stage; use another profile to relocate.
- Target: STM32H723VGTX, Cortex-M7. USB identity is not target identity.
- **The attached probe as of 2026-10-12 is a SEGGER J-Link** (profile `"probe": "jlink"`, `jlink_device` `STM32H723VG`, `jlink_serial` `602712375`), not the earlier CMSIS-DAP-v1-MuseLab/DAPLink (VID:PID 0D28:0204). Both identities are recorded because the profile changed mid-effort; confirm with the profile, not from memory.
- **OpenOCD cannot drive the J-Link**: SEGGER's kernel driver owns the device and OpenOCD reports `LIBUSB_ERROR_NOT_FOUND`. The whole `capture` (OpenOCD RTT server) path is unusable with this probe; `record` exists because of that.
- GDB: ARM GNU 14.3.Rel1, executable path recorded in profile. OpenOCD 0.12.0 (Chocolatey real executable, not shell shim) — only usable with a CMSIS-DAP-style probe.
- SystemView V3.60e: `E:\Ele\Tool\SEGGER\SystemView_V360e\SystemView.exe`; settings `C:\Users\User\AppData\Roaming\SEGGER\SEGGER SystemView.ini` (backups `.pinycore-backup*`).
- Avoid PATH `jlink`: this machine also has Java's unrelated jlink.exe.
- SWD default was 500 kHz for the CMSIS-DAP probe after an unreliable 4 MHz bulk transfer; for the J-Link the working rate is **24000 kHz** (`jlink_speed_khz`), chosen by measurement — see the trace section.
- Read-only SWD check (`identify`, CMSIS-DAP era): DBGMCU device ID `0x483` matched the profile, CPUID `0x411fc272` (Cortex-M7 r1p2), CPU free-running at both entry and exit. 2026-10-12.
- `RCC_RSR` (`0x58024cd0`) read `0x0` on 2026-10-12 — no pin, brown-out, software, watchdog, low-power or D1/D2 domain reset. The register is read-to-clear, so that history was consumed by the read.
- `S_RETIRE_ST` is never set by this adapter, so the retire probe on `identify` returns **inconclusive** (`cpu_executing: null`), never `false`. Use `registers --allow-halt` to settle whether the firmware executes.

## Build/workload

Kconfig enables `APP_USE_INS`, `EXTENSION_SENSOR`, `USE_MATHEMATICA`, `USE_MATHEMATICA_MATRIX`, `USE_STM_LOG`, `APP_USE_DAEMONS`. INS/matrix/log were already enabled; daemon was newly enabled. Chassis, gimbal, tests remain off. Disabled `EXTENSION_MOTOR` because no motor model was selected; existing CMake correctly rejected that configuration. No actuator workload was added.

```powershell
Set-Location 'E:\Ele\Project\rm\PinyCore'
python tools/script/Kconfig2h.py
cmake -S . -B build/agent-profile -G Ninja -DFETCHCONTENT_FULLY_DISCONNECTED=ON
cmake --build build/agent-profile -j 8
```

Use kconfiglib to change `.config`, then the existing generator to update `Src/Config/sdkconfig.h` and `.cmake`. These and `.config.old` are ignored/generated files, so `git status` alone does not prove they were unchanged. Do not hand-edit generated headers or change HAL/third-party libraries.

Built ELF: `E:\Ele\Project\rm\PinyCore\build\agent-profile\PinyCore.elf`.
Build passed on 2026-10-09: FLASH 218484 bytes; DTCMRAM 104736 bytes. Build mode is the project's Debug (-O0/-g3); do not call this a release-performance baseline.
Original configuration backups: workbench `artifacts/firmware-baseline/`.
The old `build/PinyCore.elf` dated September 2 is not the new profiling build.

INS constructor spins while BMI088 initialization fails. A CPU running after flash does not prove that FreeRTOS scheduling started. Daemon work is scheduled through AppManager rather than necessarily appearing as a separate named FreeRTOS task. Log enablement alone is not a fixed synthetic logging rate. Report the tasks and events actually observed.

## Trace facts

`Src/PinyCore.cpp` invokes `SEGGER_SYSVIEW_Conf()`; `FreeRTOSConfig.h` includes the v11 hooks. Target SystemView source version is 3.32; installed desktop reader is 3.60e. Native compatibility is still part of hardware acceptance.

`_SEGGER_RTT` was extracted from a DTCM dump at `0x20004FC0` (`aUp[1]` = `SysView`, 1024 B buffer, control block in DTCM — the only "SEGGER RTT" signature in DTCM, so there is no stale duplicate). `SEGGER_RTT_MAX_NUM_UP_BUFFERS` is actually **3**, not the 2 a casual read of `SEGGER_RTT.c:81-86` suggests; verified from the live control block (`MaxNumUpBuffers=3`, `MaxNumDownBuffers=3`). Default channel setting zero means auto-allocation, not channel zero. Match `SysView` in both up/down descriptors. Command 1 starts and 2 stops (target `SEGGER_SYSVIEW_Int.h` enum).

J-Link finds the control block by RAM scan, not by symbol (`JLinkRTTLogger.exe …` reported `3 up-channels found: 0: Terminal, 1: SysView, 2: (empty)`), so `-rttcbaddr` is an optimization, not a requirement.

`SEGGER_SYSVIEW_START_ON_INIT` is not defined for this build, so **the target never starts tracing by itself** and `MainContext.EnableState` stays 1 until the host sends Start. `Src/PinyCore.cpp:21` calls `SEGGER_SYSVIEW_Conf()` after the logger's RTT init; `Config/SEGGER_SYSVIEW_Conf.h` is empty of defines.

## Recording with SystemView's own command line (`record`)

`-start|-stop|-save|-load|-export|-export-contexts` are **Command Line Control** options: only an already-running instance executes them. A fresh `SystemView.exe -start -save … -quit` launch opens a window and sits there. The control socket is TCP `localhost:19050`, one command per connection, refused until the instance is listening (~10 s after launch), and its only reply is the banner `SEGGER SystemView V3.60e Command Server` — command results are never reported, so **file existence is the only evidence of success**. `-stop` needs ~17 s to flush; `-save` of a 21 s recording (3489556 B) landed ~21 s after the command (hence `SAVE_WRITE_SECONDS = 45.0`). `-save "<quoted>"` silently writes nothing; the unquoted form works.

Two ways a failed capture still looks like success:
- **`LoadDataOnStart=true`** (the machine's default in the INI): SystemView loads its most recent recording at startup, and a `-save` with no live recording writes THAT file — measured at exactly 216157 bytes, byte-size-identical to `…\Sample\OS_IP_WebServer.SVDat`. `record` sets it to `false` for the run and restores the INI afterwards.
- **A relative `-save` path**: resolved against SystemView's own working directory, so `-save artifacts\board-run2\live.svdat` wrote 0 bytes and raised `SystemView Error` 2 s later, while an absolute path in the same run wrote 2296373 bytes. `record` passes `(out/'live.svdat').resolve()` and has a regression test asserting `path.is_absolute()`.

Dialogs: the `No Commercial-Use License | SEGGER SystemView V3.60e` modal appears on every launch and is answered automatically (fraction (0.80,0.90) → click (988,588)) unless `--no-auto-accept-license`. Qt-rendered `System Information` / `SystemView Error` boxes expose **no text** to Win32 or UI Automation; they are dismissed by locating the small wide button in the bottom 30 % of the window (`piny_view.find_default_button`), never by a fixed fraction, and counted in the log's `message_boxes`. A `System Information` "… Events Loaded." box is informational, not a failure.

## Why the load numbers were wrong (probe speed)

SystemView's `CPU Load` column is `Total Run Time / recording span`, so **all contexts on one core must sum to ~100 %**; that sum is the strongest evidence a capture can be trusted. Same 6 s window, same firmware:

| J-Link rate | overflow events | load sum | Idle | busy |
|---|---|---|---|---|
| 4000 kHz | 2847 | **65.4 % (unusable)** | 48.5 % | 51.5 % (wrong) |
| 12000 kHz | 1 | 100.07 % | 76.24 % | 23.76 % |
| 24000 kHz | 0 | 99.99 % | 76.09 % | 23.91 % |

An under-speed probe cannot drain the target's 1 KB SysView buffer: events are dropped silently, Idle is understated and busy overstated. `Summarize()` reports `quality.context_load_sum`, and `context_load_sum_not_100:<core>` fails the gate. **Never lower `jlink_speed_khz` to make a run pass** — check the sum instead; `--allow-overflow` only labels the numbers, it does not validate them.

The recorded span always exceeds the request (data already buffered on the target when `-start` lands): 6 s → 11.7–11.8 s, 10 s → 15.3/16.1 s, 20 s → 26.1 s. The gate accepts `request ≤ recorded ≤ 2 × request`.

Target configuration still has the example `Cortex-M4` / FreeRTOS demo description, so traces report `N=FreeRTOS Demo Application,D=Cortex-M4,O=FreeRTOS` and every board run reports `cycle_frequency_hz 550000000` (still unverified against the firmware clock config). Do not fix third-party files implicitly. ISR tracing observed in FreeRTOS SysTick/port code does not cover every application IRQ. `USE_VSCODE_XRTOS` is off, so the guarded runtime-stats definitions do not make FreeRTOS runtime stats available.

SystemView manual: `E:\Ele\Tool\SEGGER\SystemView_V360e\Doc\UM08027_SystemView.pdf`:
- p.35: Contexts CPU Load is relative to complete recording; do not compute it from Total Run Time without accounting for interrupted time.
- pp.51-52: `-single`, `-port`, `-load`, `-export-contexts`, `-export`, `-delay`, `-quit`.
- p.56: raw `.bin` / `.SVDat` loading.

No SEGGER binary/source is redistributed with this skill. It uses the locally installed application; do not bypass license/UI requirements if an installation needs user action.

## Verification status: board trace captured and verified; register path proven, not re-proven on J-Link

- Host unit tests and local tool inventory passed (`python -X utf8 -m unittest discover -s tests -t tests` → 175 tests OK, 2 skipped; `pytest` is NOT installed). The `test_tools.py` cases cover host portability (path precedence, PATH/install-directory/registry discovery, deduplication, the missing-tool message, `--write-profile` merging and its opt-in rule, the machine-vs-target key split, and forward-vs-back slash paths in a hand-written profile), `test_linux.py` covers the Linux half of that discovery (install bases, `%VAR%` expansion, SEGGER's Linux executable spellings, version-less-directory preference, platform dispatch), and `test_view_x11.py` covers the X11 licence-dialog backend (button geometry, button identification, the XTest→XSendEvent→honest-failure staircase). All of them run against temporary directories or patched probes, so they do not depend on what is installed on the machine running them.
- **Real board, 2026-10-12, J-Link at 24000 kHz — first trustworthy load data.** `record --output artifacts\board-run4 --seconds 10` → `usable true`, `issues []`, `overflow_events 0`, `context_load_sum {'Core 0': 100.06}`, `trace_busy_percent 23.98`, span 15.300791916 s, 570739 events. `record --output artifacts\board-run5 --seconds 20` → `24.03 %`, span 26.106995436 s, 973859 events, sum 100.00, 0 overflow. Per-context (run4/run5): insTask 17.04/17.03, Scheduler 2.97/2.97, AppTask 2.27/2.28, SysTick 1.12/1.12, Tmr Svc 0.64/0.63, Idle 76.02/75.97, BuzzerTask 0.00, CmdTask 0.00. `compare` reports +0.05 percentage points between them.
- Superseded and NOT valid as measurements: `artifacts/board-run1` (busy 51.46) and `artifacts/board-run3` (51.44) were taken at 4000 kHz and sum to ~65 %, i.e. they are undercounts caused by dropped events. Keep them only as evidence for the probe-speed finding.
- The earlier `capture` runs through OpenOCD's RTT server produced `artifacts/board-run1`, which is the same undercount; `capture` itself is now unusable with the J-Link probe.
- Elsewhere in this file the SWD/identify/halt evidence belongs to the **CMSIS-DAP** probe era and has not been re-run on the J-Link; the ELF at `build/agent-profile/PinyCore.elf` was **absent** at that time (`doctor` reported `elf_exists false`), so the image in Flash is still not identified symbolically.
- `identify` on the CMSIS-DAP probe reported device ID `0x483`, CPUID `0x411fc272`, state `running` at entry and exit.
- Authorized halt (CMSIS-DAP era) proved the firmware **is executing**: PC differed between two snapshots (`0x08021bfe`, then `0x0800efe6`), both in Flash; `control 0x6` with `psp` active and differing between runs shows FreeRTOS thread mode on the process stack; PSP moved `0x20000e28` → `0x20000fc0`; the fault area `0xe000ed28..0xe000ed40` is all zero (no CFSR/HFSR fault). The flash image checks out: `vector[0] initial SP = 0x20020000`, `vector[1] Reset_Handler = 0x08019121`, `msp = 0x2001ffe0`.
- Original Flash backup is incomplete and MUST NOT be used as a restoration image.
- SEGGER's bundled `OS_IP_WebServer.SVDat` behavior is unchanged and still the offline fixture; its CPU-load export still trips the INT64 sentinel (below). The `1.94 %` busy / `168 MHz` figures in the older logs are that embOS/Kinetis sample, NOT PinyCore.
- 10726 files in HAL/third-party/Lib were hashed before and after building: no modifications or missing files. Evidence: `artifacts/protected-verification.json`.

Only update this status with fresh evidence. Do not claim the skill's hardware path is complete until the actual device checks pass.

## Stop-session handoff

Canonical handoff: `E:\Ele\Project\rm\PinyCore-debug\STATUS.md` (rewritten 2026-10-12 with the J-Link/`record` findings; the older copy had been lost between sessions along with `task_plan.md` / `findings.md` / `progress.md`, so pre-2026-10-12 narrative detail is reconstructed rather than quoted). Do not continue hardware operations without a new user request.

Historic defect (2026-10-09), root-caused 2026-10-12: the native sample export produced `CPU Load outside 0..100` with row `[{"name": "SysTick", "cpu_load": "10894144524.79 %"}]`. Cause: the last context left unclosed at the end of the recording accumulates the INT64 sentinel `9223372036854775808 ns` as its run time (its `Total Run Time` reads `9 223 372 036.854 775 808 s`, and the same sentinel appears in the Idle row's `Last Run Time`). It is an export-side defect of SystemView V3.60e's headless path — reproducible from SEGGER's own sample, so it is not caused by PinyCore, the RTT capture or the raw `.bin` import — and it is never clamped: an export containing it is rejected and re-taken. Regression tests live in `tests/test_trace.py`.

Still open after this effort: `cycle_frequency_hz 550000000` unverified against the firmware clock config; the hardcoded `FreeRTOS Demo Application` / `Cortex-M4` identity (fixing it needs an authorized third-party config edit plus a rebuild/flash); no before/after performance comparison has been run with `record` + `compare` yet; the register/GDB path has not been re-proven on the J-Link probe; the stale Codex copy at `C:\Users\User\.codex\skills\PinyCore-debug` remains as provenance only.
