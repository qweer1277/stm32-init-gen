"""ADC（模数转换器）配置模型。

ADC 与前面几个外设（USART / EXTI / TIM）相比有三处**结构上的不同**，
本模块的设计全部围绕这三点：

1. **它只有输入，没有输出。** 引脚必须配成模拟输入（``GPIO_Mode_AIN``），
   而 ``AIN`` 会关掉该引脚的数字输入缓冲 —— 这是**独占**的，同一个引脚
   不能再被任何数字外设当输入读。所以 ``validate/pins.py`` 把它标成
   ``PinDirection.ANALOG`` 而不是 ``INPUT``。

2. **"通道"与"引脚"不是一回事。** 通道号 0~15 接外部引脚，16/17 是
   内部温度传感器与 Vrefint，**不占引脚**。若把通道号直接当引脚号算，
   通道 16 会去找一个不存在的"Px16"。

3. **ADC1 与 ADC2 共享一个中断通道** ``ADC1_2_IRQn``（依据
   ``stm32f10x.h:262-263`` 的 ``STM32F10X_MD`` 分支）。所以一个配置里
   同时用 ADC1 与 ADC2 时，生成的 ISR 必须是**一个**
   ``ADC1_2_IRQHandler``，内部按实例分派 —— 与 EXTI 线 5~9 共享
   ``EXTI9_5_IRQn`` 是同一类问题。

校准顺序（本模块生成的代码按此实现，依据 ``stm32f10x_adc.c:379-445``）::

    ADC_ResetCalibration(ADCx);
    while (ADC_GetResetCalibrationStatus(ADCx) != SET);   /* 等 RSTCAL 置位 */
    ADC_StartCalibration(ADCx);
    while (ADC_GetCalibrationStatus(ADCx) != SET);        /* 等 CAL 置位 */

两个状态函数返回 ``SET`` 表示"该位仍置位"，即**还在进行/尚未完成**；
返回 ``RESET`` 才是完成。这个极性反直觉，是库里的历史包袱（函数名是
"Get...Status" 而返回值语义是"该位是否还在"），生成代码的注释里会写明。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stm32gen.models.adc_pins import default_pin as adc_default_pin
from stm32gen.models.enums import (
    ADC_CHANNEL_MAX,
    ADC_CHANNEL_MIN,
    AdcChannel,
    AdcDataAlign,
    AdcInstance,
    AdcInterrupt,
    AdcMode,
    AdcPrescaler,
    AdcSampleTime,
    AdcTrigger,
    GpioMode,
    GpioPort,
)

# 规则组序列的最大长度。
#
# 依据 ``stm32f10x_adc.h:387`` 的 ``IS_ADC_REGULAR_LENGTH``：
# ``((LENGTH) >= 0x1) && ((LENGTH) <= 0x10)`` —— 1~16。
# 但**序列长度由 SQR1 的 L 位决定，实际会转换成"配了几条通道"**，
# 所以模型层直接要求 len(channels) 在 1~16，不做单独的 length 字段：
# 让 length 与列表长度两个真相源并存，迟早会出现"填了 8 实际只配 3 条"
# 那种"看起来配了实际没生成"的配置。
ADC_SEQUENCE_MAX = 16

# 模拟看门狗阈值是 12 位，依据 ``IS_ADC_THRESHOLD``
# （stm32f10x_adc.h:346）：``(THRESHOLD) <= 0xFFF``。
ADC_THRESHOLD_MAX = 0xFFF

# 采样时间上限（周期数），用于换算转换时间。
# 12.5 = 12 位逐次逼近的固定开销（库注释与数据手册一致）。
ADC_CONVERSION_OVERHEAD_CYCLES = 12.5


class AdcChannelConfig(BaseModel):
    """规则组序列里的一条通道。

    字段名与 ``ADC_RegularChannelConfig(ADCx, ADC_Channel, Rank,
    ADC_SampleTime)`` 的参数一一对应（``channel`` ↔ ``ADC_Channel``,
    ``sample_time`` ↔ ``ADC_SampleTime``），rank 由列表顺序决定，
    不单独存字段。
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    channel: AdcChannel = Field(
        description="规则组通道。0~15 接外部引脚，16/17 是内部温度传感器与 Vrefint",
    )

    sample_time: AdcSampleTime = Field(
        default=AdcSampleTime.CYCLES_55_5,
        description=(
            "采样时间。越长允许的信号源阻抗越高、转换越慢；"
            "55.5 周期是 ST 例程与数据手册推荐的通用默认值"
        ),
    )

    # 该通道采样的模拟输入引脚。
    #
    # 与 ``TimChannelConfig`` 同样的处理：留空表示"引脚由用户在别处配"，
    # 此时本工具只生成 ADC 本体配置，不生成 GPIO_Init，也不在引脚占用
    # 表里登记 —— 否则会报出 phantom 冲突。
    port: GpioPort | None = Field(
        default=None,
        description="该通道模拟输入所在端口；留空则不生成引脚配置",
    )
    pin: int | None = Field(default=None, ge=0, le=15, description="该通道的引脚号")

    label: str = Field(default="", max_length=64, description="用户备注，只进注释")

    @property
    def needs_gpio(self) -> bool:
        """是否需要生成该引脚的 GPIO_Init。"""
        return self.port is not None and self.pin is not None

    @property
    def pin_name(self) -> str:
        """引脚名；未指定端口时返回空串。"""
        if not self.needs_gpio:
            return ""
        return f"P{self.port.letter}{self.pin}"

    @property
    def is_internal(self) -> bool:
        """是否内部通道（温度传感器 / Vrefint）。"""
        return self.channel.is_internal

    @property
    def pin_mode(self) -> GpioMode:
        """模拟输入引脚的工作模式。

        **必须是 ``GPIO_Mode_AIN``**：库的 ``GPIO_Init`` 在 AIN 下把 CNF
        配成 00（模拟输入），此时上拉/下拉与施密特触发器全部关闭，
        引脚直接接到采样电容。配成任何数字输入模式（IPU/IPD/
        IN_FLOATING）都会让施密特触发器介入，采样值被钳位在 0 或 VDD
        附近 —— 现象是"读数永远是 0 或 4095"。
        """
        return GpioMode.AIN

    @property
    def suggested_pin(self) -> tuple[str, int] | None:
        """数据手册上该通道的**默认**引脚；仅用于提示与 GUI 建议值。

        本表未经数据手册核实（见 ``models/adc_pins.py`` 的模块说明），
        生成代码不依赖它。
        """
        return adc_default_pin(self.channel)

    @property
    def conversion_cycles(self) -> float:
        """该通道一次转换占用多少 ADCCLK 周期。

        采样时间 + 12.5 个固定周期（12 位逐次逼近）。
        """
        return self.sample_time.cycles + ADC_CONVERSION_OVERHEAD_CYCLES

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("label 不能包含换行符（会破坏生成的注释）")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> AdcChannelConfig:
        """端口与引脚必须同时给或同时不给；内部通道不该配引脚。"""
        if (self.port is None) != (self.pin is None):
            raise ValueError(
                f"{self.channel.label} 的 port 与 pin 必须同时指定或同时留空"
                "（只给一个是没填完，会导致该通道的 GPIO 被静默跳过）"
            )

        # 内部通道不接引脚。配了引脚的话生成的 GPIO_Init 会去配置一个
        # 与采样毫无关系的引脚 —— 它既不会被采样，还会被误标成模拟输入，
        # 把用户真正想用的那个引脚占掉。
        if self.is_internal and self.needs_gpio:
            raise ValueError(
                f"{self.channel.label} 是内部通道，不接任何引脚；"
                f"但你给它指定了 {self.pin_name}。"
                "内部通道的引脚配置不会生效，请清空 port/pin"
            )

        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成注释。"""
        parts = [self.channel.label, self.sample_time.label]
        if self.needs_gpio:
            parts.append(f"← {self.pin_name}")
        elif not self.is_internal:
            parts.append("（引脚由别处配置）")
        if self.label:
            parts.append(f"({self.label})")
        return " ".join(parts)


class AdcConfig(BaseModel):
    """一个 ADC 实例的配置。"""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    instance: AdcInstance = Field(description="实例，如 AdcInstance.ADC1")

    mode: AdcMode = Field(
        default=AdcMode.INDEPENDENT,
        description=(
            "工作模式。INDEPENDENT 是单 ADC；其余是 ADC1+ADC2 配对的双 ADC 模式，"
            "两个实例的 mode 必须写成同一个值"
        ),
    )

    prescaler: AdcPrescaler = Field(
        default=AdcPrescaler.DIV6,
        description=(
            "ADCCLK 分频，由 PCLK2 分频而来。默认 ÷6：72MHz / 6 = 12MHz，"
            "不超过 14MHz 上限"
        ),
    )

    scan_mode: bool = Field(
        default=False,
        description=(
            "是否扫描模式（多通道）。规则组配了多条通道时必须开启，"
            "否则只有序列里的第一条被转换"
        ),
    )

    continuous: bool = Field(
        default=False,
        description=(
            "是否连续转换。开启后一次启动会不停地转换，直到 ADC_Cmd 关闭；"
            "关闭时每次转换都要重新启动（软件触发或外部触发）"
        ),
    )

    trigger: AdcTrigger = Field(
        default=AdcTrigger.SOFTWARE,
        description=(
            "规则组触发源。默认软件触发（ADC_ExternalTrigConv_None，"
            "名字有误导性：它表示由 ADC_SoftwareStartConvCmd 启动）"
        ),
    )

    data_align: AdcDataAlign = Field(
        default=AdcDataAlign.RIGHT,
        description="结果对齐。右对齐取值 0~4095；左对齐取值 0~65520",
    )

    channels: list[AdcChannelConfig] = Field(
        default_factory=list,
        description="规则组序列，**列表顺序即 rank 顺序**（第 1 项 rank=1）",
    )

    interrupts: list[AdcInterrupt] = Field(
        default_factory=list,
        description="需要使能的中断；非空时应在 nvic 里配 ADC1_2_IRQn 通道",
    )

    # 模拟看门狗。阈值是 12 位的 0~4095。
    watchdog: bool = Field(
        default=False,
        description=(
            "是否启用模拟看门狗。开启后 AWD 中断/标志在转换值越界时触发，"
            "用于把电压异常从轮询里解放出来"
        ),
    )
    watchdog_channel: AdcChannel | None = Field(
        default=None,
        description=(
            "看门狗监控哪个通道。留空表示监控规则组序列的第一条。"
            "必须调用 ADC_AnalogWatchdogSingleChannelConfig 指定通道 —— "
            "只调 ADC_AnalogWatchdogCmd 的话库按通道 0 处理，盯的是错的通道"
        ),
    )
    watchdog_high: int = Field(
        default=ADC_THRESHOLD_MAX,
        ge=0,
        le=ADC_THRESHOLD_MAX,
        description="模拟看门狗上限（越高越容易触发，最高 4095）",
    )
    watchdog_low: int = Field(
        default=0,
        ge=0,
        le=ADC_THRESHOLD_MAX,
        description="模拟看门狗下限",
    )

    # 校准。默认开启 —— 不校准时误差可达几十个 LSB。
    calibration: bool = Field(
        default=True,
        description=(
            "是否生成校准流程（RSTCAL → CAL）。默认开启：不校准时"
            "零点漂移可达几十个 LSB，是对精度最廉价的改善"
        ),
    )

    # 等待校准完成的循环上限（code-standards R4.3：硬件标志等待必须有界）
    calibration_timeout_loops: int = Field(
        default=0x1000,
        ge=1,
        le=0xFFFFF,
        description=(
            "等待校准标志的循环上限。超时后放弃校准并继续，"
            "不卡死在启动阶段（现场表现是上电没反应，比精度差难查得多）"
        ),
    )

    label: str = Field(default="", max_length=64, description="用户备注，只进注释")

    # -- 派生属性 --------------------------------------------------------

    @property
    def name(self) -> str:
        """实例名，如 ``ADC1``。"""
        return self.instance.value

    @property
    def clock_macro(self) -> str:
        """APB2 时钟使能宏。"""
        return self.instance.clock_macro

    @property
    def irqn(self) -> str:
        """中断号。ADC1/ADC2 都是 ``ADC1_2_IRQn``。"""
        return self.instance.irqn

    @property
    def handler_name(self) -> str:
        """中断服务函数名。ADC1/ADC2 都是 ``ADC1_2_IRQHandler``。"""
        return self.instance.handler_name

    @property
    def needs_nvic(self) -> bool:
        """是否需要配 NVIC 通道。"""
        return bool(self.interrupts)

    @property
    def sequence_length(self) -> int:
        """规则组序列长度（= 配置了几条通道）。

        传给 ``ADC_Init`` 的 ``ADC_NbrOfChannel``，范围 1~16。
        没有配任何通道时返回 0，``AdcSetup`` 会据此跳过 ADC_Init。
        """
        return len(self.channels)

    @property
    def has_channels(self) -> bool:
        return bool(self.channels)

    @property
    def external_channels(self) -> list[AdcChannelConfig]:
        """只采外部引脚的通道（内部通道不在其中）。"""
        return [item for item in self.channels if not item.is_internal]

    @property
    def needs_gpio(self) -> bool:
        """是否有通道配了引脚。"""
        return any(item.needs_gpio for item in self.channels)

    @property
    def uses_scan_mode(self) -> bool:
        """是否需要扫描模式。

        只要配了多条通道就必须扫描 —— 否则序列只会转换第一条。
        用户显式开了 ``scan_mode`` 也返回真（那是"我要扫描"的意图）。
        """
        return self.scan_mode or len(self.channels) > 1

    @property
    def total_conversion_cycles(self) -> float:
        """一轮扫描的总转换周期数（采样 + 固定开销，按序列累加）。"""
        return sum(item.conversion_cycles for item in self.channels)

    @property
    def effective_watchdog_channel(self) -> AdcChannel | None:
        """看门狗实际监控的通道。

        未显式指定时取序列第一条 —— 那是"单通道 + 看门狗"最常见的用法，
        也是与库默认行为（不调 SingleChannelConfig 时盯通道 0）最接近的语义。
        没有配任何通道时返回 ``None``。
        """
        if self.watchdog_channel is not None:
            return self.watchdog_channel
        return self.channels[0].channel if self.channels else None

    @property
    def effective_watchdog_channel_macro(self) -> str:
        """看门狗通道的库宏名；无通道时返回空串。"""
        channel = self.effective_watchdog_channel
        return channel.value if channel is not None else ""

    def adc_clock_hz(self, pclk2_hz: int) -> float:
        """ADCCLK 频率。"""
        return pclk2_hz / self.prescaler.divider

    def max_sample_rate_hz(self, pclk2_hz: int) -> float:
        """一轮扫描的最高速率（Hz）。

        假设连续转换、无额外开销：``ADCCLK / 总周期``。
        这就是"这套配置最多能多快采一轮"。
        """
        cycles = self.total_conversion_cycles
        if cycles <= 0:
            return 0.0
        return self.adc_clock_hz(pclk2_hz) / cycles

    def describe_clock(self, pclk2_hz: int) -> str:
        """把 ADCCLK 与采样能力写成一行，进生成注释供核对。"""
        adcclk = self.adc_clock_hz(pclk2_hz)
        text = (
            f"ADCCLK = PCLK2 {pclk2_hz / 1e6:g}MHz "
            f"{self.prescaler.label} = {adcclk / 1e6:g}MHz"
        )
        if self.channels:
            total = self.total_conversion_cycles
            text += (
                f"；一轮 {len(self.channels)} 通道共 {total:g} 周期 "
                f"→ 最高 {self.max_sample_rate_hz(pclk2_hz) / 1e3:g}kSPS"
            )
        return text

    # -- 校验 ------------------------------------------------------------

    @field_validator("interrupts")
    @classmethod
    def _dedupe_interrupts(cls, value: list[AdcInterrupt]) -> list[AdcInterrupt]:
        """去重并保持稳定顺序，避免生成重复的 ADC_ITConfig 调用。"""
        seen: set[AdcInterrupt] = set()
        result: list[AdcInterrupt] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @field_validator("channels")
    @classmethod
    def _check_channels(
        cls, value: list[AdcChannelConfig]
    ) -> list[AdcChannelConfig]:
        """通道数量与重复检查。

        同一通道在一条序列里出现两次是合法的（库允许，可用于间隔采样），
        但**本工具不做判别** —— 它几乎总是复制粘贴没改的痕迹，而且
        生成两份 ``ADC_RegularChannelConfig`` 会让用户以为配了两个源。
        宁可报错让人确认一次。
        """
        if len(value) > ADC_SEQUENCE_MAX:
            raise ValueError(
                f"规则组最多 {ADC_SEQUENCE_MAX} 条通道"
                f"（IS_ADC_REGULAR_LENGTH 要求 1~16），当前配了 {len(value)} 条"
            )

        seen: dict[AdcChannel, int] = {}
        for index, item in enumerate(value):
            if item.channel in seen:
                raise ValueError(
                    f"{item.channel.label} 在同一序列里配了多次"
                    f"（第 {seen[item.channel] + 1} 项与第 {index + 1} 项）。"
                    "同一通道每轮只需采一次；确实要间隔采样请用别的通道"
                )
            seen[item.channel] = index
        return value

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("label 不能包含换行符（会破坏生成的注释）")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> AdcConfig:
        """组合一致性检查。

        原则与 ``TimConfig`` 一致：**会产生"看起来配了实际没生成"的组合
        必须报错**，不能静默丢弃。
        """
        # 1) 无通道 = 无事可做。ADC_Init 要求 ADC_NbrOfChannel >= 1，
        #    不拦的话生成代码会触发 assert_param（或用 0 算出非法 L 位）。
        if not self.channels:
            raise ValueError(
                f"{self.name} 没有配置任何规则组通道。"
                "ADC_Init 要求 ADC_NbrOfChannel 至少为 1，空序列没法生成代码；"
                "请至少添加一条通道，或直接删掉这个 ADC 实例"
            )

        # 2) 配了要采外部信号的通道，却没给引脚 —— 这是"看起来配了
        #    实际没生成"的典型。工具仍然能生成 ADC 本体代码（引脚可能
        #    由用户在别处配），所以不在这里硬拦，交给校验层报 INFO。
        #    （模型层只拦"必然产出不能用代码"的情形。）

        # 3) 模拟看门狗阈值高低颠倒。库的 ADC_AnalogWatchdogThresholdsConfig
        #    不检查顺序，但窗口区间为空 → AWD 永远不会触发。
        if self.watchdog and self.watchdog_low > self.watchdog_high:
            raise ValueError(
                f"{self.name} 的模拟看门狗下限 {self.watchdog_low} "
                f"大于上限 {self.watchdog_high}，窗口区间为空，"
                "AWD 永远不会触发。请把两个阈值对调"
            )

        # 4) 没开看门狗却填了非默认阈值 —— 静默丢弃比报错更糟
        if not self.watchdog:
            if self.watchdog_high != ADC_THRESHOLD_MAX or self.watchdog_low != 0:
                raise ValueError(
                    f"{self.name} 没有启用模拟看门狗（watchdog=false），"
                    f"但填了阈值 {self.watchdog_low}~{self.watchdog_high}，"
                    "这两个值不会生效。请先开启 watchdog"
                )
            # 看门狗通道同理
            if self.watchdog_channel is not None:
                raise ValueError(
                    f"{self.name} 没有启用模拟看门狗（watchdog=false），"
                    f"但指定了监控通道 {self.watchdog_channel.label}，"
                    "该设置不会生效。请先开启 watchdog"
                )
            # AWD 中断同理：没开看门狗就没有 AWD 事件
            if AdcInterrupt.AWD in self.interrupts:
                raise ValueError(
                    f"{self.name} 使能了 AWD 中断，但没有启用模拟看门狗 —— "
                    "看门狗不开就不会有越界事件，该中断永远不会触发。"
                    "请把 watchdog 设为 true"
                )

        # 5) 扫描模式与单通道的组合
        #    只配一条通道却开了 scan_mode 是合法的（扫描 1 个通道），
        #    但开了 scan_mode 却只配一条通常是"以为会自动轮询更多通道"。
        #    这条给校验层报 WARNING，模型层不拦。

        # 6) ADC3 专属触发源。C8T6 上没有 ADC3，选了等于不触发。
        #    模型层只列了 ADC1/ADC2，所以这里必然矛盾 —— 报出来。
        if self.trigger.adc3_only:
            raise ValueError(
                f"{self.name} 选了触发源 {self.trigger.value}，"
                "该宏只对 ADC3 有效（stm32f10x_adc.h 的注释是 For ADC3 only），"
                "而 C8T6/C6T6 上只有 ADC1 与 ADC2，转换永远不会被触发。"
                f"可用的触发源：{'、'.join(t.label for t in AdcTrigger.options_for_chip())}"
            )

        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成注释。"""
        parts = [self.name, self.mode.name]
        if self.channels:
            parts.append("通道:" + "+".join(c.channel.name for c in self.channels))
        if self.interrupts:
            parts.append("中断:" + "+".join(i.name for i in self.interrupts))
        if self.watchdog:
            parts.append(f"看门狗:{self.watchdog_low}~{self.watchdog_high}")
        if self.label:
            parts.append(f"({self.label})")
        return " ".join(parts)


def channel_range_text() -> str:
    """通道号范围的文字说明，供报错信息复用。"""
    return f"{ADC_CHANNEL_MIN}~{ADC_CHANNEL_MAX}"
