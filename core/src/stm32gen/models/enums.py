"""与标准库宏/枚举一一对应的配置枚举。

设计要点：枚举的**值就是标准库里的符号名**（如 ``"GPIO_Mode_AF_PP"``），
而不是数值。理由：

1. 生成代码要输出的是符号名，直接存名字省去一次反查；
2. 数值会随库版本变化，名字不会；
3. 模块导入时逐个校验名字真实存在于 ``symbols.json``，
   枚举里不可能出现库里没有的选项 —— 这是"禁止编造"约束在模型层的落地。

每个枚举类都带 ``symbols()`` 与 ``verify()`` 方法，``verify()`` 由
``models/__init__.py`` 在导入时调用。
"""

from __future__ import annotations

from enum import Enum

from stm32gen.stdlib_ref import SymbolIndex, default_index


class SymbolBackedEnum(str, Enum):
    """值取自标准库符号的枚举基类。

    支持**别名解析**：用户手写 JSON 时既可以写标准库符号名
    （``"GPIO_Mode_Out_PP"``，即 ``.value``），也可以写枚举成员名
    （``"Out_PP"``，即 ``.name``），两种都能解析。

    这让配置文件既能精确对应标准库符号，又不必让人记住 ``GPIO_Mode_``
    这类冗长前缀。写入时一律用 ``.value``，保证生成代码用的是库符号名。
    """

    @classmethod
    def _missing_(cls, value: object):
        """值查找失败时的兜底：按成员名（不区分大小写）再找一次。"""
        if isinstance(value, str):
            target = value.strip()
            for member in cls:
                if member.name.lower() == target.lower():
                    return member
                # 容忍 "GPIO_MODE_OUT_PP" 这类全大写写法
                if member.value.lower() == target.lower():
                    return member
        return None

    @classmethod
    def symbols(cls) -> tuple[str, ...]:
        """本枚举引用的全部标准库符号名。"""
        return tuple(member.value for member in cls)

    @classmethod
    def verify(cls, index: SymbolIndex | None = None) -> None:
        """逐个校验符号真实存在；缺失即抛 ``InventedSymbolError``。"""
        idx = index or default_index()
        for member in cls:
            if not idx.has_any(member.value):
                raise KeyError(
                    f"{cls.__name__}.{member.name} 引用的符号 "
                    f"「{member.value}」在标准库中不存在"
                )

    @classmethod
    def labels(cls) -> list[tuple[str, str]]:
        """给 GUI 下拉框用的 ``[(符号名, 显示名)]`` 列表。"""
        return [(member.value, member.name) for member in cls]

    def __str__(self) -> str:
        return self.value


# --------------------------------------------------------------------------
# GPIO
# --------------------------------------------------------------------------


class GpioPort(SymbolBackedEnum):
    """GPIO 端口。值就是库里的外设实例宏名。"""

    A = "GPIOA"
    B = "GPIOB"
    C = "GPIOC"
    D = "GPIOD"
    E = "GPIOE"
    F = "GPIOF"
    G = "GPIOG"

    @property
    def letter(self) -> str:
        """端口字母，如 ``"A"``。"""
        return self.value[-1]

    @property
    def clock_macro(self) -> str:
        """该端口的 APB2 时钟使能宏。"""
        return f"RCC_APB2Periph_GPIO{self.letter}"


class GpioMode(SymbolBackedEnum):
    """GPIO 模式。取自 ``GPIOMode_TypeDef`` 枚举。"""

    AIN = "GPIO_Mode_AIN"
    IN_FLOATING = "GPIO_Mode_IN_FLOATING"
    IPD = "GPIO_Mode_IPD"
    IPU = "GPIO_Mode_IPU"
    Out_OD = "GPIO_Mode_Out_OD"
    Out_PP = "GPIO_Mode_Out_PP"
    AF_OD = "GPIO_Mode_AF_OD"
    AF_PP = "GPIO_Mode_AF_PP"

    @property
    def is_input(self) -> bool:
        """是否输入模式（输入模式不需要配速度）。"""
        return self in (GpioMode.AIN, GpioMode.IN_FLOATING, GpioMode.IPD, GpioMode.IPU)

    @property
    def is_output(self) -> bool:
        """是否输出模式（推挽/开漏，含复用）。"""
        return not self.is_input

    @property
    def is_alternate(self) -> bool:
        """是否复用功能模式（接外设用）。"""
        return self in (GpioMode.AF_OD, GpioMode.AF_PP)


class GpioSpeed(SymbolBackedEnum):
    """GPIO 输出速度。取自 ``GPIOSpeed_TypeDef`` 枚举。

    注意值是 ``1/2/3`` 而不是位掩码 —— 它是枚举不是宏。
    """

    SPEED_10MHz = "GPIO_Speed_10MHz"
    SPEED_2MHz = "GPIO_Speed_2MHz"
    SPEED_50MHz = "GPIO_Speed_50MHz"

    @property
    def hz(self) -> int:
        """速度的数值形式，单位 Hz。"""
        return {
            GpioSpeed.SPEED_10MHz: 10_000_000,
            GpioSpeed.SPEED_2MHz: 2_000_000,
            GpioSpeed.SPEED_50MHz: 50_000_000,
        }[self]


class GpioInitLevel(SymbolBackedEnum):
    """输出引脚的初始电平。取自 ``BitAction`` 枚举。"""

    LOW = "Bit_RESET"
    HIGH = "Bit_SET"


# --------------------------------------------------------------------------
# USART
# --------------------------------------------------------------------------


class UsartInstance(SymbolBackedEnum):
    """USART 实例。值就是库里的外设实例宏名。"""

    USART1 = "USART1"
    USART2 = "USART2"
    USART3 = "USART3"

    @property
    def index(self) -> int:
        """实例编号，1/2/3。"""
        return int(self.value[-1])

    @property
    def irqn(self) -> str:
        """对应的中断号枚举名。"""
        return f"{self.value}_IRQn"

    @property
    def is_apb2(self) -> bool:
        """是否挂在 APB2（USART1 在 APB2，USART2/3 在 APB1）。"""
        return self is UsartInstance.USART1

    @property
    def clock_macro(self) -> str:
        """时钟使能宏。"""
        bus = "APB2" if self.is_apb2 else "APB1"
        return f"RCC_{bus}Periph_{self.value}"

    @property
    def default_tx_pin(self) -> tuple[str, int]:
        """默认 TX 引脚。"""
        return {
            UsartInstance.USART1: ("A", 9),
            UsartInstance.USART2: ("A", 2),
            UsartInstance.USART3: ("B", 10),
        }[self]

    @property
    def default_rx_pin(self) -> tuple[str, int]:
        """默认 RX 引脚。"""
        return {
            UsartInstance.USART1: ("A", 10),
            UsartInstance.USART2: ("A", 3),
            UsartInstance.USART3: ("B", 11),
        }[self]


class UsartWordLength(SymbolBackedEnum):
    """字长。"""

    B8 = "USART_WordLength_8b"
    B9 = "USART_WordLength_9b"


class UsartStopBits(SymbolBackedEnum):
    """停止位。"""

    B1 = "USART_StopBits_1"
    B0_5 = "USART_StopBits_0_5"
    B2 = "USART_StopBits_2"
    B1_5 = "USART_StopBits_1_5"


class UsartParity(SymbolBackedEnum):
    """校验位。"""

    NONE = "USART_Parity_No"
    EVEN = "USART_Parity_Even"
    ODD = "USART_Parity_Odd"


class UsartFlowControl(SymbolBackedEnum):
    """硬件流控。"""

    NONE = "USART_HardwareFlowControl_None"
    RTS = "USART_HardwareFlowControl_RTS"
    CTS = "USART_HardwareFlowControl_CTS"
    RTS_CTS = "USART_HardwareFlowControl_RTS_CTS"


class UsartDirection(str, Enum):
    """收发方向。

    ``USART_Mode_Rx`` / ``USART_Mode_Tx`` 是位或组合（``0x04 | 0x08``），
    不是独立的枚举值，所以本枚举记录的是**组合方式**，
    由生成器展开成 ``USART_Mode_Rx | USART_Mode_Tx``。
    """

    RX = "RX"
    TX = "TX"
    RX_TX = "RX_TX"

    @property
    def macros(self) -> tuple[str, ...]:
        """展开成需要位或的标准库宏。"""
        return {
            UsartDirection.RX: ("USART_Mode_Rx",),
            UsartDirection.TX: ("USART_Mode_Tx",),
            UsartDirection.RX_TX: ("USART_Mode_Rx", "USART_Mode_Tx"),
        }[self]

    @property
    def expression(self) -> str:
        """写成 C 表达式。"""
        return " | ".join(self.macros)


class UsartInterrupt(SymbolBackedEnum):
    """可按需使能的 USART 中断。"""

    RXNE = "USART_IT_RXNE"
    IDLE = "USART_IT_IDLE"
    TXE = "USART_IT_TXE"
    TC = "USART_IT_TC"
    PE = "USART_IT_PE"


class UsartRemap(SymbolBackedEnum):
    """USART 引脚重映射选项。

    值为空字符串表示「不重映射」（默认引脚）。这是唯一允许的
    「非符号」取值，生成器遇到它会跳过 ``GPIO_PinRemapConfig`` 调用。
    """

    NONE = ""
    USART1 = "GPIO_Remap_USART1"
    USART2 = "GPIO_Remap_USART2"
    USART3_PARTIAL = "GPIO_PartialRemap_USART3"
    USART3_FULL = "GPIO_FullRemap_USART3"

    @classmethod
    def verify(cls, index: SymbolIndex | None = None) -> None:
        """空值不算符号，跳过校验。"""
        idx = index or default_index()
        for member in cls:
            if member.value and not idx.has_any(member.value):
                raise KeyError(
                    f"{cls.__name__}.{member.name} 引用的符号 "
                    f"「{member.value}」在标准库中不存在"
                )


# --------------------------------------------------------------------------
# NVIC
# --------------------------------------------------------------------------


class NvicPriorityGroup(SymbolBackedEnum):
    """NVIC 优先级分组。"""

    GROUP0 = "NVIC_PriorityGroup_0"
    GROUP1 = "NVIC_PriorityGroup_1"
    GROUP2 = "NVIC_PriorityGroup_2"
    GROUP3 = "NVIC_PriorityGroup_3"
    GROUP4 = "NVIC_PriorityGroup_4"

    @property
    def preempt_bits(self) -> int:
        """抢占优先级占用的位数。

        依据 ``Library\\misc.h:138-146`` 的注释表：
        Group_0 抢占 0 位、Group_1 抢占 1 位 …… Group_4 抢占 4 位。
        """
        return int(self.value[-1])

    @property
    def sub_bits(self) -> int:
        """子优先级占用的位数。STM32 共 4 位，两者互补。"""
        return 4 - self.preempt_bits

    @property
    def max_preempt(self) -> int:
        """抢占优先级上限（含）。"""
        return (1 << self.preempt_bits) - 1

    @property
    def max_sub(self) -> int:
        """子优先级上限（含）。"""
        return (1 << self.sub_bits) - 1


# --------------------------------------------------------------------------
# EXTI
# --------------------------------------------------------------------------


class ExtiMode(SymbolBackedEnum):
    """EXTI 线的工作模式。取自 ``Library\\stm32f10x_exti.h:46-50``。"""

    INTERRUPT = "EXTI_Mode_Interrupt"
    EVENT = "EXTI_Mode_Event"

    @property
    def is_interrupt(self) -> bool:
        """中断模式才需要配 NVIC 通道与中断服务函数。"""
        return self is ExtiMode.INTERRUPT


class ExtiTrigger(SymbolBackedEnum):
    """EXTI 触发边沿。取自 ``Library\\stm32f10x_exti.h:58-63``。"""

    RISING = "EXTI_Trigger_Rising"
    FALLING = "EXTI_Trigger_Falling"
    RISING_FALLING = "EXTI_Trigger_Rising_Falling"

    @property
    def label(self) -> str:
        return {
            ExtiTrigger.RISING: "上升沿",
            ExtiTrigger.FALLING: "下降沿",
            ExtiTrigger.RISING_FALLING: "双边沿",
        }[self]


# EXTI 外部中断线（0~15）与内部事件线（16~19）的分界。
#
# 库的 ``IS_GET_EXTI_LINE``（``stm32f10x_exti.h:122-131``）允许 0~19，
# 但 16~19 不接到任何 GPIO：
#
#   16 = PVD 电源电压检测输出
#   17 = RTC 闹钟事件
#   18 = USB 设备唤醒
#   19 = 以太网唤醒
#
# 它们**不需要** ``GPIO_EXTILineConfig``（没有端口来源），触发条件也由
# 对应外设决定而不是引脚边沿 —— 混进来会让"端口"字段变得没有意义。
# 本工具只支持 0~15 的外部线，内部线需要时由用户在业务代码里补。
EXTI_EXTERNAL_LINE_MIN = 0
EXTI_EXTERNAL_LINE_MAX = 15


# --------------------------------------------------------------------------
# TIM
# --------------------------------------------------------------------------


class TimInstance(SymbolBackedEnum):
    """可用的 TIM 实例。

    只列 STM32F103C8T6/C6T6（中容量 LQFP48）上**真实存在**的定时器：
    TIM1（高级，APB2）与 TIM2/3/4（通用，APB1）。

    特意不列 TIM5~TIM8 —— 实测 ``TIM5_IRQn`` / ``TIM6_IRQn`` / ``TIM7_IRQn``
    在标准库的 ``IRQn_Type`` 里不存在（那是大容量/互联型才有的），
    加了会在生成 NVIC 代码时找不到中断号。
    """

    TIM1 = "TIM1"
    TIM2 = "TIM2"
    TIM3 = "TIM3"
    TIM4 = "TIM4"

    @property
    def index(self) -> int:
        """实例编号，1~4。"""
        return int(self.value[-1])

    @property
    def is_advanced(self) -> bool:
        """是否高级定时器（TIM1/TIM8）。

        高级定时器才有：互补输出、死区（BDTR）、重复计数器（RCR），
        以及**必须显式开主输出 MOE** —— 见 ``bsp_tim.c`` 里
        ``TIM_CtrlPWMOutputs`` 的注释。
        """
        return self is TimInstance.TIM1

    @property
    def is_apb2(self) -> bool:
        """是否挂在 APB2（TIM1 在 APB2，TIM2~4 在 APB1）。"""
        return self is TimInstance.TIM1

    @property
    def clock_macro(self) -> str:
        """时钟使能宏。"""
        bus = "APB2" if self.is_apb2 else "APB1"
        return f"RCC_{bus}Periph_{self.value}"

    @property
    def irqn(self) -> str:
        """更新事件（UP）对应的中断号枚举名。

        TIM1 是高级定时器，其中断被拆成 4 个（UP/BRK/TRG_COM/CC），
        定时器溢出走 ``TIM1_UP_IRQn``；TIM2~4 各只有一个 ``TIMx_IRQn``。
        """
        if self.is_advanced:
            return "TIM1_UP_IRQn"
        return f"{self.value}_IRQn"


class TimCounterMode(SymbolBackedEnum):
    """计数模式。取自 ``Library\\stm32f10x_tim.h`` 的 TIM_Counter_Mode 组。"""

    UP = "TIM_CounterMode_Up"
    DOWN = "TIM_CounterMode_Down"
    CENTER_ALIGNED1 = "TIM_CounterMode_CenterAligned1"
    CENTER_ALIGNED2 = "TIM_CounterMode_CenterAligned2"
    CENTER_ALIGNED3 = "TIM_CounterMode_CenterAligned3"

    @property
    def is_center_aligned(self) -> bool:
        """是否中央对齐模式。

        中央对齐下更新事件的频率是边沿对齐的一半，算 PWM 频率时要注意。
        """
        return self in (
            TimCounterMode.CENTER_ALIGNED1,
            TimCounterMode.CENTER_ALIGNED2,
            TimCounterMode.CENTER_ALIGNED3,
        )


class TimClockDivision(SymbolBackedEnum):
    """时钟分频（CKD），影响数字滤波器采样时钟，**不影响计数频率**。

    这一点极易误解：CKD 只改 tDTS（死区与滤波器的时间基准），
    计数器时钟仍然是 CK_INT / (PSC+1)。想让定时器慢下来要改 prescaler。
    """

    DIV1 = "TIM_CKD_DIV1"
    DIV2 = "TIM_CKD_DIV2"
    DIV4 = "TIM_CKD_DIV4"

    @property
    def divider(self) -> int:
        return int(self.name.replace("DIV", ""))


class TimClockSource(SymbolBackedEnum):
    """定时器的时钟源。

    **默认是内部时钟**（时钟树来的 TIMxCLK），此时 PSC/ARR 由工具反算。
    其余三种都是外部时钟：计数脉冲来自芯片引脚或另一个定时器，频率**不由
    时钟树决定**，工具无法推算，所以 PSC/ARR 必须由用户自己填。

    三种外部模式的区别（依据 ``Library\\stm32f10x_tim.c`` 的实现）：

    - ``ETR_MODE1`` / ``ETR_MODE2``：外部脉冲打进 TIMx_ETR 引脚。
      两者硬件上等效（mode1 用 SMCR.SMS=111 + TS=ETRF，mode2 用 SMCR.ECE=1），
      差别只在寄存器配置方式；库里两个函数都是公开 API，都生成。
    - ``TI1`` / ``TI2`` / ``TI1_ED``：拿捕获通道 CH1/CH2 的边沿当计数脉冲
      （``TIM_TIxExternalClockConfig``）。适合信号接在 CH1/CH2 引脚上的场合。
      ``TI1_ED`` 是双边沿检测，输入信号的一个周期会被数成 2 次。
    - ``ITR0``~``ITR3``：把一个定时器的更新/输出当另一个的时钟
      （``TIM_ITRxExternalClockConfig``），**完全在片内，不占引脚**。
    """

    # 空值 = 内部时钟。与 TimRemap.NONE 同一套约定：空串表示"不生成相关代码"。
    INTERNAL = ""
    ETR_MODE1 = "TIM_ETRClockMode1Config"
    ETR_MODE2 = "TIM_ETRClockMode2Config"
    TI1 = "TIM_TIxExternalCLK1Source_TI1"
    TI2 = "TIM_TIxExternalCLK1Source_TI2"
    TI1_ED = "TIM_TIxExternalCLK1Source_TI1ED"
    ITR0 = "TIM_TS_ITR0"
    ITR1 = "TIM_TS_ITR1"
    ITR2 = "TIM_TS_ITR2"
    ITR3 = "TIM_TS_ITR3"

    @property
    def is_internal(self) -> bool:
        """是否内部时钟（工具能反算 PSC/ARR 的唯一情形）。"""
        return self is TimClockSource.INTERNAL

    @property
    def is_etr(self) -> bool:
        """是否走 ETR 引脚（需要配 ETR 预分频/极性/滤波）。"""
        return self in (TimClockSource.ETR_MODE1, TimClockSource.ETR_MODE2)

    @property
    def is_ti(self) -> bool:
        """是否走捕获通道 TI1/TI2 当外部时钟。"""
        return self in (
            TimClockSource.TI1,
            TimClockSource.TI2,
            TimClockSource.TI1_ED,
        )

    @property
    def is_itr(self) -> bool:
        """是否走定时器级联（内部，不占引脚）。"""
        return self in (
            TimClockSource.ITR0,
            TimClockSource.ITR1,
            TimClockSource.ITR2,
            TimClockSource.ITR3,
        )

    @property
    def config_function(self) -> str:
        """配置该时钟源要调用的库函数名；内部时钟返回空串。"""
        if self.is_internal:
            return ""
        if self.is_etr:
            return self.value
        if self.is_ti:
            return "TIM_TIxExternalClockConfig"
        return "TIM_ITRxExternalClockConfig"

    @property
    def label(self) -> str:
        """给 GUI 下拉用的中文说明。"""
        return {
            TimClockSource.INTERNAL: "内部时钟（时钟树）",
            TimClockSource.ETR_MODE1: "外部 ETR 引脚（模式1）",
            TimClockSource.ETR_MODE2: "外部 ETR 引脚（模式2）",
            TimClockSource.TI1: "外部 TI1 引脚（上升沿）",
            TimClockSource.TI2: "外部 TI2 引脚（上升沿）",
            TimClockSource.TI1_ED: "外部 TI1 引脚（双边沿）",
            TimClockSource.ITR0: "级联 ITR0（片内）",
            TimClockSource.ITR1: "级联 ITR1（片内）",
            TimClockSource.ITR2: "级联 ITR2（片内）",
            TimClockSource.ITR3: "级联 ITR3（片内）",
        }[self]

    @classmethod
    def verify(cls, index: SymbolIndex | None = None) -> None:
        """空值不算符号（内部时钟没有对应宏），跳过。"""
        idx = index or default_index()
        for member in cls:
            if member.value and not idx.has_any(member.value):
                raise KeyError(
                    f"{cls.__name__}.{member.name} 引用的符号 "
                    f"「{member.value}」在标准库中不存在"
                )


class TimExtTriggerPrescaler(SymbolBackedEnum):
    """ETR 外部触发信号的预分频。取自 TIM_ExtTRGPSC 组。

    与内部时钟的 PSC 不是一回事：这个在 ETR 之后就先把频率降下来，
    再进 PSC。所以外部时钟的实际计数频率是
    ``ETR频率 / ExtTRGPSC分频 / (PSC+1)``。
    """

    OFF = "TIM_ExtTRGPSC_OFF"
    DIV2 = "TIM_ExtTRGPSC_DIV2"
    DIV4 = "TIM_ExtTRGPSC_DIV4"
    DIV8 = "TIM_ExtTRGPSC_DIV8"

    @property
    def divider(self) -> int:
        """分频系数（OFF 表示不分频，即 1）。"""
        return 1 if self is TimExtTriggerPrescaler.OFF else int(
            self.name.replace("DIV", "")
        )

    @property
    def label(self) -> str:
        return "不分频" if self is TimExtTriggerPrescaler.OFF else f"÷{self.divider}"


class TimExtTriggerPolarity(SymbolBackedEnum):
    """ETR 外部触发信号的极性。取自 TIM_ExtTRGPolarity 组。"""

    NON_INVERTED = "TIM_ExtTRGPolarity_NonInverted"
    INVERTED = "TIM_ExtTRGPolarity_Inverted"

    @property
    def label(self) -> str:
        return "不反相（高电平有效）" if self is TimExtTriggerPolarity.NON_INVERTED else "反相"


class TimChannel(SymbolBackedEnum):
    """输出比较通道。取自 ``Library\\stm32f10x_tim.h`` 的 TIM_Channel 组。"""

    CH1 = "TIM_Channel_1"
    CH2 = "TIM_Channel_2"
    CH3 = "TIM_Channel_3"
    CH4 = "TIM_Channel_4"

    @property
    def number(self) -> int:
        """通道号 1~4。"""
        return int(self.value[-1])

    @property
    def init_function(self) -> str:
        """对应的初始化函数名，如 ``TIM_OC1Init``。"""
        return f"TIM_OC{self.number}Init"

    @property
    def preload_function(self) -> str:
        """对应的预装载配置函数名，如 ``TIM_OC1PreloadConfig``。"""
        return f"TIM_OC{self.number}PreloadConfig"


class TimOcMode(SymbolBackedEnum):
    """输出比较模式。取自 ``Library\\stm32f10x_tim.h`` 的 OC 模式组。"""

    TIMING = "TIM_OCMode_Timing"
    ACTIVE = "TIM_OCMode_Active"
    INACTIVE = "TIM_OCMode_Inactive"
    TOGGLE = "TIM_OCMode_Toggle"
    PWM1 = "TIM_OCMode_PWM1"
    PWM2 = "TIM_OCMode_PWM2"

    @property
    def is_pwm(self) -> bool:
        return self in (TimOcMode.PWM1, TimOcMode.PWM2)


class TimOcPolarity(SymbolBackedEnum):
    """输出极性。"""

    HIGH = "TIM_OCPolarity_High"
    LOW = "TIM_OCPolarity_Low"


class TimOcPreload(SymbolBackedEnum):
    """输出比较预装载。

    PWM 应用**必须开**：不开的话改 CCR 会在计数器跑到一半时立刻生效，
    可能产生一个残缺的脉冲（示波器上看到的窄毛刺就是这么来的）。
    """

    ENABLE = "TIM_OCPreload_Enable"
    DISABLE = "TIM_OCPreload_Disable"


class TimOutputState(SymbolBackedEnum):
    """输出使能。"""

    ENABLE = "TIM_OutputState_Enable"
    DISABLE = "TIM_OutputState_Disable"


class TimInterrupt(SymbolBackedEnum):
    """可按需使能的 TIM 中断。"""

    UPDATE = "TIM_IT_Update"
    CC1 = "TIM_IT_CC1"
    CC2 = "TIM_IT_CC2"
    CC3 = "TIM_IT_CC3"
    CC4 = "TIM_IT_CC4"
    TRIGGER = "TIM_IT_Trigger"
    BREAK = "TIM_IT_Break"

    @property
    def channel(self) -> TimChannel | None:
        """该中断对应的通道（非捕获比较中断返回 None）。"""
        return {
            TimInterrupt.CC1: TimChannel.CH1,
            TimInterrupt.CC2: TimChannel.CH2,
            TimInterrupt.CC3: TimChannel.CH3,
            TimInterrupt.CC4: TimChannel.CH4,
        }.get(self)


class TimRemap(SymbolBackedEnum):
    """TIM 引脚重映射选项。

    **注意宏名不规则**：TIM2/TIM3 用 ``GPIO_PartialRemap*`` /
    ``GPIO_FullRemap*`` 前缀，而 TIM4 只有一个 ``GPIO_Remap_TIM4``，
    TIM1 则是 ``GPIO_PartialRemap_TIM1`` / ``GPIO_FullRemap_TIM1``。
    没有 ``GPIO_Remap_TIM2`` 这个宏 —— 猜名字会直接编译失败。

    另一处不对称：**TIM4 没有部分重映射**，只有整组重映射。
    """

    NONE = ""
    # TIM1：部分重映射（CH1/CH2 → PA8/PA9 之外的引脚），完整映射见后
    TIM1_PARTIAL = "GPIO_PartialRemap_TIM1"
    TIM1_FULL = "GPIO_FullRemap_TIM1"
    # TIM2：部分映射 1 / 部分映射 2 / 完整映射
    TIM2_PARTIAL1 = "GPIO_PartialRemap1_TIM2"
    TIM2_PARTIAL2 = "GPIO_PartialRemap2_TIM2"
    TIM2_FULL = "GPIO_FullRemap_TIM2"
    # TIM3：部分 / 完整
    TIM3_PARTIAL = "GPIO_PartialRemap_TIM3"
    TIM3_FULL = "GPIO_FullRemap_TIM3"
    # TIM4：只有整组重映射
    TIM4 = "GPIO_Remap_TIM4"

    @property
    def instance(self) -> TimInstance | None:
        """该重映射属于哪个实例；NONE 返回 None。"""
        if not self.value:
            return None
        for instance in TimInstance:
            if f"_{instance.value}" in self.value or self.value.endswith(instance.value):
                return instance
        return None

    @classmethod
    def options_for(cls, instance: TimInstance) -> tuple[TimRemap, ...]:
        """该实例可用的重映射选项（含 NONE）。"""
        return tuple(
            item for item in cls if item is cls.NONE or item.instance is instance
        )

    @classmethod
    def verify(cls, index: SymbolIndex | None = None) -> None:
        """空值不算符号，跳过校验。"""
        idx = index or default_index()
        for member in cls:
            if member.value and not idx.has_any(member.value):
                raise KeyError(
                    f"{cls.__name__}.{member.name} 引用的符号 "
                    f"「{member.value}」在标准库中不存在"
                )


# --------------------------------------------------------------------------
# AFIO
# --------------------------------------------------------------------------


class AfioSwjRemap(SymbolBackedEnum):
    """SWJ（SWD + JTAG）引脚的释放选项。

    这几个宏是 **AFIO 最实用的功能**：PA13/PA14（SWDIO/SWCLK）、
    PA15（JTDI）、PB3（JTDO）、PB4（JNTRST）默认被调试口占用，
    不释放就不能当普通 GPIO 用。

    取值与语义（依据 ``Library\\stm32f10x_gpio.h`` 的 SWJ 宏）：

    - ``NONE``：保持默认（SWD + JTAG 全开）。**调试最安全**。
    - ``JTAG_DISABLE``：只关 JTAG，保留 SWD。释放 PA15/PB3/PB4，
      仍能用 SWD 下载调试 —— 最常用的选择。
    - ``SWJ_DISABLE``：把 SWD 也关掉，5 个引脚全释放。
      **之后无法再用调试器下载**，只能靠 BOOT0 或擦除整片救回来。
    - ``NO_JTRST``：只保留 NJTRST 关闭（释放 PB4），其余照旧。
    """

    NONE = ""
    JTAG_DISABLE = "GPIO_Remap_SWJ_JTAGDisable"
    SWJ_DISABLE = "GPIO_Remap_SWJ_Disable"
    NO_JTRST = "GPIO_Remap_SWJ_NoJTRST"

    @property
    def releases(self) -> tuple[str, ...]:
        """该选项释放出来的引脚名。"""
        return {
            AfioSwjRemap.NONE: (),
            AfioSwjRemap.JTAG_DISABLE: ("PA15", "PB3", "PB4"),
            AfioSwjRemap.SWJ_DISABLE: ("PA13", "PA14", "PA15", "PB3", "PB4"),
            AfioSwjRemap.NO_JTRST: ("PB4",),
        }[self]

    @property
    def keeps_swd(self) -> bool:
        """是否仍保留 SWD 下载能力。

        这是**关键安全性判据**：``SWJ_DISABLE`` 关掉 SWD 后，
        除非硬件上有 BOOT0 跳线或复位时擦除的条件，否则芯片会变砖。
        """
        return self is not AfioSwjRemap.SWJ_DISABLE

    @classmethod
    def verify(cls, index: SymbolIndex | None = None) -> None:
        """空值不算符号，跳过校验。"""
        idx = index or default_index()
        for member in cls:
            if member.value and not idx.has_any(member.value):
                raise KeyError(
                    f"{cls.__name__}.{member.name} 引用的符号 "
                    f"「{member.value}」在标准库中不存在"
                )


# --------------------------------------------------------------------------
# ADC
# --------------------------------------------------------------------------

# ADC 通道号上限（含）。库的 ``IS_ADC_CHANNEL``（stm32f10x_adc.h:196-212）
# 允许 0~17：
#
#   0~15  = 外部模拟输入引脚 ADCx_IN0 ~ ADCx_IN15
#   16    = 内部温度传感器（ADC_Channel_TempSensor 的别名）
#   17    = 内部参考电压 Vrefint（ADC_Channel_Vrefint 的别名）
#
# 16/17 是**内部通道**，不接任何引脚 —— 这正是本工具里"通道"与"引脚"
# 必须分开建模的原因：通道 16 有通道号但没有引脚号。
ADC_CHANNEL_MIN = 0
ADC_CHANNEL_MAX = 17

# 内部通道的起始号。>= 16 的通道不从引脚采样。
ADC_INTERNAL_CHANNEL_MIN = 16


class AdcInstance(SymbolBackedEnum):
    """可用的 ADC 实例。

    只列 STM32F103C8T6/C6T6（中容量 LQFP48）上**真实存在**的 ADC：
    ADC1 与 ADC2。

    特意不列 ADC3 —— 它只在大容量/互联型上才有（``ADC3_BASE`` 定义在
    ``stm32f10x.h`` 的 ``#ifdef STM32F10X_HD`` 分支里）。与 ``TimInstance``
    只列 TIM1~TIM4 是同一个理由：列进来会让 NVIC 代码找不到中断号。

    另一个容易搞错的点：**中容量没有 ``ADC1_IRQn``**。``stm32f10x.h:262-263``
    的 ``STM32F10X_MD`` 分支里是 ``ADC1_2_IRQn = 18``（ADC1 与 ADC2 共享），
    ``ADC1_IRQn`` 只出现在 ``STM32F10X_LD_VL`` / ``HD_VL`` 分支里。
    """

    ADC1 = "ADC1"
    ADC2 = "ADC2"

    @property
    def index(self) -> int:
        """实例编号，1/2。"""
        return int(self.value[-1])

    @property
    def clock_macro(self) -> str:
        """APB2 时钟使能宏，如 ``RCC_APB2Periph_ADC1``。"""
        return f"RCC_APB2Periph_ADC{self.index}"

    @property
    def irqn(self) -> str:
        """中断号枚举名。

        ADC1 与 ADC2 **共用一个** ``ADC1_2_IRQn``（依据 ``stm32f10x.h:262-263``
        的 ``STM32F10X_MD`` 分支）。所以两个实例都返回同一个名字，
        生成器必须把它们合并进**一个** ``ADC1_2_IRQHandler``。
        """
        return "ADC1_2_IRQn"

    @property
    def handler_name(self) -> str:
        """中断服务函数名。

        启动文件 ``Start\\startup_stm32f10x_md.s:171`` 的向量表写的是
        ``ADC1_2_IRQHandler``（``:333`` 有对应的 ``EXPORT ... [WEAK]``）。
        """
        return "ADC1_2_IRQHandler"


class AdcChannel(SymbolBackedEnum):
    """规则组通道。取自 ``stm32f10x_adc.h:174-191``。

    通道号 0~17，其中 0~15 接外部模拟输入引脚，16 是内部温度传感器、
    17 是内部参考电压 Vrefint。后两个**不占引脚**，所以引脚推导必须
    先判断 ``is_internal``，否则会去找一个不存在的"Px16"。

    注意库还给了两个别名 ``ADC_Channel_TempSensor`` / ``ADC_Channel_Vrefint``，
    它们与 16/17 数值相同。本枚举只保留数字名，避免同一个通道出现两种
    写法导致配置比对（如 `channel in {…}`）出现意外不等。
    """

    CH0 = "ADC_Channel_0"
    CH1 = "ADC_Channel_1"
    CH2 = "ADC_Channel_2"
    CH3 = "ADC_Channel_3"
    CH4 = "ADC_Channel_4"
    CH5 = "ADC_Channel_5"
    CH6 = "ADC_Channel_6"
    CH7 = "ADC_Channel_7"
    CH8 = "ADC_Channel_8"
    CH9 = "ADC_Channel_9"
    CH10 = "ADC_Channel_10"
    CH11 = "ADC_Channel_11"
    CH12 = "ADC_Channel_12"
    CH13 = "ADC_Channel_13"
    CH14 = "ADC_Channel_14"
    CH15 = "ADC_Channel_15"
    CH16 = "ADC_Channel_16"
    CH17 = "ADC_Channel_17"

    @property
    def number(self) -> int:
        """通道号 0~17。"""
        return int(self.name.replace("CH", ""))

    @property
    def is_internal(self) -> bool:
        """是否内部通道（温度传感器 / Vrefint），不占引脚。"""
        return self.number >= ADC_INTERNAL_CHANNEL_MIN

    @property
    def label(self) -> str:
        """给 GUI 下拉用的中文说明。"""
        if self is AdcChannel.CH16:
            return "通道 16（内部温度传感器）"
        if self is AdcChannel.CH17:
            return "通道 17（内部参考电压 Vrefint）"
        return f"通道 {self.number}"


class AdcMode(SymbolBackedEnum):
    """ADC 工作模式。取自 ``stm32f10x_adc.h:94-103`` 的 ADC_mode 组。

    除 ``INDEPENDENT`` 外都是**双 ADC 模式**：它们要 ADC1 与 ADC2 配对
    使用，库里由 ``ADC_Init`` 把模式值写进 ADC1 的 ``CR1.DUALMOD``。

    本工具的配置粒度是「每个实例一份配置」，而双 ADC 模式的参数必须
    同时写在两个实例上（两者的 ``ADC_Mode`` 要一致），所以校验层会就
    "选了双模式却只配了一个实例"给出警告（``adc.dual_mode_needs_pair``）。
    """

    INDEPENDENT = "ADC_Mode_Independent"
    REG_INJEC_SIMULT = "ADC_Mode_RegInjecSimult"
    REG_SIMULT_ALTER_TRIG = "ADC_Mode_RegSimult_AlterTrig"
    INJEC_SIMULT_FAST_INTERL = "ADC_Mode_InjecSimult_FastInterl"
    INJEC_SIMULT_SLOW_INTERL = "ADC_Mode_InjecSimult_SlowInterl"
    INJEC_SIMULT = "ADC_Mode_InjecSimult"
    REG_SIMULT = "ADC_Mode_RegSimult"
    FAST_INTERL = "ADC_Mode_FastInterl"
    SLOW_INTERL = "ADC_Mode_SlowInterl"
    ALTER_TRIG = "ADC_Mode_AlterTrig"

    @property
    def is_independent(self) -> bool:
        """是否独立模式（单 ADC 可用）。"""
        return self is AdcMode.INDEPENDENT

    @property
    def is_dual(self) -> bool:
        """是否需要 ADC1 + ADC2 成对工作。"""
        return not self.is_independent

    @property
    def is_interleaved(self) -> bool:
        """是否交替采样模式（Fast/Slow Interleaved）。

        交替模式下两个 ADC 轮流采样同一通道，等效采样率翻倍。
        """
        return self in (
            AdcMode.INJEC_SIMULT_FAST_INTERL,
            AdcMode.INJEC_SIMULT_SLOW_INTERL,
            AdcMode.FAST_INTERL,
            AdcMode.SLOW_INTERL,
        )


class AdcTrigger(SymbolBackedEnum):
    """规则通道的外部触发源。取自 ``stm32f10x_adc.h:123-138``。

    **命名陷阱**：``ADC_ExternalTrigConv_None`` 不是"没有触发"，它的实际
    含义是**软件触发**（SWSTART）—— 库把 ``EXTI[2:0] = 111`` 命名为 None，
    而写 ``CR2.EXTSEL=111`` 后转换只由 ``ADC_SoftwareStartConvCmd`` 启动。
    用户选它之后仍要调 ``ADC_SoftwareStartConvCmd(ENABLE)``，这一点写进
    生成代码的注释，否则会以为"选了 None 就自动转换"。

    另一处陷阱：``_T3_CC1`` / ``_T2_CC3`` / ``_T8_CC1`` / ``_T8_TRGO`` /
    ``_T5_CC1`` / ``_T5_CC3`` 六个宏**只对 ADC3 有效**（``/**< For ADC3 only */``）。
    C8T6 上没有 ADC3，选了它们等于不触发 —— 校验层报 ERROR。
    """

    # 软件触发（库里叫 None）。空值会让生成器什么都不写，而 CR2.EXTSEL
    # 的复位值 0x000E0000 恰好就等于 ADC_ExternalTrigConv_None，
    # 所以这里的值必须是真的宏名，不能像 TimRemap 那样用空串。
    SOFTWARE = "ADC_ExternalTrigConv_None"
    T1_CC1 = "ADC_ExternalTrigConv_T1_CC1"
    T1_CC2 = "ADC_ExternalTrigConv_T1_CC2"
    T1_CC3 = "ADC_ExternalTrigConv_T1_CC3"
    T2_CC2 = "ADC_ExternalTrigConv_T2_CC2"
    T3_TRGO = "ADC_ExternalTrigConv_T3_TRGO"
    T4_CC4 = "ADC_ExternalTrigConv_T4_CC4"
    EXT_IT11_TIM8_TRGO = "ADC_ExternalTrigConv_Ext_IT11_TIM8_TRGO"
    # 以下 6 个只对 ADC3 有效
    T3_CC1 = "ADC_ExternalTrigConv_T3_CC1"
    T2_CC3 = "ADC_ExternalTrigConv_T2_CC3"
    T8_CC1 = "ADC_ExternalTrigConv_T8_CC1"
    T8_TRGO = "ADC_ExternalTrigConv_T8_TRGO"
    T5_CC1 = "ADC_ExternalTrigConv_T5_CC1"
    T5_CC3 = "ADC_ExternalTrigConv_T5_CC3"

    @property
    def is_software(self) -> bool:
        """是否软件触发（需要生成 ``ADC_SoftwareStartConvCmd``）。"""
        return self is AdcTrigger.SOFTWARE

    @property
    def adc3_only(self) -> bool:
        """是否只对 ADC3 有效（C8T6 上不可用）。"""
        return self in (
            AdcTrigger.T3_CC1,
            AdcTrigger.T2_CC3,
            AdcTrigger.T8_CC1,
            AdcTrigger.T8_TRGO,
            AdcTrigger.T5_CC1,
            AdcTrigger.T5_CC3,
        )

    @property
    def label(self) -> str:
        """给 GUI 下拉用的中文说明。"""
        return {
            AdcTrigger.SOFTWARE: "软件触发（ADC_SoftwareStartConvCmd）",
            AdcTrigger.T1_CC1: "TIM1_CC1",
            AdcTrigger.T1_CC2: "TIM1_CC2",
            AdcTrigger.T1_CC3: "TIM1_CC3",
            AdcTrigger.T2_CC2: "TIM2_CC2",
            AdcTrigger.T3_TRGO: "TIM3_TRGO",
            AdcTrigger.T4_CC4: "TIM4_CC4",
            AdcTrigger.EXT_IT11_TIM8_TRGO: "EXTI11 / TIM8_TRGO",
            AdcTrigger.T3_CC1: "TIM3_CC1（仅 ADC3）",
            AdcTrigger.T2_CC3: "TIM2_CC3（仅 ADC3）",
            AdcTrigger.T8_CC1: "TIM8_CC1（仅 ADC3）",
            AdcTrigger.T8_TRGO: "TIM8_TRGO（仅 ADC3）",
            AdcTrigger.T5_CC1: "TIM5_CC1（仅 ADC3）",
            AdcTrigger.T5_CC3: "TIM5_CC3（仅 ADC3）",
        }[self]

    @classmethod
    def options_for_chip(
        cls, *, has_adc3: bool = False
    ) -> tuple[AdcTrigger, ...]:
        """本芯片上可用的触发源（没有 ADC3 时剔除 6 个 ADC3 专属选项）。"""
        return tuple(
            item for item in cls if has_adc3 or not item.adc3_only
        )


class AdcSampleTime(SymbolBackedEnum):
    """采样时间。取自 ``stm32f10x_adc.h:213-220`` 的 ADC_sample_time 组。

    采样时间越长，允许的信号源阻抗越高（采样电容有更长时间充电），
    但单次转换也越慢。换算成秒要除以 ADCCLK：
    ``t_sample = cycles / ADCCLK``，换算成转换总时间还要加上 12.5 个
    固定周期（``t_total = (cycles + 12.5) / ADCCLK``）。
    """

    CYCLES_1_5 = "ADC_SampleTime_1Cycles5"
    CYCLES_7_5 = "ADC_SampleTime_7Cycles5"
    CYCLES_13_5 = "ADC_SampleTime_13Cycles5"
    CYCLES_28_5 = "ADC_SampleTime_28Cycles5"
    CYCLES_41_5 = "ADC_SampleTime_41Cycles5"
    CYCLES_55_5 = "ADC_SampleTime_55Cycles5"
    CYCLES_71_5 = "ADC_SampleTime_71Cycles5"
    CYCLES_239_5 = "ADC_SampleTime_239Cycles5"

    @property
    def cycles(self) -> float:
        """采样周期的数值（单位：ADCCLK 周期）。"""
        return {
            AdcSampleTime.CYCLES_1_5: 1.5,
            AdcSampleTime.CYCLES_7_5: 7.5,
            AdcSampleTime.CYCLES_13_5: 13.5,
            AdcSampleTime.CYCLES_28_5: 28.5,
            AdcSampleTime.CYCLES_41_5: 41.5,
            AdcSampleTime.CYCLES_55_5: 55.5,
            AdcSampleTime.CYCLES_71_5: 71.5,
            AdcSampleTime.CYCLES_239_5: 239.5,
        }[self]

    @property
    def label(self) -> str:
        return f"{self.cycles:g} 周期"


class AdcDataAlign(SymbolBackedEnum):
    """转换结果对齐方式。取自 ``stm32f10x_adc.h:162-163``。

    12 位结果放进 16 位数据寄存器：右对齐时高位补 0（取值 0~4095），
    左对齐时低位补 0（取值 0~65520）。右对齐是绝大多数场合的正确选择。
    """

    RIGHT = "ADC_DataAlign_Right"
    LEFT = "ADC_DataAlign_Left"


class AdcPrescaler(SymbolBackedEnum):
    """ADCCLK 分频。取自 ``stm32f10x_rcc.h:425-428``。

    **ADCCLK 由 PCLK2 统一分频而来，不是 PCLK1**，而且上限是 14MHz
    （STM32F103 数据手册的 ADC 特性；本工具按此给 WARNING）。
    72MHz 的 PCLK2 必须至少 ÷6 才不超频 —— 这是最容易漏的一条。
    """

    DIV2 = "RCC_PCLK2_Div2"
    DIV4 = "RCC_PCLK2_Div4"
    DIV6 = "RCC_PCLK2_Div6"
    DIV8 = "RCC_PCLK2_Div8"

    @property
    def divider(self) -> int:
        """分频系数 2/4/6/8。"""
        return int(self.name.replace("DIV", ""))

    @property
    def label(self) -> str:
        return f"PCLK2 ÷{self.divider}"


class AdcInterrupt(SymbolBackedEnum):
    """可按需使能的 ADC 中断。取自 ``stm32f10x_adc.h:313-315``。"""

    EOC = "ADC_IT_EOC"  # 规则通道转换结束
    JEOC = "ADC_IT_JEOC"  # 注入通道转换结束
    AWD = "ADC_IT_AWD"  # 模拟看门狗越界

    @property
    def flag(self) -> str:
        """该中断对应的挂起标志宏（``ADC_GetFlagStatus`` 用）。

        ``ADC_ClearITPendingBit`` 与 ``ADC_GetFlagStatus`` 的参数类型不同：
        前者收 ``uint16_t ADC_IT``，后者收 ``uint8_t ADC_FLAG``。
        写 ISR 时两套宏都要用对，这是本模块把它们分开暴露的原因。
        """
        return {
            AdcInterrupt.EOC: "ADC_FLAG_EOC",
            AdcInterrupt.JEOC: "ADC_FLAG_JEOC",
            AdcInterrupt.AWD: "ADC_FLAG_AWD",
        }[self]

    @property
    def label(self) -> str:
        return {
            AdcInterrupt.EOC: "规则通道转换结束",
            AdcInterrupt.JEOC: "注入通道转换结束",
            AdcInterrupt.AWD: "模拟看门狗越界",
        }[self]


# 本模块定义的、需要校验符号存在性的枚举类。
#
# RCC 与 Flash 相关的枚举在 ``models/rcc.py`` 里（它们需要本模块的
# ``SymbolBackedEnum`` 基类，所以不能反过来在本模块导入 —— 那会形成
# 循环导入）。完整清单在 ``models/__init__.py`` 里汇总。
SYMBOL_BACKED_ENUMS: tuple[type[SymbolBackedEnum], ...] = (
    GpioPort,
    GpioMode,
    GpioSpeed,
    GpioInitLevel,
    UsartInstance,
    UsartWordLength,
    UsartStopBits,
    UsartParity,
    UsartFlowControl,
    UsartInterrupt,
    UsartRemap,
    NvicPriorityGroup,
    ExtiMode,
    ExtiTrigger,
    TimInstance,
    TimClockSource,
    TimExtTriggerPrescaler,
    TimExtTriggerPolarity,
    TimCounterMode,
    TimClockDivision,
    TimChannel,
    TimOcMode,
    TimOcPolarity,
    TimOcPreload,
    TimOutputState,
    TimInterrupt,
    TimRemap,
    AfioSwjRemap,
    AdcInstance,
    AdcChannel,
    AdcMode,
    AdcTrigger,
    AdcSampleTime,
    AdcDataAlign,
    AdcPrescaler,
    AdcInterrupt,
)


def verify_all(
    index: SymbolIndex | None = None,
    extra: tuple[type[SymbolBackedEnum], ...] = (),
) -> None:
    """校验枚举引用的符号都真实存在。

    由 ``models/__init__.py`` 在导入时调用，让编造的枚举在
    **导入阶段**就炸掉，而不是等到生成代码时才发现。

    ``extra`` 用于传入定义在其它模块的枚举类（如 ``models/rcc.py``），
    避免本模块反向导入它们造成循环依赖。
    """
    idx = index or default_index()
    for enum_cls in (*SYMBOL_BACKED_ENUMS, *extra):
        enum_cls.verify(idx)
