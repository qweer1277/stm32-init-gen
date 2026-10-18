# stm32-init-gen

STM32 标准外设库（StdPeriph V3.5.0）**初始化代码生成器**：库 + 命令行。

- 目标芯片：STM32F103C8T6（中容量，`STM32F10X_MD`）
- 首批外设：GPIO、USART、NVIC
- 技术栈：Python 3.10+ / Pydantic v2 / Jinja2 / PyYAML（**不含 GUI 依赖**）
- 生成代码**只使用标准外设库**，禁止 HAL / LL

> 桌面界面在同仓库的 [`gui\`](../gui) 目录（包名 `stm32gen_gui`）。
> 本目录只做「配置模型 → 校验 → 生成」，可单独当库用、单独跑 CLI。
> 依赖方向严格单向：`gui` → `core`，核心库不反向引用界面。

## 目录结构：工具与成果分开

生成产物**不落在源码树里**，默认写到源码树旁边的同级目录：

```
stm32-init-gen-repo\           仓库根（monorepo）
├─ core\                       本包：源码、测试、模板、示例配置、文档
│  ├─ src\stm32gen\
│  ├─ tests\
│  ├─ examples\
│  ├─ tools\                   辅助脚本（符号提取、反编造检查）
│  └─ scripts\
├─ gui\                        桌面界面（依赖 core，不反向被依赖）
│  ├─ src\stm32gen_gui\
│  ├─ tests\
│  └─ docs\GUI-使用说明.md
└─ stm32-init-gen-output\      成果：生成的 C 代码（19 个，或合并的 3 个）
   ├─ bsp_init.c / .h
   ├─ bsp_rcc.c / .h
   └─ …
```

`generate` 与 `compile` 的 `--out/-o` **默认值**就是那个成果目录，
实现见 `src/stm32gen/__init__.py` 的 `default_out_dir()`：上溯找
`pyproject.toml` 定位源码树根，再取它的兄弟目录。装成包（找不到标记文件）
时退回当前目录下的 `out`。

为什么不放在源码树里：产物一旦落在仓库根或 `src`，就会被 git 追踪、
被 ruff 扫、被 `rglob` 撞见，清理一次要记得清两处；而且 `tests/` 里
大量 `tmp_path` 比对很容易被残留产物干扰。

## 唯一事实来源（禁止编造符号）

生成器输出的每一个函数、宏、枚举，都必须能在**你在自己机器上指的那份**
标准库代码里找到。代码里没有任何硬编码路径 —— 必须显式设置环境变量
`STM32_STDLIB_ROOT`：

```powershell
# PowerShell
$env:STM32_STDLIB_ROOT = 'D:\path\to\STM32F10x_StdPeriph_Lib'
```

```cmd
:: cmd
set STM32_STDLIB_ROOT=D:\path\to\STM32F10x_StdPeriph_Lib
```

该目录的结构应为：

```
<STM32_STDLIB_ROOT>
├─ Start\        stm32f10x.h、system_stm32f10x.c/h、core_cm3.c/h、startup_stm32f10x_md.s
├─ Library\      stm32f10x_gpio / usart / rcc / ... + misc.c/h
└─ User\         stm32f10x_conf.h、stm32f10x_it.c/h
```

**这个目录只用于两件事**：编译验证（`--compile`）与符号表重新提取
（`tools\extract_symbols.py`）。**纯生成与校验不需要它** —— 符号表已经
固化在 `src\stm32gen\stdlib_ref\symbols.json`（478 个函数）里，克隆下来
就能用。

没设这个环境变量时：

| 操作 | 行为 |
|---|---|
| `generate` / `validate` | 正常工作 |
| `selftest` | 打印 `[ -- ] 标准库目录未配置（不影响生成）`，不判失败 |
| `--compile` | 报错并提示怎么设置环境变量 |
| `pytest` | 跳过依赖标准库源码的用例（`test_compile.py` / `test_keil.py` 等，共 85 项），其余 536 项照跑 |

工程宏约定（依据 `Start\stm32f10x.h`）：

| 宏 | 值 | 来源 |
|---|---|---|
| `STM32F10X_MD` | 定义 | `Start\stm32f10x.h:91-131`（未定义则 `#error`） |
| `USE_STDPERIPH_DRIVER` | 定义 | `Start\stm32f10x.h:133-140` |
| `HSE_VALUE` | `8000000` | `Start\stm32f10x.h:149-157` |

## 分层架构

依赖方向**单向向下**。GUI 已拆成独立项目，它依赖本包，本包不认识它：

```
stm32gen_gui（独立项目）──▶ models ──▶ stdlib_ref
                              │
                              ▼
                          validate ──▶ generator ──▶ templates
                                        │
                                        └──▶ compile_check（可选）
```

| 层 | 包 | 职责 |
|---|---|---|
| 配置模型 | `stm32gen.models` | Pydantic 模型，唯一配置真相 |
| 符号索引 | `stm32gen.stdlib_ref` | 从标准库头文件提取的真实符号表 |
| 校验 | `stm32gen.validate` | 引脚冲突、参数范围、依赖关系 |
| 生成器 | `stm32gen.generator` | 配置 → 模板上下文 → C 代码，可 CLI 独立调用 |
| 模板 | `stm32gen.templates` | Jinja2 `.j2` |
| 序列化 | `stm32gen.serializers` | JSON / YAML 存读 |
<span></span>

> 这条单向依赖有结构性保证：本包的 `pyproject.toml` 里没有 PySide6，
> 核心代码里也没有任何一处 `import stm32gen_gui`。装了界面的环境能跑核心，
> 只装核心的环境也能跑 —— 后者是常态（CI、脚本、别人只想生成代码）。

## 快速开始

```powershell
git clone <仓库地址> stm32-init-gen
cd stm32-init-gen\core
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-dev.txt
python -m pip install -e .
python -m stm32gen --version
python -m stm32gen selftest
```

要图形界面，再装旁边那个项目（它会把本包当依赖，共用同一个 venv 即可）：

```powershell
python -m pip install -e ..\gui
python -m stm32gen_gui
```

要跑编译验证，先指好标准库源码目录：

```powershell
$env:STM32_STDLIB_ROOT = 'D:\path\to\STM32F10x_StdPeriph_Lib'
```

> **Windows 安装踩坑记录**（本机实测，2026）：
> 1. 某些网络环境下 pip 缓存写入会无限挂起，`pip install` 可加 `--no-cache-dir`。
> 2. `pip install -e .` 的 build 隔离环境在同样环境下也会挂起，需先装
>    `setuptools wheel`，再用 `--no-build-isolation --no-deps`。
> 3. 国内网络下换源能明显提速（阿里源 / 清华源）。

## 符号提取与反编造护栏

「禁止编造函数/宏/枚举」不是靠自觉，而是靠两道工具卡住：

```powershell
# 1) 从标准库头文件提取真实符号表（产出 src\stm32gen\stdlib_ref\symbols.json）
python tools\extract_symbols.py

# 2) 校验 C 代码里用到的每个 STM32 符号都真实存在
python tools\check_no_invented.py ..\stm32-init-gen-output\
python tools\check_no_invented.py --selftest    # 护栏自测
```

当前实测规模：**宏 6572 / 枚举值 80 / 函数 473 / 类型 79**，来自 28 个头文件。
提取器带 58 项关键符号自检（含数值比对），任何一个不符就以非零码退出。

反编造护栏的两条硬性要求，都有测试守着：

- **不能漏检**：编造符号必须被抓到
- **不能误报**：误报会让人绕过护栏，比没有护栏更糟

已处理的三个误报来源：结构体成员访问（`cfg.GPIO_Pin`）、
注释与字符串字面量、以及非标准库前缀的标识符。

## 配置模型层

`src/stm32gen/models/` 是**唯一配置真相**：GUI 收集表单 → 构造模型；
CLI 读 JSON/YAML → 构造模型；生成器只接受 `Stm32Config`。

```
chip.py   芯片规格（型号 → 器件宏、可用端口、可用 USART 实例）
enums.py  与标准库宏/枚举一一对应的配置枚举
gpio.py   GpioPinConfig
usart.py  UsartConfig
nvic.py   NvicConfig / NvicChannelConfig
base.py   Stm32Config 根模型
```

### 反编造约束在这里怎么落地

`enums.py` 里每个枚举的**值就是标准库符号名**（`"GPIO_Mode_Out_PP"`），
不是数值。`import stm32gen.models` 会逐个校验这些名字真实存在于
`symbols.json`，写错名字会在**导入阶段**直接失败，而不是等到生成代码时才发现。

同时支持**别名解析**：配置文件里写 `"GPIOC"` 或 `"C"` 都能解析，
但写入时一律用 `"GPIOC"`（标准库符号名），保证生成代码用的是库符号。

### 字段名与结构体成员对齐

`GpioPinConfig` 的字段名与 `GPIO_InitTypeDef` 成员一一对应
（`pin` ↔ `GPIO_Pin`、`mode` ↔ `GPIO_Mode`、`speed` ↔ `GPIO_Speed`），
`UsartConfig`、`NvicChannelConfig` 同理。生成器转换时不需要名字映射，
减少出错面。

### 模型层拦下的组合错误

这些是「单看每个字段都合法、组合起来自相矛盾」的配置：

| 配置 | 为什么拦 |
|---|---|
| 输出模式不给 `init_level` | 上电瞬间电平不确定，典型的「上电抖一下」来源 |
| 输入模式给了 `init_level` | 说明用户理解有误，报错而非静默忽略 |
| `8b` 字长 + 校验 | 字长含校验位，实际只剩 7 数据位。要 8 数据位带校验应选 `B9`（8E1/8O1） |
| 只发 + 流控 | 单向发送配硬件流控没有意义 |
| 只发 + RXNE/IDLE 中断 | 方向与中断不匹配 |
| `TXE` 与 `TC` 同时使能 | 语义重叠 |
| 配 `GPIOE` | LQFP48 封装没引出该端口 |
| 配 `PC12` | 该引脚未引出（PC 只有 13~15） |
| 中断号不以 `_IRQn` 结尾 | 违反标准库 `IRQn_Type` 的命名约定 |

> **一个容易搞反的点**：`USART_WordLength` 指的是**含校验位的帧位数**，
> 不是纯数据位数。所以 `9b + 校验` 是正常的 8 数据位带校验（8E1/8O1），
> 必须放行；要拦的是 `8b + 校验`。初版实现把方向搞反了，
> 现在有回归测试 `test_usart_word_length_parity_9b_with_parity_allowed` 守着。

### 芯片规格

| 型号 | 器件宏 | Flash | RAM | 封装 |
|---|---|---|---|---|
| STM32F103C8T6 | `STM32F10X_MD` | 64KB | 20KB | LQFP48 |
| STM32F103C6T6 | `STM32F10X_LD` | 32KB | 10KB | LQFP48 |

LQFP48 的引脚分布：PA0~PA15、PB0~PB15、PC13~PC15、PD0~PD1（共 37 个 GPIO）。
GPIOE/F/G 不存在，配了会被拦下。

PD0/PD1 是 OSC_IN/OSC_OUT（被 HSE 晶振占用），PC14/PC15 是 OSC32_IN/OSC32_OUT，
这些在规格里标为保留并附原因，供校验层转成提示。

## 配置文件格式

值用标准库符号名，可直接对照头文件：

```yaml
version: 1
chip: STM32F103C8T6
hse_hz: 8000000
project_name: demo
gpio:
- port: GPIOC
  pin: 13
  mode: GPIO_Mode_Out_PP
  speed: GPIO_Speed_50MHz
  init_level: Bit_SET
  label: LED1 低电平点亮
usart:
- instance: USART1
  baudrate: 115200
  word_length: USART_WordLength_8b
  stop_bits: USART_StopBits_1
  parity: USART_Parity_No
  direction: RX_TX
  flow_control: USART_HardwareFlowControl_None
  interrupts:
  - USART_IT_RXNE
  remap: ''
nvic:
  priority_group: NVIC_PriorityGroup_2
  channels:
  - irq_channel: USART1_IRQn
    preemption_priority: 1
    sub_priority: 1
    enabled: true
    for_peripheral: USART1
```

`examples/demo.json` 与 `examples/demo.yaml` 是同一份配置的两种格式。

## 校验层

`src/stm32gen/validate/`。与模型层的分工：

- **模型层**拦「单条配置自相矛盾」——字段取值非法、组合起来没有意义
- **校验层**拦「多条配置之间冲突」——引脚被两处占用、波特率相对 PCLK 的误差、
  中断使能与 NVIC 通道不匹配

校验层**不抛异常**，收集全部问题一次性返回。GUI 要一次把所有问题标红给用户看，
逐个抛异常会让人改一处、跑一次、再改一处。

```
issue.py      Issue / Severity 数据结构（error / warning / info）
baudrate.py   波特率与时钟树的精确复算
pins.py       引脚占用分析（含 USART 隐式占用与重映射）
rules.py      五组规则：check_chip / check_pins / check_usart / check_nvic / check_gpio
```

### 波特率为什么要复算而不是套公式

`USART_Init` 算 BRR 用的是整数运算，末尾还有四舍五入。套「PCLK/16/波特率」
的公式算出的误差是假的，会把能用的配置判成不可用。所以 `baudrate.py`
逐行复现了 `Library\stm32f10x_usart.c:240-267`。

验证方式：与 ST 官方例程的已知 BRR 值交叉比对。

| 配置 | BRR | 校验 |
|---|---|---|
| 72MHz / 115200 | `0x0271` = 625 | ✅ |
| 72MHz / 9600 | `0x1D4C` = 7500 | ✅ |

### 一个库本身不检查的缺口

硬件要求 `USARTDIV >= 1`，即 16 倍过采样下 `BRR >= 16`（整数部分至少为 1）。
但库的 `IS_USART_BAUDRATE`（`stm32f10x_usart.h:335`）只检查固定上限
`BAUDRATE < 0x0044AA21`（约 4.5Mbps），**与当前 PCLK 无关**。

后果：36MHz 的 USART2 请求 4.5Mbps 时，库算出 `BRR=8`（整数部分 0，
分频比小于 1）而不报错，误差算出来还是 0%。校验层补上了这条
（`usart.baudrate_over_pclk_limit`）。实际上限是 `PCLK / 16`：
72MHz → 4.5Mbps，36MHz → 2.25Mbps。

### 时钟树的依据

`system_stm32f10x.c` 的 `SetSysClockTo72()` 里：

| 行 | 配置 | 结果 |
|---|---|---|
| `:1125` | `RCC_CFGR_PLLMULL9` | SYSCLK = HSE × 9 |
| `:1091` | `RCC_CFGR_HPRE_DIV1` | HCLK = SYSCLK |
| `:1094` | `RCC_CFGR_PPRE2_DIV1` | PCLK2 = HCLK |
| `:1097` | `RCC_CFGR_PPRE1_DIV2` | PCLK1 = HCLK / 2 |

> `:1096` 的注释写的是「PCLK1 = HCLK」，但代码是 `PPRE1_DIV2`。
> **注释与代码矛盾，以代码为准** —— 这是 ST 库里的文档笔误。

由此 8MHz HSE → SYSCLK 72MHz、PCLK2 72MHz、PCLK1 36MHz。
换成 12MHz 晶振而不改倍频系数会得到 108MHz，直接超频。

### USART 引脚的隐式占用

用户只在表单里填了「用 USART1」，但 PA9/PA10 实际被占用了。
`pins.py` 把这种隐式占用显式建模出来，否则会漏检
「USART1 与 GPIO PA9 冲突」——这类冲突在纯表单工具里很常见。

重映射也一并处理：选了重映射后，生效的是新引脚（如 USART1 → PB6/PB7），
默认引脚不再占用。

### 引脚冲突按方向分级

「一个引脚只能有一个功能」这句话是不准确的，所以校验不是按引脚名一刀切。
每个占用都带方向，`classify_conflict()` 据此分级：

| 情形 | 结果 |
|---|---|
| 两个以上**输出**撞一个引脚 | ERROR —— 两个外设同时推挽输出会电气打架 |
| **模拟**（ADC 等）与任何数字功能撞一个引脚 | ERROR —— `GPIO_Mode_AIN` 关掉数字输入缓冲，模拟独占 |
| 一个**输出**加若干**输入** | WARNING —— 能跑，但输入读到的是自己的输出波形 |
| 多个**输入**共用 | 不报 —— 合法 |

最后一行是关键：F1 的外设输入硬连线到引脚，`USART1_RX`(PA10) 和
`EXTI10`(PA10) 同时用完全合法。早先按引脚名分组的实现会把它误报成错误，
逼用户删掉正确的外设。

### 规则清单

| 代码 | 级别 | 说明 |
|---|---|---|
| `rcc.sysclk_over_spec` | ERROR | SYSCLK 超过 72MHz |
| `rcc.sysclk_not_max` | INFO | SYSCLK 合法但未达满速 |
| `rcc.pclk1_over_spec` | ERROR | PCLK1 超过 36MHz（APB1 上的 USART2/3、TIM2~4 等会异常） |
| `rcc.pll_input_out_of_range` | ERROR | PLL 输入不在 4~16MHz（超出会锁不住） |
| `rcc.flash_latency_too_low` | ERROR | 覆盖的 Flash 等待周期**低于**时钟所需值（取指不稳、偶发跑飞） |
| `rcc.flash_latency_high` | WARNING | 覆盖的等待周期高于所需（只降性能） |
| `rcc.prefetch_disabled` | WARNING | HCLK > 24MHz 却禁用了 Flash 预取 |
| `rcc.not_generated` | INFO | 未生成 RCC 代码，时钟由启动文件决定 |
| `chip.hse_not_default` | INFO | HSE 不是 8MHz |
| `chip.pclk_differs_from_spec` | INFO | 时钟树与芯片典型值不同 |
| `pin.conflict` | ERROR | 同一引脚有多个输出驱动源 |
| `pin.analog_conflict` | ERROR | 同一引脚同时配了模拟与数字功能 |
| `pin.direction_conflict` | WARNING | 同一引脚既有输出又有输入，输入读到的是自己的输出波形 |
| `gpio.duplicate_pin` | ERROR | 同一引脚在 gpio 列表里配了多次 |
| `pin.reserved` | WARNING | 引脚被晶振等硬件占用 |
| `pin.debug_port` | WARNING | 占用 SWDIO/SWCLK 等调试口 |
| `usart.remap_pin_missing` | ERROR | 重映射目标引脚在封装上不存在 |
| `usart.remap_pin_override` | WARNING | 既重映射又显式指定了默认端口引脚 |
| `usart.duplicate_instance` | ERROR | 同一实例配了多次 |
| `usart.baudrate_over_lib_limit` | ERROR | 超过库的固定上限 `0x0044AA21` |
| `usart.baudrate_over_pclk_limit` | ERROR | 超出 PCLK/16 能表示的上限 |
| `usart.baudrate_unrepresentable` | ERROR | BRR 算出来是 0 |
| `usart.baudrate_error_too_large` | ERROR | 误差 ≥ 5% |
| `usart.baudrate_error_large` | WARNING | 误差 ≥ 3% |
| `usart.baudrate_ok` | INFO | 附 BRR 与误差，便于核对 |
| `usart.irq_without_nvic` | ERROR | 使能了中断但没配 NVIC 通道 |
| `usart.nvic_without_irq` | WARNING | 配了 NVIC 通道但没使能中断 |
| `usart.irq_nvic_disabled` | WARNING | NVIC 通道是禁用状态 |
| `usart.flow_control_pins` | INFO | 硬件流控需要额外的 RTS/CTS 引脚 |
| `usart.usart3_on_lqfp48` | INFO | USART3 在 LQFP48 上只有默认引脚可用 |
| `nvic.preempt_out_of_range` | ERROR | 抢占优先级超出分组位宽 |
| `nvic.sub_out_of_range` | ERROR | 子优先级超出分组位宽 |
| `nvic.unknown_irqn` | ERROR | 中断号不在标准库 `IRQn_Type` 里 |
| `nvic.duplicate_channel` | ERROR | 同一通道配了多次 |
| `nvic.all_same_preempt` | WARNING | 多通道抢占优先级全 0 |
| `nvic.group_note` | INFO | 分组位宽 + 只能调用一次的限制 |
| `gpio.floating_input` | WARNING | 浮空输入，悬空时电平不确定 |
| `gpio.analog_mode` / `gpio.low_speed_output` / `gpio.alternate_mode` | INFO | 模式与速度的提示 |
| `config.empty` | INFO | 没有任何外设配置 |
| `internal.rule_failed` | ERROR | 规则自身崩溃（工具缺陷） |

> **一个曾经整块失效的规则**：`rcc.flash_latency_too_low` / `rcc.flash_latency_high`
> 最初写成拿用户覆盖值与 `tree.flash_latency` 比。而 `tree.flash_latency` 在
> 有覆盖时**就是覆盖值本身**，于是变成 `0 < 0` 恒假 —— 两条分支都永远不进，
> 整块是死代码。现在比的是 `tree.flash_latency_recommended`（始终按时钟推导，
> 与是否覆盖无关，见 `validate/baudrate.py` 里两个字段的注释）。
> 有 4 条回归测试守着，`test_flash_latency_override_too_low_is_error` 断言
> 提示里的参照值必须是推荐值而不是覆盖值。

### 校验的两条底线

每条规则都有测试守着两件事：**该报的报了**、**不该报的没报**。
只测前者会让规则越加越吵，用户最后干脆忽略全部警告。
`test_demo_config_has_no_errors` 专门断言示例配置零错误——它是用户的第一印象。

## 生成器与模板

生成器是纯函数 `Stm32Config -> GenerateResult`：**不写文件**（写盘由调用方决定）、
**不依赖 GUI**。CLI 与 GUI 调同一个 `generate()`，保证两者产物完全一致。

```
generator/
├── context.py   配置模型 → 模板上下文（所有计算与条件都在这里）
├── render.py    Jinja2 渲染，返回 GeneratedFile 列表（OUTPUT_SPECS 声明 19 个产物）
└── types.py     GeneratedFile / GenerateResult
```

模板与产物一一对应，见上面「产物」表。模板目录里的 `.j2` 与 `render.py` 的
`OUTPUT_SPECS` 必须同步——`test_all_templates_exist` 会检查这一点。

### 产物（按外设分文件，共 19 个；也可合并成 3 个）

| 文件 | 内容 |
|---|---|
| `bsp_init.h` | 总入口声明，并 `#include` 各外设头文件 |
| `bsp_init.c` | 按顺序调用各外设 init 的编排（不含外设细节） |
| `bsp_rcc.h` / `bsp_rcc.c` | 系统时钟：Flash 等待周期、HSE/PLL/总线分频、SYSCLK 切换；头文件给出 `BSP_xxx_HZ` 频率常量 |
| `bsp_afio.h` / `bsp_afio.c` | AFIO 时钟、**全部引脚重映射**（USART + TIM）、SWJ 调试口释放 |
| `bsp_gpio.h` / `bsp_gpio.c` | 端口时钟与全部 `GPIO_Init`（含 USART 的 TX/RX、EXTI 的输入引脚、TIM 的 PWM 输出引脚）、带标签引脚的宏 |
| `bsp_exti.h` / `bsp_exti.c` | 外部中断线选择（`GPIO_EXTILineConfig`）与边沿配置 |
| `bsp_usart.h` / `bsp_usart.c` | 实例时钟、`USART_Init`、中断使能、`USART_Cmd` |
| `bsp_tim.h` / `bsp_tim.c` | 时基、输出比较（每通道 `TIM_OCxInit`）、预装载、高级定时器主输出 |
| `bsp_nvic.h` / `bsp_nvic.c` | 优先级分组与各通道 `NVIC_Init` |
| `bsp_it.h` / `bsp_it.c` | USART / EXTI / TIM 中断服务函数骨架 |
| `main.c` | 最小可编译的 main，调用 `bsp_init()` |

**为什么按外设拆**：删掉某个外设时，它的模板、上下文、时钟使能一起消失，
不会在别的文件里留下孤儿代码。代价是各模块要自己管自己的时钟使能——
这是刻意的取舍，理由写在 `generator/context.py` 的模块 docstring 里。

**重映射为什么全在 `bsp_afio.c`**：重映射改的是 `AFIO->MAPR`，且**必须在
`GPIO_Init` 之前**生效。`bsp_init()` 把 `bsp_afio_init()` 排在
`bsp_gpio_init()` 之前，所以放这里能保证顺序；若按"外设归属"把 TIM 重映射
放进 `bsp_tim.c`，它会在 GPIO 初始化**之后**才跑 —— 引脚已按旧映射配成复用
输出，改了来源后那个脚不再是定时器的输出脚，现象是"PWM 没有波形"。

**时钟使能的归属**：AFIO 时钟在 `bsp_afio.c`，GPIO 端口时钟在 `bsp_gpio.c`，
USART 实例时钟在 `bsp_usart.c`，TIM 实例时钟在 `bsp_tim.c`。
**所有引脚的模式配置仍在 `bsp_gpio` 里**：引脚模式是 GPIO 的职责，
同一引脚只在一处配置，避免两处配同一引脚而模式不同。

带标签的引脚会自动生成宏，可直接用于业务代码：

```c
/* PC13 — LED1 低电平点亮 */
#define BSP_LED1_PORT      GPIOC
#define BSP_LED1_PIN       GPIO_Pin_13
```

`bsp_rcc.h` 还会给出频率常量，业务代码算定时周期/超时不该写死数字：

```c
#define BSP_SYSCLK_HZ       72000000u
#define BSP_PCLK1_HZ        36000000u
#define BSP_TIM234_HZ       72000000u   /* APB1 分频≠1 时定时器时钟是 PCLK 的 2 倍 */
```

### 两种产物布局：分文件（默认）/ 合并

一次生成要往工程里塞 19 个文件，对只想看效果、或工程里已有自己 BSP 的人来说
是负担。所以生成器支持第二种形态：

| 布局 | 产物 | 选用方式 |
|---|---|---|
| `split`（默认） | 上表 19 个文件 | 不传参数即是 |
| `merged` | `bsp.h` + `bsp.c` + `main.c` | CLI `--layout merged`；GUI 预览区「布局」下拉 |

```bash
python -m stm32gen generate config.json --layout merged
```

合并**不是另写一套模板**，而是渲染完成后对 19 份文本做一次纯函数变换
（`generator/merge.py`）：剥掉每个文件的头注释、include guard、`extern "C"`
包裹、`#include "stm32f10x.h"`、`#include "bsp_xxx.h"` 与版权尾注，正文一行不动，
按 `bsp_init → rcc → afio → gpio → exti → usart → tim → nvic → it` 的顺序拼起来。

这么做的理由：

- **不会漂移**。模板只有一套，加外设只改一处，两种布局自动同时受益。
- **`split` 产物逐字节不变**。合并层对 `split` 路径一行都不介入，稳定性由结构
  保证，而不是靠改完再比对。回归测试里有一条就是「取 `bsp_gpio.c` 的函数体，
  断言它原样出现在 `bsp.c` 里」。
- 合并**只依赖 `split` 产物的固定形态**（9 个 `.h` 各有且只有 7 个条件编译行，
  9 个 `.c` 各有 0 个）。形态变了就抛 `MergeError` 而不是产出坏文件。

合并后有几处刻意的处理：ST 模板那四行 `/* Exported types/constants/macro/functions */`
横幅会被丢掉（9 份叠加是 36 行重复分隔线，只剩噪音）；连续空行压成一个；
正文注释里的「见 bsp_gpio.c」这类跨文件指引**不改写**（靠字符串替换，模板一改
就静默失效），而是在 `bsp.h` 文件头声明「指的是同一套模板的分文件产物」。

两种布局都跑过 `armclang` 与 `armcc`，10 个 `.c` → 3 个 `.c` 均零警告；
`bsp.c` 在 demo 配置下 373 行。

### EXTI / AFIO / TIM 的几个硬件约束

这些是**物理限制**，本工具在模型或校验层拦下，不是风格问题：

| 约束 | 后果 | 处理 |
|---|---|---|
| 同一条 EXTI 线只能接一个端口 | `AFIO->EXTICR` 每条线只有一份端口选择，后写的覆盖先写的 → 其中一个中断永不触发 | `model_validator` + `exti.line_conflict`（ERROR） |
| EXTI 线号 = 引脚号 | EXTI3 只能是 Px3；EXTI12 + GPIOC 会指向不存在的 PC12 | 芯片规格校验（ERROR） |
| EXTI 引脚必须是输入模式 | 配成输出时中断永远不触发 | `model_validator`（ERROR） |
| `GPIO_EXTILineConfig` 写 `AFIO->EXTICR` | 没使能 AFIO 时钟时这句**完全无效且不报错** | `Stm32Config.needs_afio_clock` 自动包含 EXTI |
| 线 5~9 共用一个中断通道、10~15 共用一个 | 一个通道只能有一个 ISR，生成两个同名函数会重复定义 | `_build_exti()` 按 handler 分组；ISR 里逐条判标志位 |
| `GPIO_Remap_SWJ_Disable` 会关闭 SWD | 之后调试器再也连不上，只能靠 BOOT0 或整片擦除救回 | `afio.swj_remap_bricks_debugger`（**ERROR**） |
| TIM1（高级）必须开主输出 MOE | `BDTR.MOE` 复位值是 0，不开则 PWM 引脚没有波形 | 生成 `TIM_CtrlPWMOutputs`；`tim.advanced_needs_moe` 提示 |
| 库没有 `TIM_OCInit`，只有 `TIM_OC1Init`~`TIM_OC4Init` | 猜函数名会编译失败 | `TimChannel.init_function` 按通道派生 |
| `RCC_HCLKConfig` 与 `RCC_PCLK1Config` 用**不同**的分频宏 | 喂错不报错，静默写坏寄存器位 | 见 `models/rcc.py` 的分组说明 |

### 关于 TIM 默认引脚表的可信度

`src/stm32gen/models/tim_pins.py` 里有一张各定时器通道的默认复用引脚表，
但开发它的环境只有标准外设库源码、**没有数据手册**，也无法联网核对。
因此这张表被刻意限制为：

- **只用于提示，不用于拦截** —— 校验层对比时只报 WARNING
  （`tim.pin_not_default`），绝不报 ERROR
- **生成代码不依赖它** —— 真正决定引脚的是用户的
  `TimChannelConfig.port/pin`

这样即使表里某一项记错，后果也只是一句多余的提示，不会生成引脚错误的代码。
**请以你手上那份 STM32F103 数据手册的「引脚定义」表为准。**

### 定时参数反算（给周期，算 PSC/ARR）

实际需求几乎总是「20ms 进一次中断」，而不是「PSC=23、ARR=59999」。
`models/tim.py` 提供两个纯函数做反算，GUI 的「自动计算」按钮就是它们的包装：

```python
from stm32gen.models import solve_timing, solve_period

solve_timing(72_000_000, 1000)   # 按频率：1kHz
solve_period(72_000_000, 0.02)   # 按周期：20ms
```

返回 `TimTimingSolution`：`prescaler` / `period` / `actual_hz` / `divider` /
`divider_hz` / `duty_steps` / `exact` / `error_percent` / `timer_clock_hz`，
另有 `describe()` 给出人话回显。

三条设计取舍：

1. **优先把 ARR 撑大**。同一个目标频率有多种 (PSC, ARR) 组合，
   工具选 ARR 最大的那个 —— ARR 决定占空比档位数，`ARR=59999` 的分辨率是
   `ARR=599` 的 100 倍。分辨率不够时的现象是「CCR 改一位、占空比跳一大截」。
2. **除不尽就如实报误差**。求 777Hz 会返回最接近的解并给出百分比误差，
   `exact` 为假。假装精确的后果是照着注释算占空比、实测对不上。
3. **中央对齐自动折半**。中央对齐一个完整周期上下各数一遍，同样 PSC/ARR
   频率只有一半，反算前先把目标总分频减半。

反算用的定时器时钟来自 `compute_clock_tree()`，与生成代码同一来源
（APB 分频 ≠1 时是 PCLK 的 2 倍），不会两边各算一套。
`test_solve_timing_matches_model_frequency` 把反算结果代回
`TimConfig.update_frequency_hz()` 断言两者相等。

### TIM 外部时钟

定时器的计数脉冲不一定来自时钟树。`TimClockSource` 覆盖四种来源：

| 值 | 含义 | 配置函数 | 占引脚 |
|---|---|---|---|
| `INTERNAL`（默认） | 时钟树的 TIMxCLK | 不生成（复位默认） | 否 |
| `ETR_MODE1` / `ETR_MODE2` | 外部脉冲打进 TIMx_ETR 引脚 | `TIM_ETRClockMode1Config` / `...Mode2Config` | ETR 引脚 |
| `TI1` / `TI2` / `TI1_ED` | 用捕获通道的边沿计数 | `TIM_TIxExternalClockConfig` | CH1/CH2 |
| `ITR0`~`ITR3` | 另一个定时器的更新事件当钟 | `TIM_ITRxExternalClockConfig` | 否（片内） |

```json
{
  "instance": "TIM2",
  "clock_source": "ETR_MODE1",
  "etr_prescaler": "TIM_ExtTRGPSC_DIV4",
  "etr_polarity": "TIM_ExtTRGPolarity_NonInverted",
  "etr_filter": 3,
  "prescaler": 0,
  "period": 249,
  "external_clock_hz": 1000
}
```

**PSC/ARR 由你填，工具不做反算。** 外部信号的频率取决于你接了什么源，
工具无从得知 —— 与其猜一个数给出「看起来合理、实际差几个数量级」的
PSC/ARR，不如直接把这一层交回给你。

`external_clock_hz` 只用于注释与频率回显，不参与生成：

- **填了** → 注释与校验面板给出推算，如「1000Hz ÷4（ETR）÷1（PSC）
  = 计数 250Hz，÷250（ARR+1）= 更新 1Hz」
- **不填** → 注释写「频率由外部信号决定，与时钟树无关」，
  校验报一条 INFO（`tim.external_clock_frequency_unknown`），不阻塞生成

四处刻意的处理：

1. **注释不再写「定时器时钟 72MHz」**。外部时钟下那个值对计数毫无作用，
   写出来会诱导用户按它推算周期，结论全错。
2. **内部时钟配 ETR 参数直接报错**，不静默丢弃。静默丢弃比报错更糟：
   用户以为配上了预分频，生成的代码里一个字都没有。
3. **ETR 预分频叠在 PSC 之上**。`etr_total_divider` 只对 ETR 模式返回非 1，
   TI/ITR 没有这一级 —— 混起来会让频率差 2~8 倍。
4. **ITR 级联提示上游依赖**。`ITR0~ITR3` 对应哪个上游由芯片型号决定
   （本工具没有手册数据），所以只报 INFO 提示对照参考手册，不报错。

反编造护栏的配套：`_tim_clock_call()` 除了返回调用文本，还返回用到的
符号元组，交由 `_collect_symbols()` 登记 —— 否则
`TIM_ExtTRGPSC_DIV4` 这类真实宏会被判成编造符号。

### 模板只做排版，不做判断

模板里只有 `{{ 变量 }}` 与 `{% for %}` / `{% if ctx.has_xxx %}`，
所有计算都在 `context.py`。配合 Jinja2 的 `StrictUndefined`，
引用不存在的变量会直接报错，而不是静默渲染成空字符串——
C 代码里的静默空值会产出语法错误，且很难定位。

### 生成代码里几个体现「为什么」的顺序

**1. `bsp_init()` 的调用顺序不可调换** —— 时钟 → GPIO → USART → NVIC。

- 时钟必须最先：外设寄存器写操作依赖总线时钟，没配好时后续配置**全部白写**
  （这是最常见的「配了没用」原因）。`bsp_rcc_init()` 同时把 Flash 等待周期设好，
  保证接下来 HCLK 拉高后取指稳定。
- NVIC 放最后：中断通道一开中断立刻可以进，此时外设已配置完毕，
  不会出现「中断进了但外设还没配好」的窗口。

**2. Flash 等待周期必须在提高 HCLK 之前设好** —— 反过来（HCLK 已拉高而 Flash
还没加等待周期）那段窗口里取指不稳，是「偶发跑飞」的经典成因。
`bsp_rcc.c` 里的顺序是 Flash → HSE/PLL → 总线分频 → SYSCLK 切换。
**总线上必须让 PCLK1 ≤ 36MHz**，超过这个值挂在 APB1 上的外设工作不正常。

**3. 等待硬件标志的循环都带上限**（code-standards R4.3）。
`bsp_rcc.c` 用 `BSP_RCC_WAIT_LOOPS`（`0x4000u`）作为上限：晶振不起振或
PLL 锁不住时退回默认时钟，而不是卡死在启动阶段——现场表现是「上电没反应」，
比「主频不对」难查得多。HSE 则直接用库自带超时的 `RCC_WaitForHSEStartUp()`。

**4. 初始电平必须在 `GPIO_Init` 之前写**（在 `bsp_gpio.c` 里）：

```c
GPIO_SetBits(GPIOC, GPIO_Pin_13);   /* 先写 ODR */
GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_PP;
GPIO_Init(GPIOC, &GPIO_InitStructure);       /* 再切输出模式 */
```

`GPIO_SetBits` 写的是 ODR，对输入模式引脚同样有效。先写好 ODR 再切到输出，
切换瞬间输出即为目标电平。反过来会让引脚先抖一下。

**5. 重映射必须在 `GPIO_Init` 之前配置** —— 它改的是引脚的复用功能来源，
配好之后再设引脚模式才有意义。AFIO 时钟由 `context.py` 自动加入（归 GPIO 模块）。

### 中断清标志方式按中断源区分（关键正确性点）

| 中断 | 清标志方式 | 依据 |
|---|---|---|
| `RXNE` | 读 DR | 在 `IS_USART_CLEAR_IT` 里 |
| `TC` | `USART_ClearITPendingBit` | 在 `IS_USART_CLEAR_IT` 里 |
| **`IDLE`** | **先读 SR 再读 DR** | **不在** `IS_USART_CLEAR_IT` 里 |
| **`PE`** | **先读 SR 再读 DR** | **不在** `IS_USART_CLEAR_IT` 里 |
| `TXE` | 只能靠写 DR | 无清除 API |

`IS_USART_CLEAR_IT` 定义在 `Library\stm32f10x_usart.h:256-257`，
只允许 `TC` / `RXNE` / `LBD` / `CTS`。对 `IDLE` 调 `USART_ClearITPendingBit`
会触发 `assert_param` 失败，且标志实际清不掉——中断会一直重入。
依据 `Library\stm32f10x_usart.c:1003-1011` 的 `@note`。

### 生成后的自动核对

`GenerateResult.symbols_used` 记录生成器引用到的每个标准库符号。
测试 `test_symbols_used_covers_generated_code` 会**反向**从生成代码里
提取所有库前缀标识符，核对符号收集没有遗漏——遗漏会让反编造护栏失去意义。

另外 `test_generated_code_passes_anti_invention_guard` 用 6 组不同配置
（含空配置、仅 USART、重映射、多中断、混合 GPIO）跑一遍护栏，
确保**输出**层面也没有编造符号。

## 桌面界面（独立项目）

图形界面不在本仓库，见 [`stm32-init-gen-gui`](../gui)：

- 使用说明（含各页签截图与常见问题）：`stm32-init-gen-gui/docs/GUI-使用说明.md`
- 安装：`pip install -e ..\gui`，然后 `stm32-init-gui`
- 界面侧自检：`python -m stm32gen_gui --selfcheck`

为什么拆出去：PySide6 是重量级依赖（本机约 200 MB），而生成器与 CLI 的
常见用法是脚本、CI、批处理 —— 那些场景根本不需要 Qt。合在一起时
`pip install stm32-init-gen` 会连带装一整套 GUI 框架。拆开后核心库的
依赖只有 Pydantic / Jinja2 / PyYAML。

界面**不含任何生成或校验逻辑**：预览调的就是本包的 `generate()`，与
`python -m stm32gen generate` 完全同一条代码路径，所以界面上看到的
内容与命令行产出的文件逐字节一致。

## 编译验证

把生成的代码真的交给编译器过一遍。只编译（不链接）——链接需要完整启动文件与
链接脚本，而本工具的职责只是验证生成代码本身的语法与类型正确性。

### 四种工具链，能力必须区分

| 工具链 | 参数风格 | 检查范围 | 措辞 |
|---|---|---|---|
| **armclang**（Keil AC6） | Clang | 真实目标编译 | 「编译通过」 |
| **armcc**（Keil AC5） | ARMCC 专有 | 真实目标编译 | 「编译通过」 |
| **arm-none-eabi-gcc** | GCC | 真实目标编译 | 「编译通过」 |
| 宿主 `gcc`（降级） | GCC | **仅**语法与架构无关的类型检查 | 「语法检查通过」 |

**降级结果不能称为「编译通过」**，`CompileResult.is_native` 决定措辞，
CLI 与 GUI 都会额外打印一行警告，说明宿主 gcc 发现不了内联汇编约束、
寄存器类型、intrinsic 用法等 ARM 专属问题。

三款 ARM 编译器中 **armclang 优先**：它基于 Clang，对标准的遵循最严、
诊断最清晰。

### 两套 Keil 编译器的参数差异（实测）

这不是理论区别，是踩出来的：

| | ARMCLANG (AC6) | ARMCC (AC5) |
|---|---|---|
| 目标参数 | `--target=arm-arm-none-eabi -mcpu=cortex-m3 -mthumb` | `--cpu=Cortex-M3` |
| 严格告警 | `-Wall -Wextra` | `--strict`（**不认** `-Wall`，会报 `C3900U: Unrecognized option '-all'`） |
| 只检查不产出 | `-fsyntax-only` | **不支持**，只能 `-c` 产目标文件 |
| 版本查询 | `--version` | 直接运行（**不认** `--version`，报 `C3900U`） |
| 诊断格式 | `file:line:col: error: msg` | `"file", line 42: Error: #35: msg` |

另外 ARMCC **默认会自动链接**——不加 `-c` 时会报 `L6218E: Undefined symbol
GPIO_Init`。那说明编译本身过了、失败在链接阶段，正是「只编译不链接」要避免的。

ARMCC 需要写目标文件，所以 `check_sources` 用临时目录收着，跑完即删。

### 工具链查找

```
ARMCLANG / ARMCC / ARM_GCC / HOST_CC 环境变量
  → PATH
  → Keil 安装目录（KEIL_ROOT 可指定）
```

**为什么必须搜安装目录**：Keil 安装后只把路径加到*持久化的*用户 PATH，
已经在跑的进程（包括本工具）看不到。实测本机 Keil 装在 `E:\download\keil5`，
持久化 PATH 里有但当前会话 PATH 里没有——只查 PATH 会白白漏掉它能提供的
真实编译验证。

设 `STM32_NO_HOST_FALLBACK=1` 禁用宿主 gcc 降级。这个开关的意义：CI 里想
强制要求真实 ARM 工具链，不希望「降级通过」掩盖工具链缺失。

### 编译宏与包含路径

三个编译宏缺一不可，否则 `stm32f10x.h` 直接 `#error`：

```
-DSTM32F10X_MD -DUSE_STDPERIPH_DRIVER -DHSE_VALUE=8000000
```

包含路径是标准库的 `Start` / `Library` / `User` 三层，加上生成代码所在目录。
**生成代码目录必须用绝对路径**——执行时 cwd 会切到源码目录，相对路径会变成
`out/out` 这种不存在的路径，编译器只是忽略它而不报错，包含路径就静默失效了。

### 用法

```powershell
# 生成后顺带验证
python -m stm32gen generate examples\demo.json --compile
python -m stm32gen generate examples\demo.json --compile --compile-mode keil

# 验证已有目录
python -m stm32gen compile examples\demo.json
python -m stm32gen compile examples\demo.json --mode keil       # 只用 Keil
python -m stm32gen compile examples\demo.json --mode armclang   # 精确指定 AC6
python -m stm32gen compile examples\demo.json --mode armcc      # 精确指定 AC5
python -m stm32gen compile examples\demo.json --mode arm        # 任意真实 ARM
python -m stm32gen compile examples\demo.json --mode host       # 强制降级
```

`--mode` 的精确模式（`armclang`/`armcc`/`gcc`/`host`）找不到就返回失败，
**不降级** —— 便于 CI 里强制要求某种工具链。

GUI 里的「工具 → 编译验证」（`Ctrl+B`）在**临时目录**里做，不在用户目录留文件——
编译验证是只读性质的检查，用户可以先验证再决定导出。

### 结果区分三种状态

| 情况 | `ran` | `ok` | 含义 |
|---|---|---|---|
| 无编译器 | False | False | 「没做」，不是「失败」——环境问题 |
| 编译报错 | True | False | 「失败」——代码问题 |
| 编译通过 | True | True | |

`ok` 要求 `ran` 为真，所以**「不可用」不会被误读为「成功」**。

## 命令行

### 子命令

| 命令 | 作用 |
|---|---|
| `selftest` | 环境自检：Python、依赖、标准库目录、符号表、模型层、生成器、编译器（界面侧另有 `python -m stm32gen_gui --selfcheck`） |
| `load` | 读取并校验配置，打印摘要与编译宏 |
| `validate` | 跑校验层，逐条打印问题 |
| `generate` | 生成代码（含反编造自检，可选编译验证） |
| `compile` | 校验已生成目录里的代码能否编译 |

### 退出码契约（可当 CI 门禁）

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 文件不存在 / 格式错误 / 编译器缺失 |
| `2` | 配置校验有阻断性错误，生成代码含编造符号，或编译失败 |

```powershell
python -m stm32gen --version
python -m stm32gen selftest

python -m stm32gen load examples\demo.json
python -m stm32gen validate examples\demo.json
python -m stm32gen validate examples\demo.json --no-info    # 只看错误与警告

# 生成（默认写入同级目录 stm32-init-gen-output\）
python -m stm32gen generate examples\demo.json
python -m stm32gen generate examples\demo.json --compile   # 顺带编译验证
python -m stm32gen generate examples\demo.json --dry-run     # 只打印，不写盘
python -m stm32gen generate examples\demo.json --print       # 既写盘又打印
python -m stm32gen generate examples\demo.json --no-overwrite  # 跳过已存在文件

# 编译验证已有目录
python -m stm32gen compile examples\demo.json --mode keil

# 故意构造的冲突配置：会被拒绝生成（退出码 2）
python -m stm32gen generate examples\conflict.json

# 工具脚本
python tools\extract_symbols.py
python tools\check_no_invented.py ..\stm32-init-gen-output\
python tools\check_no_invented.py --selftest
```

### `generate` 的三个关键行为

**1. 校验有阻断性错误时拒绝生成。**

引脚冲突、NVIC 优先级越界这类错误必然产出不能用的代码，所以默认
直接拒绝（退出码 2），一个文件都不写。确需强行生成用 `--force`，
但会打印警告。

**2. 反编造自检默认开启。**

生成后立刻扫描产物里的每个标准库符号，发现编造的立即报错并返回 2。
这是「禁止编造」约束的最后一道防线，`--no-check` 可以关掉，
但等于放弃核心保证。

**3. `--no-overwrite` 是「跳过」而不是「报错终止」。**

遇到已存在文件时跳过并继续写其余的，最后报告「已写入 N 个 / 跳过 M 个」。
参数名的字面含义就是跳过——报错终止会逼用户逐个删文件才能继续。

> 内部有两套语义，别混淆：`GeneratedFile.write(overwrite=False)` 失败即抛
> `FileExistsError`（全有或全无，供 GUI 做覆盖确认），
> `GenerateResult.write_all(skip_existing=True)` 跳过并继续（供 CLI 增量补齐）。

## 测试

```powershell
python -m pytest tests\ -q
python -m ruff check src tools tests
```

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_symbols.py` | 73 | 符号索引：数值精确性、条件编译、结构体成员 |
| `test_generator.py` | 102 | 上下文构建、渲染、按外设分文件的产物结构、条件分支、反编造集成、**合并布局**（3 文件、单 guard、正文逐字节不变、形态变化时抛 MergeError） |
| `test_models.py` | 67 | 模型层：反编造、组合约束、芯片规格、别名解析 |
| `test_validate.py` | 70 | 八组校验规则的正例与反例（含 Flash 等待周期对比、引脚冲突按方向分级） |
| `test_peripherals.py` | 54 | EXTI / AFIO / TIM **模型层**：枚举派生属性、频率数学、组合约束、芯片规格、`used_ports` 与 `needs_afio_clock` 推导、PSC/ARR 反算、**外部时钟三来源与 ETR 预分频串联** |
| `test_peripherals_validate.py` | 41 | EXTI / AFIO / TIM 的**校验与生成**：SWJ_ERROR、同线冲突、共享通道合并、重映射归 AFIO、MOE、非默认引脚只警告、外部时钟频率未知只 INFO、ITR 级联提示 |
| `test_cli.py` | 47 | 退出码契约、拒绝生成、编译模式、覆盖策略、`--layout merged` 的产物与真编译 |
| `test_compile.py` | 35 | 诊断解析、命令构造、真实编译与错误检出 |
| `test_serializers.py` | 32 | JSON/YAML 往返、错误路径 |
| `test_baudrate.py` | 28 | 时钟树、BRR 复算、可表示性 |
| `test_tools.py` | 30 | 提取器与护栏本身（含 ST 惯例结构体变量名的豁免） |
| `test_keil.py` | 27 | AC5/AC6 参数与诊断差异、两套编译器各自的编译与错误检出 |

合计 **621 项，本机实测全部通过（0 跳过）**。

> 界面那侧的 157 项测试在 `stm32-init-gen-gui` 项目里（`tests/test_gui.py`），
> 用同一个 venv 单独跑：`cd ..\gui; python -m pytest -q`。
> 拆开后核心的测试不再需要 Qt，装 PySide6 与否都能跑。

### 三层验证，互相补充

生成代码的可用性由三道独立检查守着：

| 层 | 手段 | 能发现什么 | 发现不了什么 |
|---|---|---|---|
| 文本护栏 | `check_no_invented.py` 比对符号表 | 引用了库里不存在的符号 | 符号用错地方（参数类型、顺序） |
| 编译验证 | armclang / armcc / arm-gcc | 语法错误、类型不符、未声明标识符 | 逻辑错误、寄存器配置是否正确 |
| 校验层 | `validate()` | 引脚冲突、波特率误差、NVIC 越界 | 编译期能发现的语法问题 |

三者都不能省。`test_both_keil_compilers_detect_invented_symbol` 逐套编译器验证
「确实能抓到编造符号」——如果编译验证只是走过场（比如把错误吞掉），那条测试会失败。

### 环境相关的测试不依赖本机装了什么

凡是「有没有工具链」的测试都用 `monkeypatch` 伪造环境，而不是 `pytest.skip`：

- `test_selftest_reports_missing_toolchain` —— 替身掉 `find_toolchain` 模拟无编译器
- `test_compile_mode_arm_without_arm_toolchain` —— 只放行宿主 gcc
- `test_selftest_warns_when_only_host_available` —— 同上，验证降级警告

否则装了 Keil 之后这些测试全部跳过，等于白写。

> **替身的坑**：`find_toolchain(prefer=None)` 是默认调用形式，
> 守卫条件若写成 `if prefer is not None and prefer.is_native` 就拦不住它，
> 会原样返回 ARM 工具链，替身形同虚设。必须单独处理 `prefer is None` 分支。
> 这个 bug 让三条测试同时失效，是测试失败时报出来的。

### 界面测试的 offscreen 约定

界面测试（现在在 `stm32-init-gen-gui/tests/`）跑在 `QT_QPA_PLATFORM=offscreen`
下，并把所有模态对话框替换成空实现或「用户取消」。不设平台的话 `QMessageBox`
会进入自己的事件循环等用户点击，测试**永久挂住**而不是失败。

核心库的测试不需要这些 —— 本仓库的 `pytest` 不导入 PySide6，所以装不装界面
都能跑。

## 当前进度

- [x] 第 1 步：项目目录结构 + 依赖
- [x] 第 2 步：标准库符号提取 + 反编造护栏
- [x] 第 3 步：Pydantic 配置模型 + JSON/YAML 序列化
- [x] 第 4 步：校验层
- [x] 第 5 步：Jinja2 模板 + 生成器
- [x] 第 6 步：CLI generate + 退出码契约
- [x] 第 7 步：PySide6 GUI（**已拆出**为独立项目 `stm32-init-gen-gui`，见上）
- [x] 第 8 步：编译验证（Keil AC5/AC6 + arm-none-eabi-gcc + 宿主 gcc 降级）
- [x] 第 9 步：按外设拆分生成文件 + 生成 RCC/Flash 代码
      （`bsp_init` / `bsp_rcc` / `bsp_gpio` / `bsp_usart` / `bsp_nvic` / `bsp_it` / `main`，共 13 个文件；
      含 `tree.flash_latency_recommended` 对比 bug 的修复）
- [x] 第 10 步：EXTI / AFIO / TIM 三类外设
      （产物 13 → **19 个**；`bsp_afio` 承接全部重映射与 SWJ 释放并排在 GPIO 之前；
      模型含芯片规格校验，校验层新增 check_afio / check_exti / check_tim；
      生成代码通过 armclang + armcc 双编译器零警告）
