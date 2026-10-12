# PinyCore-debug-skill

[![CI](https://github.com/Minhyuin/PinyCore-debug-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/Minhyuin/PinyCore-debug-skill/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Minhyuin/PinyCore-debug-skill?include_prereleases&sort=semver)](https://github.com/Minhyuin/PinyCore-debug-skill/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078d4)
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

需要 Windows + Python 3.10+（主机脚本只用标准库）、[Arm GNU Toolchain](https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads)、[SEGGER J-Link](https://www.segger.com/downloads/jlink/) 与 [SystemView](https://www.segger.com/downloads/systemview/)。

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

采集 SystemView：

```powershell
python -X utf8 scripts\piny_trace.py --output artifacts\baseline record --seconds 20
python -X utf8 scripts\piny_trace.py compare --before artifacts\baseline --after artifacts\after
```

作为 agent 技能安装时，把整个目录放到技能目录下（例如 `<PinyCore>/.dsh/skills/PinyCore-debug/`），入口是 `SKILL.md`。

## 三条来自实测的硬结论

这个包的规则不是设计出来的，是被板子纠正出来的，所以在这里点名，避免后来者"顺手改回去"：

1. **`DHCSR` 不能用来判断固件是否在跑。** 实板实测：一颗确实在以 ~1009 Hz 跑 RTOS tick 的核，`DHCSR` 返回冻结的 `0x30003`（`C_HALT` 置位），且跨会话读数完全相同。唯一可信的见证是**固件自己的 `xTickCount`**。`DWT->CYCCNT` 在本板同样不可用。
2. **J-Link 连接后的第一次内存读是 attach 时的调试缓存值。** 同一会话两次读间隔 4 s（中间无运行控制）得到同一个 `213916`，而板子确实在跑；`monitor go` 之后同一地址连续三次读是 `217714/217715/217716`。因此探活顺序必须是 `resume()` → 读 → 睡 → 读。
3. **采集速率是测量值，不是偏好。** 4000 kHz 时 1 KB RTT 缓冲排不空，丢 2847 个事件，上下文占比之和塌到 65.4 %、Idle 被低估成 48.5 %；12000 kHz 为 1 个溢出、和 100.07 %；24000 kHz 为 0 个溢出、和 99.99 %。质量门是 `context_load_sum` 在 100±1 且 `overflow_events == 0`——**永远不要为了"让这次跑通"而降低速率**。

另外两个坑写在 `SKILL.md` 里：SystemView V3.60e 必须先应答 `No Commercial-Use License` 模态框才会解析命令行（脚本用 `PostMessage(WM_KEYDOWN, VK_RETURN)` 定点投递，桌面锁定时也可用）；headless 导出会把最后一个未闭合上下文写成 `2^63 ns` 哨兵值，产生 `CPU Load outside 0..100` 的 CSV，脚本检测到就拒绝而不是截断。

## 可迁移性

迁移到另一台 Windows 机器：**[references/portability.md](references/portability.md)**。

- 工具按 **profile 值 → PATH → 常见安装目录 → SEGGER/注册表** 四级解析，每个结果都报出是哪一步命中的（`source`）。profile 里已指向真实文件的路径**永远优先**，探测不会悄悄改掉一个能用的配置。
- 路径写错时给出的不再是 `FileNotFoundError: [WinError 2]`，而是点名 profile 键、说明该键用途、并指出本机哪里有可用替代。
- profile 键分成两类：`machine` 键（本机路径，可以抄错也能被重新定位）与 `target` 键（板子/探针属性，**抄到另一套硬件上正是"看起来正常的配置给出错误数字"的来源**）。
- `jlink_serial` 缺失时只报告、绝不猜；`freertos_tick_symbol` 可以省略，会从 ELF 自动解析。
- **诚实的边界**：以上结论来自打桩测试与"伪造外来 profile"的本机演练，**尚未在第二台真实机器上验证**。到新机器先跑 `doctor --portable` 并核对 `source` 字段。

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
  piny_tools.py           工具探测层，只用标准库
  piny_view.py            模态框应答（ctypes + user32，无第三方依赖）
tests/                    130 个用例，全部在临时目录或打桩下运行，不依赖本机装了什么：
                          test_debug.py（探针、烧录、liveness）、test_tools.py（可迁移性与探测）、
                          test_trace.py（质量门、导出校验）、test_view.py（模态框应答布局）
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
| `ci.yml` | PR、推送到 `main`/`master`、手动 | `tests` 任务：Ubuntu + Windows × Python 3.10/3.13 跑全部单测与两个 `--version`；`repo-checks` 任务跑下面那四个自检脚本（不需要探针、板子和 SEGGER） |
| `release.yml` | 推送 tag `v*.*.*`、手动 | 先校验 tag 与脚本里的 `__version__` 一致、单测通过，再打包源码 zip + `SHA256SUMS` 发 GitHub Release |
| `labeler.yml` | PR | 按分支名与改动文件打 `bug`/`enhancement`/`documentation`/`portability`/`tracing`/`registers` 等标签 |

`.github/release.yml` 不是工作流：它定义 GitHub 自动生成 release notes 时的分类（与 PinyCore 的写法同名同义）。PinyCore 的 `cd.yml`、`request.yml` 这里没有对应物——没有固件要编译，也没有要强制填写的 PR 清单。

CI 用 `python -X utf8 -m unittest discover -s tests -t tests`，因为本项目不依赖 `pytest`——**这个仓库里的每个脚本都只用标准库**，CI 里没有任何 pip 安装步骤。

两个平台上跑的用例数略有差别，这是有意的：Windows 上 130 个全跑，Ubuntu 上跳过 8 个 Windows 专属用例——模态框应答（`piny_view.py` 在非 Windows 上直接返回"须人工应答"）与 `%VAR%` 形式的安装目录展开（`os.path.expandvars` 在 POSIX 上不认这种写法）。跳过的正是"这条平台根本不执行的代码路径"，**不是**把跑不过的断言藏起来：这两个类在 Windows 上照常执行并通过。

### 本地自检

四个脚本，CI 跑的就是它们，任何机器上都能直接跑（不需要板子）：

```powershell
python -X utf8 tools\check_repo.py          # 文档与代码是否还对得上（见下）
python -X utf8 tools\check_repo.py --tag v0.1.0   # 发布前：tag 是否与 __version__ 一致
python -X utf8 tools\check_workflows.py     # 工作流文件本身是否合规（无 PyYAML，故意的）
python -X utf8 tools\smoke_doctor.py        # 只用模板 profile 能否走完一次诊断
python -X utf8 tools\release_notes.py v0.1.0      # 按 conventional commit 生成发布说明
```

`check_repo.py` 是防文档腐烂的闸门，它实际验证的是：`PACKAGE_FILES` 里声明的文件都存在且都在 `STATUS.md`/`README.md` 里被点到名；每个文档里写的子命令确实属于它提到的那个脚本；示例 profile 合法且全是占位符；`profiles/pinycore.json` 没有被 git 跟踪；四份文档里的测试基线数字与 `unittest discover` 实际跑出来的一致；`--tag` 与 `__version__` 一致。

## 范围与边界

- 仅针对 **PinyCore / STM32H723 / Windows + SEGGER J-Link** 验证过；不是通用 STM32 烧录工具，也不是 ESP32 开发技能。
- J-Link 探针下 OpenOCD 路径不可用（SEGGER 内核驱动独占，OpenOCD 报 `LIBUSB_ERROR_NOT_FOUND`）。
- `identify`/`registers` 的连接会短暂停下实时任务，进而影响 PWM/CAN 控制——`flash` 与 `registers` 都需要显式授权参数。
- 不改 HAL 与 `ThirdParty/Lib`；不终止使用者已有的 GDB/J-Link/IDE/SystemView 进程；不为了让测试通过而截断 >100 % 的 CPU 负载。
- 安装成功、单元测试、SEGGER 样例分析或 ELF 构建成功，**都不能算作实板性能验证通过**。

## Scope and limits

Verified only against PinyCore on an STM32H723 with a SEGGER J-Link on Windows. The host scripts are
standard-library-only Python 3.10+. `DHCSR` is not a liveness witness on this probe (a core running
at ~1009 Hz FreeRTOS tick reported a frozen halted state); the firmware's own `xTickCount` is the
only trusted witness, and RTT drain rate is a measured per-rig value that must not be lowered to
make a capture pass. Portability beyond this machine is implemented and unit-tested but has not been
verified on a second physical machine.

## License

[MIT](LICENSE)
