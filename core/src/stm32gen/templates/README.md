# Jinja2 模板

**按外设分文件**：每个外设一个 `.h`/`.c` 对，加上聚合入口与中断骨架，共 **19 个产物**。

另有 `--layout merged` 把 19 个产物合并成 `bsp.h` / `bsp.c` / `main.c`。合并
**不在这里做**，模板只有下面这一套：`generator/merge.py` 在渲染后剥掉文件级
脚手架再拼接。所以加外设只需加一对模板，两种布局自动同时受益。

| 模板 | 产物 | 内容 |
|---|---|---|
| `bsp_init.h.j2` | `bsp_init.h` | 总入口声明，并 `#include` 各外设头文件 |
| `bsp_init.c.j2` | `bsp_init.c` | 按顺序调用各外设 init 的编排（不含外设细节） |
| `bsp_rcc.h.j2` | `bsp_rcc.h` | 系统时钟接口 + `BSP_xxx_HZ` 频率常量 |
| `bsp_rcc.c.j2` | `bsp_rcc.c` | Flash 等待周期、HSE/PLL/总线分频、SYSCLK 切换 |
| `bsp_afio.h.j2` | `bsp_afio.h` | AFIO 接口（重映射 + SWJ 释放） |
| `bsp_afio.c.j2` | `bsp_afio.c` | AFIO 时钟、**全部引脚重映射**（USART + TIM）、SWJ 释放 |
| `bsp_gpio.h.j2` | `bsp_gpio.h` | GPIO 接口 + 带标签引脚的 `BSP_xxx_PORT`/`BSP_xxx_PIN` 宏 |
| `bsp_gpio.c.j2` | `bsp_gpio.c` | 端口时钟与全部 `GPIO_Init`（含 USART/EXTI/TIM 的引脚） |
| `bsp_exti.h.j2` | `bsp_exti.h` | EXTI 接口 |
| `bsp_exti.c.j2` | `bsp_exti.c` | 外部中断线选择（`GPIO_EXTILineConfig`）与边沿配置 |
| `bsp_usart.h.j2` | `bsp_usart.h` | USART 接口 |
| `bsp_usart.c.j2` | `bsp_usart.c` | 实例时钟、`USART_Init`、中断使能、`USART_Cmd` |
| `bsp_tim.h.j2` | `bsp_tim.h` | TIM 接口 |
| `bsp_tim.c.j2` | `bsp_tim.c` | 时基、输出比较（每通道 `TIM_OCxInit`）、预装载、高级定时器主输出 |
| `bsp_nvic.h.j2` | `bsp_nvic.h` | NVIC 接口 |
| `bsp_nvic.c.j2` | `bsp_nvic.c` | 优先级分组与各通道 `NVIC_Init` |
| `bsp_it.h.j2` | `bsp_it.h` | 中断服务函数声明 |
| `bsp_it.c.j2` | `bsp_it.c` | USART / EXTI / TIM 中断服务函数骨架 |
| `main.c.j2` | `main.c` | 最小可编译的 main，调用 `bsp_init()` |

> **重映射为什么在 `bsp_afio.c` 而不是各外设文件里**：
> `GPIO_PinRemapConfig` 写的是 `AFIO->MAPR`，且必须在 `GPIO_Init` **之前**生效。
> `bsp_afio_init()` 由 `bsp_init()` 排在 `bsp_gpio_init()` 之前调用，所以放这里
> 能保证顺序。若放进 `bsp_tim.c`，它会在 GPIO 初始化之后才跑 —— 引脚已按旧映射
> 配成复用输出，改映射来源后那个脚不再是定时器的输出脚，现象是"PWM 没有波形"。

## 铁律：模板只做排版，不做判断

模板里**只允许** `{{ 变量 }}` 与 `{% for %}` / `{% if ctx.has_xxx %}`。
所有计算（时钟分组、波特率复算、引脚推导、中断清标志方式、RCC 字段映射）都在
`generator/context.py` 里完成。

好处：

- 生成逻辑可以用 Python 单测覆盖，变量拼错、条件写反都在那里暴露
- 模板保持可读，改样式不会碰逻辑
- `StrictUndefined` 会让引用不存在的变量直接报错，而不是静默渲染成空字符串
  （C 代码里的静默空值会产出语法错误，且很难定位）

## 加新外设的步骤

1. 在 `templates/` 下加 `bsp_xxx.h.j2` 与 `bsp_xxx.c.j2`
2. 在 `render.py` 的 `OUTPUT_SPECS` 里加两行（模板名、输出文件名、说明）
3. 在 `context.py` 里加对应的 dataclass 与 `_build_xxx()` 函数，
   并在 `TemplateContext` 上加字段
4. 把新符号加进 `_collect_symbols()`——**否则反编造护栏会失去意义**
5. 在 `bsp_init.c.j2` 里加上 `bsp_xxx_init()` 调用（含顺序理由的注释），
   在 `bsp_init.h.j2` 里加上 `#include`
6. 在 `tests/test_generator.py` 里加断言；`test_symbols_used_covers_generated_code`
   会反向核对符号收集的完整性

## 环境配置的三个选择

```python
Environment(
    undefined=StrictUndefined,       # 变量缺失直接报错
    trim_blocks=True,                # {% %} 独占一行时不留下空行
    lstrip_blocks=True,              # 同上，去掉行首缩进
    keep_trailing_newline=True,      # 文件末尾保留换行
    autoescape=False,                # 生成 C 代码，不需要 HTML 转义
)
```

`trim_blocks` + `lstrip_blocks` 是必需的：C 代码对空行敏感，
不这样配会生成大量空白行。

## 写模板时的一个坑

**注释里的编号别用 `1)` 这种半括号**：`test_all_generated_files_have_matching_parens`
按字符数括号配平，一个只有 `)` 没有 `(` 的编号注释会让测试失败。
用 `(1)` 或 `步骤一`。这个坑在拆分时踩过一次。

## 生成的代码里几个体现「为什么」的地方

模板不只是拼字符串，几处关键顺序与写法都有理由，注释里写明了：

**1. `bsp_init()` 的调用顺序：时钟 → GPIO → USART → NVIC**

时钟必须最先：外设寄存器写操作依赖总线时钟，没配好时后续配置全部白写
——这是最常见的「配了没用」原因。NVIC 放最后：通道一开中断立刻可以进，
此时外设已配置完毕，不会出现「中断进了但外设还没配好」的窗口。

**2. `bsp_rcc.c` 内部的顺序：Flash → HSE/PLL → 总线分频 → SYSCLK 切换**

Flash 等待周期必须在提高 HCLK **之前**设好。反过来那段窗口里 HCLK 已拉高
而 Flash 还没加等待周期，取指不稳，是「偶发跑飞」的经典成因。
总线分频也必须在切 SYSCLK 之前设好，否则中间那段时间挂在总线上的外设超频运行。

**3. 等待硬件标志的循环都带上限**（code-standards R4.3）

`bsp_rcc.c` 用 `BSP_RCC_WAIT_LOOPS`（`0x4000u`）作上限。晶振不起振或 PLL
锁不住时退回默认时钟，而不是卡死在启动阶段——现场表现是「上电没反应」，
比「主频不对」难查得多。HSE 则直接用库自带超时的 `RCC_WaitForHSEStartUp()`。

**4. 总线分频的三套宏不能混用**

```
RCC_HCLKConfig()  用 RCC_SYSCLK_Div*   （HPRE  bit[7:4]）
RCC_PCLK1Config() 用 RCC_HCLK_Div*     （PPRE1 bit[10:8]）
RCC_PCLK2Config() 用 RCC_HCLK_Div*     （PPRE2 bit[13:11]）
```

喂错宏库不会报错（`assert_param` 用的就是对应那一套），只会静默写坏寄存器位。
见 `models/rcc.py` 的分组说明。

**5. 初始电平必须在 `GPIO_Init` 之前写**（在 `bsp_gpio.c` 里）

```c
GPIO_SetBits(GPIOC, GPIO_Pin_13);   /* 先写 ODR */
GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_PP;
GPIO_Init(GPIOC, &GPIO_InitStructure);   /* 再切输出模式 */
```

`GPIO_SetBits` 写的是 ODR，对输入模式引脚同样有效。先写好 ODR 再切到
输出模式，切换瞬间输出即为目标电平。反过来（先 `GPIO_Init` 再设电平），
配置成输出的那一刻 ODR 还是复位值，引脚会先抖一下。

**6. 重映射必须在 `GPIO_Init` 之前配置**

重映射改的是引脚的复用功能来源，配好之后再设引脚模式才有意义。
前提是 AFIO 时钟已使能（`context.py` 在有重映射时自动加入，归 GPIO 模块）。

**7. 中断清标志方式按中断源区分**

各中断源的清标志方式不同，且**不能混用**：

| 中断 | 清标志方式 | 依据 |
|---|---|---|
| `RXNE` | 读 DR | `IS_USART_CLEAR_IT` 含它 |
| `TC` | `USART_ClearITPendingBit` | `IS_USART_CLEAR_IT` 含它 |
| **`IDLE`** | **先读 SR 再读 DR** | `IS_USART_CLEAR_IT` **不含**它 |
| **`PE`** | **先读 SR 再读 DR** | `IS_USART_CLEAR_IT` **不含**它 |
| `TXE` | 只能靠写 DR | 无清除 API |

`IS_USART_CLEAR_IT` 的定义在 `Library\stm32f10x_usart.h:256-257`，
只允许 `TC` / `RXNE` / `LBD` / `CTS`。对 `IDLE` 调
`USART_ClearITPendingBit` 会触发 `assert_param` 失败，且标志实际清不掉
——中断会一直重入。

这条依据来自 `Library\stm32f10x_usart.c:1003-1011` 的 `@note`，
有回归测试守着（`test_idle_does_not_use_clear_it_pending_bit`）。

**8. 中断函数名与启动文件向量表一致**

`USART1_IRQHandler` 这类名字必须与
`Start\startup_stm32f10x_md.s` 向量表及 `EXPORT ... [WEAK]` 段里的符号
完全一致，否则链接时不会覆盖弱定义，中断进了也执行不到。
