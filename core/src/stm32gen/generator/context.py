"""把配置模型转换成 Jinja2 模板上下文。

设计原则：**模板只做排版，不做判断**。所有条件分支、分组、预计算都在本模块
完成，模板里只有 ``{{ 变量 }}`` 和 ``{% for %}``。

这样做的好处：生成逻辑可以用 Python 单测覆盖（模板语法错误、变量拼错都在
这里暴露），模板保持可读，改样式不会碰逻辑。

产物结构（按外设分文件）
------------------------

    bsp_init.h/c     聚合入口：bsp_init() 依次调用下面四个
    bsp_rcc.h/c      系统时钟：HSE/PLL/SYSCLK/总线分频 + Flash 等待周期与预取
    bsp_gpio.h/c     GPIO：端口时钟、AFIO、重映射、全部 GPIO_Init、引脚宏
    bsp_usart.h/c    USART：实例时钟、USART_Init、中断使能、USART_Cmd
    bsp_nvic.h/c     NVIC：优先级分组 + 各通道 NVIC_Init
    bsp_it.h/c       中断服务函数骨架
    main.c           最小可编译的 main

**时钟使能按归属拆到各模块**，不集中在一个 ``bsp_clock_enable()`` 里：

- GPIO 端口时钟与 AFIO 时钟归 ``bsp_gpio``
- USART 实例时钟归 ``bsp_usart``

理由是维护成本：删掉某个外设时，它的时钟使能跟着同一个文件一起消失，
不会在聚合文件里留下一句「使能了已删除外设的时钟」。代价是各模块
自己负责开关自己的时钟，需在注释里写明。

**USART 的 TX/RX 引脚仍在 ``bsp_gpio`` 里配置**（与拆分前一致）：
引脚模式是 GPIO 的职责，同一引脚只在一处配置，避免两处 ``GPIO_Init``
同一引脚而模式不同的隐患。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from stm32gen.generator.layout import DEFAULT_LAYOUT, OutputLayout
from stm32gen.models import Stm32Config
from stm32gen.models.enums import (
    GpioMode,
    GpioSpeed,
    TimRemap,
    UsartInterrupt,
    UsartRemap,
)
from stm32gen.validate.baudrate import compute_clock_tree, evaluate_baudrate
from stm32gen.validate.pins import effective_usart_pins

# 生成代码的统一缩进（与 code-standards 的 4 空格一致）
INDENT = "    "


# --------------------------------------------------------------------------
# 上下文片段
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockEnable:
    """一条时钟使能语句。

    拆分后每个外设模块自带自己的时钟使能，所以这里的 ``macros`` 通常
    只包含同一模块的外设（GPIO 模块含端口与 AFIO，USART 模块含实例）。
    """

    bus: str  # "APB1" 或 "APB2"
    function: str  # "RCC_APB2PeriphClockCmd"
    macros: tuple[str, ...]  # 要位或的时钟宏
    comment: str = ""  # 为什么要使能这些时钟

    @property
    def expression(self) -> str:
        """拼成 ``A | B | C`` 形式。"""
        return " | ".join(self.macros)

    @property
    def c_line(self) -> str:
        """完整的 C 语句。"""
        return f"{self.function}({self.expression}, ENABLE);"


@dataclass(frozen=True)
class RccSetup:
    """系统时钟初始化（``bsp_rcc.c``）的全部生成信息。

    字段名与 ``RCC_*Config()`` 的参数一一对应，模板不需要做任何映射。

    这里把「配置顺序」也编码成了字段组合：``bsp_rcc.c.j2`` 按
    Flash → 时钟源 → PLL → 总线分频 → SYSCLK 切换 的顺序渲染，
    顺序不可调换的理由写在各自注释里（见模板）。
    """

    # 是否生成真正的时钟配置。False 表示沿用 SystemInit() 的配置。
    enabled: bool = True

    # ── Flash：必须在提高 HCLK 之前设好 ──
    flash_latency: str = "FLASH_Latency_2"  # 生效值
    flash_latency_ws: int = 2  # 生效值的数值，写进注释便于核对
    flash_latency_recommended_ws: int = 2  # 按时钟推导的推荐值
    flash_latency_is_override: bool = False  # 是否来自用户覆盖
    prefetch: str = "FLASH_PrefetchBuffer_Enable"
    prefetch_enabled: bool = True

    # ── 时钟源 ──
    uses_hse: bool = True
    hse_state: str = "RCC_HSE_ON"
    # 用 HSI 作 PLL 源时要显式使能 HSI 并等待稳定
    needs_hsi: bool = False

    # ── PLL ──
    needs_pll: bool = True
    pll_source: str = "RCC_PLLSource_HSE_Div1"
    pll_multiplier: str = "RCC_PLLMul_9"
    pll_factor: int = 9

    # ── 总线分频：三套宏的位域不同，绝不能混用 ──
    ahb_prescaler: str = "RCC_SYSCLK_Div1"  # HPRE，用 RCC_SYSCLK_Div*
    apb1_prescaler: str = "RCC_HCLK_Div2"  # PPRE1，用 RCC_HCLK_Div*
    apb2_prescaler: str = "RCC_HCLK_Div1"  # PPRE2，用 RCC_HCLK_Div*

    # ── SYSCLK 切换 ──
    sysclk_source: str = "RCC_SYSCLKSource_PLLCLK"
    # 切换完成后 SWS 位应有的值（0x00 HSI / 0x04 HSE / 0x08 PLL），
    # 用于生成等待循环。见 models/rcc.py 的 RccSysclkSource.sws_value。
    sysclk_sws: int = 0x08

    # ── 注释里用的推导值 ──
    pll_input_mhz: str = "8"
    sysclk_mhz: str = "72"
    hclk_mhz: str = "72"
    pclk1_mhz: str = "36"
    pclk2_mhz: str = "72"
    hse_mhz: str = "8"

    # ── 头文件里的频率常量（Hz）。业务代码算定时周期/超时要它们 ──
    sysclk_hz: int = 72_000_000
    hclk_hz: int = 72_000_000
    pclk1_hz: int = 36_000_000
    pclk2_hz: int = 72_000_000
    # 定时器时钟：APB 分频系数 != 1 时，定时器时钟是 PCLK 的 2 倍。
    # 这极易算错：36MHz 的 PCLK1 对应 72MHz 的 TIM2 时钟，算定时周期差一倍。
    tim1_hz: int = 72_000_000
    tim234_hz: int = 72_000_000



@dataclass(frozen=True)
class GpioConfig:
    """一次 ``GPIO_Init`` 调用所需的全部信息。"""

    port: str  # "GPIOA"
    pin_macro: str  # "GPIO_Pin_9" 或 "GPIO_Pin_9 | GPIO_Pin_10"
    mode: str  # "GPIO_Mode_AF_PP"
    speed: str  # "GPIO_Speed_50MHz"
    comment: str  # 生成的注释，说明这条配置是干什么的
    # 初始电平；None 表示输入模式，不需要 GPIO_SetBits/ResetBits
    init_level: str | None = None

    @property
    def needs_level_init(self) -> bool:
        """是否需要生成初始电平设置。"""
        return self.init_level is not None


@dataclass(frozen=True)
class NamedPin:
    """带标签的引脚，会生成 ``BSP_xxx_PORT`` / ``BSP_xxx_PIN`` 宏。

    只有用户填了 ``label`` 的引脚才生成 —— 没有语义名字的引脚给宏
    反而会让头文件变噪音。
    """

    macro_name: str  # "BSP_LED1"
    port: str  # "GPIOC"
    pin_macro: str  # "GPIO_Pin_13"
    comment: str  # 原始标签与引脚名


@dataclass(frozen=True)
class PinRemap:
    """一条引脚重映射配置。

    重映射必须在 ``GPIO_Init`` **之前**配置：它改的是引脚的复用功能来源，
    配好之后再设引脚模式才有意义。前提是 AFIO 时钟已使能。
    """

    remap: str  # "GPIO_Remap_USART1"
    comment: str  # "USART1 → PB6/PB7"


@dataclass(frozen=True)
class UsartInterruptHandler:
    """一个 USART 中断服务函数的生成信息。"""

    instance: str  # "USART1"
    handler: str  # "USART1_IRQHandler"
    irqn: str  # "USART1_IRQn"
    # 每个中断的检查片段：[(中断宏, 清标志方式, 说明)]
    checks: tuple[InterruptCheck, ...]

    @property
    def is_multi(self) -> bool:
        """是否检查多个中断源。"""
        return len(self.checks) > 1


@dataclass(frozen=True)
class InterruptCheck:
    """中断服务函数里的一个中断源分支。"""

    it_macro: str  # "USART_IT_RXNE"
    name: str  # "RXNE"
    # 清标志语句；空字符串表示由用户的业务代码负责
    clear_stmt: str
    # 清标志的说明（写进注释，解释为什么这样清）
    clear_note: str
    # 该中断的典型用法提示
    hint: str


@dataclass(frozen=True)
class UsartConfig:
    """一个 USART 实例的生成信息。"""

    instance: str
    clock_macro: str
    baudrate: int
    brr_hex: str
    baud_actual: str
    baud_error: str
    word_length: str
    stop_bits: str
    parity: str
    mode_expression: str
    flow_control: str
    interrupts: tuple[str, ...]
    remap: str
    label: str
    # 是否已使能（生成 USART_Cmd 调用）
    enable: bool = True

    @property
    def has_interrupts(self) -> bool:
        return bool(self.interrupts)


@dataclass(frozen=True)
class NvicChannel:
    """一个 NVIC 通道的生成信息。"""

    irq_channel: str
    preemption: int
    sub: int
    comment: str


# --------------------------------------------------------------------------
# AFIO / EXTI / TIM 的上下文片段
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AfioSetup:
    """``bsp_afio.c`` 的生成信息。

    AFIO 在标准库里是两件功能的载体，本上下文**两件都管**：

    1. **引脚重映射**（``AFIO->MAPR``）—— USART 与 TIM 的重映射都在这里。
       放这里而不是各外设文件里的理由是**执行顺序**：重映射必须在
       ``GPIO_Init`` 之前生效。若把 TIM 重映射放在 ``bsp_tim.c``，它会
       在 ``bsp_gpio_init()`` 之后才跑 —— 引脚先被配成复用推挽输出，
       再改映射来源，结果是"配的引脚不再是 TIM 的输出脚"（表现为无波形）。
       ``bsp_afio_init()`` 排在 ``bsp_gpio_init()`` 之前，两个问题一起解决。
    2. **SWJ 调试口释放** —— 把 PA13/PA14/PA15/PB3/PB4 抢回来当普通 GPIO。

    ``enabled`` 为假时模板生成空实现，保持 ``bsp_init()`` 的调用序列一致。
    """

    enabled: bool = False
    # AFIO 时钟使能（APB2）。AFIO 的任何寄存器操作前都必须先开它，
    # 而 GPIO_EXTILineConfig / GPIO_PinRemapConfig 都直接写 AFIO 寄存器。
    clock: ClockEnable | None = None
    # 全部引脚重映射（USART + TIM），顺序即生成顺序
    remaps: tuple[PinRemap, ...] = ()
    # SWJ 释放
    swj_remap: str = ""  # "GPIO_Remap_SWJ_JTAGDisable"
    swj_name: str = ""  # "JTAG_DISABLE"
    releases: tuple[str, ...] = ()  # 释放出来的引脚名
    keeps_swd: bool = True  # 是否保留 SWD 下载能力
    bricks_debugger: bool = False  # 是否会导致再也连不上调试器

    @property
    def has_remaps(self) -> bool:
        return bool(self.remaps)

    @property
    def has_swj(self) -> bool:
        return bool(self.swj_remap)

    @property
    def has_clock(self) -> bool:
        return self.clock is not None

    @property
    def has_content(self) -> bool:
        """是否有实际内容要生成。"""
        return self.has_clock or self.has_remaps or self.has_swj


@dataclass(frozen=True)
class ExtiLine:
    """一条 EXTI 线的生成信息。"""

    line: int
    line_macro: str  # "EXTI_Line0"
    pin_name: str  # "PA0"
    port_source: str  # "GPIO_PortSourceGPIOA"
    pin_source: str  # "GPIO_PinSource0"
    mode: str  # "EXTI_Mode_Interrupt"
    trigger: str  # "EXTI_Trigger_Falling"
    trigger_label: str  # "下降沿"
    irqn: str  # "EXTI0_IRQn"
    label: str = ""
    needs_nvic: bool = True


@dataclass(frozen=True)
class ExtiHandler:
    """一个 EXTI 中断服务函数的生成信息。

    **5~9 共用一个通道、10~15 共用一个通道**，所以一个 handler 里
    可能有多条线 —— 这正是 ``lines`` 是元组而不是单值的原因。
    ISR 里必须逐个判标志位才知道是哪条线触发的。
    """

    handler: str  # "EXTI9_5_IRQHandler"
    irqn: str  # "EXTI9_5_IRQn"
    lines: tuple[ExtiLine, ...]

    @property
    def is_shared(self) -> bool:
        """是否是多条线共享的通道（ISR 需要有多个分支）。"""
        return len(self.lines) > 1

    @property
    def description(self) -> str:
        """通道覆盖的线号说明，如 ``EXTI5/EXTI6``。"""
        return "/".join(item.line_macro.replace("EXTI_Line", "EXTI") for item in self.lines)


@dataclass(frozen=True)
class TimChannelInfo:
    """一个 TIM 输出比较通道的生成信息。"""

    channel: str  # "TIM_Channel_1"
    number: int  # 1
    init_function: str  # "TIM_OC1Init"
    preload_function: str  # "TIM_OC1PreloadConfig"
    mode: str  # "TIM_OCMode_PWM1"
    pulse: int  # 500
    polarity: str  # "TIM_OCPolarity_High"
    preload: str  # "TIM_OCPreload_Enable"
    output_state: str  # "TIM_OutputState_Enable"
    is_pwm: bool  # 是否 PWM 模式
    duty_percent: str  # "50.00"（字符串，直接进注释）
    pin_name: str = ""  # "PA6"，未指定引脚时为空
    pin_port: str = ""  # "GPIOA"
    pin_macro: str = ""  # "GPIO_Pin_6"
    label: str = ""

    @property
    def needs_gpio(self) -> bool:
        return bool(self.pin_name)


@dataclass(frozen=True)
class TimConfig:
    """一个 TIM 实例的生成信息。"""

    instance: str  # "TIM3"
    clock_macro: str  # "RCC_APB1Periph_TIM3"
    prescaler: int
    period: int
    counter_mode: str  # "TIM_CounterMode_Up"
    clock_division: str  # "TIM_CKD_DIV1"
    repetition_counter: int
    is_advanced: bool  # TIM1 才有 BDTR/MOE/RCR
    channels: tuple[TimChannelInfo, ...]
    interrupts: tuple[str, ...]  # ("TIM_IT_Update",)
    remap: str  # "" 或 "GPIO_PartialRemap_TIM3"
    irqn: str  # "TIM3_IRQn"
    label: str = ""
    # 频率推导（写进注释供核对）
    timer_clock_mhz: str = ""
    counter_hz: str = ""
    update_hz: str = ""
    frequency_note: str = ""
    # 时钟源。内部时钟时 clock_function 为空串，模板不生成时钟源配置语句。
    clock_source: str = ""  # "" 或 "TIM_ETRClockMode1Config" 等
    clock_argument: str = ""  # 如 "TIM_ExtTRGPSC_DIV2, TIM_ExtTRGPolarity_NonInverted, 0"
    external_clock: bool = False
    external_clock_hz: float | None = None
    # 外部时钟配置的参数里用到的符号（ETR 预分频/极性宏、TI 源宏等），
    # 供 _collect_symbols 登记，避免被反编造护栏误判。
    clock_symbols: tuple[str, ...] = ()

    @property
    def has_interrupts(self) -> bool:
        return bool(self.interrupts)

    @property
    def has_channels(self) -> bool:
        return bool(self.channels)

    @property
    def has_pwm(self) -> bool:
        """是否有 PWM 通道 —— 只有这时才需要 ``TIM_CtrlPWMOutputs``。"""
        return any(item.is_pwm for item in self.channels)

    @property
    def needs_gpio(self) -> bool:
        """是否有通道指定了引脚。"""
        return any(item.needs_gpio for item in self.channels)

    @property
    def has_clock_config(self) -> bool:
        """是否需要生成时钟源配置语句（外部时钟才需要）。"""
        return bool(self.clock_source)


@dataclass(frozen=True)
class AdcChannelInfo:
    """ADC 规则组序列里一条通道的生成信息。"""

    channel: str  # "ADC_Channel_3"
    channel_label: str  # "通道 3"
    rank: int  # 1~16，即序列中的位置
    sample_time: str  # "ADC_SampleTime_55Cycles5"
    sample_cycles: str  # "55.5"（字符串，直接进注释）
    is_internal: bool  # 温度传感器 / Vrefint，不占引脚
    pin_name: str = ""  # "PA3"，未指定引脚时为空
    pin_port: str = ""  # "GPIOA"
    pin_macro: str = ""  # "GPIO_Pin_3"
    label: str = ""

    @property
    def needs_gpio(self) -> bool:
        return bool(self.pin_name)


@dataclass(frozen=True)
class AdcConfig:
    """一个 ADC 实例的生成信息（``bsp_adc.c``）。

    字段全是字符串/数值/布尔，模板拿到的就是最终要写进 C 代码的值。
    """

    instance: str  # "ADC1"
    clock_macro: str  # "RCC_APB2Periph_ADC1"
    mode: str  # "ADC_Mode_Independent"
    mode_name: str  # "INDEPENDENT"
    prescaler: str  # "RCC_PCLK2_Div6"
    prescaler_label: str  # "PCLK2 ÷6"
    # ADC_Init 的四个字段
    scan_mode: bool
    continuous: bool
    data_align: str  # "ADC_DataAlign_Right"
    external_trigger: str  # "ADC_ExternalTrigConv_None"
    sequence_length: int  # ADC_NbrOfChannel
    channels: tuple[AdcChannelInfo, ...]
    interrupts: tuple[str, ...]  # ("ADC_IT_EOC",)
    irqn: str  # "ADC1_2_IRQn"
    label: str = ""

    # 模拟看门狗
    watchdog: bool = False
    watchdog_high: int = 0xFFF
    watchdog_low: int = 0
    # 看门狗监控的通道宏。必须单独给出：不调 SingleChannelConfig 时库按通道 0 处理
    watchdog_channel: str = ""
    # 校准
    calibration: bool = True
    calibration_timeout_loops: int = 0x1000
    # 软件触发需要生成 ADC_SoftwareStartConvCmd
    software_trigger: bool = False
    # 频率推导（进注释供核对）
    clock_note: str = ""
    # 中断标志宏（ISR 清挂起位与判标志用），与 interrupts 一一对应
    interrupt_flags: tuple[str, ...] = ()

    @property
    def has_channels(self) -> bool:
        return bool(self.channels)

    @property
    def has_interrupts(self) -> bool:
        return bool(self.interrupts)

    @property
    def needs_gpio(self) -> bool:
        """是否有通道指定了引脚。"""
        return any(item.needs_gpio for item in self.channels)

    @property
    def needs_calibration(self) -> bool:
        return self.calibration


@dataclass(frozen=True)
class AdcHandler:
    """一个 ADC 中断服务函数的生成信息。

    **ADC1 与 ADC2 共用 ``ADC1_2_IRQn``**，所以正常情况下
    ``instances`` 会有 1~2 项。生成**一个** handler、内部按实例分派 ——
    与 ``ExtiHandler`` 处理共享通道的方式一致：生成两个同名函数会
    直接重复定义编译失败。
    """

    handler: str  # "ADC1_2_IRQHandler"
    irqn: str  # "ADC1_2_IRQn"
    instances: tuple[AdcConfig, ...]

    @property
    def is_shared(self) -> bool:
        """是否多个实例共享（ISR 需要按实例分派）。"""
        return len(self.instances) > 1

    @property
    def description(self) -> str:
        """覆盖的实例名，如 ``ADC1/ADC2``。"""
        return "/".join(item.instance for item in self.instances)


@dataclass
class TemplateContext:
    """渲染模板用的完整上下文。"""

    project_name: str
    chip: str
    device_macro: str
    hse_hz: int
    hse_mhz: str
    c_flags: tuple[str, ...]
    clock_tree_note: str

    # 系统时钟（bsp_rcc.h/c）
    rcc: RccSetup = field(default_factory=RccSetup)

    # GPIO（bsp_gpio.h/c）
    gpio_clock: ClockEnable | None = None
    gpio_configs: list[GpioConfig] = field(default_factory=list)
    named_pins: list[NamedPin] = field(default_factory=list)

    # USART（bsp_usart.h/c）
    # 一个列表而不是单个：USART1 在 APB2、USART2/3 在 APB1，
    # 而「调试口用 USART1 + 业务口用 USART2」是常见组合，
    # 两条总线必须各生成一条语句。按 APB2 → APB1 排序保证稳定。
    usart_clocks: list[ClockEnable] = field(default_factory=list)
    usart_configs: list[UsartConfig] = field(default_factory=list)

    # NVIC（bsp_nvic.h/c）
    nvic_group: str = ""
    nvic_group_note: str = ""
    nvic_channels: list[NvicChannel] = field(default_factory=list)

    # AFIO（bsp_afio.h/c）：SWJ 调试口释放
    afio: AfioSetup = field(default_factory=AfioSetup)

    # EXTI（bsp_exti.h/c）
    exti_lines: list[ExtiLine] = field(default_factory=list)
    exti_handlers: list[ExtiHandler] = field(default_factory=list)

    # TIM（bsp_tim.h/c）
    tim_clocks: list[ClockEnable] = field(default_factory=list)
    tim_configs: list[TimConfig] = field(default_factory=list)
    tim_remaps: list[PinRemap] = field(default_factory=list)

    # ADC（bsp_adc.h/c）
    # 全部分频一致时只有一条 RCC_ADCCLKConfig —— 校验层会保证这一点。
    adc_clock: ClockEnable | None = None
    adc_prescaler: str = ""
    adc_prescaler_label: str = ""
    adc_configs: list[AdcConfig] = field(default_factory=list)
    adc_handlers: list[AdcHandler] = field(default_factory=list)

    # 中断服务函数（bsp_it.h/c）
    handlers: list[UsartInterruptHandler] = field(default_factory=list)

    # 产物布局：模板据此省掉或保留文件级脚手架（include guard 等）。
    # 默认 SPLIT，保证不传该参数的调用方产物逐字节不变。
    layout: OutputLayout = DEFAULT_LAYOUT

    # 生成器引用到的标准库符号，供反编造护栏核对
    symbols_used: set[str] = field(default_factory=set)

    @property
    def merged(self) -> bool:
        """是否为合并布局。

        模板里用 ``{% if not ctx.merged %}`` 包住文件级脚手架：
        include guard、``extern "C"``、``#include "stm32f10x.h"``、
        自身头文件 include、版权尾注。合并后这些由聚合文件统一提供。
        """
        return self.layout.is_merged

    @property
    def has_rcc(self) -> bool:
        """是否需要 bsp_rcc 的内容。

        未生成 RCC 代码时 ``bsp_rcc.c`` 仍会产出（提供 ``bsp_rcc_init()``
        的说明与空实现），所以这里恒为真 —— 保留该属性是为了让模板
        与测试有统一的判断点。
        """
        return True

    @property
    def has_gpio(self) -> bool:
        return bool(self.gpio_configs)

    @property
    def has_usart(self) -> bool:
        return bool(self.usart_configs)

    @property
    def has_nvic(self) -> bool:
        return bool(self.nvic_channels)

    @property
    def has_afio(self) -> bool:
        """是否有 SWJ 释放配置。"""
        return self.afio.enabled and bool(self.afio.swj_remap)

    @property
    def has_exti(self) -> bool:
        return bool(self.exti_lines)

    @property
    def has_tim(self) -> bool:
        return bool(self.tim_configs)

    @property
    def has_adc(self) -> bool:
        return bool(self.adc_configs)

    @property
    def has_interrupts(self) -> bool:
        """是否有任何中断服务函数（USART / EXTI / TIM / ADC）。

        ``bsp_it.c`` 的整体空骨架判断用它 —— 只看 USART 的 handlers
        会让"只有 EXTI 中断"的配置生成一份空的中断文件。
        """
        return (
            bool(self.handlers or self.exti_handlers or self.adc_handlers)
            or any(item.has_interrupts for item in self.tim_configs)
        )

    @property
    def has_handlers(self) -> bool:
        return bool(self.handlers)

    @property
    def has_named_pins(self) -> bool:
        return bool(self.named_pins)

    @property
    def has_remaps(self) -> bool:
        return bool(self.remaps)

    @property
    def apb2_clock(self) -> ClockEnable | None:
        """GPIO 模块的 APB2 时钟（兼容旧断言：端口与 AFIO 都在这条里）。"""
        return self.gpio_clock

    @property
    def apb1_clock(self) -> ClockEnable | None:
        """USART 模块的 APB1 时钟。"""
        for item in self.usart_clocks:
            if item.bus == "APB1":
                return item
        return None

    @property
    def usart_apb2_clock(self) -> ClockEnable | None:
        """USART 模块的 APB2 时钟（USART1）。"""
        for item in self.usart_clocks:
            if item.bus == "APB2":
                return item
        return None



# --------------------------------------------------------------------------
# 中断清标志的规则
# --------------------------------------------------------------------------

# 各中断源的清标志方式。
#
# **关键正确性点**：库的 ``IS_USART_CLEAR_IT``（stm32f10x_usart.h:256-257）
# 只允许 TC / RXNE / LBD / CTS 用 ``USART_ClearITPendingBit``。
# IDLE 与 PE 的挂起位**必须**用「先读 SR 再读 DR」的软件序列清除
# （依据 stm32f10x_usart.c:1003-1011 的 @note）。
# 对 IDLE 调 ``USART_ClearITPendingBit`` 会触发 assert_param 失败，
# 且标志实际清不掉 —— 中断会一直重入。
_INTERRUPT_RULES: dict[UsartInterrupt, tuple[str, str, str]] = {
    UsartInterrupt.RXNE: (
        "(void)USART_ReceiveData({inst});",
        "RXNE 挂起位由读 USART_DR 清除（ReceiveData 内部即读 DR）",
        "把收到的字节取出并处理，注意 ISR 内只做最小必要工作",
    ),
    UsartInterrupt.IDLE: (
        "(void)USART_ReceiveData({inst});",
        "IDLE 挂起位必须用「先读 SR 再读 DR」清除，"
        "USART_ClearITPendingBit 不支持 IDLE（IS_USART_CLEAR_IT 不含它）；"
        "上面的 USART_GetITStatus 已读 SR，这里读 DR 即完成序列",
        "总线空闲，通常用于判定一帧结束（配合 DMA 或环形缓冲）",
    ),
    UsartInterrupt.TXE: (
        "/* TXE 挂起位由写 USART_DR 清除，请在下面填入发送逻辑 */",
        "TXE 挂起位只能通过写 USART_DR 清除",
        "发送数据寄存器已空，可以写入下一个字节",
    ),
    UsartInterrupt.TC: (
        "USART_ClearITPendingBit({inst}, USART_IT_TC);",
        "TC 挂起位可用 USART_ClearITPendingBit 清除",
        "整帧发送完成，通常用于切换 RS-485 收发方向",
    ),
    UsartInterrupt.PE: (
        "(void)USART_ReceiveData({inst});",
        "PE 挂起位必须用「先读 SR 再读 DR」清除，"
        "USART_ClearITPendingBit 不支持 PE",
        "校验错误，建议计数上报而不是静默忽略",
    ),
}


def _build_interrupt_checks(
    instance: str, interrupts: tuple[UsartInterrupt, ...]
) -> tuple[InterruptCheck, ...]:
    """构造中断检查片段。"""
    checks: list[InterruptCheck] = []
    for item in interrupts:
        clear_stmt, clear_note, hint = _INTERRUPT_RULES[item]
        checks.append(
            InterruptCheck(
                it_macro=item.value,
                name=item.name,
                clear_stmt=clear_stmt.format(inst=instance),
                clear_note=clear_note,
                hint=hint,
            )
        )
    return tuple(checks)


# --------------------------------------------------------------------------
# 上下文构建
# --------------------------------------------------------------------------


def _build_gpio_clock(config: Stm32Config) -> ClockEnable | None:
    """GPIO 模块自己的时钟使能（APB2）：仅端口时钟。

    **AFIO 时钟不在这里**：它由 ``bsp_afio.c`` 使能（那个模块要写
    ``AFIO->MAPR`` 做重映射与 SWJ 释放，且它排在 ``bsp_gpio_init()``
    之前）。早期版本把 AFIO 时钟放在这里，拆出 AFIO 模块后归位到
    ``_build_afio()``，避免两处都开同一条时钟。
    """
    macros: list[str] = []

    # GPIO 端口时钟：按字母排序，保证生成结果稳定（便于 diff 与快照测试）
    for letter in config.used_ports:
        macro = f"RCC_APB2Periph_GPIO{letter}"
        if macro not in macros:
            macros.append(macro)

    if not macros:
        return None

    return ClockEnable(
        bus="APB2",
        function="RCC_APB2PeriphClockCmd",
        macros=tuple(macros),
        comment="GPIO 端口",
    )


def _build_usart_clock(config: Stm32Config) -> ClockEnable | None:
    """USART 模块自己的时钟使能。

    USART1 在 APB2，USART2/3 在 APB1 —— 一次配置里可能两者都有，
    但一个 ``ClockEnable`` 只描述一条语句（一个总线），所以这里
    返回**主总线**上的那条，另一条由 ``usart_extra_clock`` 补。

    实际上同一配置里同时用 USART1 与 USART2/3 是常见的（调试口 + 业务口），
    所以不能只保留一条。见 ``build_context`` 里的组装方式。
    """
    macros: list[str] = []
    for usart in config.usart:
        if usart.instance.is_apb2:
            continue
        if usart.clock_macro not in macros:
            macros.append(usart.clock_macro)
    if not macros:
        return None
    return ClockEnable(
        bus="APB1",
        function="RCC_APB1PeriphClockCmd",
        macros=tuple(macros),
        comment="USART 实例（USART2/3 挂在 APB1）",
    )


def _build_usart_clock_apb2(config: Stm32Config) -> ClockEnable | None:
    """USART1 的时钟在 APB2 —— 单独一条语句。"""
    macros: list[str] = []
    for usart in config.usart:
        if not usart.instance.is_apb2:
            continue
        if usart.clock_macro not in macros:
            macros.append(usart.clock_macro)
    if not macros:
        return None
    return ClockEnable(
        bus="APB2",
        function="RCC_APB2PeriphClockCmd",
        macros=tuple(macros),
        comment="USART 实例（USART1 挂在 APB2）",
    )



def _label_to_macro(label: str, pin_name: str) -> str:
    """把用户标签转成合法的 C 标识符片段。

    标签是自由文本（"LED1 低电平点亮"），不能直接当宏名。
    非字母数字字符替换为下划线，连续下划线合并，转大写。
    结果为空时回退到引脚名（如 ``PC13`` → ``PC13``）。
    """
    cleaned = re.sub(r"[^0-9A-Za-z]+", "_", label).strip("_")
    if not cleaned:
        return pin_name.upper()
    if cleaned[0].isdigit():
        cleaned = f"P{cleaned}"
    return cleaned.upper()


def _build_named_pins(config: Stm32Config) -> list[NamedPin]:
    """为带标签的 GPIO 生成端口/引脚宏。

    同名标签会加序号后缀，避免宏重定义 —— C 里重定义同名宏且值不同是警告，
    值相同则是静默覆盖，两种情况都不该出现。
    """
    result: list[NamedPin] = []
    used: dict[str, int] = {}

    for pin in sorted(config.gpio, key=lambda item: (item.port.letter, item.pin)):
        if not pin.label:
            continue
        base = f"BSP_{_label_to_macro(pin.label, pin.name)}"
        count = used.get(base, 0)
        used[base] = count + 1
        macro_name = base if count == 0 else f"{base}_{count + 1}"

        comment = f"{pin.name} — {pin.label}" if pin.label != pin.name else pin.name
        result.append(
            NamedPin(
                macro_name=macro_name,
                port=pin.port.value,
                pin_macro=pin.pin_macro,
                comment=comment,
            )
        )

    return result


def _build_remaps(config: Stm32Config) -> list[PinRemap]:
    """构造引脚重映射配置。

    只处理 USART 的重映射。重映射宏的值与生效引脚来自
    ``Library\\stm32f10x_gpio.h:170-173``。
    """
    from stm32gen.validate.pins import remapped_pins

    result: list[PinRemap] = []
    for usart in config.usart:
        if usart.remap is UsartRemap.NONE:
            continue
        tx, rx = remapped_pins(usart.instance.value, usart.remap)
        target = (
            f"P{tx[0]}{tx[1]}/P{rx[0]}{rx[1]}"
            if tx[0] != "?"
            else "（引脚未知）"
        )
        result.append(
            PinRemap(
                remap=usart.remap.value,
                comment=f"{usart.name} → {target}",
            )
        )
    return result


def _build_gpio_configs(config: Stm32Config) -> list[GpioConfig]:
    """构造全部 ``GPIO_Init`` 调用。

    顺序：先用户配置的 GPIO（按端口、引脚排序），再 USART 的引脚
    （按实例顺序）。同一端口的多个引脚如果模式和速度相同，可以合并成
    一次 ``GPIO_Init``（用 ``|`` 连接引脚宏）—— 这是标准库的常见写法。
    """
    result: list[GpioConfig] = []

    # 1) 用户配置的 GPIO：同端口 + 同模式 + 同速度的合并
    grouped: dict[tuple[str, str, str, str | None], list] = {}
    for pin in config.gpio:
        key = (
            pin.port.value,
            pin.mode.value,
            pin.speed.value,
            pin.init_level.value if pin.init_level else None,
        )
        grouped.setdefault(key, []).append(pin)

    for (port, mode, speed, level), pins in sorted(grouped.items()):
        pin_macros = " | ".join(pin.pin_macro for pin in sorted(pins, key=lambda p: p.pin))
        labels = [pin.label for pin in pins if pin.label]
        names = "、".join(pin.name for pin in sorted(pins, key=lambda p: p.pin))
        comment = f"{names} {GpioMode(mode).name}"
        if labels:
            comment += f"（{'、'.join(labels)}）"
        result.append(
            GpioConfig(
                port=port,
                pin_macro=pin_macros,
                mode=mode,
                speed=speed,
                comment=comment,
                init_level=level,
            )
        )

    # 2) USART 的引脚：TX 复用推挽输出，RX 浮空输入
    for usart in config.usart:
        tx, rx = effective_usart_pins(
            usart.instance.value,
            usart.remap,
            usart.tx_pin,
            usart.rx_pin,
            usart.instance.default_tx_pin,
            usart.instance.default_rx_pin,
        )
        # TX：复用推挽输出，速度按 50MHz（串口波特率上限 4.5Mbps，50MHz 足够）
        result.append(
            GpioConfig(
                port=f"GPIO{tx[0]}",
                pin_macro=f"GPIO_Pin_{tx[1]}",
                mode=GpioMode.AF_PP.value,
                speed=GpioSpeed.SPEED_50MHz.value,
                comment=f"{usart.name} TX（P{tx[0]}{tx[1]}）",
            )
        )
        # RX：浮空输入（串口空闲时线路为高，外部通常已有上拉）
        result.append(
            GpioConfig(
                port=f"GPIO{rx[0]}",
                pin_macro=f"GPIO_Pin_{rx[1]}",
                mode=GpioMode.IN_FLOATING.value,
                speed=GpioSpeed.SPEED_50MHz.value,
                comment=f"{usart.name} RX（P{rx[0]}{rx[1]}）",
            )
        )

    # 3) EXTI 的引脚：默认上拉输入。
    #
    # 输入模式不需要速度，但 GPIO_InitTypeDef 的 GPIO_Speed 字段总要填 ——
    # 给 2MHz（最低档）而不是 50MHz：输入模式下该字段无效，但填低值能避免
    # 读者误以为"这个输入引脚需要高速"。
    for line in config.exti:
        if not line.configure_pin:
            continue
        label = f"（{line.label}）" if line.label else ""
        result.append(
            GpioConfig(
                port=f"GPIO{line.port.letter}",
                pin_macro=f"GPIO_Pin_{line.line}",
                mode=line.pin_mode.value,
                speed=GpioSpeed.SPEED_2MHz.value,
                comment=(
                    f"{line.pin_name} {line.pin_mode.name}"
                    f" ← {line.name} {line.trigger.label}{label}"
                ),
            )
        )

    # 4) TIM 的 PWM 输出引脚：复用推挽输出。
    #
    # 只有用户显式指定了引脚的通道才配 —— 没指定时该通道的引脚由用户在
    # 别处配（比如同一个引脚还要做别的复用），工具不该替他决定。
    for timer in config.tim:
        for channel in timer.channels:
            if not channel.needs_gpio:
                continue
            label = f"（{channel.label}）" if channel.label else ""
            result.append(
                GpioConfig(
                    port=f"GPIO{channel.port.letter}",
                    pin_macro=f"GPIO_Pin_{channel.pin}",
                    mode=GpioMode.AF_PP.value,
                    speed=GpioSpeed.SPEED_50MHz.value,
                    comment=(
                        f"{timer.name}_{channel.channel.name} 输出"
                        f"（{channel.pin_name}）{label}"
                    ),
                )
            )

    return result


def _build_usart_configs(config: Stm32Config) -> list[UsartConfig]:
    """构造全部 USART 配置。"""
    tree = compute_clock_tree(config.hse_hz, config.rcc)
    result: list[UsartConfig] = []

    for usart in config.usart:
        pclk = tree.pclk_of(usart.instance)
        baud = evaluate_baudrate(pclk, usart.baudrate)
        result.append(
            UsartConfig(
                instance=usart.instance.value,
                clock_macro=usart.clock_macro,
                baudrate=usart.baudrate,
                brr_hex=baud.brr_hex,
                baud_actual=f"{baud.actual_exact:.1f}",
                baud_error=f"{baud.error_pct:+.3f}",
                word_length=usart.word_length.value,
                stop_bits=usart.stop_bits.value,
                parity=usart.parity.value,
                mode_expression=usart.mode_expression,
                flow_control=usart.flow_control.value,
                interrupts=tuple(item.value for item in usart.interrupts),
                remap=usart.remap.value,
                label=usart.label,
            )
        )

    return result


def _build_nvic(config: Stm32Config) -> tuple[str, str, list[NvicChannel]]:
    """构造 NVIC 分组与通道配置。"""
    group = config.nvic.priority_group
    note = (
        f"{group.value}：抢占 {group.preempt_bits} 位（0~{group.max_preempt}）"
        f"/ 子优先级 {group.sub_bits} 位（0~{group.max_sub}）"
    )

    channels: list[NvicChannel] = []
    for channel in config.nvic.channels:
        if not channel.enabled:
            continue
        comment = channel.for_peripheral or channel.irq_channel.replace("_IRQn", "")
        channels.append(
            NvicChannel(
                irq_channel=channel.irq_channel,
                preemption=channel.preemption_priority,
                sub=channel.sub_priority,
                comment=comment,
            )
        )
    return group.value, note, channels


def _build_handlers(config: Stm32Config) -> list[UsartInterruptHandler]:
    """构造中断服务函数。只给使能了中断的 USART 生成。"""
    result: list[UsartInterruptHandler] = []
    for usart in config.usart:
        if not usart.interrupts:
            continue
        result.append(
            UsartInterruptHandler(
                instance=usart.instance.value,
                handler=f"{usart.instance.value}_IRQHandler",
                irqn=usart.instance.irqn,
                checks=_build_interrupt_checks(
                    usart.instance.value, tuple(usart.interrupts)
                ),
            )
        )
    return result


# --------------------------------------------------------------------------
# AFIO / EXTI / TIM 的上下文构建
# --------------------------------------------------------------------------


def _build_afio(config: Stm32Config) -> AfioSetup:
    """构造 AFIO 上下文：AFIO 时钟 + 全部重映射 + SWJ 释放。

    **重映射为什么统一放这里**：重映射必须在 ``GPIO_Init`` **之前**生效。
    ``bsp_afio_init()`` 排在 ``bsp_gpio_init()`` 之前，所以放这里能保证顺序；
    若按"外设归属"把 TIM 重映射放进 ``bsp_tim.c``，它会在 ``bsp_gpio_init()``
    之后才跑 —— 引脚已经按旧映射配成复用推挽输出了，改映射来源后
    那个引脚不再是定时器的输出脚，现象是"PWM 没有波形"。

    AFIO 时钟也在这里使能：本模块要写 ``AFIO->MAPR``（重映射与 SWJ 都写它），
    没时钟时写入无效且不报错。EXTI 的 ``AFIO->EXTICR`` 同样依赖它，
    所以 ``config.needs_afio_clock`` 为真（含 EXTI）时也要开 —— 但 EXTI
    的引脚配置在 ``bsp_gpio.c`` 之后，AFIO 时钟由本模块先开好即可。
    """
    afio = config.afio

    # 全部重映射：USART 的来自各实例，TIM 的来自各定时器
    remaps: list[PinRemap] = list(_build_remaps(config))
    remaps.extend(_build_tim_remaps(config))

    swj_enabled = afio.enabled and afio.has_swj

    # AFIO 时钟：只要有任何一项要用 AFIO 寄存器就得开。
    # 注意用 config.needs_afio_clock 而不是只看本模型的字段 ——
    # 它已经把"有 EXTI / 有 USART 或 TIM 重映射 / 有 SWJ"都算进去了。
    clock: ClockEnable | None = None
    if config.needs_afio_clock:
        clock = ClockEnable(
            bus="APB2",
            function="RCC_APB2PeriphClockCmd",
            macros=("RCC_APB2Periph_AFIO",),
            comment="AFIO（重映射 / SWJ 释放 / EXTI 线选择都要写 AFIO 寄存器）",
        )

    if not remaps and not swj_enabled and clock is None:
        return AfioSetup()

    return AfioSetup(
        enabled=True,
        clock=clock,
        remaps=tuple(remaps),
        swj_remap=afio.swj_remap.value if swj_enabled else "",
        swj_name=afio.swj_remap.name if swj_enabled else "",
        releases=afio.releases_pins if swj_enabled else (),
        keeps_swd=afio.swj_remap.keeps_swd if swj_enabled else True,
        bricks_debugger=afio.bricks_debugger if swj_enabled else False,
    )


def _build_exti(config: Stm32Config) -> tuple[list[ExtiLine], list[ExtiHandler]]:
    """构造 EXTI 线与中断服务函数。

    返回 ``(全部线, 按中断通道分组后的 handler)``。

    分组是必需的：线 5~9 共用 ``EXTI9_5_IRQn``、线 10~15 共用
    ``EXTI15_10_IRQn``，一个通道只能有一个 ISR —— 生成两个同名函数
    会直接编译失败（重复定义）。
    """
    lines: list[ExtiLine] = []
    by_handler: dict[str, list[ExtiLine]] = {}
    order: list[str] = []

    for item in config.exti:
        line = ExtiLine(
            line=item.line,
            line_macro=item.line_macro,
            pin_name=item.pin_name,
            port_source=item.port_source_macro,
            pin_source=item.pin_source_macro,
            mode=item.mode.value,
            trigger=item.trigger.value,
            trigger_label=item.trigger.label,
            irqn=item.irqn,
            label=item.label,
            needs_nvic=item.needs_nvic,
        )
        lines.append(line)
        # 事件模式不产生中断，不需要 ISR
        if not item.needs_nvic:
            continue
        handler = item.handler_name
        if handler not in by_handler:
            by_handler[handler] = []
            order.append(handler)
        by_handler[handler].append(line)

    handlers = [
        ExtiHandler(handler=name, irqn=by_handler[name][0].irqn, lines=tuple(by_handler[name]))
        for name in order
    ]
    return lines, handlers


def _build_tim_clock(config: Stm32Config) -> list[ClockEnable]:
    """TIM 模块自己的时钟使能，按总线分组。

    TIM1 在 APB2、TIM2~4 在 APB1。同一总线的多个实例合并成一条语句。
    """
    apb2: list[str] = []
    apb1: list[str] = []
    for timer in config.tim:
        target = apb2 if timer.instance.is_apb2 else apb1
        if timer.clock_macro not in target:
            target.append(timer.clock_macro)

    result: list[ClockEnable] = []
    if apb2:
        result.append(
            ClockEnable(
                bus="APB2",
                function="RCC_APB2PeriphClockCmd",
                macros=tuple(apb2),
                comment="TIM 实例（TIM1 挂在 APB2）",
            )
        )
    if apb1:
        result.append(
            ClockEnable(
                bus="APB1",
                function="RCC_APB1PeriphClockCmd",
                macros=tuple(apb1),
                comment="TIM 实例（TIM2~4 挂在 APB1）",
            )
        )
    return result


def _build_tim_remaps(config: Stm32Config) -> list[PinRemap]:
    """TIM 的引脚重映射。

    重映射宏**名字不规则**（见 ``TimRemap`` 的说明）：TIM2/3 用
    ``GPIO_PartialRemap*`` / ``GPIO_FullRemap*``，TIM4 是
    ``GPIO_Remap_TIM4``，没有 ``GPIO_Remap_TIM2`` 这个宏。
    """
    result: list[PinRemap] = []
    for timer in config.tim:
        if timer.remap is TimRemap.NONE:
            continue
        result.append(
            PinRemap(
                remap=timer.remap.value,
                comment=f"{timer.name} 引脚重映射（{timer.remap.name}）",
            )
        )
    return result


def _tim_clock_call(timer) -> tuple[str, str, tuple[str, ...]]:
    """构造外部时钟源配置的调用文本。

    返回 ``(函数名, 参数列表文本, 参数里用到的符号名)``。第三项供
    ``_collect_symbols`` 登记，漏了它反编造护栏会把 ``TIM_ExtTRGPSC_DIV4``
    这类真实宏当成编造符号。

    - ETR（模式1/2）：``(TIMx, 预分频, 极性, 滤波)``，后三个是普通宏/常数
    - TI1/TI2：``(TIMx, 源宏, 极性, 滤波)``，极性只能是 Rising（库断言只认
      ``TIM_ICPolarity_Rising``；这只影响计数边沿，不改变"数脉冲"的语义）
    - ITR：``(TIMx, 触发源宏)``，只两个参数

    内部时钟返回三个空值。
    """
    source = timer.clock_source
    if source.is_internal:
        return "", "", ()

    if source.is_etr:
        args = (
            f"{timer.etr_prescaler.value}, "
            f"{timer.etr_polarity.value}, "
            f"{timer.etr_filter}"
        )
        symbols = (timer.etr_prescaler.value, timer.etr_polarity.value)
        return source.config_function, args, symbols

    if source.is_ti:
        # TI 外部时钟固定用上升沿计数：一个上升沿 = 一个脉冲是"外部时钟"
        # 的常规语义。双边沿由 TI1_ED 这个源宏自己表达。
        args = f"{source.value}, TIM_ICPolarity_Rising, {timer.etr_filter}"
        return source.config_function, args, (source.value, "TIM_ICPolarity_Rising")

    # ITR 级联：只有一个参数
    return source.config_function, source.value, (source.value,)


def _build_tim(config: Stm32Config) -> list[TimConfig]:
    """构造全部 TIM 配置上下文。"""
    tree = compute_clock_tree(config.hse_hz, config.rcc)
    result: list[TimConfig] = []

    for timer in config.tim:
        # 定时器时钟不是 PCLK：APB 分频 != 1 时是 PCLK 的 2 倍。
        # 算周期时弄错就差一倍，所以这里取 tim_hz 而不是 pclk_hz。
        # 外部时钟下这个值只进注释里的「时钟树标称值」，不参与频率推导。
        clock_hz = tree.tim1_hz if timer.instance.is_advanced else tree.tim234_hz
        clock_function, clock_argument, clock_symbols = _tim_clock_call(timer)

        channels: list[TimChannelInfo] = []
        for channel in timer.channels:
            channels.append(
                TimChannelInfo(
                    channel=channel.channel.value,
                    number=channel.channel.number,
                    init_function=channel.init_function,
                    preload_function=channel.preload_function,
                    mode=channel.mode.value,
                    pulse=channel.pulse,
                    polarity=channel.polarity.value,
                    preload=channel.preload.value,
                    output_state=channel.output_state.value,
                    is_pwm=channel.mode.is_pwm,
                    duty_percent=f"{timer.duty_percent(channel):.2f}",
                    pin_name=channel.pin_name,
                    pin_port=f"GPIO{channel.port.letter}" if channel.needs_gpio else "",
                    pin_macro=f"GPIO_Pin_{channel.pin}" if channel.needs_gpio else "",
                    label=channel.label,
                )
            )

        result.append(
            TimConfig(
                instance=timer.instance.value,
                clock_macro=timer.clock_macro,
                prescaler=timer.prescaler,
                period=timer.period,
                counter_mode=timer.counter_mode.value,
                clock_division=timer.clock_division.value,
                repetition_counter=timer.repetition_counter,
                is_advanced=timer.is_advanced,
                channels=tuple(channels),
                interrupts=tuple(item.value for item in timer.interrupts),
                remap=timer.remap.value,
                irqn=timer.irqn,
                label=timer.label,
                timer_clock_mhz=f"{clock_hz / 1e6:g}",
                counter_hz=f"{timer.counter_frequency_hz(clock_hz):g}",
                update_hz=f"{timer.update_frequency_hz(clock_hz):g}",
                frequency_note=timer.describe_frequency(clock_hz),
                clock_source=clock_function,
                clock_argument=clock_argument,
                external_clock=timer.uses_external_clock,
                external_clock_hz=timer.external_clock_hz,
                clock_symbols=clock_symbols,
            )
        )

    return result


def _build_adc_clock(config: Stm32Config) -> ClockEnable | None:
    """ADC 模块的实例时钟（APB2）。

    ADC1 与 ADC2 都在 APB2 上，同一条语句里合并。

    **ADCCLK 的分频不在这里** —— 它由 ``RCC_ADCCLKConfig`` 单独一条语句
    设置（见 ``TemplateContext.adc_prescaler``），因为它是**全局一份**
    的预分频（CFGR.ADCPRE），不属于某个实例。
    """
    macros: list[str] = []
    for adc in config.adc:
        if adc.clock_macro not in macros:
            macros.append(adc.clock_macro)
    if not macros:
        return None
    return ClockEnable(
        bus="APB2",
        function="RCC_APB2PeriphClockCmd",
        macros=tuple(macros),
        comment="ADC 实例（ADC1/ADC2 都挂在 APB2）",
    )


def _build_adc(config: Stm32Config) -> tuple[list[AdcConfig], list[AdcHandler]]:
    """构造 ADC 配置与中断服务函数。

    返回 ``(全部实例, 按中断通道分组后的 handler)``。

    分组与 ``_build_exti`` 同一个理由：ADC1 与 ADC2 共用
    ``ADC1_2_IRQn``，一个通道只能有一个 ISR —— 生成两个同名函数会
    直接编译失败（重复定义）。
    """
    tree = compute_clock_tree(config.hse_hz, config.rcc)
    pclk2 = tree.pclk2_hz

    result: list[AdcConfig] = []
    for adc in config.adc:
        channels: list[AdcChannelInfo] = []
        for rank, channel in enumerate(adc.channels, start=1):
            channels.append(
                AdcChannelInfo(
                    channel=channel.channel.value,
                    channel_label=channel.channel.label,
                    rank=rank,
                    sample_time=channel.sample_time.value,
                    sample_cycles=f"{channel.sample_time.cycles:g}",
                    is_internal=channel.is_internal,
                    pin_name=channel.pin_name,
                    pin_port=f"GPIO{channel.port.letter}" if channel.needs_gpio else "",
                    pin_macro=f"GPIO_Pin_{channel.pin}" if channel.needs_gpio else "",
                    label=channel.label,
                )
            )

        result.append(
            AdcConfig(
                instance=adc.instance.value,
                clock_macro=adc.clock_macro,
                mode=adc.mode.value,
                mode_name=adc.mode.name,
                prescaler=adc.prescaler.value,
                prescaler_label=adc.prescaler.label,
                scan_mode=adc.uses_scan_mode,
                continuous=adc.continuous,
                data_align=adc.data_align.value,
                external_trigger=adc.trigger.value,
                sequence_length=adc.sequence_length,
                channels=tuple(channels),
                interrupts=tuple(item.value for item in adc.interrupts),
                irqn=adc.irqn,
                label=adc.label,
                watchdog=adc.watchdog,
                watchdog_high=adc.watchdog_high,
                watchdog_low=adc.watchdog_low,
                watchdog_channel=adc.effective_watchdog_channel_macro,
                calibration=adc.calibration,
                calibration_timeout_loops=adc.calibration_timeout_loops,
                software_trigger=adc.trigger.is_software,
                clock_note=adc.describe_clock(pclk2),
                interrupt_flags=tuple(item.flag for item in adc.interrupts),
            )
        )

    # 按 handler 名分组。ADC1/ADC2 都返回 ADC1_2_IRQHandler，所以只要
    # 两个实例都使能了中断，就会并进同一个 handler。
    by_handler: dict[str, list[AdcConfig]] = {}
    order: list[str] = []
    for item in result:
        if not item.has_interrupts:
            continue
        handler = item.irqn.replace("_IRQn", "_IRQHandler")
        if handler not in by_handler:
            by_handler[handler] = []
            order.append(handler)
        by_handler[handler].append(item)

    handlers = [
        AdcHandler(
            handler=name,
            irqn=by_handler[name][0].irqn,
            instances=tuple(by_handler[name]),
        )
        for name in order
    ]
    return result, handlers


def _collect_symbols(    rcc: RccSetup,
    clock_enables: list[ClockEnable],
    gpio_configs: list[GpioConfig],
    remaps: list[PinRemap],
    usart_configs: list[UsartConfig],
    nvic_group: str,
    nvic_channels: list[NvicChannel],
    handlers: list[UsartInterruptHandler],
    afio: AfioSetup | None = None,
    exti_lines: list[ExtiLine] | None = None,
    exti_handlers: list[ExtiHandler] | None = None,
    tim_configs: list[TimConfig] | None = None,
    tim_clocks: list[ClockEnable] | None = None,
    adc_configs: list[AdcConfig] | None = None,
    adc_clock: ClockEnable | None = None,
    adc_prescaler: str = "",
) -> set[str]:
    """收集生成代码里用到的全部标准库符号。

    这些符号会交给 ``tools/check_no_invented.py`` 核对，
    确保生成代码没有引用库里不存在的东西。

    **漏收集会让反编造护栏失去意义**：模板里手写了一个符号而这里没记录，
    护栏就不会去核它。``test_symbols_used_covers_generated_code`` 反向
    从生成代码里提取符号来守这条，所以每次加模板字段都要同步这里。
    """
    # 可选参数归一化，避免下面处处判 None。
    # 注意用 ``is None`` 而不是 ``or``：AfioSetup 是普通 dataclass，
    # 空实例也是真值，``or`` 会在传入空实例时被跳过（行为一致但意图模糊）。
    if afio is None:
        afio = AfioSetup()
    exti_lines = exti_lines or []
    exti_handlers = exti_handlers or []
    tim_configs = tim_configs or []
    tim_clocks = tim_clocks or []
    adc_configs = adc_configs or []

    used: set[str] = set()

    # 函数
    used.update(
        {
            "GPIO_Init",
            "USART_Init",
            "USART_Cmd",
            "NVIC_Init",
            "NVIC_PriorityGroupConfig",
        }
    )
    for item in clock_enables:
        used.add(item.function)

    # ── RCC / Flash（bsp_rcc.c）──
    # enabled=False 时 bsp_rcc.c 只有一个空实现，除函数名外不引用任何符号，
    # 所以这一整块都要按实际生成与否来收集 —— 多收集虽不会让护栏误报，
    # 但会让 symbols_used 失去"生成代码引用了什么"的准确性。
    if rcc.enabled:
        used.update(
            {
                "FLASH_SetLatency",
                "FLASH_PrefetchBufferCmd",
                "RCC_GetSYSCLKSource",
                rcc.flash_latency,
                rcc.prefetch,
            }
        )
        if rcc.uses_hse:
            used.update({"RCC_HSEConfig", rcc.hse_state, "RCC_WaitForHSEStartUp"})
        if rcc.needs_hsi:
            used.update({"RCC_HSICmd", "RCC_GetFlagStatus", "RCC_FLAG_HSIRDY"})
        if rcc.needs_pll:
            used.update(
                {
                    "RCC_PLLConfig",
                    rcc.pll_source,
                    rcc.pll_multiplier,
                    "RCC_PLLCmd",
                    "RCC_GetFlagStatus",
                    "RCC_FLAG_PLLRDY",
                }
            )
        used.update({"RCC_HCLKConfig", rcc.ahb_prescaler})
        used.update({"RCC_PCLK1Config", rcc.apb1_prescaler})
        used.update({"RCC_PCLK2Config", rcc.apb2_prescaler})
        used.update({"RCC_SYSCLKConfig", rcc.sysclk_source})

    # 结构体类型名也出现在生成代码里（GPIO_InitTypeDef GPIO_InitStructure;）
    if gpio_configs:
        used.add("GPIO_InitTypeDef")
    if usart_configs:
        used.add("USART_InitTypeDef")
    if nvic_channels:
        used.add("NVIC_InitTypeDef")
    # 只要生成了 GPIO 初始化，就必然要设初始电平（输出引脚）
    if any(item.needs_level_init for item in gpio_configs):
        used.update({"GPIO_SetBits", "GPIO_ResetBits"})
    if any(item.has_interrupts for item in usart_configs):
        used.add("USART_ITConfig")
    if remaps:
        used.add("GPIO_PinRemapConfig")
        used.update(item.remap for item in remaps)
    for handler in handlers:
        used.add("USART_GetITStatus")
        for check in handler.checks:
            used.add(check.it_macro)
            if "ClearITPendingBit" in check.clear_stmt:
                used.add("USART_ClearITPendingBit")
            if "ReceiveData" in check.clear_stmt:
                used.add("USART_ReceiveData")

    # ── AFIO（bsp_afio.c）──
    if afio.enabled and afio.swj_remap:
        used.update({"GPIO_PinRemapConfig", afio.swj_remap})

    # ── EXTI（bsp_exti.c）──
    if exti_lines:
        used.update({"GPIO_EXTILineConfig", "EXTI_Init", "EXTI_InitTypeDef"})
        for line in exti_lines:
            used.update(
                {
                    line.line_macro,
                    line.port_source,
                    line.pin_source,
                    line.mode,
                    line.trigger,
                }
            )
    # ISR 骨架（bsp_it.c）对每个 EXTI 通道都调这两个函数，
    # 所以只要有一个 handler 就要记 —— 不看 handler 内部细节。
    if exti_handlers:
        used.update({"EXTI_GetITStatus", "EXTI_ClearITPendingBit"})

    # ── TIM（bsp_tim.c）──
    if tim_configs:
        used.update({"TIM_TimeBaseInit", "TIM_TimeBaseInitTypeDef", "TIM_Cmd"})
    for timer in tim_configs:
        used.add(timer.counter_mode)
        used.add(timer.clock_division)
        if timer.has_channels:
            # 有通道时模板会调 TIM_ARRPreloadConfig（让 ARR 的修改在更新
            # 事件时生效，避免改周期产生异常波形）与 TIM_OCInitTypeDef。
            used.update({"TIM_OCInitTypeDef", "TIM_ARRPreloadConfig"})
        for channel in timer.channels:
            used.update(
                {
                    channel.init_function,
                    channel.preload_function,
                    channel.mode,
                    channel.polarity,
                    channel.preload,
                    channel.output_state,
                }
            )
            if channel.needs_gpio:
                used.update({"GPIO_InitTypeDef", channel.pin_macro})
        if timer.has_interrupts:
            # 有中断时模板会生成 ISR 骨架（bsp_it.c），其中调用
            # TIM_GetITStatus / TIM_ClearITPendingBit 判标志与清挂起位。
            used.update(
                {"TIM_ITConfig", "TIM_GetITStatus", "TIM_ClearITPendingBit"}
            )
        used.update(timer.interrupts)
        # 外部时钟源配置。漏收的话反编造护栏会把这些库函数判成编造符号，
        # 明明是标准库公开 API。
        if timer.has_clock_config:
            used.add(timer.clock_source)
            used.update(timer.clock_symbols)
        # 高级定时器：PWM 必须开主输出，否则引脚没有波形
        if timer.is_advanced and timer.channels:
            used.add("TIM_CtrlPWMOutputs")

    for item in tim_clocks:
        used.add(item.function)
        used.update(item.macros)

    # ── ADC（bsp_adc.c）──
    if adc_configs:
        used.update(
            {
                "ADC_Init",
                "ADC_InitTypeDef",
                "ADC_Cmd",
                "ADC_RegularChannelConfig",
            }
        )
    for adc in adc_configs:
        used.add(adc.mode)
        used.add(adc.data_align)
        used.add(adc.external_trigger)
        # 通道宏与采样时间宏：ADC_RegularChannelConfig 的参数位置
        for channel in adc.channels:
            used.add(channel.channel)
            used.add(channel.sample_time)
        if adc.watchdog:
            used.update(
                {
                    "ADC_AnalogWatchdogCmd",
                    "ADC_AnalogWatchdogThresholdsConfig",
                    # 不登记下面这个函数与通道宏，模板里的调用就会被反编造
                    # 护栏判成编造符号 —— 参数位置上的宏同样要登记。
                    "ADC_AnalogWatchdogSingleChannelConfig",
                    "ADC_AnalogWatchdog_SingleRegEnable",
                    adc.watchdog_channel,
                }
            )
        if adc.software_trigger:
            used.add("ADC_SoftwareStartConvCmd")
        if adc.needs_calibration:
            used.update(
                {
                    "ADC_ResetCalibration",
                    "ADC_GetResetCalibrationStatus",
                    "ADC_StartCalibration",
                    "ADC_GetCalibrationStatus",
                }
            )
        if adc.has_interrupts:
            used.update({"ADC_ITConfig", "ADC_GetITStatus", "ADC_ClearITPendingBit"})
        used.update(adc.interrupts)

    if adc_clock is not None:
        used.add(adc_clock.function)
        used.update(adc_clock.macros)
    if adc_prescaler and adc_configs:
        # RCC_ADCCLKConfig 只在有 ADC 时才生成
        used.update({"RCC_ADCCLKConfig", adc_prescaler})

    # 时钟宏
    for item in clock_enables:
        used.update(item.macros)

    # GPIO 模式/速度/引脚宏
    for item in gpio_configs:
        used.add(item.mode)
        used.add(item.speed)
        for token in item.pin_macro.split("|"):
            token = token.strip()
            if token:
                used.add(token)
        if item.init_level:
            used.add(item.init_level)

    # USART 参数宏
    for item in usart_configs:
        used.update(
            {
                item.word_length,
                item.stop_bits,
                item.parity,
                item.flow_control,
            }
        )
        # mode_expression 是位或表达式（"USART_Mode_Rx | USART_Mode_Tx"），
        # 要拆开逐个记录，否则会漏掉收发方向的宏
        for token in item.mode_expression.split("|"):
            token = token.strip()
            if token:
                used.add(token)
        used.update(item.interrupts)
        if item.remap:
            used.add(item.remap)

    # NVIC
    used.add(nvic_group)
    used.add("ENABLE")

    return {name for name in used if name}


def _build_rcc(config: Stm32Config) -> RccSetup:
    """构造 ``bsp_rcc.c`` 的时钟配置上下文。

    所有取值都来自 ``RccConfig`` 与推导出的时钟树 —— 模板不做任何判断。
    """
    rcc = config.rcc
    tree = compute_clock_tree(config.hse_hz, rcc)

    return RccSetup(
        enabled=rcc.enabled,
        flash_latency=rcc.flash_latency_override.value
        if rcc.flash_latency_override is not None
        else {
            0: "FLASH_Latency_0",
            1: "FLASH_Latency_1",
            2: "FLASH_Latency_2",
        }[tree.flash_latency_recommended],
        flash_latency_ws=tree.flash_latency,
        flash_latency_recommended_ws=tree.flash_latency_recommended,
        flash_latency_is_override=rcc.flash_latency_override is not None,
        prefetch=rcc.flash_prefetch.value,
        prefetch_enabled=rcc.flash_prefetch.name == "ENABLE",
        uses_hse=rcc.uses_hse,
        hse_state=rcc.hse_state.value,
        needs_hsi=rcc.pll_source.name == "HSI_Div2",
        needs_pll=rcc.needs_pll,
        pll_source=rcc.pll_source.value,
        pll_multiplier=rcc.pll_multiplier.value,
        pll_factor=rcc.pll_multiplier.factor,
        ahb_prescaler=rcc.ahb_prescaler.value,
        apb1_prescaler=rcc.apb1_prescaler.value,
        apb2_prescaler=rcc.apb2_prescaler.value,
        sysclk_source=rcc.sysclk_source.value,
        sysclk_sws=rcc.sysclk_source.sws_value,
        pll_input_mhz=f"{tree.pll_input_hz / 1e6:g}",
        sysclk_mhz=f"{tree.sysclk_hz / 1e6:g}",
        hclk_mhz=f"{tree.hclk_hz / 1e6:g}",
        pclk1_mhz=f"{tree.pclk1_hz / 1e6:g}",
        pclk2_mhz=f"{tree.pclk2_hz / 1e6:g}",
        hse_mhz=f"{config.hse_hz / 1e6:g}",
        sysclk_hz=tree.sysclk_hz,
        hclk_hz=tree.hclk_hz,
        pclk1_hz=tree.pclk1_hz,
        pclk2_hz=tree.pclk2_hz,
        tim1_hz=tree.tim1_hz,
        tim234_hz=tree.tim234_hz,
    )


def build_context(config: Stm32Config) -> TemplateContext:
    """把配置转换成完整的模板上下文。

    这是生成器唯一的输入转换点：模板拿到的都是算好的值，
    模板里不需要做任何计算或判断。
    """
    rcc_ctx = _build_rcc(config)
    gpio_clock = _build_gpio_clock(config)
    usart_clocks = [
        item
        for item in (
            _build_usart_clock_apb2(config),
            _build_usart_clock(config),
        )
        if item is not None
    ]
    gpio_configs = _build_gpio_configs(config)
    named_pins = _build_named_pins(config)
    usart_configs = _build_usart_configs(config)
    nvic_group, nvic_note, nvic_channels = _build_nvic(config)
    handlers = _build_handlers(config)

    afio = _build_afio(config)
    exti_lines, exti_handlers = _build_exti(config)
    tim_configs = _build_tim(config)
    tim_clocks = _build_tim_clock(config)
    adc_clock = _build_adc_clock(config)
    adc_configs, adc_handlers = _build_adc(config)
    # ADCCLK 分频只有一份（RCC_CFGR.ADCPRE），取第一个实例的值。
    # 校验层的 adc.prescaler_conflict 会拦住"多个实例分频不同"的配置，
    # 所以这里不会静默丢掉某个值。
    adc_prescaler = config.adc[0].prescaler.value if config.adc else ""
    adc_prescaler_label = config.adc[0].prescaler.label if config.adc else ""

    tree = compute_clock_tree(config.hse_hz, config.rcc)
    clock_note = (
        f"HSE {config.hse_hz / 1e6:g}MHz × {tree.pll_factor} = "
        f"SYSCLK {tree.sysclk_hz / 1e6:g}MHz，"
        f"PCLK2 {tree.pclk2_hz / 1e6:g}MHz，PCLK1 {tree.pclk1_hz / 1e6:g}MHz"
    )

    # 把所有时钟使能汇到一处喂给符号收集，避免遗漏。
    # AFIO 的时钟由 afio 上下文单独持有，也要算进去。
    all_clocks = [
        item
        for item in (gpio_clock, *usart_clocks, *tim_clocks, afio.clock)
        if item is not None
    ]

    return TemplateContext(
        project_name=config.project_name,
        chip=config.chip.value,
        device_macro=config.device_macro,
        hse_hz=config.hse_hz,
        hse_mhz=f"{config.hse_hz / 1e6:g}",
        c_flags=tuple(config.c_flags),
        clock_tree_note=clock_note,
        rcc=rcc_ctx,
        gpio_clock=gpio_clock,
        gpio_configs=gpio_configs,
        named_pins=named_pins,
        usart_clocks=usart_clocks,
        usart_configs=usart_configs,
        nvic_group=nvic_group,
        nvic_group_note=nvic_note,
        nvic_channels=nvic_channels,
        afio=afio,
        exti_lines=exti_lines,
        exti_handlers=exti_handlers,
        tim_clocks=tim_clocks,
        tim_configs=tim_configs,
        adc_clock=adc_clock,
        adc_prescaler=adc_prescaler,
        adc_prescaler_label=adc_prescaler_label,
        adc_configs=adc_configs,
        adc_handlers=adc_handlers,
        handlers=handlers,
        symbols_used=_collect_symbols(
            rcc_ctx,
            all_clocks,
            gpio_configs,
            afio.remaps,
            usart_configs,
            nvic_group,
            nvic_channels,
            handlers,
            afio=afio,
            exti_lines=exti_lines,
            exti_handlers=exti_handlers,
            tim_configs=tim_configs,
            tim_clocks=tim_clocks,
            adc_configs=adc_configs,
            adc_clock=adc_clock,
            adc_prescaler=adc_prescaler,
        ),
    )

