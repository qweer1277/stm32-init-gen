"""EXTI / AFIO / TIM 模型层的测试。

三类重点：

1. **枚举符号真实存在** —— 反编造约束在这些外设上同样成立
2. **组合约束** —— 哪些配置组合是自相矛盾或物理上不可能的
3. **芯片规格约束** —— 线号即引脚号、LQFP48 上没有的引脚不能被配置
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stm32gen.models import (
    AfioConfig,
    AfioSwjRemap,
    ExtiLineConfig,
    ExtiMode,
    Stm32Config,
    TimChannelConfig,
    TimClockDivision,
    TimClockSource,
    TimConfig,
    TimCounterMode,
    TimExtTriggerPolarity,
    TimExtTriggerPrescaler,
    TimInstance,
    TimRemap,
    solve_period,
    solve_timing,
)

# --------------------------------------------------------------------------
# 枚举与派生属性
# --------------------------------------------------------------------------


def test_tim_instances_are_only_the_four_real_ones() -> None:
    """中容量 LQFP48 只有 TIM1~TIM4。

    TIM5~TIM7 的 ``TIMx_IRQn`` 在标准库 ``IRQn_Type`` 里不存在
    （实测 ``TIM5_IRQn`` / ``TIM6_IRQn`` / ``TIM7_IRQn`` 都查不到），
    所以绝不能出现在选项里 —— 加了会在生成 NVIC 代码时找不到中断号。
    """
    assert [item.value for item in TimInstance] == ["TIM1", "TIM2", "TIM3", "TIM4"]


def test_tim1_is_the_only_advanced_timer() -> None:
    assert TimInstance.TIM1.is_advanced
    for name in ("TIM2", "TIM3", "TIM4"):
        assert not TimInstance(name).is_advanced


def test_tim_irqn_per_instance() -> None:
    """TIM1 的更新事件用 ``TIM1_UP_IRQn``（高级定时器中断被拆成 4 个）。"""
    assert TimInstance.TIM1.irqn == "TIM1_UP_IRQn"
    assert TimInstance.TIM2.irqn == "TIM2_IRQn"
    assert TimInstance.TIM4.irqn == "TIM4_IRQn"


def test_tim_clock_macro_per_bus() -> None:
    assert TimInstance.TIM1.clock_macro == "RCC_APB2Periph_TIM1"
    assert TimInstance.TIM2.clock_macro == "RCC_APB1Periph_TIM2"


def test_tim_remap_options_are_asymmetric() -> None:
    """重映射选项按实例过滤，且**不对称**：TIM4 没有部分重映射。

    宏名也不规则：TIM2/TIM3 用 ``GPIO_PartialRemap*``/``GPIO_FullRemap*``，
    TIM4 是 ``GPIO_Remap_TIM4`` —— 猜 ``GPIO_Remap_TIM2`` 会直接编译失败。
    """
    tim4 = [item.name for item in TimRemap.options_for(TimInstance.TIM4)]
    assert tim4 == ["NONE", "TIM4"]

    tim2 = [item.name for item in TimRemap.options_for(TimInstance.TIM2)]
    assert tim2 == ["NONE", "TIM2_PARTIAL1", "TIM2_PARTIAL2", "TIM2_FULL"]

    tim3 = [item.name for item in TimRemap.options_for(TimInstance.TIM3)]
    assert tim3 == ["NONE", "TIM3_PARTIAL", "TIM3_FULL"]


def test_tim_remap_instance_inference() -> None:
    assert TimRemap.TIM3_PARTIAL.instance is TimInstance.TIM3
    assert TimRemap.TIM4.instance is TimInstance.TIM4
    assert TimRemap.NONE.instance is None


def test_afio_swj_release_sets() -> None:
    """各选项释放哪些引脚、是否保留 SWD。"""
    assert AfioSwjRemap.NONE.releases == ()
    assert AfioSwjRemap.JTAG_DISABLE.releases == ("PA15", "PB3", "PB4")
    assert AfioSwjRemap.SWJ_DISABLE.releases == ("PA13", "PA14", "PA15", "PB3", "PB4")
    assert AfioSwjRemap.NO_JTRST.releases == ("PB4",)

    # keeps_swd 是关键安全性判据
    assert AfioSwjRemap.JTAG_DISABLE.keeps_swd
    assert AfioSwjRemap.NO_JTRST.keeps_swd
    assert not AfioSwjRemap.SWJ_DISABLE.keeps_swd


# --------------------------------------------------------------------------
# EXTI
# --------------------------------------------------------------------------


def test_exti_derived_names() -> None:
    line = ExtiLineConfig(line=3, port="B")
    assert line.name == "EXTI3"
    assert line.pin_name == "PB3"
    assert line.line_macro == "EXTI_Line3"
    assert line.port_source_macro == "GPIO_PortSourceGPIOB"
    assert line.pin_source_macro == "GPIO_PinSource3"


def test_exti_shared_irq_channels() -> None:
    """线 0~4 各自独立，5~9 共用一个通道，10~15 共用一个。

    这是 STM32 的硬件分组，直接决定生成几个 ISR。
    """
    assert ExtiLineConfig(line=0, port="A").irqn == "EXTI0_IRQn"
    assert ExtiLineConfig(line=4, port="A").irqn == "EXTI4_IRQn"
    assert ExtiLineConfig(line=5, port="A").irqn == "EXTI9_5_IRQn"
    assert ExtiLineConfig(line=9, port="A").irqn == "EXTI9_5_IRQn"
    assert ExtiLineConfig(line=10, port="A").irqn == "EXTI15_10_IRQn"
    assert ExtiLineConfig(line=15, port="A").irqn == "EXTI15_10_IRQn"


def test_exti_handler_names_match_startup_vector() -> None:
    """ISR 函数名必须与启动文件向量表一致。

    实测 ``Start\\startup_stm32f10x_md.s`` 里是 ``EXTI0_IRQHandler``
    与 ``EXTI9_5_IRQHandler`` / ``EXTI15_10_IRQHandler``。
    """
    assert ExtiLineConfig(line=0, port="A").handler_name == "EXTI0_IRQHandler"
    assert ExtiLineConfig(line=5, port="A").handler_name == "EXTI9_5_IRQHandler"
    assert ExtiLineConfig(line=12, port="A").handler_name == "EXTI15_10_IRQHandler"


def test_exti_event_mode_needs_no_nvic() -> None:
    """事件模式不产生中断，不需要 NVIC 通道。"""
    interrupt = ExtiLineConfig(line=0, port="A", mode=ExtiMode.INTERRUPT)
    event = ExtiLineConfig(line=0, port="A", mode=ExtiMode.EVENT)
    assert interrupt.needs_nvic
    assert not event.needs_nvic


def test_exti_line_range_enforced() -> None:
    """线号 16~19 是内部事件线（PVD/RTC/USB/ETH），本工具不支持。"""
    with pytest.raises(ValidationError):
        ExtiLineConfig(line=16, port="A")
    with pytest.raises(ValidationError):
        ExtiLineConfig(line=-1, port="A")


def test_exti_rejects_output_pin_mode() -> None:
    """EXTI 检测引脚上的外部电平变化，引脚必须配成输入。"""
    with pytest.raises(ValidationError, match="输入模式"):
        ExtiLineConfig(line=0, port="A", pin_mode="Out_PP")


def test_exti_pin_must_exist_on_chip() -> None:
    """线号即引脚号：EXTI12 + GPIOC = PC12，LQFP48 上 PC 只有 13/14/15。"""
    with pytest.raises(ValidationError, match="引脚"):
        Stm32Config(exti=[ExtiLineConfig(line=12, port="C")])


def test_exti_valid_pin_accepted() -> None:
    config = Stm32Config(exti=[ExtiLineConfig(line=13, port="C")])
    assert config.exti[0].pin_name == "PC13"


# --------------------------------------------------------------------------
# TIM
# --------------------------------------------------------------------------


def test_tim_frequency_math() -> None:
    """更新频率 = 定时器时钟 / ((PSC+1)(ARR+1))。"""
    timer = TimConfig(instance="TIM2", prescaler=71, period=999)
    # 72MHz / (72 × 1000) = 1000Hz
    assert timer.update_frequency_hz(72_000_000) == pytest.approx(1000.0)
    assert timer.counter_frequency_hz(72_000_000) == pytest.approx(1_000_000.0)


def test_tim_center_aligned_halves_update_frequency() -> None:
    """中央对齐下计数器先升后降，一个完整周期走 2 倍计数。"""
    up = TimConfig(instance="TIM2", prescaler=71, period=999, counter_mode=TimCounterMode.UP)
    center = TimConfig(
        instance="TIM2",
        prescaler=71,
        period=999,
        counter_mode=TimCounterMode.CENTER_ALIGNED1,
    )
    assert center.update_frequency_hz(72_000_000) == pytest.approx(
        up.update_frequency_hz(72_000_000) / 2
    )


def test_tim_ckd_does_not_affect_frequency() -> None:
    """CKD 只改滤波器/死区的时间基准，**不影响计数频率**。

    这是最常见的误解：想改周期却去调 CKD，结果频率纹丝不动。
    """
    base = TimConfig(instance="TIM2", prescaler=71, period=999)
    with_ckd = TimConfig(
        instance="TIM2",
        prescaler=71,
        period=999,
        clock_division=TimClockDivision.DIV4,
    )
    assert with_ckd.update_frequency_hz(72_000_000) == pytest.approx(
        base.update_frequency_hz(72_000_000)
    )


def test_tim_duty_percent() -> None:
    timer = TimConfig(
        instance="TIM2",
        period=999,
        channels=[TimChannelConfig(channel="TIM_Channel_1", pulse=250)],
    )
    assert timer.duty_percent(timer.channels[0]) == pytest.approx(25.0)


def test_tim_repetition_counter_only_for_advanced() -> None:
    """RCR 只有高级定时器（TIM1/TIM8）有寄存器，通用定时器上无意义。"""
    TimConfig(instance="TIM1", repetition_counter=3)  # 合法
    with pytest.raises(ValidationError, match="高级定时器"):
        TimConfig(instance="TIM2", repetition_counter=3)


def test_tim_remap_must_match_instance() -> None:
    """TIM3 不能选 TIM2 的重映射宏。"""
    with pytest.raises(ValidationError, match="重映射"):
        TimConfig(instance="TIM3", remap="GPIO_PartialRemap1_TIM2")


def test_tim_remap_matching_instance_ok() -> None:
    TimConfig(instance="TIM3", remap="GPIO_PartialRemap_TIM3")
    TimConfig(instance="TIM4", remap="GPIO_Remap_TIM4")


def test_tim_cc_interrupt_requires_channel() -> None:
    """没有通道就没有捕获比较事件，CC 中断永远不会触发。"""
    with pytest.raises(ValidationError, match="通道"):
        TimConfig(instance="TIM2", interrupts=["TIM_IT_CC1"])


def test_tim_cc_interrupt_with_channel_ok() -> None:
    TimConfig(
        instance="TIM2",
        channels=[TimChannelConfig(channel="TIM_Channel_1")],
        interrupts=["TIM_IT_CC1"],
    )


def test_tim_break_trigger_only_for_advanced() -> None:
    with pytest.raises(ValidationError, match="高级定时器"):
        TimConfig(instance="TIM2", interrupts=["TIM_IT_Break"])
    # TIM1 可以
    TimConfig(
        instance="TIM1",
        channels=[TimChannelConfig(channel="TIM_Channel_1")],
        interrupts=["TIM_IT_Break"],
    )


def test_tim_duplicate_channel_rejected() -> None:
    """同一通道配两次会生成两次 TIM_OCxInit，后一次生效，静默丢配置。"""
    with pytest.raises(ValidationError, match="配了多次"):
        TimConfig(
            instance="TIM2",
            channels=[
                TimChannelConfig(channel="TIM_Channel_1", pulse=1),
                TimChannelConfig(channel="TIM_Channel_1", pulse=2),
            ],
        )


def test_tim_channel_port_and_pin_must_pair() -> None:
    """端口与引脚必须同时给或同时留空 —— 只给一个会静默跳过 GPIO 配置。"""
    with pytest.raises(ValidationError, match="同时"):
        TimChannelConfig(channel="TIM_Channel_1", port="A")
    with pytest.raises(ValidationError, match="同时"):
        TimChannelConfig(channel="TIM_Channel_1", pin=6)


def test_tim_channel_pin_must_exist_on_chip() -> None:
    with pytest.raises(ValidationError):
        Stm32Config(
            tim=[
                TimConfig(
                    instance="TIM3",
                    channels=[TimChannelConfig(channel="TIM_Channel_1", port="C", pin=6)],
                )
            ]
        )


def test_tim_register_range_enforced() -> None:
    """PSC / ARR 都是 16 位寄存器。"""
    with pytest.raises(ValidationError):
        TimConfig(instance="TIM2", prescaler=0x10000)
    with pytest.raises(ValidationError):
        TimConfig(instance="TIM2", period=0x10000)


def test_tim_describe_frequency_mentions_clock() -> None:
    text = TimConfig(instance="TIM2", prescaler=71, period=999).describe_frequency(
        72_000_000
    )
    assert "72MHz" in text
    assert "1000" in text


# --------------------------------------------------------------------------
# AFIO
# --------------------------------------------------------------------------


def test_afio_enabled_follows_swj_remap() -> None:
    """选了 SWJ 释放就自动启用，不必再回答"要不要启用"。"""
    config = AfioConfig(swj_remap="GPIO_Remap_SWJ_JTAGDisable")
    assert config.enabled
    assert config.has_swj
    assert config.needs_afio_clock


def test_afio_none_is_disabled() -> None:
    config = AfioConfig()
    assert not config.enabled
    assert not config.has_swj
    assert not config.needs_afio_clock


def test_afio_swj_disable_flagged_as_bricking() -> None:
    """关掉 SWD 会让调试器再也连不上 —— 校验层必须报 ERROR。"""
    config = AfioConfig(swj_remap="GPIO_Remap_SWJ_Disable")
    assert config.bricks_debugger
    assert not AfioConfig(swj_remap="GPIO_Remap_SWJ_JTAGDisable").bricks_debugger


def test_afio_enabled_false_with_swj_is_contradictory() -> None:
    """显式关闭却选了释放：生成的代码不会释放，但配置看起来像已释放。"""
    with pytest.raises(ValidationError, match="enabled"):
        AfioConfig(swj_remap="GPIO_Remap_SWJ_JTAGDisable", enabled=False)


# --------------------------------------------------------------------------
# 整配置层面的引脚推导
# --------------------------------------------------------------------------


def test_used_ports_includes_exti_and_tim() -> None:
    """每个"会隐式占用引脚"的外设都要在 used_ports 里登记。

    漏掉的话那个端口的 ``RCC_APB2Periph_GPIOx`` 时钟不会被使能，
    ``GPIO_Init`` 静默无效。
    """
    config = Stm32Config(
        exti=[ExtiLineConfig(line=0, port="B")],
        tim=[
            TimConfig(
                instance="TIM3",
                channels=[TimChannelConfig(channel="TIM_Channel_1", port="A", pin=6)],
            )
        ],
    )
    assert config.used_ports == ["A", "B"]


def test_needs_afio_clock_for_exti_only() -> None:
    """**EXTI 也必须开 AFIO 时钟** —— 这条最容易漏。

    ``GPIO_EXTILineConfig`` 写的是 ``AFIO->EXTICR``，
    用户往往以为"外部中断是 EXTI 外设的事"，不开 AFIO 时钟时这句
    完全无效且不报错。
    """
    assert Stm32Config(exti=[ExtiLineConfig(line=0, port="A")]).needs_afio_clock
    assert not Stm32Config().needs_afio_clock


def test_needs_afio_clock_for_tim_remap() -> None:
    assert Stm32Config(tim=[TimConfig(instance="TIM4", remap="GPIO_Remap_TIM4")]).needs_afio_clock

# --------------------------------------------------------------------------
# 定时参数反算：由期望周期推 PSC/ARR
# --------------------------------------------------------------------------


def test_solve_timing_exact_for_round_targets() -> None:
    """能整除的目标必须给出零误差解，不能"差不多就行"。"""
    for clock, target in [(72_000_000, 1000), (72_000_000, 50), (8_000_000, 1)]:
        solution = solve_timing(clock, target)
        assert solution.exact, solution.describe()
        assert solution.actual_hz == pytest.approx(target)


def test_solve_timing_prefers_large_arr() -> None:
    """同一总分频下优先让 ARR 大 —— ARR 越大占空比档位越多。

    72MHz 求 1kHz 需要总分频 72000；``PSC=9/ARR=7199`` 与
    ``PSC=1/ARR=35999`` 频率相同，但后者档位是前者的 5 倍。
    """
    solution = solve_timing(72_000_000, 1000)
    assert solution.duty_steps > 10_000
    # PSC 能小就小，不白白牺牲占空比分辨率
    assert solution.prescaler < 10


def test_solve_timing_center_aligned_halves_frequency() -> None:
    """中央对齐要上下各数一遍，同样的 PSC/ARR 频率只有一半。"""
    edge = solve_timing(72_000_000, 1000)
    center = solve_timing(72_000_000, 1000, center_aligned=True)
    assert center.actual_hz == pytest.approx(1000)
    assert center.divider * 2 == pytest.approx(edge.divider)


def test_solve_timing_matches_model_frequency() -> None:
    """反算结果代回模型必须得到同一个频率（两套算法不能各算一套）。"""
    for target in (1000, 50, 20_000):
        solution = solve_timing(72_000_000, target)
        config = TimConfig(
            instance="TIM2", prescaler=solution.prescaler, period=solution.period
        )
        assert config.update_frequency_hz(72_000_000) == pytest.approx(solution.actual_hz)


def test_solve_timing_reports_error_when_not_divisible() -> None:
    """除不尽时如实报误差，不假装精确。"""
    solution = solve_timing(72_000_000, 777)
    assert not solution.exact
    assert solution.error_percent != 0.0
    assert "误差" in solution.describe()


def test_solve_timing_rejects_impossible_targets() -> None:
    """超范围的目标要报错，而不是给出一个跑不对的 PSC/ARR。"""
    with pytest.raises(ValueError, match="高于"):
        solve_timing(72_000_000, 100_000_000)
    with pytest.raises(ValueError, match="太低"):
        solve_timing(72_000_000, 0.001)
    with pytest.raises(ValueError, match="大于 0"):
        solve_timing(72_000_000, 0)
    with pytest.raises(ValueError, match="大于 0"):
        solve_timing(0, 1000)


def test_solve_period_accepts_seconds() -> None:
    """按周期反算与按频率反算必须等价。"""
    by_period = solve_period(72_000_000, 0.02)
    by_freq = solve_timing(72_000_000, 50)
    assert by_period.prescaler == by_freq.prescaler
    assert by_period.period == by_freq.period


def test_timing_solution_formats_human_readable_period() -> None:
    """周期文案要带单位，否则 0.02 到底是秒还是毫秒看不出来。"""
    assert "ms" in solve_timing(72_000_000, 50).describe()
    assert "us" in solve_timing(72_000_000, 20_000).describe()
    assert solve_period(72_000_000, 2.5).describe().count("2.5s") == 1

# --------------------------------------------------------------------------
# TIM 外部时钟
# --------------------------------------------------------------------------


def test_clock_source_enum_covers_three_external_kinds() -> None:
    """外部时钟的三种来源都要有：ETR 引脚、TI 捕获通道、ITR 级联。

    只做 ETR 会漏掉最常见的"用另一个定时器当时钟"（ITR）；
    只做 TI 会漏掉专用 ETR 引脚。
    """
    assert any(item.is_etr for item in TimClockSource)
    assert any(item.is_ti for item in TimClockSource)
    assert any(item.is_itr for item in TimClockSource)
    assert TimClockSource.INTERNAL.is_internal
    assert not TimClockSource.INTERNAL.is_etr


def test_clock_source_config_function_names_match_library() -> None:
    """配置函数名必须与库里的真实名字一致。

    这几个名字是库源码 ``stm32f10x_tim.c`` 里的实际函数名；写错了
    生成出来的代码编译不过（而且会被反编造护栏拦下，双重保险）。
    """
    from stm32gen.stdlib_ref import default_index

    index = default_index()
    for source in TimClockSource:
        if source.is_internal:
            assert source.config_function == ""
            continue
        assert index.has_any(source.config_function), (
            f"{source.name} 的 config_function {source.config_function} 不在库里"
        )


def test_etr_prescaler_dividers() -> None:
    """ETR 预分频的分频系数：OFF 是 1，不是 0。"""
    assert TimExtTriggerPrescaler.OFF.divider == 1
    assert TimExtTriggerPrescaler.DIV2.divider == 2
    assert TimExtTriggerPrescaler.DIV4.divider == 4
    assert TimExtTriggerPrescaler.DIV8.divider == 8


def test_etr_clock_uses_external_frequency_not_clock_tree() -> None:
    """外部时钟的频率推导只认 external_clock_hz，绝不能用时钟树的 TIMxCLK。

    这是本功能最容易搞错的地方：传 72MHz 进去若被当成计数时钟，
    会算出一个看着合理、实际差几个数量级的频率。
    """
    timer = TimConfig(
        instance="TIM2",
        clock_source=TimClockSource.ETR_MODE1,
        etr_prescaler=TimExtTriggerPrescaler.DIV4,
        prescaler=0,
        period=99,
        external_clock_hz=1000.0,
    )
    # 1000Hz ÷ 4（ETR）÷ 1（PSC） = 250Hz 计数；÷100（ARR+1） = 2.5Hz 更新
    assert timer.counter_frequency_hz(72_000_000) == pytest.approx(250.0)
    assert timer.update_frequency_hz(72_000_000) == pytest.approx(2.5)


def test_external_clock_without_frequency_reports_unknown() -> None:
    """没填外部频率时频率算不出来，必须返回 None / 0 而不是编一个数。"""
    timer = TimConfig(
        instance="TIM2",
        clock_source=TimClockSource.ETR_MODE1,
        prescaler=0,
        period=99,
    )
    assert timer.effective_clock_hz(72_000_000) is None
    assert timer.update_frequency_hz(72_000_000) == 0.0
    assert "未知" in timer.describe_frequency(72_000_000)


def test_internal_clock_ignores_external_frequency_field() -> None:
    """内部时钟下 external_clock_hz 不参与计算。

    留着它只为让用户切回内部时钟时不丢输入；若真参与计算，
    内部时钟的周期就错了。
    """
    timer = TimConfig(instance="TIM2", prescaler=71, period=999, external_clock_hz=1.0)
    assert timer.effective_clock_hz(72_000_000) == 72_000_000
    assert timer.update_frequency_hz(72_000_000) == pytest.approx(1000.0)


def test_internal_clock_rejects_etr_fields() -> None:
    """内部时钟配 ETR 参数要报错，而不是静默丢弃。

    静默丢弃比报错更糟：用户以为配上了 ETR 预分频，生成的代码里
    一个字都没有，然后去示波器上找问题。
    """
    with pytest.raises(ValueError, match="内部时钟"):
        TimConfig(instance="TIM2", etr_prescaler=TimExtTriggerPrescaler.DIV2)
    with pytest.raises(ValueError, match="内部时钟"):
        TimConfig(instance="TIM2", etr_polarity=TimExtTriggerPolarity.INVERTED)
    with pytest.raises(ValueError, match="内部时钟"):
        TimConfig(instance="TIM2", etr_filter=5)


def test_etr_filter_range_is_enforced() -> None:
    """滤波值上限 15：库里的断言是 ``IS_TIM_EXT_FILTER(x) ((x) <= 0xF)``。"""
    TimConfig(instance="TIM2", clock_source=TimClockSource.ETR_MODE1, etr_filter=15)
    with pytest.raises(ValueError):
        TimConfig(instance="TIM2", clock_source=TimClockSource.ETR_MODE1, etr_filter=16)


def test_etr_total_divider_only_applies_to_etr() -> None:
    """ETR 预分频只对 ETR 模式生效；TI/ITR 没有这一级。"""
    etr = TimConfig(
        instance="TIM2",
        clock_source=TimClockSource.ETR_MODE1,
        etr_prescaler=TimExtTriggerPrescaler.DIV8,
    )
    assert etr.etr_total_divider == 8

    ti = TimConfig(instance="TIM2", clock_source=TimClockSource.TI1)
    assert ti.etr_total_divider == 1

    itr = TimConfig(instance="TIM2", clock_source=TimClockSource.ITR0)
    assert itr.etr_total_divider == 1
