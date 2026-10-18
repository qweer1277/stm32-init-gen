# stm32-init-gen

给 STM32F103C8T6（`STM32F10X_MD`）生成标准外设库初始化代码。写一份 YAML 或
JSON 配置，输出一套能直接编译的 `bsp_*.c` / `bsp_*.h`。另附一个 PySide6 桌面界面。

只用标准外设库（StdPeriph V3.5.0），不生成 HAL / LL。

## 背景

手搭一个标准库工程，要抄 RCC 时钟树、算 Flash 等待周期、逐个配 GPIO 模式、
复算 USART 的 BRR 值、排 NVIC 优先级、理清中断清标志的先后。这些地方错了
也照样编译通过，只是跑起来不对。

工具把这些计算收在一处，并在生成的注释里写明依据。注释讲的是为什么这么配，
不是这一行在做什么。

## 目录

```
stm32-init-gen-repo\
├─ core\      stm32gen      生成核心 + 命令行，纯 Python
└─ gui\       stm32gen_gui  桌面界面，依赖 core
```

gui 依赖 core，反过来没有。两个包可以分别安装：只在 CI 里生成代码就不必装 Qt。

## 快速开始

### 命令行

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

默认把 19 个 C 文件写到 `..\stm32-init-gen-output\`，与源码树并排，不落在
仓库里。`-o` 可以改。

### 加桌面界面

```powershell
python -m pip install -e ..\gui
python -m stm32gen_gui
```

七个配置页：全局 / GPIO / AFIO / EXTI / USART / TIM / NVIC。右侧实时预览，
Ctrl+E 导出。

### 编译验证

需要一个 ST 标准外设库源码目录，路径要显式给：

```powershell
$env:STM32_STDLIB_ROOT = 'D:\path\to\STM32F10x_StdPeriph_Lib'
python -m stm32gen generate examples\demo.json --compile
```

目录里应有 `Start\`、`Library\`、`User\` 三层。

生成和校验不需要这个目录。符号表已经固化在
`core\src\stm32gen\stdlib_ref\symbols.json`（478 个函数，宏与枚举齐全），
克隆下来就能用。没设环境变量时：

| 操作 | 行为 |
|---|---|
| `generate` / `validate` | 正常工作 |
| `selftest` | 提示「标准库目录未配置（不影响生成）」，不判失败 |
| `--compile` | 报错并提示怎么设置 |
| `pytest` | 跳过 87 项需要库源码的用例，其余 641 项照跑 |

## 生成产物

默认 19 个文件，按外设分：

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

`--layout merged` 可以合并成 3 个文件（`bsp.h` / `bsp.c` / `main.c`）。内部
仍按外设分模板，删外设时不会留下孤儿代码。

## 几条硬性约束

生成代码里出现的每个函数、宏、枚举，都要能在标准库源码里找到。找不到就在
生成阶段报错，不会写进产物。两道关卡：`tools\extract_symbols.py` 从库源码
提取符号表，`stdlib_ref\guard.py` 在渲染完成后核对。

等硬件标志的循环都带超时。`RCC_WaitForHSEStartUp` 用库自带的超时版本，其余
用 guard 计数器。晶振不起振时走失败分支，不会卡在那里。

TIM 的默认引脚表（`models\tim_pins.py`）没有数据手册可核对，所以校验时只报
WARN，代码生成完全不读这个表。

## 测试

```powershell
cd core;  python -m pytest -q    # 728 项（无标准库源码时 641 passed, 87 skipped）
cd ..\gui; python -m pytest -q   # 179 项，offscreen 平台
```

风格由 ruff 把关（`line-length = 100`，`select = ["E", "F", "W", "I", "UP", "B"]`）。

## 文档

- [`core\README.md`](core/README.md) —— 分层架构、配置格式、校验规则清单、
  每处生成的依据、硬件约束与踩坑记录
- [`gui\README.md`](gui/README.md) —— 界面安装与打包
- [`gui\docs\GUI-使用说明.md`](gui/docs/GUI-使用说明.md) —— 逐页操作说明

## 许可

MIT，见 [LICENSE](LICENSE)。

生成的代码不含本项目代码（是模板渲染出的标准库调用），可自由用于任何项目，
包括商业闭源项目。
