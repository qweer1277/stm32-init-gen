# stm32-init-gen

STM32 标准外设库（StdPeriph V3.5.0）**初始化代码生成器** —— 从一份 YAML/JSON
配置生成可直接编译的 `bsp_*.c` / `bsp_*.h` 工程骨架，配一个 PySide6 桌面界面。

目标芯片：**STM32F103C8T6**（中容量，`STM32F10X_MD`）。
生成代码只使用标准外设库，**不生成 HAL / LL**。

## 这解决什么问题

手工搭一个 STM32 标准库工程，要先抄 `RCC` 时钟树、算 Flash 等待周期、
逐个配 GPIO 模式、复算 USART 波特率的 BRR 值、排 NVIC 优先级、写中断
清标志的顺序……每一步都有一堆「填错了也能编译通过、但跑起来不对」的坑。

这个工具把这些计算集中到一处，并且**对每一处都给出理由**。生成的注释写的是
「为什么这样配」，不是「这一行是什么」。

## 两个项目

```
stm32-init-gen-repo\
├─ core\      stm32gen        生成核心 + 命令行（Python，无 GUI 依赖）
└─ gui\       stm32gen_gui    桌面界面（PySide6，依赖 core）
```

依赖方向严格单向：`gui → core`，核心库不反向引用界面。
两个包都能单独安装、单独使用 —— 只想在 CI 里生成代码时不用装 Qt。

## 快速开始

### 只用命令行

```powershell
git clone <仓库地址> stm32-init-gen
cd stm32-init-gen\core

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .

python -m stm32gen selftest
python -m stm32gen generate examples\demo.json
```

默认把 19 个 C 文件写到 `..\stm32-init-gen-output\`（与源码树并排，
不污染仓库）。`-o` 可改。

### 加桌面界面

```powershell
python -m pip install -e ..\gui
python -m stm32gen_gui
```

GUI 有七个配置页：全局 / GPIO / AFIO / EXTI / USART / TIM / NVIC，
右侧实时预览生成结果，Ctrl+E 导出。

### 要编译验证

编译验证需要一个 ST 标准外设库源码目录，**必须显式指定**：

```powershell
$env:STM32_STDLIB_ROOT = 'D:\path\to\STM32F10x_StdPeriph_Lib'
python -m stm32gen generate examples\demo.json --compile
```

目录里应有 `Start\`、`Library\`、`User\` 三层。

**纯生成与校验不需要它。** 符号表已固化在
`core\src\stm32gen\stdlib_ref\symbols.json`（478 个函数 + 全部宏与枚举），
克隆下来就能用。没设这个变量时：

| 操作 | 行为 |
|---|---|
| `generate` / `validate` | 正常工作 |
| `selftest` | 提示「标准库目录未配置（不影响生成）」，不判失败 |
| `--compile` | 报错并提示怎么设置 |
| `pytest` | 跳过 87 项需要库源码的用例，其余 641 项照跑 |

## 生成产物

默认 **19 个文件**，按外设分文件：

| 文件 | 职责 |
|---|---|
| `bsp_init.c/.h` | 聚合入口，按序调各外设初始化 |
| `bsp_rcc.c/.h` | 时钟树、Flash 等待周期、预取 |
| `bsp_afio.c/.h` | 重映射（USART/TIM）、SWJ 释放、AFIO 时钟 |
| `bsp_gpio.c/.h` | 引脚模式与初值 |
| `bsp_exti.c/.h` | 外部中断线与触发方式 |
| `bsp_usart.c/.h` | 波特率（复算 BRR）、字长、校验、中断 |
| `bsp_tim.c/.h` | 时基、PWM 通道、外部时钟（ETR/TI/ITR） |
| `bsp_nvic.c/.h` | 优先级分组与通道 |
| `bsp_it.c/.h` | 中断服务函数骨架 |
| `main.c` | 主函数 |

也可以 `--layout merged` **合并成 3 个文件**（`bsp.h` / `bsp.c` / `main.c`）——
内部仍按外设分模板，删外设时不会留下孤儿代码。

## 三条设计底线

**1. 不编造符号。** 生成的每一个函数、宏、枚举都必须能在标准库源码里找到，
否则生成阶段就失败。靠两道关卡：符号表提取器（`tools\extract_symbols.py`）
与反编造护栏（`stdlib_ref\guard.py`）。

**2. 有界等待。** 所有等硬件标志的循环都有超时，不会因为晶振没起振而卡死。
`RCC_WaitForHSEStartUp` 用库自带的超时版本，其余用带 guard 计数器的循环。

**3. 不确定就说不知道。** 比如 TIM 的默认引脚表在 `models\tim_pins.py`，
没有数据手册可核对，所以只 WARN 不 ERROR、且完全不参与代码生成。宁可少一个
提示，也不要给一个假的肯定。

## 测试

```powershell
cd core;  python -m pytest -q    # 728 项（无标准库源码时 641 passed, 87 skipped）
cd ..\gui; python -m pytest -q   # 179 项，offscreen 平台
```

代码风格由 ruff 把关（`line-length = 100`，`select = ["E", "F", "W", "I", "UP", "B"]`）。

## 文档

- [`core\README.md`](core/README.md) —— 完整技术说明：分层架构、配置格式、
  校验规则清单、每处生成的「为什么」、硬件约束与踩坑记录
- [`gui\README.md`](gui/README.md) —— 界面安装与打包
- [`gui\docs\GUI-使用说明.md`](gui/docs/GUI-使用说明.md) —— 逐页操作说明

## 许可

MIT，见 [LICENSE](LICENSE)。

生成的代码本身不含本项目代码（是模板渲染出的标准库调用），
可自由用于任何项目，包括商业闭源项目。
