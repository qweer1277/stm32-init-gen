# 贡献指南

## 环境准备

```powershell
git clone <仓库地址> stm32-init-gen
cd stm32-init-gen\core

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-dev.txt
python -m pip install -e .
```

界面部分共用同一个 venv：

```powershell
python -m pip install -e ..\gui
```

需要编译验证时才设置：

```powershell
$env:STM32_STDLIB_ROOT = 'D:\path\to\STM32F10x_StdPeriph_Lib'
```

## 改代码前必读

### 1. 不许编造符号

生成的每一个函数、宏、枚举都必须能在标准库源码里找到。新增外设支持时，
先确认符号表中确实有它：

```powershell
python -c "from stm32gen.stdlib_ref import default_index; \
           print(default_index().has_any('TIM_ETRClockMode1Config'))"
```

符号表由 `tools\extract_symbols.py` 从库源码提取（需要
`STM32_STDLIB_ROOT`）；日常开发直接用已固化在仓库里的那份即可。

护栏在 `core\src\stm32gen\stdlib_ref\guard.py`，它拦下所有 `LIB_PREFIXES`
开头但不在 `symbols.json` 里的标识符。

有一个例外名单：ST 惯例的结构体变量名（`GPIO_InitStructure` /
`USART_InitStructure` / `NVIC_InitStructure` / `EXTI_InitStructure` /
`TIM_TimeBaseStructure` / `TIM_OCInitStructure`）是**刻意**闭集豁免的。
不要把它改成 `*_InitStructure` 后缀正则 —— 那样 `GPIO_WhateverInitStructure`
也会漏过去，护栏就废了。

### 2. 模板只做排版，不做判断

`core\src\stm32gen\templates\*.j2` 里只允许 `{{ 变量 }}`、`{% for %}`、
`{% if ctx.has_xxx %}`。所有计算在 `generator\context.py`。

理由：Jinja2 里写逻辑很容易写出「看着对、边界错」的代码，而且难以单测。
放 Python 里能直接断言。

### 3. 注释写「为什么」，不写「什么」

好：`/* 先配重映射再配 GPIO：MAPR 必须在 GPIO_Init 之前生效，
        否则引脚按旧映射初始化，PWM 引脚上不会有波形 */`

不好：`/* 配置重映射 */`

也不要写没有核对过的数据手册章节号。

### 4. 有界等待

任何等硬件标志的循环都必须有超时或 guard 计数器。晶振不起振、外设
不响应时不能死等。

## 提交前

```powershell
# 两边的测试与 lint 都要绿
cd core;  python -m pytest -q;  python -m ruff check src tools tests
cd ..\gui; python -m pytest -q; python -m ruff check src tests
```

测试分两类：

- **不依赖标准库源码**（绝大多数）—— 生成、校验、模型层测试，任何机器都能跑
- **依赖标准库源码**（`test_compile.py`、`test_keil.py` 等）——
  没设 `STM32_STDLIB_ROOT` 时自动 skip

新增测试时想清楚属于哪类。如果测的是「生成结果」，它不该依赖标准库。

## 新增一个外设支持的完整流程

以「加 TIM 外部时钟」为例，改动面是固定的六处：

1. **枚举** `core\src\stm32gen\models\enums.py` —— 值必须是真实库符号名，
   `SymbolBackedEnum` 在导入时校验
2. **模型** `core\src\stm32gen\models\<外设>.py` —— 字段与校验
3. **校验** `core\src\stm32gen\validate\rules.py` —— 新增 `check_xxx` 并加进
   `ALL_RULES`
4. **上下文** `core\src\stm32gen\generator\context.py` —— 把模型算成模板要的
   数据，`_collect_symbols()` 里登记用到的库符号
5. **模板 + OUTPUT_SPECS** —— 新增 `.j2`，在 `render.py` 的 `OUTPUT_SPECS`
   里注册（注意 `merge.py` 的 `SOURCE_ORDER` / `HEADER_ORDER` 也要同步）
6. **测试** —— 模型层、校验层、生成层各一组

界面那边（`gui\src\stm32gen_gui\`）另加 `bridge.py` 的表单类与选项函数、
`main_window.py` 的页签与处理函数。

## 提交信息

```
<范围>: <做了什么>

<为什么这么做>（可选，但改动有取舍时强烈建议写）
```

范围示例：`models`、`validate`、`generator`、`templates`、`gui`、`docs`、`ci`。

## 报告问题

请附上：

- 配置文件（脱敏后）
- `python -m stm32gen selftest` 的输出
- 期望行为与实际行为

生成代码的问题请说明芯片型号与标准库版本。
