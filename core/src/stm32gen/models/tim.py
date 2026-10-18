"""TIM 定时器配置模型。

定时器有三个层次的参数，**它们的耦合关系最容易算错**：

1. ``prescaler``（PSC）与 ``period``（ARR）决定**更新事件频率**
2. ``pulse``（CCR）决定**占空比**（相对 ARR）
3. ``clock_division``（CKD）**不影响计数频率** —— 它只改数字滤波器与
   死区的时间基准 tDTS

第 3 点是最常见的误解：想"让定时器慢一点"却去调 CKD，结果计数频率
纹丝不动。所以本模块把三者分开建模，并在 :meth:`TimConfig.describe_frequency`
里把算出来的实际频率直接写进生成的注释 —— 让用户一眼能核对。

**定时器时钟不是 PCLK**：APB 分频系数 != 1 时，定时器时钟是 PCLK 的 2 倍
（STM32F103 时钟树特性）。36MHz 的 PCLK1 对应 72MHz 的 TIM2 时钟，
算周期时差一倍。本模块从时钟树取 ``tim234_hz`` / ``tim1_hz``
而不是 ``pclk1_hz`` / ``pclk2_hz``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stm32gen.models.enums import (
    GpioPort,
    TimChannel,
    TimClockDivision,
    TimClockSource,
    TimCounterMode,
    TimExtTriggerPolarity,
    TimExtTriggerPrescaler,
    TimInstance,
    TimInterrupt,
    TimOcMode,
    TimOcPolarity,
    TimOcPreload,
    TimOutputState,
    TimRemap,
)

# PSC 与 ARR 都是 16 位寄存器，范围 0~65535（ARRAY 最大 65535）。
TIM_REGISTER_MAX = 0xFFFF

# ETR 数字滤波器 ICFilter/ExtTRGFilter 的合法范围。库里的断言是
# ``IS_TIM_EXT_FILTER(FILTER) ((FILTER) <= 0xF)``，0 表示不采样。
TIM_EXT_FILTER_MAX = 0xF

# (PSC+1) 与 (ARR+1) 的最大值：寄存器写 65535 时进来的是 65536 分频。
TIM_FACTOR_MAX = TIM_REGISTER_MAX + 1


@dataclass(frozen=True)
class TimTimingSolution:
    """由期望周期反算出的 PSC/ARR 组合。

    反算而不是让用户试凑的理由：``(PSC+1) × (ARR+1)`` 是两个整数的乘积，
    想要 1kHz 更新频率时，8MHz 时钟需要乘积 8000 —— 而 8000 有几十种因数
    分解，每种的总分频相同但**占空比分辨率差很多**。让人手工试凑既慢又
    几乎必然选到分辨率差的那一组。
    """

    prescaler: int
    period: int
    timer_clock_hz: int
    target_hz: float
    actual_hz: float
    center_aligned: bool

    @property
    def divider(self) -> int:
        """总分频系数 ``(PSC+1) × (ARR+1)``。"""
        return (self.prescaler + 1) * (self.period + 1)

    @property
    def error_percent(self) -> float:
        """相对误差（%）。正数表示实际比目标快。"""
        if self.target_hz == 0:
            return 0.0
        return (self.actual_hz - self.target_hz) / self.target_hz * 100.0

    @property
    def exact(self) -> bool:
        """是否零误差（总分频恰好整除，没有取整损失）。"""
        return abs(self.error_percent) < 1e-9

    @property
    def period_seconds(self) -> float:
        """实际更新周期（秒）。"""
        return 1.0 / self.actual_hz if self.actual_hz > 0 else 0.0

    @property
    def duty_steps(self) -> int:
        """占空比可调的档位数 = ``ARR+1``。

        这个数决定了 PWM 分辨率的物理上限：ARR=999 只能调出 0.1% 的台阶，
        ARR=9 则只有 10% 一档。所以反算时优先把分频塞进 ARR。
        """
        return self.period + 1

    def describe(self) -> str:
        """一行说明，供状态栏与生成注释使用。"""
        text = (
            f"PSC={self.prescaler} ARR={self.period} → "
            f"更新 {self.actual_hz:g}Hz（周期 {_format_seconds(self.period_seconds)}）"
        )
        if self.exact:
            text += "，零误差"
        else:
            text += f"，相对误差 {self.error_percent:+.3f}%"
        return text


def _format_seconds(seconds: float) -> str:
    """把秒数写成易读形式（1.5s / 200ms / 40us）。"""
    if seconds >= 1.0:
        return f"{seconds:g}s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:g}ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:g}us"
    return f"{seconds * 1e9:g}ns"


def solve_timing(
    timer_clock_hz: int,
    target_hz: float,
    *,
    center_aligned: bool = False,
) -> TimTimingSolution:
    """由期望更新频率反算 PSC/ARR。

    公式（与 :meth:`TimConfig.update_frequency_hz` 互为逆运算）::

        f_update = f_tim / ((PSC+1)(ARR+1) × 2?)      # 2 只在中央对齐时出现

    分配策略：**PSC 尽量小、ARR 尽量大**。总分频相同的前提下 ARR 越大，
    占空比可调的档位越多（见 :attr:`TimTimingSolution.duty_steps`）；
    反过来「PSC 大 ARR 小」虽然频率一样，PWM 分辨率会很难看。

    为什么枚举而不是只做一次除法：``(PSC+1)×(ARR+1)`` 是乘积，
    同样的目标分频可能对应多组因数（如 8000 = 1×8000 = 2×4000 = …），
    一次除法只会撞上其中一组。遍历 PSC 再对 ARR 取整，能在 65536 次
    以内找到**误差最小**的那一组，代价可以忽略（用户点一次按钮）。

    无法精确整除时给出最接近的整数组合并如实报告误差 —— 不隐藏误差，
    因为「看起来配好了但周期差 3%」在串口/编码器时序里就是真故障。
    """
    if timer_clock_hz <= 0:
        raise ValueError("定时器时钟必须大于 0")
    if target_hz <= 0:
        raise ValueError("期望频率必须大于 0")

    # 中央对齐时一个完整周期要上下各数一遍，等效于再分频 2
    edge_factor = 2 if center_aligned else 1
    divider_target = timer_clock_hz / (target_hz * edge_factor)
    max_divider = TIM_FACTOR_MAX * TIM_FACTOR_MAX

    if divider_target < 1.0:
        raise ValueError(
            f"期望 {target_hz:g}Hz 高于这颗定时器能产生的最高更新频率 "
            f"({timer_clock_hz / edge_factor:g}Hz)"
        )
    if divider_target > max_divider:
        raise ValueError(
            f"期望 {target_hz:g}Hz 太低：需要 {divider_target:.0f} 分频，"
            f"而 PSC×ARR 最多只能到 {max_divider}"
        )

    # PSC+1 不会超过目标分频（否则 ARR+1 会取到 0）
    prescaler_limit = min(TIM_FACTOR_MAX, math.ceil(divider_target))
    best: tuple[float, int, int] | None = None  # (误差, PSC+1, ARR+1)
    for prescaler_factor in range(1, prescaler_limit + 1):
        period_factor = round(divider_target / prescaler_factor)
        if not 1 <= period_factor <= TIM_FACTOR_MAX:
            continue
        divider = prescaler_factor * period_factor
        # 相对误差取绝对值最小的组合
        error = abs(divider / divider_target - 1.0)
        # 误差相同时保留 PSC 更小的那组 → ARR 更大 → PWM 档位更多
        if best is None or error < best[0]:
            best = (error, prescaler_factor, period_factor)
        if error == 0.0:
            break

    if best is None:  # pragma: no cover - 前面的范围检查已保证有解
        raise ValueError(f"无法为 {target_hz:g}Hz 找到 PSC/ARR 组合")

    _, prescaler_factor, period_factor = best
    divider = prescaler_factor * period_factor
    actual_hz = timer_clock_hz / (divider * edge_factor)
    return TimTimingSolution(
        prescaler=prescaler_factor - 1,
        period=period_factor - 1,
        timer_clock_hz=timer_clock_hz,
        target_hz=target_hz,
        actual_hz=actual_hz,
        center_aligned=center_aligned,
    )


def solve_period(
    timer_clock_hz: int,
    period_seconds: float,
    *,
    center_aligned: bool = False,
) -> TimTimingSolution:
    """由期望更新周期（秒）反算 PSC/ARR。"""
    if period_seconds <= 0:
        raise ValueError("期望周期必须大于 0")
    return solve_timing(
        timer_clock_hz, 1.0 / period_seconds, center_aligned=center_aligned
    )


class TimChannelConfig(BaseModel):
    """一个输出比较通道的配置。

    字段名与 ``TIM_OCInitTypeDef`` 的成员一一对应
    （``mode`` ↔ ``TIM_OCMode``、``pulse`` ↔ ``TIM_Pulse``、
    ``polarity`` ↔ ``TIM_OCPolarity``），另外补了预装载与输出使能。
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    channel: TimChannel = Field(description="通道，如 TimChannel.CH1")

    mode: TimOcMode = Field(
        default=TimOcMode.PWM1,
        description="输出比较模式；PWM 应用用 PWM1 或 PWM2",
    )

    pulse: int = Field(
        default=0,
        ge=0,
        le=TIM_REGISTER_MAX,
        description="比较值（CCR）。占空比 = pulse / (period + 1)，PWM1 下",
    )

    polarity: TimOcPolarity = Field(default=TimOcPolarity.HIGH)
    preload: TimOcPreload = Field(
        default=TimOcPreload.ENABLE,
        description="输出比较预装载；PWM 必须开，否则改 CCR 会产生残缺脉冲",
    )
    output_state: TimOutputState = Field(default=TimOutputState.ENABLE)

    # PWM 输出要占用的 GPIO 引脚。留空表示"由用户在别处配"，
    # 此时本工具只配定时器本体，不生成 GPIO_Init。
    port: GpioPort | None = Field(
        default=None,
        description="该通道 PWM 输出所在端口；留空则不生成引脚配置",
    )
    pin: int | None = Field(default=None, ge=0, le=15, description="该通道 PWM 输出引脚号")

    label: str = Field(default="", max_length=64, description="用户备注，只进注释")

    @property
    def needs_gpio(self) -> bool:
        """是否需要生成 GPIO 配置。"""
        return self.port is not None and self.pin is not None

    @property
    def pin_name(self) -> str:
        """引脚名；未指定端口时返回空串。"""
        if not self.needs_gpio:
            return ""
        return f"P{self.port.letter}{self.pin}"

    @property
    def init_function(self) -> str:
        """初始化函数名，如 ``TIM_OC1Init``。"""
        return self.channel.init_function

    @property
    def preload_function(self) -> str:
        """预装载配置函数名，如 ``TIM_OC1PreloadConfig``。"""
        return self.channel.preload_function

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("label 不能包含换行符（会破坏生成的注释）")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> TimChannelConfig:
        """端口与引脚必须同时给或同时不给。

        只给一个是典型的没填完 —— 那种状态下 ``needs_gpio`` 为假，
        生成器会静默跳过 GPIO 配置，用户却发现引脚没配好。
        静默不生效比报错难查得多。
        """
        if (self.port is None) != (self.pin is None):
            raise ValueError(
                f"{self.channel.value} 的 port 与 pin 必须同时指定或同时留空"
                "（只给一个是没填完，会导致该通道的 GPIO 被静默跳过）"
            )

        # 输出极性为低时 PWM1 的占空比语义会翻转，这是合法配置但容易误解，
        # 不拦；真正的脉冲宽度由硬件按极性换算，注释里会说明。
        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成注释。"""
        parts = [f"{self.channel.value} {self.mode.name}",
                 f"CCR={self.pulse}"]
        if self.polarity is TimOcPolarity.LOW:
            parts.append("低有效")
        if self.needs_gpio:
            parts.append(f"→ {self.pin_name}")
        if self.label:
            parts.append(f"({self.label})")
        return " ".join(parts)


class TimConfig(BaseModel):
    """一个 TIM 实例的配置。"""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    instance: TimInstance = Field(description="实例，如 TimInstance.TIM2")

    clock_source: TimClockSource = Field(
        default=TimClockSource.INTERNAL,
        description=(
            "时钟源。默认内部时钟（时钟树来的 TIMxCLK）；"
            "选外部时钟后计数脉冲来自引脚或另一个定时器，频率不由时钟树决定，"
            "prescaler/period 必须自己填，工具不做反算"
        ),
    )
    etr_prescaler: TimExtTriggerPrescaler = Field(
        default=TimExtTriggerPrescaler.OFF,
        description="ETR 外部触发预分频，只在 ETR 模式下生效",
    )
    etr_polarity: TimExtTriggerPolarity = Field(
        default=TimExtTriggerPolarity.NON_INVERTED,
        description="ETR 触发极性，只在 ETR 模式下生效",
    )
    etr_filter: int = Field(
        default=0,
        ge=0,
        le=TIM_EXT_FILTER_MAX,
        description=(
            "ETR/TI 数字滤波器采样次数 0~15，0 表示不采样。"
            "信号有抖动时调大，代价是响应变慢"
        ),
    )
    external_clock_hz: float | None = Field(
        default=None,
        gt=0,
        description=(
            "外部时钟频率（Hz），**仅用于注释与校验提示**，不影响生成的 PSC/ARR。"
            "填了之后工具能在注释里写出实际的更新频率与占空比，"
            "填 0 或不填就只写「外部时钟，频率未知」"
        ),
    )

    prescaler: int = Field(
        default=71,
        ge=0,
        le=TIM_REGISTER_MAX,
        description="预分频值 PSC。计数频率 = 定时器时钟 / (PSC + 1)",
    )
    period: int = Field(
        default=999,
        ge=0,
        le=TIM_REGISTER_MAX,
        description="自动重装载值 ARR。更新频率 = 计数频率 / (ARR + 1)",
    )
    counter_mode: TimCounterMode = Field(default=TimCounterMode.UP)
    clock_division: TimClockDivision = Field(
        default=TimClockDivision.DIV1,
        description="CKD，只影响数字滤波器与死区的时间基准，不影响计数频率",
    )
    repetition_counter: int = Field(
        default=0,
        ge=0,
        le=0xFF,
        description="重复计数器 RCR（仅高级定时器 TIM1 有效）",
    )

    channels: list[TimChannelConfig] = Field(
        default_factory=list,
        description="输出比较通道；为空表示只做定时（更新中断）",
    )

    interrupts: list[TimInterrupt] = Field(
        default_factory=list,
        description="需要使能的中断；非空时应在 nvic 里配对应通道",
    )

    remap: TimRemap = Field(
        default=TimRemap.NONE,
        description="引脚重映射；NONE 表示用默认引脚。注意 TIM2/3 用 PartialRemap/FullRemap",
    )

    label: str = Field(default="", max_length=64, description="用户备注，只进注释")

    @property
    def name(self) -> str:
        """实例名，如 ``TIM2``。"""
        return self.instance.value

    @property
    def clock_macro(self) -> str:
        """时钟使能宏。"""
        return self.instance.clock_macro

    @property
    def is_advanced(self) -> bool:
        """是否高级定时器（TIM1）。"""
        return self.instance.is_advanced

    @property
    def irqn(self) -> str:
        """更新事件的中断号。"""
        return self.instance.irqn

    @property
    def irq_handler(self) -> str:
        """更新事件的中断服务函数名。"""
        return self.irqn.replace("_IRQn", "_IRQHandler")

    @property
    def needs_nvic(self) -> bool:
        """是否需要配 NVIC 通道。"""
        return bool(self.interrupts)

    @property
    def uses_external_clock(self) -> bool:
        """是否用外部时钟（PSC/ARR 由用户负责，工具不反算）。"""
        return not self.clock_source.is_internal

    @property
    def etr_total_divider(self) -> int:
        """外部时钟进入 PSC 之前的额外分频（只有 ETR 模式有）。

        ETR 模式下计数频率是
        ``外部频率 / etr_prescaler / (PSC+1)``，两级分频串联。
        TI/ITR 模式没有这一级，返回 1。
        """
        if not self.clock_source.is_etr:
            return 1
        return self.etr_prescaler.divider

    @property
    def external_clock_note(self) -> str:
        """外部时钟的一句话说明，进生成注释。

        为什么要写这句：外部时钟下注释若还写「定时器时钟 72MHz」就是错的 ——
        那个 72MHz 对计数毫无影响，用户会拿它去推算周期，结论全错。
        """
        source = self.clock_source
        if source.is_internal:
            return ""
        if source.is_etr:
            detail = (
                f"{source.label}，ETR 预分频 {self.etr_prescaler.label}、"
                f"{self.etr_polarity.label}、滤波 {self.etr_filter}"
            )
        elif source.is_ti:
            detail = f"{source.label}、滤波 {self.etr_filter}"
        else:
            detail = source.label
        return (
            f"计数脉冲来自 {detail} —— 频率由外部信号决定，"
            "与时钟树无关；PSC/ARR 由用户给定，工具未做反算"
        )

    @property
    def counter_divider(self) -> int:
        """总的分频系数 ``(PSC+1) × (ARR+1)``。

        中央对齐模式下实际还要再除以 2（上升+下降各计一遍），
        由 :meth:`update_frequency_hz` 处理。
        """
        return (self.prescaler + 1) * (self.period + 1)

    def counter_frequency_hz(self, timer_clock_hz: int) -> float:
        """计数频率（``输入频率 / ETR预分频 / (PSC+1)``）。

        必须经过 :meth:`effective_clock_hz`：外部时钟下真正决定计数的是
        外部信号频率，直接拿传进来的 ``timer_clock_hz``（时钟树的 TIMxCLK）
        会算出一个 3600 万赫兹这种看着像真的、其实毫无意义的数字。
        算不出来时返回 0。
        """
        clock = self.effective_clock_hz(timer_clock_hz)
        if clock is None:
            return 0.0
        return clock / self.etr_total_divider / (self.prescaler + 1)

    def effective_clock_hz(self, timer_clock_hz: int) -> float | None:
        """本次实际可用的时钟频率；算不出来返回 ``None``。

        内部时钟用时钟树的 TIMxCLK；外部时钟只能用用户填的
        ``external_clock_hz``。没填就返回 ``None`` —— 调用方必须处理这个
        ``None``，不能悄悄拿 TIMxCLK 顶替（那会算出一个看起来很合理、
        实际毫无意义的数字）。
        """
        if self.uses_external_clock:
            return self.external_clock_hz
        return float(timer_clock_hz)

    def update_frequency_hz(self, timer_clock_hz: int) -> float:
        """更新事件频率。

        边沿对齐：``f = timer_clk / ((PSC+1)(ARR+1))``
        中央对齐：``f = timer_clk / (2(PSC+1)(ARR+1))``
        —— 中央对齐下计数器先升后降，一个完整周期要走 2 倍的计数。

        外部时钟且没填频率时，``timer_clock_hz`` 那一路会被忽略，
        结果按 0 处理 —— 调用方应当先用 :meth:`effective_clock_hz`
        判断是否算得出来。
        """
        clock = self.effective_clock_hz(timer_clock_hz)
        if clock is None:
            return 0.0
        base = clock / self.etr_total_divider / self.counter_divider
        return base / 2 if self.counter_mode.is_center_aligned else base

    def duty_percent(self, channel: TimChannelConfig) -> float:
        """占空比（%）。

        PWM1 模式下 CCR < ARR 时输出有效电平，占空比 = CCR/(ARR+1)。
        极性为低时"有效电平"是低电平，占空比语义翻转 —— 这里算的是
        **寄存器意义上的比值**，注释里会说明极性的影响。
        """
        return channel.pulse / (self.period + 1) * 100.0

    @field_validator("interrupts")
    @classmethod
    def _dedupe_interrupts(cls, value: list[TimInterrupt]) -> list[TimInterrupt]:
        """去重并保持稳定顺序，避免生成重复的 TIM_ITConfig 调用。"""
        seen: set[TimInterrupt] = set()
        result: list[TimInterrupt] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @field_validator("channels")
    @classmethod
    def _dedupe_channels(
        cls, value: list[TimChannelConfig]
    ) -> list[TimChannelConfig]:
        """同一通道只能配一次，否则会生成两次 TIM_OCxInit 且后一次生效。"""
        seen: set[TimChannel] = set()
        result: list[TimChannelConfig] = []
        for item in value:
            if item.channel in seen:
                raise ValueError(
                    f"{item.channel.value} 配了多次；同一通道只能有一份配置"
                )
            seen.add(item.channel)
            result.append(item)
        return result

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("label 不能包含换行符（会破坏生成的注释）")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> TimConfig:
        """组合一致性检查。"""
        # 重复计数器只对高级定时器有效
        if self.repetition_counter and not self.is_advanced:
            raise ValueError(
                f"{self.name} 不是高级定时器，TIM_RepetitionCounter 仅对 TIM1/TIM8 有效"
                "（通用定时器没有 RCR 寄存器）"
            )

        # 重映射必须属于本实例
        if self.remap is not TimRemap.NONE and self.remap.instance is not self.instance:
            raise ValueError(
                f"{self.name} 选了 {self.remap.value}，那是 "
                f"{self.remap.instance.value if self.remap.instance else '未知实例'}"
                " 的重映射宏，两者不匹配"
            )

        # 捕获比较中断要有对应通道
        configured = {item.channel for item in self.channels}
        for interrupt in self.interrupts:
            channel = interrupt.channel
            if channel is not None and channel not in configured:
                raise ValueError(
                    f"{self.name} 使能了 {interrupt.name} 中断，"
                    f"但没有配置 {channel.value} 通道 —— "
                    "该中断永远不会触发（没有通道就没有捕获比较事件）"
                )

        # BREAK/TRIGGER 只对高级定时器有效
        if not self.is_advanced:
            for interrupt in self.interrupts:
                if interrupt in (TimInterrupt.BREAK, TimInterrupt.TRIGGER):
                    raise ValueError(
                        f"{self.name} 不是高级定时器，{interrupt.name} 中断仅对 TIM1/TIM8 有效"
                    )

        # 外部时钟相关的组合检查
        if not self.uses_external_clock:
            # 内部时钟下这几项没有任何效果。静默忽略比报错更糟：用户以为
            # 配了 ETR 预分频，生成的代码里却一个字都没有。
            if self.etr_prescaler is not TimExtTriggerPrescaler.OFF:
                raise ValueError(
                    f"{self.name} 用的是内部时钟，etr_prescaler 不会生效；"
                    "请先把 clock_source 改成 ETR 模式"
                )
            if self.etr_polarity is not TimExtTriggerPolarity.NON_INVERTED:
                raise ValueError(
                    f"{self.name} 用的是内部时钟，etr_polarity 不会生效；"
                    "请先把 clock_source 改成 ETR 模式"
                )

        # ETR 滤波与 TI 滤波是同一个寄存器字段（ICFilter/ExtTRGFilter），
        # 只在外部时钟下有实际作用；内部时钟下非 0 同样会被静默丢弃。
        if not self.uses_external_clock and self.etr_filter:
            raise ValueError(
                f"{self.name} 用的是内部时钟，etr_filter 不会生效；"
                "滤波器只对外部时钟信号有意义"
            )

        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成注释。"""
        parts = [f"{self.name} PSC={self.prescaler} ARR={self.period}",
                 self.counter_mode.name]
        if self.uses_external_clock:
            parts.append(f"时钟:{self.clock_source.name}")
        if self.channels:
            parts.append("通道:" + "+".join(c.channel.name for c in self.channels))
        if self.interrupts:
            parts.append("中断:" + "+".join(i.name for i in self.interrupts))
        if self.remap is not TimRemap.NONE:
            parts.append(f"重映射:{self.remap.name}")
        if self.label:
            parts.append(f"({self.label})")
        return " ".join(parts)

    def describe_frequency(self, timer_clock_hz: int) -> str:
        """把算出来的频率写成一行，进生成注释供核对。

        写进注释的理由：这三个参数（PSC/ARR/中央对齐）与最终频率的关系
        容易算错，把结果直接摆出来，用户对着示波器一眼就能确认。

        外部时钟下**不能**写「定时器时钟 xxMHz」—— 那个值对计数毫无作用，
        写出来会让人按它推算周期，结论全错。改成说明外部信号来源；
        用户填了 ``external_clock_hz`` 才顺带给出推算的实际频率。
        """
        if self.uses_external_clock:
            text = self.external_clock_note
            update_hz = self.update_frequency_hz(timer_clock_hz)
            if self.external_clock_hz and update_hz > 0:
                text += (
                    f"。按你给的外部频率 {self.external_clock_hz:g}Hz 推算："
                    f"计数 {self.counter_frequency_hz(timer_clock_hz):g}Hz"
                    f"、更新 {update_hz:g}Hz"
                )
            elif update_hz <= 0:
                text += "。未填外部时钟频率，故更新频率未知"
            return text

        update_hz = self.update_frequency_hz(timer_clock_hz)
        counter_hz = self.counter_frequency_hz(timer_clock_hz)
        text = (
            f"定时器时钟 {timer_clock_hz / 1e6:g}MHz / (PSC+1={self.prescaler + 1}) "
            f"= 计数 {counter_hz:g}Hz"
        )
        if self.counter_mode.is_center_aligned:
            text += f"；中央对齐模式下更新频率再减半 = {update_hz:g}Hz"
        else:
            text += f" / (ARR+1={self.period + 1}) = 更新 {update_hz:g}Hz"
        return text
