# PinyCore-debug — 状态交接

最后更新：2026-10-12（J-Link 实板验证 + SystemView 采集打通 + J-Link 烧录打通 + liveness 见证修正 + 可迁移性改造 + 独立仓库与 CI/CD）
包位置：`E:\Ele\Project\rm\PinyCore-debug`（源码与源码测试，独立 git 仓库，远端 <https://github.com/Minhyuin/PinyCore-debug-skill>）；已安装副本：`E:\Ele\Project\rm\PinyCore\.dsh\skills\PinyCore-debug`（agent 实际加载的就是这一份，改完必须同步）

## 这个包要做什么

让 agent 在 PinyCore（STM32H723VGTX）实板上自动完成两件事，支撑后续的性能优化迭代：

1. 启动 GDB，读取 MCU 寄存器/符号变量（`scripts/piny_debug.py`）。
2. 用 SEGGER SystemView 采集实板 CPU 负载并给出可比较的报告（`scripts/piny_trace.py`）。
3. 模态授权框的自动应答（`scripts/piny_view.py` 分派 + `scripts/piny_view_win32.py` / `scripts/piny_view_x11.py` 两套后端，被上面两个脚本共用）。

**换机器（可迁移性，2026-10-12 完成）**：脚本里没有写死任何本机路径，全部从 `--profile` 的 JSON 读取；换机器的动作就是让那份 JSON 在目标机器上成立。`doctor --portable` 会按 profile → PATH → 常见安装目录 → SEGGER 注册表的顺序探测每个工具、**报出是哪一步命中的**，并打印可直接粘贴的 profile 修正；`--write-profile <path>` 才写文件（必须点名）。`profiles/pinycore.example.json` 是模板。`jlink_serial` 缺失只报告、不猜。

## 当前状态：两条主路径都已在实板验证通过

### 1. 寄存器读取（J-Link + GDB/MI）— 可用

```powershell
cd E:\Ele\Project\rm\PinyCore-debug
python -X utf8 scripts\piny_debug.py --output artifacts\identify-jlink3 identify
python -X utf8 scripts\piny_debug.py --output artifacts\snapshot-jlink registers --allow-halt
```

实测结果（evidence：`artifacts/identify-jlink/result.json`、`artifacts/snapshot-jlink/result.json`）：

| 项 | 值 |
|---|---|
| 设备 ID | `0x483`（与 profile 的 `expected_device_id` 一致） |
| CPUID | `0x411fc272`（Cortex-M7 r1p1） |
| DHCSR / DEMCR | `0x30003` / `0x1000000`——但见下方「DHCSR 不可作为运行证据」 |
| ELF | `E:\Ele\Project\rm\PinyCore\build\agent-profile\PinyCore.elf`，sha256 `2609383cd4f5606c3caba55b9e447befa05da46a2ffb868484824be0d24fab98`（4930700 B） |
| ELF↔Flash | 全部可加载段 `compare-sections` 报 `matched`（`.isr_vector` `0x8000000`、`.text` `0x80002d0`、`.rodata` `0x8033cb0`、`.ARM`/`.init_array`/`.fini_array`、`.data` `0x8035200`） |
| 符号 | `SystemCoreClock = 550000000`；`pxCurrentTCB = 0x20006778` |
| 故障寄存器 | `0xe000ed28` 起 24 B，CFSR/HFSR 全部标志为 0 |
| 固件 liveness | `xTickCount` @ `0x20007e5c`，实测 **~1000–1043 Hz** 稳定递增（`artifacts/identify-liveness2`：1.5 s 内 `79605 → 81119`，`rate_hz 1009.3`） |

### DHCSR 不可作为运行证据（本板实测，2026-10-12）

一次 `identify` 实测：`DHCSR` 返回冻结的 `0x30003`（`C_HALT` 置位 = “已暂停”），而同一颗核的 RTOS tick 正在以 ~1009 Hz 递增；跨会话重复读取得到**完全相同**的 `0x30003`，即读数不是活的。一度据此误判“板子被停住了”，随后用 `xTickCount` 推翻。

因此：

- **判断固件是否在跑，只认 `xTickCount`**（`0x20007e5c`，由 RTOS tick 中断写入，调试器无法伪造）。`DHCSR`/`S_RETIRE_ST` 在这条探针上可以报告错误状态，只能作为辅助。
- 不要用 `DWT->CYCCNT` 当 liveness 见证：本板实测它只在会话内动过，且每个新会话都读回 `1`；`printf` 读它还会因“读即暂停-恢复”而采样失真（`artifacts/_scratch/exp56-dwt-liveness.ps1`、`exp57-cyccnt-between-sessions.ps1`）。
- **读 tick 前必须先 `monitor go`**，否则第一次读拿到的是 attach 时的缓存值（见「已知缺陷」第一条）。这也是 `probe_liveness` 里 `resume()` 排在两次读取之前的原因。
- `identify` 现在在 `state=halted` 时会观察 1.5 s 的 tick 来裁决，并自动恢复被本次连接停下的核（`--leave-halted` 可禁止）；`flash` 结果里也带 `liveness`。
- `freertos_tick_symbol` 是**镜像相关**地址，任何重建后都会变（相邻符号：`0x20007e58 uxCurrentNumberOfTasks`、`0x20007e64 xSchedulerRunning`）。**现在可以不写**：缺省时用 `arm-none-eabi-nm` 从 profile 指向的 ELF 解析 `xTickCount`，解析结果显示在 `doctor` 的 `tick_symbol_resolved`；写了则以 profile 为准。

### 3. 烧录（J-Link，实板已验证）— 可用

```powershell
python -X utf8 scripts\piny_debug.py --output artifacts\flash-jlink6 flash --allow-flash
```

`artifacts/flash-jlink5`（`ok true`，9.18 s）：设备身份 `0x483`/`0x1001`，写前与写后所有可加载段 `compare-sections` 全部 `matched`（`reported_match true`），`flash_verified true`，`monitor reset 0` 之后 `final_state "running"`，并且 `liveness` 用固件自己的 tick 证明核确实重新跑起来了（`tick_before 0` → `tick_after 1512`，`rate_hz 1008.0`）。

要点：

- J-Link 下 `flash` 走 GDB 的 `-target-download`（GDB 自己对每个写入块做校验），不再依赖 OpenOCD。
- 写前比较**不一致不阻止**烧录——不一致正是要烧录的理由；写后比较不一致才失败。
- **`monitor reset 0` 之后核是暂停的**，所以流程必须以「恢复运行 + 用 tick 验证」收尾，否则会把刚写好的板子留在静止状态。
- 烧录会**复位并运行**目标，等于改变被测系统；历史授权不延续，每次都需要当次授权与 `--allow-flash`。

关键实现约束（改动时不要回退）：

- GDB 必须用 `--quiet --nx --nh --interpreter=mi2 -ex "set mi-async on"` 启动。缺 `-ex` 时 `-exec-interrupt` 15 s 超时、后续命令全部挂死（对照实验 `artifacts/_scratch/exp35-mi-startup.py`）；运行中也无法事后设置（报 `Cannot change this setting while the inferior is running`）。
- GDB/MI 没有分区比较命令（`-data-compare-sections` → `Undefined MI command`），所以走 `-interpreter-exec console "compare-sections"`，并对 `MISMATCH` 或缺 `matched` 行的情况判失败（fail-closed）。
- 控制台文本以 MI STREAM 记录（`~"..."`）返回，`GDB.command(cmd,timeout,streams=...)` 负责收集。
- `ProbeSession` 判断后端一律用 `isinstance(...,OpenOCD)`，不用 `is OpenOCD`（后者在单测替换模块全局时会静默走 J-Link 分支）。

### 2. CPU 负载采集（J-Link + SystemView 命令行控制）— 可用

```powershell
python -X utf8 scripts\piny_trace.py record --output artifacts\board-run8 --seconds 10
```

最近一次完整跑通：`artifacts/board-run7`，全程 63.2 s，`ok true`：

| 字段 | 值 |
|---|---|
| `trace_busy_percent` | **24.05**（= 100 − Idle 75.95） |
| `recorded_seconds` | 13.889995313（请求 10 s） |
| `event_count` | 518140 |
| `cycle_frequency_hz` | 550000000 |
| `quality` | `usable true`，`issues []`，`overflow_events 0`，`start_events 1`，`stop_events 1`，`context_load_sum {"Core 0": 100.0}` |

各上下文（`cpu_percent`）：`SysTick 1.12`、`Scheduler 2.97`、`AppTask 2.28`、`BuzzerTask 0.00`、`Tmr Svc 0.63`、`insTask 17.05`、`CmdTask 0.00`、`Idle 75.95`。

另有两次早前通过质量门的采集：`artifacts/board-run4`（busy 23.98，15.300791916 s，570739 事件）、`artifacts/board-run5`（busy 24.03，26.107 s，973859 事件）；`compare artifacts\board-run4\summary.json artifacts\board-run5\summary.json` → `busy_delta_percentage_points 0.05`。

**这条路径能采信的唯一硬判据是「同核上下文 CPU Load 之和 ≈ 100 %」**，因为 SystemView 的 `CPU Load` 列 = `Total Run Time / 记录窗口`。同一段 6 s 窗口的实测对比（`artifacts/_scratch/exp24-speed.py`）：

| J-Link 速率 | 溢出事件 | 上下文负载合计 | Idle | busy |
|---|---|---|---|---|
| 4000 kHz | 2847 | **65.4 %（不可信）** | 48.5 % | 51.5 %（错） |
| 12000 kHz | 1 | 100.07 % | 76.24 % | 23.76 % |
| 24000 kHz | 0 | 99.99 % | 76.09 % | 23.91 % |

速率不够时 1 KB 的 SysView RTT 缓冲被填满、事件被丢，Idle 被低估、busy 被高估约 2 倍。`profiles/pinycore.json` 的 `jlink_speed_khz` 已定为 **24000**，**不要为了凑一次“成功”把它调低**。`artifacts/board-run1`（51.46）与 `board-run3`（51.44）就是 4000 kHz 时代的假结果，只可作为“速率不足”的例证，不得当基线。

## 已知缺陷与边界（不要重犯）

- **首次内存读数是 attach 时的调试缓存值（会骗人）**：J-Link GDB server 在会话建立时把内存值缓存，**attach 后的第一次读**返回这个缓存样本。实测：同一会话内两次读 `xTickCount`，间隔 4 s、中间不做任何运行控制，两次都是 `213916`——而板子明明在跑。加了运行控制后缓存被丢弃：`monitor go` 之后同一地址连续三次读是 `217714 / 217715 / 217716`（**中间完全没有延时**，说明差别不在时间而在缓存）。
  - 因此 liveness 窗口必须**先把核跑起来再取第一个样本**（`probe_liveness` 与 `tick_advances` 现在都是 `resume()` → 读 → sleep → 读）。曾经写成「先读、再 resume、再读」，那个窗口比较的是同一个陈旧值的两份拷贝，等于没测。
  - 一般教训：**不要用「隔一段时间的两次读」来证明任何东西在动**，除非确认第一次读不是缓存样本。`MONITOR go/halt` 除了控制运行，还是唯一已知的缓存失效手段。

- **SystemView V3.60e 无头导出的 INT64 哨兵**：录制结束时仍处于打开状态的上下文，其累计运行时间会写成 `9223372036854775808 ns`（`9223372036.854775808 s`），`CPU Load` 因此出现 `10894144524.79 %`，且 `number()` 还会撞上 `nan%` 单元格。已用 SEGGER 自带样例 `E:\Ele\Tool\SEGGER\SystemView_V360e\Sample\OS_IP_WebServer.SVDat` 复现同一现象，确认是导出侧缺陷而非 PinyCore 的记录或 RTT 采集。现在的处理是**拒绝**（`CPU Load outside 0..100` / `Invalid numeric CSV field: nan%`），**绝不 clamp、绝不删行**。同一份记录用 GUI 打开显示 0.49 %。
- **授权模态框必须用投递鼠标消息应答**：锁屏/气隙桌面下 `SetCursorPos`+`mouse_event` 全部丢失，`WindowFromPoint` 会返回 explorer 的 `LockScreenBackstopFrame`。`PostMessageW` 不需要输入桌面。按钮从左到右是 License Manager / Accept / Decline，客户区 720×352 时 Accept 中心是 `(613,327)`；**按右下角分数位置点击会点到 Decline**。投递回车（Return）也会 Decline 并让 SystemView rc=1 退出——对话框消失但进程退出，绝不能算成功（`accept_license` 用 `GetExitCodeProcess` 区分）。
- **`-save` 必须绝对路径、不能加引号**：SystemView 按自己的工作目录解析相对路径，实测相对路径写出 0 字节 + `SystemView Error`。
- **`LoadDataOnStart` 每次运行前置 false、结束还原**（备份 `SEGGER SystemView.ini.pinycore-record-backup`）。开着它时采集失败会把上一次的记录当成本次结果写出来，字节数与 SEGGER 样例一致——看起来完全像成功。
- **记录窗口总是比请求长**：`-start` 时目标 RTT 缓冲里还压着数据，实测请求 6 s → ~11.8 s、请求 10 s → 15.3–16.1 s。质量门接受 `请求 ≤ 记录 ≤ 2×请求`。
- **J-Link 探针下 OpenOCD 不可用**（SEGGER 内核驱动独占，`LIBUSB_ERROR_NOT_FOUND`），因此 `flash` 的 OpenOCD 分支在实机上不可用（`flash` 本身已有 J-Link 实现，见未完成项 1）。
- 不把安装成功、单元测试通过、SEGGER 样例分析、ELF 构建成功说成实板性能验证通过。

## 硬约束

- 显示名与目录名 `PinyCore-debug`，技能机器标识 `pinycore-debug`（兼容校验规范）。
- 保持分层：项目配置 / 执行器 / 分析器；通过 `--profile` 扩展；暂不加 MCP。
- 不改 HAL 或 `ThirdParty/Lib`。
- 复位与烧录只在**当前任务**明确授权下执行，历史授权不延续；`flash` 需要 `--allow-flash`。
- 核心寄存器快照会短暂暂停 CPU，需 `--allow-halt`；外设读取需 `--allow-peripheral-read`。
- 不终止用户自己的 GDB/J-Link/IDE/SystemView 进程；探针被占用时报错退出。
- 连接失败要记录完整日志与目标恢复状态；不得循环复位或盲目提速率。
- INS 的 Mathematica 矩阵依赖必须保持启用。
- 不得为了让测试通过而截断 >100 的 CPU 负载。

## 文件与证据地图

| 路径 | 内容 |
|---|---|
| `SKILL.md` | 技能说明：范围/边界/运行配方/迁移到另一台 Windows 机器/仓库与本地自检/授权框实测行为 |
| `STATUS.md` | 本文件 |
| `README.md` | 对外说明：能做什么、快速开始、三条实测硬结论、可迁移性、仓库与 CI/CD、范围与边界 |
| `LICENSE` | MIT |
| `.gitignore` | 忽略 `artifacts/`、`profiles/pinycore.json`（本机配置，永不入库）、缓存 |
| `.github/` | `workflows/{ci,release,labeler}.yml`、`labeler.yml`、`release.yml`（changelog 分类）、PR 与 issue 模板 |
| `tools/check_repo.py` | 防文档腐烂闸门：包内文件与文档互指、子命令归属、示例 profile 合法性、真实 profile 未被跟踪、四份文档的测试基线与实跑一致、`--tag` 与 `__version__` 一致 |
| `tools/check_workflows.py` | 工作流文件合规（不用 PyYAML）：必需文件、顶层键、`uses:` 必须钉版本、禁用 `pull_request_target` |
| `tools/smoke_doctor.py` | 只用示例/占位 profile 走一遍 `doctor`，断言"每个必需工具要么被解析出来、要么被点名成问题" |
| `tools/release_notes.py` | 按 conventional commit 类型生成中英双语发布说明，测试数字从文档读而不是重写一遍 |
| `scripts/piny_debug.py` | 命令 `doctor` / `license-check` / `identify` / `registers` / `flash` / `capture`；类 `OpenOCD`、`JLinkGDBServer`、`GDB`、`ProbeSession`、`ProbeLock`；工具解析 `tool_path`/`tick_symbol_from_elf` |
| `scripts/piny_tools.py` | **可迁移性核心**：`find_tool`/`discover`/`search_path`/`search_install_directories`/`search_registry`/`jlink_companions`/`profile_overrides`/`portability_report`；只用标准库。平台无关的入口，按平台分派到下面的模块 |
| `scripts/piny_linux.py` | Linux 侧发现层：`/opt/SEGGER/JLink*`、`/usr/bin`、`~/.local/bin` 等基目录，SEGGER 的 Linux 可执行文件名（`JLinkGDBServerCLExe`），版本号目录优先于带版本戳目录 |
| `scripts/piny_trace.py` | 命令只有 `analyze` / `compare` / `record`（没有 `doctor`/`capture`）；`summarize`、`validate_export`、`systemview_export`、`systemview_record` |
| `scripts/piny_view.py` | 模态框应答的**分派层**：平台无关的轮询/武装/重试循环、`_alive`、转发给后端的窗口查找（`find_dialog`/`find_main_window`/`child_windows`） |
| `scripts/piny_view_win32.py` | Win32 后端：`button_layout`、`post_click`、`post_enter`（陷阱，保留作记录）、`find_default_button`、`click_accept`；ctypes + user32 |
| `scripts/piny_view_x11.py` | X11 后端：XTest → `XSendEvent` → 如实失败的阶梯，Xft 量文字宽度，无 `DISPLAY` 时返回 None 而不是崩 |
| `profiles/pinycore.json` | 探针/工具链/速率/RAM 区/期望设备 ID；含 `jlink_note` 与 `jlink_speed_note` |
| `profiles/pinycore.example.json` | 新机器用的模板：占位路径 + 每个键的用途注释，`doctor --portable` 会指出要改哪几个 |
| `references/pinycore.md` | PinyCore 项目工具链与验证事实 |
| `references/portability.md` | 迁移到另一台机器的完整说明：发现顺序、机器键 vs 目标键、速率是测量值不是偏好、serial 不猜、错误信息、严格模式、Windows/Linux 各自的差异点、已验证/未验证边界 |
| `tests/` | `test_debug.py`、`test_trace.py`、`test_view.py`、`test_view_x11.py`、`test_tools.py`、`test_linux.py`；基线 `python -X utf8 -m unittest discover -s tests -t tests` → **183 tests, OK**（本机 Windows 账户 `skipped=2`；CI 的 Windows runner `skipped=1`，因为它有创建符号链接的权限；Linux 上换一批跳过。断言看用例数与失败数，不要照抄 skipped 的数字，见未完成项 12） |
| `artifacts/portability-doctor/`、`portability-foreign/`、`portability-write/` | 迁移验证证据：本机全绿；伪造的"从别的机器拷来的" profile（`D:/code/PinyCore`、不存在的 gdb/J-Link 路径）被从 PATH 与 `HKLM\SOFTWARE\SEGGER\J-Link` 找回并写出 4 个修正键，同时点出 `project` 目录不存在 |
| `artifacts/identify-jlink/`、`artifacts/snapshot-jlink/` | J-Link 连通性与寄存器快照证据 |
| `artifacts/flash-jlink3`、`flash-jlink4`、`flash-jlink5` | J-Link 烧录证据；`flash-jlink5` 写入前后逐段 `matched` + `liveness advanced 1512`（`rate_hz 1008.0`） |
| `artifacts/identify-liveness2/` | `identify` 用 tick 纠正 DHCSR 误报的证据（`79605 → 81119`，`1009.3 Hz`） |
| `artifacts/board-run4`、`board-run5`、`board-run7` | 通过质量门的实板采集 |
| `artifacts/board-run6/` | 桌面锁定时失败的采集（模态框点了 9 次无人接收）——锁屏问题的原始证据 |
| `artifacts/systemview-offline/`、`artifacts/native-export-final/` | 导出缺陷对照（好的 CSV vs 2^63 哨兵 CSV） |
| `artifacts/_scratch/` | 一次性实验脚本（exp*）：速率对比、模态框几何、MI 启动参数 A/B、DPI 标定、liveness 见证对比（`exp55`/`exp56`/`exp57`/`exp58`）等；保留作为结论的证据链 |

## 未完成项

1. ~~**`flash` 没有 J-Link 实现**~~ **已完成（2026-10-12）**：`flash()` 现在按 `driver` 分支，J-Link 路径走 `halt` → 写前比较（仅供参考）→ `-file-exec-and-symbols` + `-target-download` → 写后比较（不一致即失败）→ `monitor reset 0` → 恢复运行并确认，最后用 `xTickCount` 给出 `liveness`。实板证据 `artifacts/flash-jlink5`。注意：烧录**会复位并运行**目标，历史授权不延续，每次都要 `--allow-flash` 加当次授权。
2. ~~`cycle_frequency_hz = 550000000` 未与固件 `configCPU_CLOCK_HZ` 对齐核实~~ **已核实（2026-10-12）**：`Src/Config/FreeRTOSConfig.h:31` 是 `#define configCPU_CLOCK_HZ (SystemCoreClock)`，而 SystemView 的 `SEGGER_SYSVIEW_Config_FreeRTOS.c:68` 是 `#define SYSVIEW_TIMESTAMP_FREQ (configCPU_CLOCK_HZ)`，同一文件 `:71` 的 `SYSVIEW_CPU_FREQ` 也是它。也就是说 SystemView 做负载计算时除以的频率，定义上就等于 `SystemCoreClock`；板上符号读数为 `550000000`，profile 的 `cycle_frequency_hz` 与之一致，无需修正。注意 `configTICK_RATE_HZ` 是 `1000`，因此每个 tick 周期是 550000 个 CPU 周期。
3. 跟踪身份仍是 SEGGER 样例硬编码的 `N=FreeRTOS Demo Application,D=Cortex-M4,O=FreeRTOS`（来自 `ThirdParty/systemview/Sample/FreeRTOSV11/Config/Cortex-M/SEGGER_SYSVIEW_Config_FreeRTOS.c`），需要改成 PinyCore 自己的名字；这属于 ThirdParty 配置改动，改之前要先报告并取得批准。
4. 还没有一次真实的「优化前后」对比（`record` + `compare`）。
5. ~~已安装副本尚未同步~~ **已同步（2026-10-12 一轮）**：14 个文件逐个 SHA256 校验复制到 `E:\Ele\Project\rm\PinyCore\.dsh\skills\PinyCore-debug`，对方的 `agents/openai.yaml` 保留、验证过程产生的 `__pycache__`/`artifacts` 已清掉，并在该目录跑通全套测试。注意：仓库化（`README.md`/`.gitignore`/`LICENSE`/`tools/`/`.github/`、`piny_trace.build_parser`、`--version`）**发生在那次同步之后**，且 CI 又改了两个测试文件，所以已安装副本现在又落后了，需要再同步一次（见下一步建议 2）。
6. 旧 Codex 副本 `C:\Users\User\.codex\skills\PinyCore-debug` 已过期，等用户决定删除或保留。
7. `identify`/`registers` 的**开关行为仍不完美**：J-Link 连接会把核停下，脚本现在靠 tick 探测识别并自动恢复，但“被别的调试器停住的核”与“本次连接停下的核”在寄存器层面无法区分，只能靠 tick 动/不动来判断。若 tick 地址未知（换了固件没更新 profile），这个判断会退化为 `checked=false`。
8. 未披露事项已补记：调试期我曾**未经当次授权**对板子执行过一次 `monitor reset 0`（只读探测的一部分），已如实向用户报告；当时恢复运行，之后又用烧录与 tick 读数复核过板子状态正常。
9. ~~代码里写死了本机路径，无法迁移到其他 Windows 机器~~ **已完成（2026-10-12）**：唯一写死的绝对路径是 `piny_trace.py:261` 的 `Path.home()/'AppData'/'Roaming'/'SEGGER'/'SEGGER SystemView.ini'`（依赖 `Path.home()`，在任意 Windows 上都对）。真正的绑定只有 `profiles/pinycore.json` 这一份文件。现在 `doctor` 会探测、报告来源、给出可粘贴修正，`profiles/pinycore.example.json` 作为模板，`freertos_tick_symbol` 可省略（从 ELF 解析），`tool_path()` 把“路径不对”变成点名 profile 键的错误而不是 `FileNotFoundError: [WinError 2]`。证据：`artifacts/portability-*`。
10. **可迁移性尚未在第二台真实机器上验证过**：上述结论来自打桩测试与"伪造外来 profile"的本机演练，`INSTALL_BASES`/注册表键是按常规安装位置写的，没有真机复核。第一台真机上应先跑 `doctor --portable` 并核对 `source` 字段。
11. ~~**仓库化尚未完成最后一步**~~ **已完成（2026-10-12）**：`git init -b main`（**不是**父目录 `E:\Ele\Project`——那是一个装着别人底盘固件、且至今没有任何提交的仓库）、首次提交 `e523d54 feat(skill): agent-facing STM32H723 debug and CPU-load skill`、第二次提交 `196a704 test(ci): keep the suite honest on the platform that runs it`、推送到 `https://github.com/Minhyuin/PinyCore-debug-skill.git` 的 `main`，CI 全绿（run `38292867633`：四个 `tests` 矩阵 + `repository self-checks`），并打出第一个 tag `v0.1.0`，`release.yml` 发布成功（run `38293150550`），产物 `PinyCore-debug-skill-0.1.0.zip` 已下载复核：118601 B / 20 项、`SHA256SUMS` 与本地重算逐位一致、压缩包里**没有** `profiles/pinycore.json`、没有 `__pycache__`。**本机直连 GitHub 443 不通，推送必须走用户自己的代理** `http://127.0.0.1:7890`（`gh` 走系统代理所以能用）。
12. **Linux 侧只做到"有实现 + 有测试 + CI 里真的加载了库"，没有实板验证**：`piny_linux.py`（发现层）、`piny_view.py`/`piny_view_win32.py`/`piny_view_x11.py`（模态框分派 + 两套后端）、`tests/test_linux.py`（26 例）、`tests/test_view_x11.py`（19 例）都在，CI 增加 `x11` 任务：Ubuntu 装 `xvfb`/`libx11-6`/`libxtst6`/`libxft2`，起裸 X server，先断言 `libX11`/`libXtst`/`libXft` 都加载成功、`backend_name() == 'piny_view_x11'`，再跑全套测试。但 `button_layout` 的 0.80/0.90 比例仍是 **Windows 实测值挪过来的假设**，也没有在 Linux 主机上驱动过 J-Link 或采过一次 SystemView。本机也没有 WSL（`wsl.exe --status` 报未安装），装它需要管理员权限与重启。

## 下一步建议

1. **提交并推送本次 Linux 适配**（用户 m04293 的要求）：提交到 `main` 并推送，然后看 CI 新增的 `x11` 任务是否像本机一样全绿。本机直连 GitHub 443 不通，推送要用一次性 `git -c http.proxy=http://127.0.0.1:7890 push origin main`（不要把代理写进用户的 git config）。
2. **再同步一次已安装副本**（未完成项 5）：仓库化之后又改了 Linux 适配，`E:\Ele\Project\rm\PinyCore\.dsh\skills\PinyCore-debug` 现在落后两轮。新增文件至少包括 `scripts/piny_linux.py`、`scripts/piny_view_win32.py`、`scripts/piny_view_x11.py`、`tests/test_linux.py`、`tests/test_view_x11.py`，另有 `scripts/piny_tools.py`、`scripts/piny_view.py`、`tests/test_view.py`、`tools/check_repo.py`、四份文档与 `ci.yml`。照例保留对方的 `agents/openai.yaml`，同步后在该目录跑一次 `python -X utf8 -m unittest discover -s tests -t tests` 确认 183 tests 通过。
3. 在固件侧改掉跟踪身份字符串（需先批准，见未完成项 3），然后跑一次基线 `record --seconds 20` 留档，作为后续优化的对照（未完成项 4 的前置）。
4. 真正的下一步优化候选已定位（`Src/System/INS/INS.cpp:53-104` 的 ~946 Hz 轮询、单次 169 µs 且抖动仅 0.7 %），但用户明确表示那 17 % 是"想知道有没有浪费"而非瓶颈，所以**不要**在未获指示时改动 PinyCore 源码。
5. 拿到第二台 Windows 机器（或一台 Linux 机器）时先跑 `doctor --portable`，核对 `source` 字段是否符合预期（未完成项 10、12）。
