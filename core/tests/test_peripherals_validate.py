"""EXTI / AFIO / TIM 的校验规则与生成器测试。

分两块：

- **校验**（check_afio / check_exti / check_tim）—— 该报的报了、不该报的没报
- **生成** —— 产物结构、调用顺序、关键正确性点（MOE、重映射归 AFIO 等）
"""

from __future__ import annotations

from stm32gen.generator import build_context, generate
from stm32gen.models import (
    AfioConfig,
    ExtiLineConfig,
    ExtiMode,
    NvicChannelConfig,
    NvicConfig,
    Stm32Config,
    TimChannelConfig,
    TimConfig,
    UsartConfig,
)
from stm32gen.validate import Severity, has_blocking, validate


def codes(issues, severity: Severity | None = None) -> set[str]:
    return {i.code for i in issues if severity is None or i.severity is severity}


# --------------------------------------------------------------------------
# check_afio
# --------------------------------------------------------------------------


def test_swj_disable_is_error() -> None:
    """关闭 SWD 后调试器再也连不上 —— 必须 ERROR，不是提示。

    这是全工具唯一一处"不可逆操作层面风险"的规则：用户在下拉里选中
    ``GPIO_Remap_SWJ_Disable`` 时多半只看到"多出 5 个 GPIO"，
    不会意识到代价是芯片变砖风险。
    """
    issues = validate(Stm32Config(afio=AfioConfig(swj_remap="GPIO_Remap_SWJ_Disable")))
    assert "afio.swj_remap_bricks_debugger" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "afio.swj_remap_bricks_debugger")
    # hint 必须给出安全替代方案
    assert "JTAGDisable" in item.hint


def test_jtag_disable_is_safe() -> None:
    """只关 JTAG、保留 SWD 是安全选项，不该报 ERROR。"""
    issues = validate(
        Stm32Config(afio=AfioConfig(swj_remap="GPIO_Remap_SWJ_JTAGDisable"))
    )
    assert not has_blocking(issues)


def test_afio_unused_release_is_info() -> None:
    """释放了引脚却没用到 —— 提示一下，避免白担风险。"""
    issues = validate(
        Stm32Config(afio=AfioConfig(swj_remap="GPIO_Remap_SWJ_JTAGDisable"))
    )
    assert "afio.swj_remap_unused" in codes(issues)


def test_afio_clock_auto_enabled_info_mentions_exti() -> None:
    """AFIO 时钟的自动使能说明里要提到 EXTI 的理由。

    ``GPIO_EXTILineConfig`` 写 ``AFIO->EXTICR``，这条最容易漏。
    """
    issues = validate(Stm32Config(exti=[ExtiLineConfig(line=0, port="A")]))
    item = next(i for i in issues if i.code == "afio.clock_auto_enabled")
    assert "EXTI" in item.message


# --------------------------------------------------------------------------
# check_exti
# --------------------------------------------------------------------------


def test_exti_same_line_conflict_is_error() -> None:
    """同一条线只能接一个端口 —— 硬件约束。

    AFIO 的 EXTICR 每条线只有一份端口选择，后写的会覆盖先写的，
    表现为其中一个中断永不触发。
    """
    config = Stm32Config(
        exti=[ExtiLineConfig(line=0, port="A"), ExtiLineConfig(line=0, port="B")]
    )
    issues = validate(config)
    assert "exti.line_conflict" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "exti.line_conflict")
    assert "PA0" in item.message and "PB0" in item.message


def test_exti_different_lines_no_conflict() -> None:
    config = Stm32Config(
        exti=[ExtiLineConfig(line=0, port="A"), ExtiLineConfig(line=1, port="B")]
    )
    assert "exti.line_conflict" not in codes(validate(config))


def test_exti_irq_without_nvic_is_error() -> None:
    config = Stm32Config(exti=[ExtiLineConfig(line=1, port="A")])
    issues = validate(config)
    assert "exti.irq_without_nvic" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "exti.irq_without_nvic")
    assert "EXTI1_IRQn" in item.message


def test_exti_irq_with_nvic_ok() -> None:
    config = Stm32Config(
        exti=[ExtiLineConfig(line=1, port="A")],
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="EXTI1_IRQn")]),
    )
    assert "exti.irq_without_nvic" not in codes(validate(config))


def test_exti_event_mode_needs_no_nvic() -> None:
    """事件模式不产生中断，不配 NVIC 是**正确**的，不能报错。"""
    config = Stm32Config(
        exti=[ExtiLineConfig(line=2, port="A", mode=ExtiMode.EVENT)]
    )
    assert "exti.irq_without_nvic" not in codes(validate(config))


def test_exti_event_mode_with_nvic_warns() -> None:
    """事件模式却配了 NVIC 通道 —— 该通道永远不会被触发。"""
    config = Stm32Config(
        exti=[ExtiLineConfig(line=2, port="A", mode=ExtiMode.EVENT)],
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="EXTI2_IRQn")]),
    )
    issues = validate(config)
    assert "exti.event_with_nvic" in codes(issues, Severity.WARNING)


def test_exti_shared_channel_is_info() -> None:
    """线 5~9 共用一个 ISR —— 提示用户必须逐个判标志位。"""
    config = Stm32Config(
        exti=[ExtiLineConfig(line=5, port="A"), ExtiLineConfig(line=6, port="A")],
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="EXTI9_5_IRQn")]),
    )
    issues = validate(config)
    assert "exti.shared_irq_channel" in codes(issues)
    item = next(i for i in issues if i.code == "exti.shared_irq_channel")
    assert "EXTI9_5_IRQn" in item.message


def test_exti_nvic_disabled_warns() -> None:
    config = Stm32Config(
        exti=[ExtiLineConfig(line=1, port="A")],
        nvic=NvicConfig(
            channels=[NvicChannelConfig(irq_channel="EXTI1_IRQn", enabled=False)]
        ),
    )
    assert "exti.irq_nvic_disabled" in codes(validate(config), Severity.WARNING)


# --------------------------------------------------------------------------
# check_tim
# --------------------------------------------------------------------------


def test_tim_duplicate_instance_is_error() -> None:
    config = Stm32Config(tim=[TimConfig(instance="TIM2"), TimConfig(instance="TIM2")])
    assert "tim.duplicate_instance" in codes(validate(config), Severity.ERROR)


def test_tim_irq_without_nvic_is_error() -> None:
    config = Stm32Config(
        tim=[TimConfig(instance="TIM2", interrupts=["TIM_IT_Update"])]
    )
    assert "tim.irq_without_nvic" in codes(validate(config), Severity.ERROR)


def test_tim_frequency_summary_reported() -> None:
    """把算出来的频率直接摆进提示里，便于对着示波器核对。"""
    config = Stm32Config(tim=[TimConfig(instance="TIM2", prescaler=71, period=999)])
    issues = validate(config)
    assert "tim.frequency_summary" in codes(issues)
    item = next(i for i in issues if i.code == "tim.frequency_summary")
    assert "1000" in item.message


def test_tim_frequency_summary_mentions_timer_clock_not_pclk() -> None:
    """定时器时钟是 PCLK 的 2 倍时，提示里要说清楚。"""
    config = Stm32Config(tim=[TimConfig(instance="TIM2", prescaler=71, period=999)])
    item = next(
        i for i in validate(config) if i.code == "tim.frequency_summary"
    )
    assert item.hint and "2 倍" in item.hint


def test_tim_advanced_moe_hint() -> None:
    """TIM1 有 PWM 通道时必须开主输出，否则引脚没有波形。"""
    config = Stm32Config(
        tim=[
            TimConfig(
                instance="TIM1",
                channels=[TimChannelConfig(channel="TIM_Channel_1", port="A", pin=8)],
            )
        ]
    )
    issues = validate(config)
    assert "tim.advanced_needs_moe" in codes(issues)


def test_tim_general_timer_no_moe_hint() -> None:
    """通用定时器（TIM2~4）没有 BDTR 寄存器，不该出现 MOE 提示。"""
    config = Stm32Config(
        tim=[
            TimConfig(
                instance="TIM3",
                channels=[TimChannelConfig(channel="TIM_Channel_1", port="A", pin=6)],
            )
        ]
    )
    assert "tim.advanced_needs_moe" not in codes(validate(config))


def test_tim_pin_not_default_is_warning_not_error() -> None:
    """非默认引脚只给 WARNING，绝不报 ERROR。

    本环境拿不到数据手册（``src/stm32gen/models/tim_pins.py`` 里写明了
    这张表仅作参考），用户按手册选了非默认引脚是完全合理的 ——
    报 ERROR 会挡住正确配置，那是工具在犯错。
    """
    config = Stm32Config(
        tim=[
            TimConfig(
                instance="TIM3",
                channels=[TimChannelConfig(channel="TIM_Channel_1", port="B", pin=5)],
            )
        ]
    )
    issues = validate(config)
    assert "tim.pin_not_default" in codes(issues, Severity.WARNING)
    assert "tim.pin_not_default" not in codes(issues, Severity.ERROR)


def test_tim_nvic_without_irq_warns() -> None:
    config = Stm32Config(
        tim=[TimConfig(instance="TIM2")],
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="TIM2_IRQn")]),
    )
    assert "tim.nvic_without_irq" in codes(validate(config), Severity.WARNING)


# --------------------------------------------------------------------------
# 生成：产物结构
# --------------------------------------------------------------------------


def test_generates_afio_exti_tim_files() -> None:
    result = generate(Stm32Config())
    for name in ("bsp_afio.c", "bsp_afio.h", "bsp_exti.c", "bsp_exti.h",
                 "bsp_tim.c", "bsp_tim.h"):
        assert result.by_name(name) is not None, f"缺产物 {name}"


def test_init_call_order_full() -> None:
    """bsp_init() 的调用顺序：rcc → afio → gpio → exti → usart → tim → nvic。

    两个关键约束：
    - **afio 必须在 gpio 之前**：重映射要先于 GPIO_Init 生效
    - **exti 必须在 gpio 之后**：引脚要先配成输入，否则会误触发
    """
    content = generate(Stm32Config()).by_name("bsp_init.c").content
    body = content.split("void bsp_init(void)")[1]
    order = [
        "bsp_rcc_init()",
        "bsp_afio_init()",
        "bsp_gpio_init()",
        "bsp_exti_init()",
        "bsp_usart_init()",
        "bsp_tim_init()",
        "bsp_nvic_init()",
    ]
    positions = [body.index(call) for call in order]
    assert positions == sorted(positions), f"调用顺序不对：{order}"


def test_remap_lives_in_afio_not_peripheral() -> None:
    """全部重映射统一在 bsp_afio.c。

    若按"外设归属"把 TIM 重映射放进 bsp_tim.c，它会在 bsp_gpio_init()
    之后才跑 —— 引脚已经按旧映射配成复用输出了，改映射来源后那个脚
    不再是定时器的输出脚，现象是"PWM 没有波形"。
    """
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1", remap="USART1")],
        tim=[TimConfig(instance="TIM3", remap="GPIO_PartialRemap_TIM3")],
    )
    result = generate(config)
    afio = result.by_name("bsp_afio.c").content
    assert "GPIO_Remap_USART1" in afio
    assert "GPIO_PartialRemap_TIM3" in afio
    # 各外设文件里不该有重映射调用
    assert "GPIO_PinRemapConfig(" not in result.by_name("bsp_tim.c").content
    assert "GPIO_PinRemapConfig(" not in result.by_name("bsp_gpio.c").content


def test_exti_line_config_before_exti_init() -> None:
    """必须先 GPIOLineConfig 选端口，再 EXTI_Init 配边沿。"""
    config = Stm32Config(exti=[ExtiLineConfig(line=0, port="A")])
    content = generate(config).by_name("bsp_exti.c").content
    line_cfg = content.index("GPIO_EXTILineConfig(")
    exti_init = content.index("EXTI_Init(")
    assert line_cfg < exti_init


def test_exti_event_mode_still_generates_config() -> None:
    """事件模式也要配 EXTI（只是不产生中断）。"""
    config = Stm32Config(exti=[ExtiLineConfig(line=2, port="A", mode=ExtiMode.EVENT)])
    content = generate(config).by_name("bsp_exti.c").content
    assert "EXTI_Mode_Event" in content
    # 事件模式没有 ISR
    assert "EXTI2_IRQHandler" not in generate(config).by_name("bsp_it.c").content


def test_shared_exti_lines_merged_into_one_handler() -> None:
    """共用一个通道的多条线必须合并到同一个 ISR。

    生成两个同名函数会直接编译失败（重复定义）。
    """
    config = Stm32Config(
        exti=[ExtiLineConfig(line=5, port="A"), ExtiLineConfig(line=6, port="A"),
              ExtiLineConfig(line=9, port="A")],
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="EXTI9_5_IRQn")]),
    )
    content = generate(config).by_name("bsp_it.c").content
    assert content.count("void EXTI9_5_IRQHandler(void)") == 1
    # 三条线各有分支
    for line in (5, 6, 9):
        assert f"EXTI_Line{line}" in content


def test_tim1_needs_moe_call() -> None:
    """TIM1 有 PWM 通道必须调 TIM_CtrlPWMOutputs。"""
    config = Stm32Config(
        tim=[
            TimConfig(
                instance="TIM1",
                channels=[TimChannelConfig(channel="TIM_Channel_1", port="A", pin=8)],
            )
        ]
    )
    content = generate(config).by_name("bsp_tim.c").content
    assert "TIM_CtrlPWMOutputs(TIM1, ENABLE)" in content


def test_tim3_has_no_moe_call() -> None:
    """通用定时器没有 BDTR 寄存器，不该出现 MOE **调用**。

    断言带左括号的调用形式：文件头的说明注释里会提到这个函数名
    （解释高级定时器为什么必须调它），只查名字会误判。
    """
    config = Stm32Config(
        tim=[
            TimConfig(
                instance="TIM3",
                channels=[TimChannelConfig(channel="TIM_Channel_1", port="A", pin=6)],
            )
        ]
    )
    content = generate(config).by_name("bsp_tim.c").content
    assert "TIM_CtrlPWMOutputs(" not in content


def test_tim_channel_uses_dedicated_init_function() -> None:
    """每个通道用各自的 TIM_OCxInit —— 库里没有统一的 ``TIM_OCInit``。"""
    config = Stm32Config(
        tim=[
            TimConfig(
                instance="TIM2",
                channels=[
                    TimChannelConfig(channel="TIM_Channel_1", port="A", pin=0),
                    TimChannelConfig(channel="TIM_Channel_3", port="A", pin=2),
                ],
            )
        ]
    )
    content = generate(config).by_name("bsp_tim.c").content
    assert "TIM_OC1Init(TIM2" in content
    assert "TIM_OC3Init(TIM2" in content
    assert "TIM_OC2Init" not in content


def test_tim_pwm_pin_configured_as_af_pp() -> None:
    config = Stm32Config(
        tim=[
            TimConfig(
                instance="TIM3",
                channels=[TimChannelConfig(channel="TIM_Channel_1", port="A", pin=6)],
            )
        ]
    )
    gpio = generate(config).by_name("bsp_gpio.c").content
    assert "GPIO_Mode_AF_PP" in gpio
    assert "TIM3_CH1" in gpio


def test_exti_pin_configured_as_input() -> None:
    """EXTI 引脚默认上拉输入 —— 悬空输入会产生随机中断。"""
    config = Stm32Config(exti=[ExtiLineConfig(line=0, port="A", label="KEY1")])
    gpio = generate(config).by_name("bsp_gpio.c").content
    assert "GPIO_Mode_IPU" in gpio
    assert "EXTI0" in gpio


def test_afio_empty_when_unused() -> None:
    """没有任何 AFIO 功能时不生成多余调用。"""
    content = generate(Stm32Config()).by_name("bsp_afio.c").content
    assert "GPIO_PinRemapConfig(" not in content
    assert "RCC_APB2Periph_AFIO" not in content


def test_multiple_tim_on_both_buses() -> None:
    """TIM1 在 APB2、TIM3 在 APB1 —— 两条时钟语句都要有。"""
    config = Stm32Config(
        tim=[TimConfig(instance="TIM1"), TimConfig(instance="TIM3")]
    )
    content = generate(config).by_name("bsp_tim.c").content
    assert "RCC_APB2Periph_TIM1" in content
    assert "RCC_APB1Periph_TIM3" in content


def test_no_template_leftovers_with_all_peripherals() -> None:
    """全外设配置下渲染不能残留 Jinja 标记。"""
    config = Stm32Config(
        afio=AfioConfig(swj_remap="GPIO_Remap_SWJ_JTAGDisable"),
        exti=[ExtiLineConfig(line=0, port="A")],
        tim=[TimConfig(instance="TIM2",
                       channels=[TimChannelConfig(channel="TIM_Channel_1", port="A", pin=0)])],
    )
    for item in generate(config).files:
        for token in ("{{", "{%", "{#"):
            assert token not in item.content, f"{item.name} 残留 {token}"


def test_context_has_peripheral_flags() -> None:
    config = Stm32Config(
        afio=AfioConfig(swj_remap="GPIO_Remap_SWJ_JTAGDisable"),
        exti=[ExtiLineConfig(line=0, port="A")],
        tim=[TimConfig(instance="TIM2")],
    )
    ctx = build_context(config)
    assert ctx.has_afio
    assert ctx.has_exti
    assert ctx.has_tim


def test_empty_config_has_no_peripheral_flags() -> None:
    ctx = build_context(Stm32Config())
    assert not ctx.has_afio
    assert not ctx.has_exti
    assert not ctx.has_tim
    assert not ctx.has_interrupts

# --------------------------------------------------------------------------
# TIM 外部时钟的校验
# --------------------------------------------------------------------------


def test_external_clock_without_frequency_is_info_not_error() -> None:
    """外部时钟没填频率只报 INFO。

    生成完全没问题（PSC/ARR 是用户给的），只是工具算不出实际频率。
    报 ERROR 会让合理的配置被拦下。
    """
    config = Stm32Config.model_validate({
        "project_name": "t",
        "tim": [{"instance": "TIM2", "clock_source": "ETR_MODE1", "prescaler": 9, "period": 999}],
    })
    issues = validate(config, include_info=True)
    hits = [i for i in issues if i.code == "tim.external_clock_frequency_unknown"]
    assert len(hits) == 1
    assert hits[0].severity is not Severity.ERROR
    assert hits[0].severity is Severity.INFO


def test_external_clock_with_frequency_reports_summary() -> None:
    """填了外部频率就照常报频率摘要。"""
    config = Stm32Config.model_validate({
        "project_name": "t",
        "tim": [{"instance": "TIM2", "clock_source": "ETR_MODE1",
                 "prescaler": 0, "period": 99, "external_clock_hz": 1000.0}],
    })
    issues = validate(config, include_info=True)
    codes = {i.code for i in issues}
    assert "tim.frequency_summary" in codes
    assert "tim.external_clock_frequency_unknown" not in codes


def test_external_clock_without_frequency_still_reports_duty() -> None:
    """频率未知不影响占空比 —— 占空比只由 CCR/(ARR+1) 决定。

    这一条实际有实用价值：拿外部脉冲做 PWM 时，用户最关心的正是
    占空比，而它不需要知道输入频率。
    """
    config = Stm32Config.model_validate({
        "project_name": "t",
        "tim": [{"instance": "TIM2", "clock_source": "ETR_MODE1",
                 "prescaler": 0, "period": 99,
                 "channels": [{"channel": "CH1", "pulse": 25}]}],
    })
    issues = validate(config, include_info=True)
    duty = [i for i in issues if i.code == "tim.duty_summary"]
    assert len(duty) == 1
    assert "25.00%" in duty[0].message


def test_itr_cascade_warns_about_upstream() -> None:
    """ITR 级联必须提示上游依赖：上游停了本定时器根本不计数。

    这条与频率无关，所以无论填没填频率都要出现 —— 曾经因为
    ``continue`` 写在校验之前而整条丢失。
    """
    for extra in ({}, {"external_clock_hz": 10000.0}):
        payload = {"instance": "TIM3", "clock_source": "ITR0",
                   "prescaler": 0, "period": 49, **extra}
        config = Stm32Config.model_validate({"project_name": "t", "tim": [payload]})
        codes = {i.code for i in validate(config, include_info=True)}
        assert "tim.itr_cascade" in codes, f"填了频率={bool(extra)} 时丢了级联提示"


def test_internal_clock_does_not_mention_external() -> None:
    """内部时钟不应出现任何外部时钟相关的提示。"""
    config = Stm32Config.model_validate({
        "project_name": "t", "tim": [{"instance": "TIM2"}],
    })
    codes = {i.code for i in validate(config, include_info=True)}
    assert "tim.external_clock_frequency_unknown" not in codes
    assert "tim.itr_cascade" not in codes
