---
name: pinycore-debug
description: "为 PinyCore 的 STM32H723/J-Link 开发环境自动启动 GDB 读取寄存器与符号变量，并通过 SEGGER SystemView 采集实板 CPU 负载，导出可比较的性能报告。适用于 PinyCore 实板调试、故障寄存器读取、性能采样及优化前后比较；不是通用 STM32 烧录或 ESP32 开发技能。"
---

# PinyCore-debug

显示名与目录名保留 `PinyCore-debug`；技能机器标识为兼容校验规范的 `pinycore-debug`。

## 范围与状态

当前实板验证只发生在 **PinyCore / STM32H723 / Windows**；探针是接在板上的 SEGGER J-Link（设备 `STM32H723VG`，序列号 `602712375`）。宿主脚本本身**同时支持 Windows 与 Linux**：工具发现按平台分派（`piny_tools.py` → `piny_linux.py`），许可证模态框也有两套后端（`piny_view_win32.py` / `piny_view_x11.py`），但 Linux 侧还没有在真机上驱动过这块探针、也没采过一次跟踪。架构分为项目配置、GDB/J-Link 执行器、SystemView 采集分析器；通过 `--profile` 扩展配置，不宣称已支持未测试探针。当前不需要 MCP 服务。

先读 [项目与验证状态](references/pinycore.md)。2026-10-12 实板复测：

- **寄存器读取已打通**：`identify` 与 `registers --allow-halt` 都走 J-Link + GDB/MI，设备 ID `0x483`、CPUID `0x411fc272`、`state_at_entry`/`state_at_exit` 如实上报，ELF 与 Flash 逐段 `compare-sections` 全部 `matched`。
- **CPU 负载已有可用数据**：`record` 走 J-Link + SystemView 命令行控制，质量门通过时给出 `trace_busy_percent`（见下文“采集 SystemView”与 `artifacts/board-run4/`、`board-run5`、`board-run7`）。
- 仍要注意：J-Link 探针下 OpenOCD 路径不可用（SEGGER 内核驱动独占，OpenOCD 报 `LIBUSB_ERROR_NOT_FOUND`），所以寄存器读与烧录都走 GDB/J-Link，跟踪采集走 SystemView。
- **`flash` 在 J-Link 上已可用并实板验证**（`artifacts/flash-jlink5`，写前/写后逐段 `matched`，`final_state running`，`liveness advanced 1512`）。但 `identify`/`registers` 这类只读命令的一次连接也可能把核停下，所以命令结束后要用 `identify` 的 `liveness` 或直接读 `xTickCount` 确认板子确实在跑，不要凭 `DHCSR` 下结论。
- `cycle_frequency_hz` = `550000000`，已核实与固件 `configCPU_CLOCK_HZ (SystemCoreClock)` 一致（`Src/Config/FreeRTOSConfig.h:31`），而 SystemView 的 `SYSVIEW_TIMESTAMP_FREQ`/`SYSVIEW_CPU_FREQ` 定义上就是这个量，所以负载百分比的分母可信。
- 不要把安装成功、单元测试、SEGGER 样例分析或 ELF 构建成功说成实板性能验证通过。

## 操作边界

- 默认先 `doctor`，不连接 MCU。`doctor` 会探测本机工具位置、检查 `project`/ELF 是否存在，并给出可粘贴的 profile 修正；它**默认只读**，只有在显式传 `--write-profile <path>` 时才写文件。
- SystemView V3.60e 启动时先弹出 `No Commercial-Use License` 模态框，应答之前进程根本不解析命令行，`-load/-export/-quit` 会一直挂住（实测 150 s 无任何输出）。分析器因此按窗口标题找到该对话框、算出按钮位置后**投递鼠标消息**应答 Accept，并把对话框矩形、三个按钮位置与点击坐标写进日志；这是替用户应答其本机已选择接受的非商业授权，不是绕过许可。要求人工处理时传 `--no-auto-accept-license`，脚本只观察不点击。该对话框也是不能把 SystemView 当纯命令行工具的原因：必须监督进程，不能用 `subprocess.run` 假定它自己退出。
- 核心寄存器快照会短暂暂停 CPU。先说明对实时任务、PWM/CAN 控制的影响，并确认操作在本次任务范围内；传入 `--allow-halt` 才执行。
- 复位/烧录必须获得当前任务的明确授权，才执行 `flash --allow-flash`。不把历史授权扩展到新任务。
- 不改 HAL 或 ThirdParty/Lib。若发现需要改变跟踪插桩、缓存布局或第三方配置，先报告原因并请求单独批准。
- 不假设“只读”没有副作用。外设/FIFO/读清零寄存器只在知道读语义且得到授权时访问；不自动扫所有外设。系统/外设地址读取需 `--allow-peripheral-read`。
- 不终止用户已有 GDB/J-Link/IDE/SystemView 进程。脚本仅管理自己启动的进程；探针被占用时报告并停止。
- 连接/传输失败时记录完整日志及目标恢复状态，不循环复位或盲目提高 SWD 速率。失去通信时不能保证 CPU 已恢复，必须明确告知。

## 运行

使用已安装技能目录下的脚本；路径有空格时按 PowerShell 规则引用。Python 3.10+，主机脚本仅用标准库。

```powershell
$skill = 'E:\Ele\Project\rm\PinyCore\.dsh\skills\PinyCore-debug'
$tool = Join-Path $skill 'scripts\piny_debug.py'
$trace = Join-Path $skill 'scripts\piny_trace.py'
python $tool doctor
python $tool doctor --portable
```

本机工具的路径全部来自 profile；换一台机器时从 `profiles/pinycore.example.json` 起一份新配置，详见下面的「迁移到另一台机器（Windows / Linux）」。

源码工作区在 `E:\Ele\Project\rm\PinyCore-debug`（同一套脚本，另有 `SKILL.md`、`STATUS.md`、`tests/` 与 `artifacts/`）。改动后要同步到上面这个已安装目录才会生效。

所有主工具命令接受前置 `--profile <json>` 与 `--output <新目录>`；`--output` 是**全局**选项，必须写在子命令之前。默认输出在技能目录 `artifacts/`，也可显式指定工作区。每次使用独立目录，保留 `result.json`、命令、GDB/MI、OpenOCD/Tcl 日志。

通用探针选项（`identify` / `registers`；旧版 OpenOCD 用法的 `piny_debug.py capture` 不含）：`--probe openocd|jlink`、`--device`、`--speed`（kHz）。默认取 profile 的 `probe`/`jlink_device`/`jlink_speed_khz`。`record` 另有自己的 `--device`/`--speed`。

两个入口的分工：`piny_debug.py` 是**探针/目标**侧（`doctor` / `license-check` / `identify` / `registers` / `flash` / `capture`），`piny_trace.py` 是**跟踪**侧（只有 `analyze` / `compare` / `record`）。写命令前先 `-h` 确认，别把 `capture` 或 `doctor` 记到 `piny_trace.py` 上。

### 加一块新芯片：profile 里只写一个 `chip`

芯片相关的常量（CPUID、DBGMCU 器件号与地址、复位原因寄存器与位名、RAM 区间、主频）集中在 `scripts/piny_chips.py` 的 `CHIPS` 表里，**不写在脚本逻辑里**。当前有 `stm32h723vgtx` 与 `stm32f407ighx` 两项，每项的 `source` 字段写明这些数字是从哪个项目文件读出来的（H723 的几项还有实板读数佐证）。

```jsonc
// profiles/pinycore-f407.json —— 换型号时唯一必须写的目标键
{ "chip": "stm32f407ighx", "probe": "jlink", "jlink_serial": "…", "elf": "build/PinyCore.elf", … }
```

- 表**只填 profile 没写的键**（`setdefault`）：profile 里写了的永远以 profile 为准，`doctor --portable` 会逐键标出这一项来自 `chip table` 还是 `profile`。
- **未知型号不是错误**：指名一个表里没有的 `chip`，只是拿不到表的取值，脚本会说明"不在本包认识范围内"，并且**不会**继承任何别的型号的期望器件号；此时 `identify` 对身份**不给结论**，`flash` 直接拒绝（"set `chip` to one of …"），而不是默认放行。
- `identify` 还会读 `SystemCoreClock` 并与 profile 的 `cycle_frequency_hz` 对照：分频写错只会让所有 CPU 负载按同一个比例缩放，不会报错，所以这里要主动比一次。
- **同一颗 SoC 的两个工程会写同一个 `build/PinyCore.elf`**（本项目 H723 与 F407 的构建产物同名同路径）：因此挡住烧错片子的不是 ELF 名字，而是 `flash` 前的身份闸门（F407 `DEV_ID 0x413` vs H723 `0x483`）。

### 只读连通性检查

```powershell
python $tool identify
python $tool identify --reset-cause --allow-peripheral-read
python $tool identify --resume-if-halted
python $tool license-check
```

- `identify` 不暂停、不复位、不读 Flash：读 DBGMCU 设备标识、CPUID、DHCSR/DEMCR，并报告进入与退出时 CPU 状态；探针被别的会话占用时直接报错退出，不会抢占。
- **`DHCSR` 不能用来判断“固件是否在跑”（本板实测）。** 实测中 `DHCSR` 对一颗确实在以 ~1009 Hz 跑 RTOS tick 的核返回冻结的 `0x30003`（`C_HALT` 置位），且跨会话读数完全相同，即“报告已暂停”的核其实在运行。因此 `identify` 在 `state=halted` 时会用**固件自己的 `xTickCount`**（profile 的 `freertos_tick_symbol`，缺省时从 ELF 解析）观察 1.5 s 来裁决：
  - tick 在动 → 该核本来在运行（是本次连接把它停下的），已自动恢复运行；`state_at_entry/state_at_exit` 报 `running`，并在 `dhcsr_note`/`notes` 里写明 DHCSR 不可信。
  - tick 不动 → 该核确实是停止的，保持原状不写任何寄存器，`notes` 说明 `--resume-if-halted` 可启动它。
- J-Link 会话**无法**报告“连接前的状态”（OpenOCD 的 `state_at_connect` 在 `init` 内采样，J-Link 没有对应物），所以不能用寄存器区分“本来就是停止的”和“被本次连接停下的”——上面这个 tick 探测就是用来区分二者的。
- `freertos_tick_symbol` 是**该镜像的地址**（每次重建都可能变）。**它可以不写**：缺省时由 `arm-none-eabi-nm` 从 profile 的 ELF 解析 `xTickCount`，所以重新构建后不必手工改 profile（写了则以 profile 为准）。解析不到（没有 ELF、gdb 旁没有 `arm-none-eabi-nm`、或镜像里没有该符号）时 `liveness.checked=false`，脚本明确说“未验证”，不会假装 running。
- **读 tick 前必须先让核跑起来**：J-Link GDB server 的**首次内存读**返回 attach 时的调试缓存值。实测同一会话两次读间隔 4 s（中间无运行控制）得到同一个 `213916`，而板子确实在跑；`monitor go` 之后同一地址连续三次读是 `217714/217715/217716`（无任何延时），说明问题在缓存而不在时间。所以 `probe_liveness`/`tick_advances` 的顺序是 `resume()` → 读 → 睡 → 读，不能改成「先读再 resume」——那样比较的是同一个陈旧值的两份拷贝。一般教训：**别用「隔一段时间的两次读」证明东西在动**，除非确认第一次不是缓存样本。
- `--reset-cause` 额外读该型号的复位原因寄存器（H723 是 `RCC_RSR` `0x58024cd0`，F407 是 `RCC_CSR` `0x40023874`，位名完全不同，所以由型号表给），属于外设，故必须同时给 `--allow-peripheral-read`。注意该寄存器**读清零**：这一次读取会消耗掉复位历史，之后无法追溯；它说明的是“上次复位原因”，不是“此刻是否被按住”。profile 既没写 `chip` 也没写 `reset_cause_address` 时脚本**不猜**——读错家族的寄存器是在破坏状态。
- 进入时 CPU 已暂停时，脚本保持暂停并在 `notes` 里说明；要让它跑起来用 `--resume-if-halted`（也可用 `--leave-halted` 显式表达保持暂停）。
- 目标状态在本命令内发生变化时，`notes` 会出现 `WARNING: target state changed during this command (halted -> running)`，以 `state_at_entry`/`state_at_exit` 为准。
- `license-check` 只在 SEGGER 自带样例上验证 SystemView 授权对话框能被自动应答，不接触目标板。

### 读取核心寄存器

```powershell
python $tool registers --allow-halt
python $tool registers --allow-halt --symbol SystemCoreClock
python $tool registers --allow-halt --memory 0x20000000 32
```

- 无 `--symbol` 时不要求 Flash 与 ELF 一致；只报告原始寄存器/指定地址值，不据此做符号化堆栈归因。
- 符号变量读取会先暂停并验证 ELF 与 Flash 一致；不一致就停止，不自动烧录“修好”它。
- GDB/MI 没有分区比较命令（`-data-compare-sections` 报 `Undefined MI command`），因此校验走 `-interpreter-exec console "compare-sections"`；出现 `MISMATCH` 或没有任何 `matched` 行都判失败。控制台文本以 MI STREAM 记录（`~"..."`）返回。
- 用户表达式只允许变量、成员、常量数组下标，不能执行目标函数或赋值。
- 自动保存 SCB 故障状态原始字节；解释时按 Cortex-M7 位定义分析，不把非零残留状态直接判作当前故障。
- 结束时恢复进入会话前的 running/halted 状态；无法恢复时以失败返回并保留原始错误链。
- **目标已运行时读取会明确失败**，不会挂住：GDB 以 `set mi-async on` 启动，运行态下任何寄存器/内存读取都立刻返回 `Cannot execute this command while the target is running.`。这个启动参数不能事后设置（运行中改会报 `Cannot change this setting while the inferior is running`），也不能省——省掉时 `-exec-interrupt` 15 s 超时、后续命令全部挂死。

### 构建与烧录

按项目现有 Kconfig/CMake 流程操作，详见参考文件。确认 ELF 来自本次需要的配置，再运行：

```powershell
python $tool flash --allow-flash
```

该操作先校验设备族标识，再擦写 ELF 涉及的 Flash 区域，随后用 `compare-sections` 验证，最后复位并确认运行。J-Link 路径可用（GDB 的 `-target-download` 走 SEGGER GDB server），`--probe jlink` 已由 profile 指定。

- J-Link 路径的关键顺序（实测）：`halt` → 记录写入前的分区比较（仅供参考，不一致**不**阻止烧录，因为不一致正是要烧录的理由）→ `-file-exec-and-symbols` → `-target-download` → 写入后比较，不一致即失败 → `monitor reset 0` → 恢复运行并确认。
- **`monitor reset 0` 之后核是暂停的**（实测），所以烧录流程必须以“恢复运行 + 验证”结尾，否则会把一块刚写好的板子留在静止状态。`--allow-flash` 之前请确认目标当前状态，因为 `identify`/`registers` 的一次连接本身也可能让核停下。
- 结果里的 `liveness` 是**固件自己**给出的运行证据：`tick_before` → `tick_after`、`advanced`、`rate_hz`。实测一次成功的烧录：`tick_before 0` → `tick_after 1512`（`rate_hz 1008.0`），即复位后固件确实重新开始跑；仅凭 `final_state: running` 不足以证明这一点（`final_state` 来自 DHCSR，而 DHCSR 在 J-Link 上不可信）。
- 返回 `running` 仅代表 CPU 状态，不证明 INS 初始化或所有任务健康；随后读取状态并采集任务事件确认。

### 采集 SystemView

实板跟踪走 J-Link + SystemView 自己的命令行控制（不要用 `capture`，那条路依赖 OpenOCD 的 RTT server，J-Link 探针下不可用）：

```powershell
python $trace record --output artifacts\board-run8 --seconds 10
python $trace record --output artifacts\board-run8b --seconds 10 --speed 12000
python $trace record --output artifacts\board-run8c --seconds 10 --allow-overflow
```

`record` 每次都**显式告诉 SystemView 控制块在哪**（`-rttcbaddr 0x…`），因为 SystemView 不会自己去搜：它把上次录制那块板的地址记在 `%APPDATA%\SEGGER\SEGGER SystemView.ini` 的 `[Recorder]`（`J-Link\RDG_RTT_MODE=Address`、`J-Link\TXT_RTT_CB_LOCATION=0x20004FC0`），命令行不给 `-rttcbaddr`/`-rttcbrange` 时它**直接读那个记下来的地址**——换到 F407 时它照着 H723 的地址读一段没有 `SEGGER RTT` 的内存，永远不打开 `SysView` 通道、也永远不给目标发 start（`_SYSVIEW_Globals.EnableState` 停在 0），`-save` 写不出文件、退出码 1，而它自己的错误框在 V3.60e 里是坏的（`No such method MainWindow::OnError`），所以失败完全静默。地址解析顺序：profile 的 `rtt_control_block` → ELF 里的 `_SEGGER_RTT`（符号名可用 `rtt_control_block_symbol` 改）→ 都没有才退回 `-rttcbrange auto` 让 SystemView 搜；实际用了哪个写在 `recording_meta.rtt_control_block` 与 `rtt_control_block_source` 里，命令行原样留底在 `<out>/systemview-record.log`。录制动作由 SystemView 通过目标的下行通道发 Start/Stop，并随之更新 RTT 读指针：这**会写目标**，汇报时必须说明。目标上若已有 SystemView 会话在录制，则拒绝接管。

流程与实测：应答 `No Commercial-Use License` 模态框 → 等控制端口 `localhost:19050` 应答（约 8 s）→ `-start` → 等 `--seconds` → `-stop`（排空实测 ~16 s）→ `-save`（写盘约 38 s）→ SystemView 原生导出 CSV → 质量门。整轮实测 63 s（`artifacts/board-run7/systemview-record.log`）。

- **探针速率是这份数据的成败关键。** SystemView 的 `CPU Load` 列 = `Total Run Time / 记录窗口`，所以**同一核上所有上下文的 CPU Load 之和必须约等于 100 %**；这个和是判断采集能否采信的最强证据。同一段 6 s 窗口实测：

  | J-Link 速率 | 溢出事件 | 上下文负载合计 | Idle | busy |
  |---|---|---|---|---|
  | 4000 kHz | 2847 | **65.4 %（不可信）** | 48.5 % | 51.5 %（错） |
  | 12000 kHz | 1 | 100.07 % | 76.24 % | 23.76 % |
  | 24000 kHz | 0 | 99.99 % | 76.09 % | 23.91 % |

  速率不够时 1 KB 的 SysView RTT 缓冲被填满、事件被丢掉：Idle 被低估、busy 被高估。**不要为了凑一次“成功”调低速率**，要看负载合计。profile 的 `jlink_speed_khz` 已定为 24000，`--speed` 只用于排查。
- 必须看 `quality.context_load_sum` 与 `overflow_events`；`quality.usable=false` 时不得把数字当性能基线。`--allow-overflow` 只是让脚本明确记录“这些是低估”，不是让门通过。
- 记录窗口一定比请求的长：`-start` 时目标 RTT 缓冲里还压着数据，而且 `-start` 的应答要等采集器把 RTT 通道接上才回来（实测 `-start` 落在 8.32 s、`-stop` 落在 21.34 s，其间 `time.sleep(5)` 却过了 13.02 s）。实测请求 6 s 得到 ~11.8 s、请求 10 s 得到 15.3–16.1 s、F407 请求 5 s 得到 10.68 s，即大约 5.7–6.1 s 的 attach 延迟。质量门接受 `请求时长 ≤ 记录时长 ≤ 请求时长 + ATTACH_ALLOWANCE_SECONDS(7.0)`（比早先的 `2 × 请求时长` 对长窗口更严：21 s 窗口从允许 42 s 收到 28 s）。**一次把完整记录判成不合格的闸门本身就是缺陷**——第一次成功的 F407 采集就是被旧的 `2 ×` 规则挡下来的（负载求和 100.0 %、零丢弃、有 `Start` 事件）。
- `-save` 必须给**绝对路径**且不能加引号。SystemView 不按调用者的工作目录解析：实测 `-save artifacts\board-run2\live.svdat` 一个字节都没写，2 s 后弹出 `SystemView Error`；同一次运行改用绝对路径写出 2296373 字节。
- `LoadDataOnStart` 每次运行前改成 `false`、结束还原（备份 `SEGGER SystemView.ini.pinycore-record-backup`）。开着它时 SystemView 启动即载入最近一次记录，采集失败时 `-save` 会把那份旧文件当成本次结果写出来——实测写出与 SEGGER 样例字节数完全相同的文件，**看起来完全像成功**。
- 每一步（时间戳、命令、应答）、退出码、`ini_restored`、被应答的对话框都写进 `<out>/systemview-record.log`；SystemView 自身的输出写到 `systemview-record-console.log`（它在应答控制端口前就退出时，原因只出现在这里）。系统信息/错误类弹窗由监督线程按形状找按钮点掉（Qt 自绘文字读不到，只能截图确认），次数记在 `message_boxes`。`System Information`“43466 Events Loaded.”这类弹窗是提示不是错误。

`capture` 是旧的 OpenOCD RTT 路径，保留给 OpenOCD 可用的探针：

```powershell
python $trace capture --seconds 5 --allow-halt --allow-rtt-write
```

流程：暂停校验 ELF 和 RTT 描述符 → 按 `SysView` 名称识别上下行通道 → 恢复运行 → OpenOCD RTT TCP 采集 → Stop 并排空尾部 → SystemView 原生导入/导出 → JSON 摘要。导出校验、监督与越界值判定规则与 `record` 相同。

- `trace.bin` 是原始证据；`contexts.csv` 与 `events.csv` 由 SEGGER 原生程序导出，而不是截图/OCR 或自行猜测二进制协议。
- `summary.json` 给出各任务/上下文占比、记录时长、频率与质量标记。
- 导出文件在采信之前先被重新读入并校验：缺 contexts、缺 Idle、CPU Load 超界、时间戳逆序、导出行数明显少于事件数、记录过短，都判定为“导出不完整”。SystemView 的退出码不可信（同一失败既出现过 0 也出现过 1），因此判定只依据 CSV 内容。
- 导出过程受监督：CSV 不再增长而进程仍存活 25 s，或始终没有写出任何 CSV 达 20 s，就杀掉这次启动并重试，最多 3 次。实测约每 3 次启动有 1 次写出半截 `contexts.csv` 后卡住，重试即得完整导出。
- 部分运行会写出真实的**越界值**而不是截断：最后一帧仍处于打开状态的上下文，其累计运行时间会变成 INT64 哨兵 `9223372036854775808 ns`（= `9223372036.854775808 s`），于是 `CPU Load` 变成 `10894144524.79 %`。已用 SEGGER 自带样例复现同一现象，所以这是 V3.60e 导出侧缺陷，不是记录的负载；同一份记录用 GUI 打开显示 0.49 %。绝不 clamp、也绝不删除这类行来让分析通过——它们意味着这份导出不可用，必须重导。
- 缺 Idle、溢出、时间戳逆序、时长或频率异常时，结果不能作为有效性能基线。不得忽略 `quality.usable=false`。
- `trace_busy_percent = 100 - SystemView Idle CPU Load`，是整个记录窗口的跟踪归因值，不是 GUI 当前缩放窗口值。应用 ISR 插桩不完整时，CPU 忙碌率和任务占比都可能偏差；明确报告覆盖范围。

### 分析已有文件与比较

```powershell
python $trace analyze 'E:\path\recording.SVDat' --output 'E:\path\new-analysis'
python $trace analyze 'E:\path\recording.SVDat' --output 'E:\path\new-analysis' --recover-existing
python $trace compare 'E:\path\before\summary.json' 'E:\path\after\summary.json'
```

`analyze` 默认拒绝在有旧 CSV 的目录里工作：目录里已有**完整**导出时要求显式 `--recover-existing` 才复用，已有**不完整**导出时报错并要求换新目录，避免拿半截证据继续算。`--export-timeout` 只影响整轮重试的总预算，不是单次启动的等待时间。

比较输出百分点变化；只有有效记录允许比较。另核对输入负载、预热、运行时长、构建优化级别、CPU 时钟、探针速率和插桩一致性。单次差异不等于已证明优化有效。

## 迁移到另一台机器（Windows / Linux）

整套脚本里**没有一处写死本机路径**：所有可执行文件与项目路径都从 `--profile` 指向的 JSON 读取。换机器的全部工作就是让那份 JSON 在那边成立。

```powershell
# 1) 看这台机器缺什么、能补什么（不连板、不写任何文件）
python $tool doctor --portable
# 2) 从模板起一份新 profile
Copy-Item profiles\pinycore.example.json profiles\my-rig.json
# 3) 确认无误后，让 doctor 把探测到的路径写进去（必须显式点名才写）
python $tool doctor --profile profiles\my-rig.json --write-profile profiles\my-rig.json
```

- `doctor` 按 **profile 值 → PATH → 常见安装目录 → SEGGER/注册表（仅 Windows）** 的顺序查每个工具，并**报出是哪一步命中的**（`source`）。profile 里已经指向真实文件的路径永远优先，探测**不会**悄悄改掉一个能用的配置。
- 平台差异集中在两个地方，命令与报告结构在两平台上一致：工具发现（Linux 走 `piny_linux.py`：`/opt/SEGGER/JLink*`、`/usr/bin`、`~/.local/bin`，并认识 SEGGER 在 Linux 上的名字 `JLinkGDBServerCLExe`）与许可证模态框后端（`piny_view_win32.py` / `piny_view_x11.py`）。
- 查不到时不再抛 `FileNotFoundError: [WinError 2] 系统找不到指定的文件`（那种报错既不说哪个键、也不说哪个工具），而是：`Profile key "gdb" points at ... which is not a file on this machine. Set it to the right path in <profile>. Found one here instead: ... -- run "doctor" to see the full list.`
- `doctor` 同时检查 `project` 目录与其中的 ELF，并把每个问题写成一句可执行的话（`problems[]`）。`usable` 只在必需工具与项目目录都在时为 true。
- `--portable` 把 profile 的键分成三类，**这个区分是迁移时最容易出错的地方**：
  - `machine`（`project`/`gdb`/`openocd`/`systemview`/`jlink_gdb_server`）：属于这台电脑，可以从旧机器抄错、也可以被探测重新定位。
  - `target`（`probe`/`jlink_device`/`jlink_serial`/`jlink_speed_khz`/`expected_device_id`/`idcode_address`/`reset_cause_address`/`reset_cause_flags`/`freertos_tick_symbol`/`cycle_frequency_hz`/`ram_ranges`）：属于板子和探针。**抄到另一套硬件上正是"看起来正常的 profile 给出错误数字"的来源**，必须在新机器上重新确认而不是沿用。
  - `chip`（型号键：`core`/`expected_cpuid`/`expected_device_id`/`idcode_address`/`reset_cause_address`/`reset_cause_flags`/`ram_ranges`/`cycle_frequency_hz` 这些**只取决于型号**的值，由 `chip` 这一个键名指出来）：**同一型号的所有板子都一样**，所以住在 `scripts/piny_chips.py` 的可审查表里，不必每块板抄一遍；`doctor --portable` 会给每个键标出来源是 `chip table` 还是 `profile`。
- `jlink_serial` **缺失时只报告、绝不猜**：多探针同时在线时，替你挑一个等于替你改变目标。
- `freertos_tick_symbol` 现在**可以不写**：缺省时用 `arm-none-eabi-nm` 从 profile 指向的 ELF 里解析 `xTickCount`，因此重新构建后地址变化不再需要手工维护。写了则以 profile 为准（用于故意监视另一个镜像）。解析不到时 liveness 报 `checked:false`，即"未验证"，不会假装 running。
- 严格模式：`load_profile(..., discover_tools=False)`（或直接不给 `_tools`）只用 profile 原值，便于复现问题或在受控机器上定位。
- `doctor` 是**唯一会写文件**的命令，写的前提是你点名 `--write-profile`；但它和其它命令一样会在自己的工作目录下建一个本次运行的 `artifacts/<时间戳>/` 目录（即使什么都没做）。如果你把技能装在被版本管理的目录里，记得忽略该目录。
- **Linux 上尚未实测的部分**：`doctor` 的 Linux 发现逻辑与 X11 后端有单元测试，CI 还会在 Xvfb 里断言 `libX11`/`libXtst`/`libXft` 真的加载、选中的确实是 X11 后端；但 X11 的按钮位置比例（0.80/0.90）是从 Windows 实测值挪过来的假设，且没有在 Linux 主机上驱动过探针或采过跟踪。第一次在 Linux 上跑，先 `doctor --portable` 核对 `source`，再 `identify`。

## 授权对话框的实测行为（不要退回旧做法）

`scripts/piny_view.py` 是分派层，负责应答模态框；平台后端是 `piny_view_win32.py`（ctypes + user32）与 `piny_view_x11.py`（XTest，其次 `XSendEvent`）。下面这些规则来自实测，不是猜的：

- **必须投递鼠标消息，不能合成鼠标移动。** 控制台可能处于锁定状态：全屏截图会拿到锁屏壁纸，对话框上每一点的顶层窗口都是 explorer 的 `LockScreenBackstopFrame`，`SetCursorPos`+`mouse_event` 只有效于无人观看的桌面。`PostMessageW` 直接投给指定窗口，不需要输入桌面，而 Qt 会照常做命中测试：点对话框正文或标题栏什么都不发生，点按钮行才会动作。
- **三个按钮从左到右是 License Manager、Accept、Decline。** 按客户区算出来是 `Decline` 贴右边、`Accept` 在中间、`License Manager` 在左边（9 pt Segoe UI 文字宽度 + 14 px 内边距、间隔 10 px、距右/下 12 px、高 26 px）。两轮实测：客户区 900×441 时 `(859,418)` 会 Decline 并 rc=1、`(793,418)` 无反应、`(708,418)` 打开主窗口；客户区 720×352 时 `(613,327)` 被接受、控制服务器 2 s 后应答。**按右下角分数位置点击会点到 Decline**，这正是早期每次启动都失败的原因。
- **投递回车（Return）是陷阱**：对话框会消失，看起来像成功，但 SystemView 随即 rc=1 退出——回车走的是 Decline 分支。`post_enter()` 只作为记录在案的陷阱保留，不要用它应答模态框。
- **“对话框消失”不能单独作为成功证据**：`accept_license` 只有在进程仍存活（`GetExitCodeProcess == STILL_ACTIVE`）时才把消失当接受，否则报 `the license dialog vanished but SystemView exited; the modal was DECLINED, not accepted (posting Return does this)`。
- UI Automation 在本机不是出路：该对话框是 `QWidget`，没有子窗口，也没有任何可访问子元素（`FindFirst(Descendants, Name='Accept')` 找不到），`comtypes` 未安装。
- 上面三条（投递鼠标消息、三按钮几何、回车是 Decline）都是 **Windows/user32 上测得的行为**。X11 后端没有 `PostMessageW`，所以走另一条阶梯：`XTestFakeMotionEvent`+`XTestFakeButtonEvent`（等价于 `xdotool` 的做法，不需要窗口管理器配合）→ 失败则 `XSendEvent` 直接投给对话框窗口（Qt 通常忽略这类非可信事件）→ 都不可用就**如实报失败**。X11 侧的 `button_layout` 复用了 Windows 测出的 0.80/0.90 比例，属于**假设**，所以 `click_accept` 还有"点按钮带内子窗口中心"的兜底，报告里用 `where: fraction|child` 说明是哪一步成功的。
- 无 `DISPLAY` 的会话（CI runner、容器、纯 ssh）不会崩：`available()` 返回 false，`accept_license` 报 `no dialog backend for this session`，让调用者明确知道要人工应答，而不是假装成功。

## 仓库、版本与本地自检

本包有独立仓库 <https://github.com/Minhyuin/PinyCore-debug-skill>（`README.md` 是对外说明，本文件是 agent 入口）。改完东西**在提交前**跑一遍：

```powershell
python -X utf8 tools\check_repo.py                # 文档与代码是否还对得上
python -X utf8 tools\check_workflows.py           # .github/ 里的工作流是否合规
python -X utf8 tools\smoke_doctor.py              # 只用示例 profile 能否走完一次诊断
```

- **`profiles/pinycore.json` 不入库**（`.gitignore` 里点名忽略）：它写着本机绝对路径、这块探针的序列号和本镜像的符号地址，属于这台机器。入库的只有 `pinycore.example.json` 模板；发布流水线还会在打包时再删一次，双保险。
- `tools\check_repo.py` 是防文档腐烂的闸门，它检查的不是"文件在不在"这一件事：`PACKAGE_FILES` 里声明的文件都被 `STATUS.md`/`README.md` 点到名，且 `scripts`/`tests`/`tools` 下**每个 `*.py` 都必须被声明过**（新模块漏声明就直接失败——文件地图就是这样悄悄过期的）；文档里写的子命令确实属于它提到的那个脚本（`doctor`/`capture` 只在 `piny_debug.py`，`piny_trace.py` 只有 `analyze`/`compare`/`record`）；示例 profile 合法且不含真实路径；`profiles/pinycore.json` 没被跟踪；**四份文档里的测试基线数字与 `unittest discover` 实际跑出来的一致**（所以改完测试要同步 `SKILL.md`、`STATUS.md`、`README.md`、`references/pinycore.md`）；`--tag` 与 `scripts/piny_debug.py` 的 `__version__` 一致。
- 版本只有一处真源：`scripts/piny_debug.py` 的 `__version__`（`piny_trace.py` 从它导入）。发版走 tag：`.github/workflows/release.yml` 在 `v*.*.*` tag 上先校验 tag 与 `__version__` 一致、单测通过，再打包源码 zip + `SHA256SUMS` 发 Release；**tag 与 `__version__` 必须在同一个提交里改**。
- CI 有三个 job：`tests`（Ubuntu + Windows × Python 3.10/3.13）、`x11`（Ubuntu + Xvfb，断言 `libX11`/`libXtst`/`libXft` 加载成功且后端确实是 `piny_view_x11`）、`repo-checks`（上面三个自检脚本）——**都不需要探针、板子和 SEGGER 安装**。这也是一条设计约束：任何新检查都不要引入这些依赖，也不要引入 pip 依赖（本包只用标准库）。
- 平台条件是**按能力判断，不是按 `sys.platform`**：`test_view.py` 的模态框与按钮识别用例在 `piny_view.available()` 为真时运行（Windows 有 user32、Linux 有 X display 都算），只有两者都没有时才跳过——按钮识别规则住在两个后端里，没有后端时 `find_default_button` 返回的 None 是"没得问"，不是"没找到按钮"，这两种情况必须分开；Windows 专属的用例（内置安装目录词表的 `%VAR%` 展开、注册表、只描述内置遍历的探测用例）用 `pt.inventory() is None` 守卫，也就是"本机确实走的是内置遍历"，而不是把平台名写死。这样两个平台跑到的用例数一致（213），跳过的个数不同，而且这个数字是**本机事实**：普通 Windows 账户没有创建符号链接的权限所以跳过该用例（`skipped=2`），CI 的 Windows runner 有该权限所以运行它（`skipped=1`），Linux 上则换一批跳过（没有 Windows 安装目录词表与注册表，且按有没有 X display 决定模态框与按钮识别用例）。跳过的是"这台机器上根本没有的东西"，而不是藏起跑不过的断言。

## 验收和汇报

1. 跑 `tests/` 中的单元测试（在工作区源码包内），再做本次涉及的实际路径检查。当前基线：`python -X utf8 -m unittest discover -s tests -t tests` → 213 tests, OK（本机 Windows 账户 `skipped=2`，CI 的 Windows runner `skipped=1`，Linux 上跳过的是另一批；**断言看用例数与失败数，不要照抄 skipped 的数字**）。`tests/test_tools.py` 与 `tests/test_linux.py` 覆盖可迁移性：路径优先级、PATH/安装目录/注册表探测、按文件身份去重、缺失工具的报错文案、`--write-profile` 的合并与"不点名就不写"、machine/target/chip 分类，以及 Linux 侧的安装目录/可执行文件名/版本号目录优先级/平台分派；`tests/test_chips.py` 覆盖型号表本身（条目与实板读数/固件源码对齐、CPUID 与复位原因解码、两个家族的复位寄存器确实不同）；`tests/test_view_x11.py` 覆盖 X11 后端的几何、按钮识别、降级阶梯与 Xlib 错误处理器（libX11 对未处理协议错误是 `exit()`，不是异常）。这些用例全部在临时目录或打桩下运行，**不依赖本机装了什么**。
2. 存在文件、非空数据、原生导出成功、目标实际状态分别核验。MCP 如未来加入，还需握手/工具枚举；不以注册成功代替执行验证。
3. 汇报：实际读取/采集结果、配置/固件标识、数据来源、目标恢复状态、失败/覆盖限制、日志路径。不要把官方样例负载标为 PinyCore 实测。
4. 扩展从 `profiles/*.json` 和执行器接口入手，避免为第二个项目复制一套逻辑。新增探针或 RTOS 必须有独立验证，之后再考虑薄 MCP 封装。
