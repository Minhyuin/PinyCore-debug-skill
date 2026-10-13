# PinyCore-debug-skill

[![CI](https://github.com/Minhyuin/PinyCore-debug-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/Minhyuin/PinyCore-debug-skill/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Minhyuin/PinyCore-debug-skill?include_prereleases&sort=semver)](https://github.com/Minhyuin/PinyCore-debug-skill/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform: Windows and Linux](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-0078d4)
![Python: 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab)

一个给编码 agent 用的调试技能包：在 **[PinyCore](https://github.com/SCNU-PIONEER/PinyCore)**（STM32H723 + FreeRTOS）的实板上自动启动 GDB 读取寄存器与符号变量，并通过 SEGGER SystemView 采集 CPU 负载，导出可比较的性能报告。

目标是让 agent **自己**完成「读寄存器 → 采集负载 → 改代码 → 烧录 → 再采集 → 对比」这个闭环，而不是每一步都要人手动开 IDE。

**English**: an agent-facing debug skill for STM32H723 / FreeRTOS firmware: drive GDB over J-Link to read registers and symbol variables, capture real-board CPU load through SEGGER SystemView, and compare before/after performance. See [Scope and limits](#scope-and-limits).

---

## 它解决什么

agent 做嵌入式优化时最难的不是改代码，而是**拿到可信的反馈**。这个包把反馈路径做成可审计的命令：

| 命令 | 做什么 | 是否需要停下 CPU |
|---|---|---|
| `doctor` | 探测本机工具链、检查项目与 ELF、给出可粘贴的 profile 修正 | 否（不连板） |
| `identify` | 读设备 ID / CPUID / DHCSR / DEMCR，判定并恢复运行状态 | 连接会停下，脚本自动裁决并恢复 |
| `registers` | 读核心寄存器、符号变量、指定内存地址 | 是，需 `--allow-halt` |
| `flash` | 写 ELF、写前写后逐段 `compare-sections`、复位后确认真的在跑 | 是，且需当次 `--allow-flash` 授权 |
| `record` | 通过 SystemView 采集 CPU 负载与各任务占用，过质量门才给数 | 否（后台采集） |
| `compare` | 两次采集的逐任务差异对比 | 否 |

## 快速开始

需要 **Windows 或 Linux** + Python 3.10+（主机脚本只用标准库）、[Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads)、[SEGGER J-Link](https://www.segger.com/downloads/jlink/) 与 [SystemView](https://www.segger.com/downloads/systemview/)。

```powershell
git clone https://github.com/Minhyuin/PinyCore-debug-skill.git
cd PinyCore-debug-skill

# 1) 从模板起一份本机配置（真实 profile 不入库，见下方说明）
Copy-Item profiles\pinycore.example.json profiles\pinycore.json
#    编辑 project / elf / jlink_device / jlink_serial

# 2) 先看这台机器有什么。只读，不连板，不写文件（除非点名 --write-profile）
python -X utf8 scripts\piny_debug.py doctor --portable

# 3) 让 doctor 把探测到的工具路径写进 profile
python -X utf8 scripts\piny_debug.py doctor --write-profile profiles\pinycore.json

# 4) 只读连通性检查，然后读寄存器
python -X utf8 scripts\piny_debug.py identify
python -X utf8 scripts\piny_debug.py --output artifacts\regs registers --allow-halt --symbol SystemCoreClock
```

Linux 上把反斜杠换成斜杠、`Copy-Item` 换成 `cp` 即可；`doctor --portable` 会去 `/opt/SEGGER/JLink*`、`/usr/bin`、`~/.local/bin` 等位置找工具，并识别 SEGGER 在 Linux 上的可执行文件名（`JLinkGDBServerCLExe`）。许可证模态框在 Linux 上走 X11 后端（XTest，其次 `XSendEvent`），需要 `libX11`/`libXtst`/`libXft`；无 `DISPLAY` 时会明确报告"须人工应答"而不会假装成功。

采集 SystemView：

```powershell
python -X utf8 scripts\piny_trace.py --output artifacts\baseline record --seconds 20
python -X utf8 scripts\piny_trace.py compare --before artifacts\baseline --after artifacts\after
```

作为 agent 技能安装时，把整个目录放到技能目录下（例如 `<PinyCore>/.dsh/skills/PinyCore-debug/`），入口是 `SKILL.md`。

## 换一块板子：只写一个 `chip`

同一台机器上接第二块不同型号的板子（例如 STM32H723 之外再加一块 STM32F407），**不需要改代码**：profile 里写一个 `chip` 键，其余型号相关的事实由 `scripts/piny_chips.py` 提供。

```jsonc
{ "chip": "stm32f407ighx" }   // 型号表补齐 CPUID、DBGMCU 器件号、复位原因寄存器、内存区间、时钟
```

- 表里现在有 `stm32h723vgtx` 与 `stm32f407ighx` 两条，每条都记录了数值是从哪个项目文件读出来的（`source`）；型号表是"同一型号所有板子都一样"的事实，所以它可以随包分发、也可以被测试固定。
- **profile 里写了的永远优先**，`doctor` 会逐键标出这个值来自 `chip table` 还是 `profile`。
- 表里没有的型号**不是错误**：只按 profile 写明的值工作，并且不再假装知道它的 CPUID（以前会把 H723 的期望值套到任何板子上，于是第二块板子被第一块板子的检查拒绝）。
- 认不出的型号 + 没写 `expected_device_id` 时，`identify` 只报告读到的器件号、不给"匹配/不匹配"的结论；`flash` 的身份闸门在这两种值缺任何一个时**拒绝烧录**，而不是默认放行。
- 型号相关但只在特定型号上存在的寄存器（`reset_cause_address`）不会被猜：没有型号也没有写地址时，`--read-reset-cause` 会说明"读不了"，因为那个寄存器**读一次就清标志**，猜错等于毁掉复位历史。

## 四条来自实测的硬结论

这个包的规则不是设计出来的，是被板子纠正出来的，所以在这里点名，避免后来者"顺手改回去"：

1. **`DHCSR` 不能用来判断固件是否在跑。** 实板实测：一颗确实在以 ~1009 Hz 跑 RTOS tick 的核，`DHCSR` 返回冻结的 `0x30003`（`C_HALT` 置位），且跨会话读数完全相同。唯一可信的见证是**固件自己的 `xTickCount`**。`DWT->CYCCNT` 在本板同样不可用。
2. **J-Link 连接后的第一次内存读是 attach 时的调试缓存值。** 同一会话两次读间隔 4 s（中间无运行控制）得到同一个 `213916`，而板子确实在跑；`monitor go` 之后同一地址连续三次读是 `217714/217715/217716`。因此探活顺序必须是 `resume()` → 读 → 睡 → 读。
3. **采集速率是测量值，不是偏好。** 4000 kHz 时 1 KB RTT 缓冲排不空，丢 2847 个事件，上下文占比之和塌到 65.4 %、Idle 被低估成 48.5 %；12000 kHz 为 1 个溢出、和 100.07 %；24000 kHz 为 0 个溢出、和 99.99 %。质量门是 `context_load_sum` 在 100±1 且 `overflow_events == 0`——**永远不要为了"让这次跑通"而降低速率**。
4. **SystemView 不会自己去找 RTT 控制块。** 它把上次录制的那块板的地址记在 `%APPDATA%\SEGGER\SEGGER SystemView.ini`（`J-Link\RDG_RTT_MODE=Address` + `J-Link\TXT_RTT_CB_LOCATION`），命令行不给 `-rttcbaddr`/`-rttcbrange` 时**直接读那个记下来的地址**：换到 F407 时它照着 H723 的 `0x20004FC0` 去读一段没有控制块的内存，找不到通道就不发 start，于是 `-save` 写不出文件、退出码 1，而它自己的错误框在 V3.60e 里是坏的（`No such method MainWindow::OnError`），失败完全静默。`record` 因此**每次都显式带上 `-rttcbaddr`**（从 profile 或 ELF 里的 `_SEGGER_RTT` 解析），解析不出才退回 `-rttcbrange auto`。

另外两个坑写在 `SKILL.md` 里：SystemView V3.60e 必须先应答 `No Commercial-Use License` 模态框才会解析命令行（脚本用 `PostMessage(WM_KEYDOWN, VK_RETURN)` 定点投递，桌面锁定时也可用）；headless 导出会把最后一个未闭合上下文写成 `2^63 ns` 哨兵值，产生 `CPU Load outside 0..100` 的 CSV，脚本检测到就拒绝而不是截断。

## 可迁移性

迁移到另一台 **Windows 或 Linux** 机器：**[references/portability.md](references/portability.md)**。

- 工具按 **profile 值 → PATH → 常见安装目录 → SEGGER/注册表（仅 Windows）** 解析，每个结果都报出是哪一步命中的（`source`）。profile 里已指向真实文件的路径**永远优先**，探测不会悄悄改掉一个能用的配置。
- 平台差异集中在两个模块里：`piny_linux.py`（Linux 安装目录与 SEGGER 的 Linux 可执行文件名）、`piny_view_win32.py` / `piny_view_x11.py`（许可证模态框的两套后端）。上层 `piny_tools.py` / `piny_view.py` 只做分派，命令与报告结构在两个平台上一致。
- 换型号（不是换机器）只需要一个 `chip` 键：型号事实住在 `piny_chips.py` 的表里，见上面「换一块板子」。
- 路径写错时给出的不再是 `FileNotFoundError: [WinError 2]`，而是点名 profile 键、说明该键用途、并指出本机哪里有可用替代。
- profile 键分成三类：`machine` 键（本机路径，可以抄错也能被重新定位）、`target` 键（板子/探针属性，**抄到另一套硬件上正是"看起来正常的配置给出错误数字"的来源**）、`chip` 键（**同一型号的所有板子都一样**，所以住在一张可审查、可测试、随包分发的型号表里，profile 只写一个型号名）。
- `jlink_serial` 缺失时只报告、绝不猜；`freertos_tick_symbol` 可以省略，会从 ELF 自动解析。
- **诚实的边界**：工具探测的两套搜索逻辑都有单元测试覆盖（`test_tools.py` / `test_linux.py`），X11 后端在 CI 里用 Xvfb 真实加载 `libX11`/`libXtst` 跑通，但**尚未在第二台真实机器上验证过真机安装**，也还没在 Linux 实板上采过一次。到新机器先跑 `doctor --portable` 并核对 `source` 字段。

## 仓库说明

```
SKILL.md                  agent 入口：范围、边界、运行配方、实测行为
STATUS.md                 交接文档：已验证事实、实测数值、已知缺陷、未完成项
README.md                 本文件：对外介绍与下一步
LICENSE                   MIT
profiles/
  pinycore.example.json   新机器的模板（入库）
  pinycore.json           本机真实配置（.gitignore，不入库）
scripts/
  piny_debug.py           探针侧：doctor / license-check / identify / registers / flash / capture
  piny_trace.py           跟踪侧：analyze / compare / record
  piny_chips.py           芯片表：每种型号的 CPUID、DBGMCU 器件号、复位原因寄存器、内存区间
  piny_tools.py           工具探测层（分派到平台模块），只用标准库
  piny_linux.py           Linux 安装目录与 SEGGER 的 Linux 可执行文件名
  piny_view.py            模态框应答的分派层：平台无关的轮询/武装/重试循环
  piny_view_win32.py      Win32 后端（ctypes + user32，无第三方依赖）
  piny_view_x11.py        X11 后端（XTest，其次 XSendEvent）
tests/                    213 个用例，全部在临时目录或打桩下运行，不依赖本机装了什么：
                          test_debug.py（探针、烧录、liveness、识别门）、test_chips.py（芯片表
                          自洽性与解码）、test_tools.py（可迁移性与探测）、
                          test_linux.py（Linux 发现层）、test_trace.py（质量门、导出校验）、
                          test_view.py（模态框应答流程）、test_view_x11.py（X11 后端的几何、
                          按钮识别、降级阶梯与 Xlib 错误处理器）
tools/                    仓库自检与发布辅助，见下面「本地自检」一节：
                          check_repo.py、check_workflows.py、release_notes.py、smoke_doctor.py
references/               pinycore.md（项目事实）、portability.md（迁移指南）
.github/                  工作流、模板与 changelog 分类
agents/openai.yaml        agent 清单
```

`profiles/pinycore.json` 里是本机绝对路径与这块探针的序列号，因此**不随仓库分发**；克隆后从 `pinycore.example.json` 起一份即可，`doctor` 会把路径补齐。

## CI/CD

模仿 [PinyCore](https://github.com/SCNU-PIONEER/PinyCore) 的流水线，但按本项目调整——这里没有固件可编译，可验证的东西是**技能包本身**：

| 工作流 | 触发 | 做什么 |
|---|---|---|
| `ci.yml` | PR、推送到 `main`/`master`、手动 | `tests` 任务：Ubuntu + Windows × Python 3.10/3.13 跑全部单测与两个 `--version`；`x11` 任务：Ubuntu 上装 `xvfb`/`libx11`/`libXtst`/`libXft`，起一个裸 X server，断言选中的后端确实是 `piny_view_x11` 而不是被静默跳过；`repo-checks` 任务跑下面那四个自检脚本。三者都不需要探针、板子和 SEGGER |
| `release.yml` | 推送 tag `v*.*.*`、手动 | 先校验 tag 与脚本里的 `__version__` 一致、单测通过，再打包源码 zip + `SHA256SUMS` 发 GitHub Release |
| `labeler.yml` | PR | 按分支名与改动文件打 `bug`/`enhancement`/`documentation`/`portability`/`tracing`/`registers` 等标签 |

`.github/release.yml` 不是工作流：它定义 GitHub 自动生成 release notes 时的分类（与 PinyCore 的写法同名同义）。PinyCore 的 `cd.yml`、`request.yml` 这里没有对应物——没有固件要编译，也没有要强制填写的 PR 清单。

CI 用 `python -X utf8 -m unittest discover -s tests -t tests`，因为本项目不依赖 `pytest`——**这个仓库里的每个脚本都只用标准库**，CI 里没有任何 pip 安装步骤。

两个平台上**跑到的用例数一样（183），跳过的个数不一样**，因为跳过的正是"这个平台上没有对应代码路径 / 没有对应权限"的那些，所以这个数字本身也是本机事实，不必硬记。差异来自三处：`test_tools.py` 里只描述内置 Windows 遍历的探测用例（`%VAR%` 安装目录词表、注册表）在 Linux 上跳过、Windows 上运行；`test_view.py` 里驱动真实后端的用例（模态框应答、按钮按形状识别）只在既没有 user32 也没有 X display 时才跳过——判断条件是 `piny_view.backend()` 有没有后端，而不是平台名，因为按钮识别规则本身住在两个后端里，没有后端时"返回 None"与"没找到按钮"是两回事；符号链接用例在没有创建权限的账户上跳过——本机（普通用户）跳过它，CI 的 Windows runner 有该权限所以运行它，于是本机是 `skipped=2`、CI 上是 `skipped=1`。跳过的是"这台机器上根本不存在的东西"，而不是藏起跑不过的断言：`test_tools.py` 里 Windows 专属的用例用 `pt.inventory() is None` 守卫（也就是"本机走的就是内置遍历"），Linux 那一半的同一个契约由 `test_linux.py` 覆盖（27 条发现层用例）。X11 那一侧不是靠"跳过就算过"来交差的：`ci.yml` 的 `x11` 任务会先断言 `libX11`/`libXtst`/`libXft` 都加载成功、后端确实是 X11 后端，才让它跑测试。

**尚未实测的部分要说清楚**：X11 后端在 CI 上验证的是"能连上裸 X server、几何计算与降级路径正确"，`button_layout` 用的 0.80/0.90 两个比例仍是 Windows 上的实测值，在 Linux 上属于假设——真正的 Linux 实板采集还没跑过。

另外三条是第一次 Linux CI 跑出来的真实教训，值得单独记住：**（1）**`piny_view.py` 把转发项拷进后端模块时是"谁最后调用就留谁的补丁"，跨调用会留下状态——在真 X display 上表现成拿一个只存在于测试里的窗口句柄去查询，libX11 直接以 `BadWindow` 结束进程；现在转发项是**调用时**才解析的代理，补丁的作用范围跟调用者一致。**（2）**libX11 对未处理的协议错误是 `exit()`，不是异常，所以 X11 后端装了 `XSetErrorHandler`：窗口在"枚举到查询"之间消失时得到的是空结果，而不是一次没有报告的崩溃。**（3）**平台专属的分支**只有在该平台上真正跑到才算测过**：`piny_view_x11.child_windows` 把句柄先包成 `ctypes.c_ulong`，在 Windows 上完全看不出来（那半边是另一份实现），一到真 X server 就是 `TypeError: 'c_ulong' object cannot be interpreted as an integer`；此前所有用例要么打桩 `child_windows` 要么走代理，枚举循环一次都没真跑。所以现在有一条"把补丁取回后后端不能再看见它"的回归用例会**真的调用**后端自己的枚举——它先是暴露了这个缺陷，也顺带证明了代理没把别人的补丁留在后端里。

### 本地自检

四个脚本，CI 跑的就是它们，任何机器上都能直接跑（不需要板子）：

```powershell
python -X utf8 tools\check_repo.py          # 文档与代码是否还对得上（见下）
python -X utf8 tools\check_repo.py --tag v0.1.0   # 发布前：tag 是否与 __version__ 一致
python -X utf8 tools\check_workflows.py     # 工作流文件本身是否合规（无 PyYAML，故意的）
python -X utf8 tools\smoke_doctor.py        # 只用模板 profile 能否走完一次诊断
python -X utf8 tools\release_notes.py v0.1.0      # 按 conventional commit 生成发布说明
```

`check_repo.py` 是防文档腐烂的闸门，它实际验证的是：`PACKAGE_FILES` 里声明的文件都存在、`scripts`/`tests`/`tools` 下每个 `*.py` 都被声明过（漏声明的模块会让文件地图悄悄过期），且都在 `STATUS.md`/`README.md` 里被点到名；每个文档里写的子命令确实属于它提到的那个脚本；示例 profile 合法且全是占位符；`profiles/pinycore.json` 没有被 git 跟踪；四份文档里的测试基线数字与 `unittest discover` 实际跑出来的一致；`--tag` 与 `__version__` 一致。

## 范围与边界

- 仅在 **Windows 与 Linux** 上实现；实板验证到目前为止只发生在 **PinyCore / STM32H723 / Windows + SEGGER J-Link** 这一套上。不是通用 STM32 烧录工具，也不是 ESP32 开发技能。
- Linux 侧的实板采集**尚未跑过**：工具发现与 X11 后端有测试与 CI 覆盖，但那不是"在 Linux 上量过一次"。
- J-Link 探针下 OpenOCD 路径不可用（SEGGER 内核驱动独占，OpenOCD 报 `LIBUSB_ERROR_NOT_FOUND`）。
- `identify`/`registers` 的连接会短暂停下实时任务，进而影响 PWM/CAN 控制——`flash` 与 `registers` 都需要显式授权参数。
- 不改 HAL 与 `ThirdParty/Lib`；不终止使用者已有的 GDB/J-Link/IDE/SystemView 进程；不为了让测试通过而截断 >100 % 的 CPU 负载。
- 安装成功、单元测试、SEGGER 样例分析或 ELF 构建成功，**都不能算作实板性能验证通过**。

## Scope and limits

Verified against PinyCore on an STM32H723 with a SEGGER J-Link on Windows. The host scripts are
standard-library-only Python 3.10+ and run on Windows and Linux; the Linux half (tool discovery in
`piny_linux.py`, the X11 licence-dialog backend in `piny_view_x11.py`) is unit-tested and exercised
against a real Xvfb in CI, but no Linux host has driven this probe or captured a trace yet. `DHCSR` is
not a liveness witness on this probe (a core running at ~1009 Hz FreeRTOS tick reported a frozen
halted state); the firmware's own `xTickCount` is the only trusted witness, and RTT drain rate is a
measured per-rig value that must not be lowered to make a capture pass. Portability to a second
physical machine is implemented and unit-tested but has not been verified on one.

## License

[MIT](LICENSE)
