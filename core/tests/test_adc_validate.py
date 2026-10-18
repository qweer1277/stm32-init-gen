"""ADC 校验规则与生成器测试。

分两块：

- **校验**（check_adc）—— 该报的报了、不该报的没报；特别测「只 WARN
  不 ERROR」那类（没有数据手册核实的判断）
- **生成** —— 产物结构、调用顺序、关键正确性点（共享 ISR、校准顺序、
  看门狗通道、ADCCLK 全局分频）
"""

from __future__ import annotations

from stm32gen.generator import build_context, generate
from stm32gen.models import (
    AdcChannelConfig,
    AdcConfig,
    NvicChannelConfig,
    NvicConfig,
    Stm32Config,
)
from stm32gen.validate import Severity, has_blocking, validate


def codes(issues, severity: Severity | None = None) -> set[str]:
    return {i.code for i in issues if severity is None or i.severity is severity}


def nvic(*channels: str, group: str = "NVIC_PriorityGroup_2") -> NvicConfig:
    return NvicConfig(
        priority_group=group,
        channels=[
            NvicChannelConfig(irq_channel=name, preemption_priority=1, sub_priority=0)
            for name in channels
        ],
    )


def sample_config(**kwargs) -> Stm32Config:
    """一份最小可用的 ADC 配置，关键字参数用来逐个改坏某一项。"""
    data = {
        "project_name": "adc_test",
        "adc": [
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_3", port="GPIOA", pin=3)],
            )
        ],
    }
    data.update(kwargs)
    return Stm32Config.model_validate(data)


# --------------------------------------------------------------------------
# check_adc：ERROR 级
# --------------------------------------------------------------------------


def test_duplicate_instance_is_error() -> None:
    config = Stm32Config.model_validate(
        {
            "project_name": "t",
            "adc": [
                {"instance": "ADC1", "channels": [{"channel": "ADC_Channel_0"}]},
                {"instance": "ADC1", "channels": [{"channel": "ADC_Channel_1"}]},
            ],
        }
    )
    issues = validate(config)
    assert "adc.duplicate_instance" in codes(issues, Severity.ERROR)


def test_dual_mode_without_pair_is_error() -> None:
    """双 ADC 模式要 ADC1+ADC2 成对，只配一个必然拿到无效数据。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                mode="ADC_Mode_RegSimult",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            )
        ]
    )
    issues = validate(config)
    assert "adc.dual_mode_needs_pair" in codes(issues, Severity.ERROR)


def test_dual_mode_pair_with_mismatched_mode_is_error() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                mode="ADC_Mode_RegSimult",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            ),
            AdcConfig(
                instance="ADC2",
                mode="ADC_Mode_FastInterl",
                channels=[AdcChannelConfig(channel="ADC_Channel_1")],
            ),
        ]
    )
    issues = validate(config)
    assert "adc.dual_mode_mismatch" in codes(issues, Severity.ERROR)


def test_dual_mode_pair_with_same_mode_is_ok() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                mode="ADC_Mode_RegSimult",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            ),
            AdcConfig(
                instance="ADC2",
                mode="ADC_Mode_RegSimult",
                channels=[AdcChannelConfig(channel="ADC_Channel_1")],
            ),
        ]
    )
    issues = validate(config)
    assert "adc.dual_mode_needs_pair" not in codes(issues)
    assert "adc.dual_mode_mismatch" not in codes(issues)


def test_prescaler_conflict_is_error() -> None:
    """ADCCLK 分频只有一份（CFGR.ADCPRE），两个实例不同必然静默覆盖。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                prescaler="RCC_PCLK2_Div6",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            ),
            AdcConfig(
                instance="ADC2",
                prescaler="RCC_PCLK2_Div4",
                channels=[AdcChannelConfig(channel="ADC_Channel_1")],
            ),
        ]
    )
    issues = validate(config)
    assert "adc.prescaler_conflict" in codes(issues, Severity.ERROR)
    assert has_blocking(issues)


def test_same_prescaler_on_both_instances_is_ok() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                prescaler="RCC_PCLK2_Div6",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            ),
            AdcConfig(
                instance="ADC2",
                prescaler="RCC_PCLK2_Div6",
                channels=[AdcChannelConfig(channel="ADC_Channel_1")],
            ),
        ]
    )
    assert "adc.prescaler_conflict" not in codes(validate(config))


def test_irq_without_nvic_is_error() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            )
        ]
    )
    issues = validate(config)
    assert "adc.irq_without_nvic" in codes(issues, Severity.ERROR)


def test_irq_with_nvic_is_ok() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            )
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    issues = validate(config)
    assert "adc.irq_without_nvic" not in codes(issues)
    assert not has_blocking(issues)


# --------------------------------------------------------------------------
# check_adc：只 WARN 不 ERROR 的那类（无数据手册可核）
# --------------------------------------------------------------------------


def test_clock_over_spec_is_warning_not_error() -> None:
    """ADCCLK 超 14MHz 只 WARN。

    14MHz 是数据手册值，超了通常仍能转换、只是精度变差，不会"必然产出
    不能用代码"。把手册值当硬错误拦生成，风险比收益大。
    """
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                prescaler="RCC_PCLK2_Div2",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            )
        ]
    )
    issues = validate(config)
    over = [i for i in issues if i.code == "adc.clock_over_spec"]
    assert len(over) == 1
    assert over[0].severity is Severity.WARNING
    assert not has_blocking(issues)
    # 提示里要给出建议分频
    assert "建议" in over[0].hint


def test_clock_over_spec_hint_suggests_valid_divider() -> None:
    """PCLK2=72MHz 时 ÷6 才不超 14MHz，提示应指向 ÷6。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                prescaler="RCC_PCLK2_Div4",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            )
        ]
    )
    issue = next(i for i in validate(config) if i.code == "adc.clock_over_spec")
    assert "PCLK2 ÷6" in issue.hint


def test_default_prescaler_is_within_spec() -> None:
    """默认 ÷6 在 72MHz PCLK2 下是 12MHz，不该有任何警告。"""
    issues = validate(sample_config())
    assert "adc.clock_over_spec" not in codes(issues)
    assert not [i for i in issues if i.severity is Severity.WARNING and i.code.startswith("adc.")]


def test_pin_not_default_is_warning_not_error() -> None:
    """引脚与手册默认值不一致只 WARN —— 本工具没有数据手册。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[
                    AdcChannelConfig(channel="ADC_Channel_0", port="GPIOB", pin=5)
                ],
            )
        ]
    )
    issues = validate(config)
    warn = [i for i in issues if i.code == "adc.pin_not_default"]
    assert len(warn) == 1
    assert warn[0].severity is Severity.WARNING
    assert not has_blocking(issues)
    assert "数据手册" in warn[0].hint


def test_pin_on_default_location_has_no_warning() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[
                    AdcChannelConfig(channel="ADC_Channel_0", port="GPIOA", pin=0)
                ],
            )
        ]
    )
    assert "adc.pin_not_default" not in codes(validate(config))


def test_trigger_timer_missing_is_warning_not_error() -> None:
    """外部触发源依赖的定时器没配 —— 定时器可能由用户自己的代码初始化。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                trigger="ADC_ExternalTrigConv_T1_CC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            )
        ]
    )
    issues = validate(config)
    warn = [i for i in issues if i.code == "adc.trigger_timer_missing"]
    assert len(warn) == 1
    assert warn[0].severity is Severity.WARNING
    assert "TIM1" in warn[0].message


def test_trigger_timer_present_has_no_warning() -> None:
    config = Stm32Config.model_validate(
        {
            "project_name": "t",
            "tim": [{"instance": "TIM1", "prescaler": 71, "period": 999}],
            "adc": [
                {
                    "instance": "ADC1",
                    "trigger": "ADC_ExternalTrigConv_T1_CC1",
                    "channels": [{"channel": "ADC_Channel_0"}],
                }
            ],
        }
    )
    assert "adc.trigger_timer_missing" not in codes(validate(config))


def test_nvic_without_irq_warns() -> None:
    config = sample_config(nvic=nvic("ADC1_2_IRQn"))
    issues = validate(config)
    assert "adc.nvic_without_irq" in codes(issues, Severity.WARNING)


def test_nvic_disabled_warns() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            )
        ],
        nvic=NvicConfig(
            priority_group="NVIC_PriorityGroup_2",
            channels=[
                NvicChannelConfig(
                    irq_channel="ADC1_2_IRQn",
                    preemption_priority=1,
                    sub_priority=0,
                    enabled=False,
                )
            ],
        ),
    )
    issues = validate(config)
    assert "adc.irq_nvic_disabled" in codes(issues, Severity.WARNING)


# --------------------------------------------------------------------------
# check_adc：INFO 级
# --------------------------------------------------------------------------


def test_shared_irq_channel_is_info() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            ),
            AdcConfig(
                instance="ADC2",
                channels=[AdcChannelConfig(channel="ADC_Channel_1")],
                interrupts=["ADC_IT_EOC"],
            ),
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    issues = validate(config)
    assert "adc.shared_irq_channel" in codes(issues, Severity.INFO)


def test_pin_not_configured_is_info() -> None:
    """外部通道没配引脚不报错 —— 引脚可能由用户在别处配。"""
    config = sample_config(
        adc=[],
    )
    config = Stm32Config.model_validate(
        {
            "project_name": "t",
            "adc": [
                {"instance": "ADC1", "channels": [{"channel": "ADC_Channel_0"}]},
            ],
        }
    )
    issues = validate(config)
    assert "adc.pin_not_configured" in codes(issues, Severity.INFO)
    assert not has_blocking(issues)


def test_frequency_summary_reports_adcclk_and_rate() -> None:
    issues = validate(sample_config())
    summary = next(i for i in issues if i.code == "adc.frequency_summary")
    assert "ADCCLK" in summary.message
    assert "12MHz" in summary.message
    assert "kSPS" in summary.message


def test_short_sample_time_is_info() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[
                    AdcChannelConfig(
                        channel="ADC_Channel_0", sample_time="ADC_SampleTime_1Cycles5"
                    )
                ],
            )
        ]
    )
    assert "adc.short_sample_time" in codes(validate(config))


def test_software_trigger_note_only_without_continuous() -> None:
    """连续转换下不需要每轮手动启动，提示不该出现。"""
    continuous = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                continuous=True,
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            )
        ]
    )
    assert "adc.software_trigger_note" not in codes(validate(continuous))


def test_config_empty_counts_adc() -> None:
    """有 ADC 配置时不该报 config.empty。"""
    assert "config.empty" not in codes(validate(sample_config()))


def test_config_empty_still_reported_when_only_adc_absent() -> None:
    config = Stm32Config.model_validate({"project_name": "t"})
    assert "config.empty" in codes(validate(config))


# --------------------------------------------------------------------------
# 生成：产物结构
# --------------------------------------------------------------------------


def test_generates_adc_files() -> None:
    result = generate(sample_config())
    names = {item.name for item in result.files}
    assert "bsp_adc.h" in names
    assert "bsp_adc.c" in names
    # 产物从 19 个变成 21 个
    assert len(result.files) == 21


def test_adc_source_contains_expected_calls() -> None:
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_adc.c"
    )
    assert "ADC_InitTypeDef ADC_InitStructure;" in content
    assert "ADC_Init(ADC1, &ADC_InitStructure);" in content
    assert "ADC_RegularChannelConfig(ADC1, ADC_Channel_3, 1, ADC_SampleTime_55Cycles5);" in content
    assert "ADC_Cmd(ADC1, ENABLE);" in content
    assert "RCC_APB2PeriphClockCmd(RCC_APB2Periph_ADC1, ENABLE);" in content
    assert "RCC_ADCCLKConfig(RCC_PCLK2_Div6);" in content


def test_adc_header_declares_init() -> None:
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_adc.h"
    )
    assert "void bsp_adc_init(void);" in content
    assert "__BSP_ADC_H" in content


def test_empty_adc_still_generates_kept_stub() -> None:
    """没有 ADC 配置时函数保留空实现，保证 bsp_init 调用序列一致。"""
    config = Stm32Config.model_validate({"project_name": "t"})
    content = next(
        item.content for item in generate(config).files if item.name == "bsp_adc.c"
    )
    assert "void bsp_adc_init(void)" in content
    assert "没有配置任何 ADC" in content


def test_all_templates_render_with_adc() -> None:
    """每个模板都要能渲染 —— StrictUndefined 下漏变量会直接抛错。"""
    result = generate(sample_config())
    assert len(result.files) == 21


# --------------------------------------------------------------------------
# 生成：调用顺序
# --------------------------------------------------------------------------


def test_init_call_order_puts_adc_after_tim_before_nvic() -> None:
    """bsp_init 的顺序：rcc → afio → gpio → exti → usart → tim → adc → nvic。"""
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_init.c"
    )
    order = [
        "bsp_rcc_init()",
        "bsp_afio_init()",
        "bsp_gpio_init()",
        "bsp_exti_init()",
        "bsp_usart_init()",
        "bsp_tim_init()",
        "bsp_adc_init()",
        "bsp_nvic_init()",
    ]
    positions = [content.index(f"{name};") for name in order]
    assert positions == sorted(positions)


def test_adc_init_declared_in_bsp_init_header() -> None:
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_init.h"
    )
    assert '#include "bsp_adc.h"' in content


def test_calibration_before_adc_enable() -> None:
    """校准必须在 ADC_Cmd 之前 —— 先开 ADON 再校准会让首次转换不可信。"""
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_adc.c"
    )
    assert content.index("ADC_ResetCalibration(ADC1);") < content.index("ADC_Cmd(ADC1, ENABLE);")
    assert content.index("ADC_StartCalibration(ADC1);") < content.index("ADC_Cmd(ADC1, ENABLE);")


def test_calibration_sequence_order() -> None:
    """复位校准 → 等 → 启动校准 → 等。"""
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_adc.c"
    )
    pos = [
        content.index("ADC_ResetCalibration(ADC1);"),
        content.index("ADC_GetResetCalibrationStatus(ADC1)"),
        content.index("ADC_StartCalibration(ADC1);"),
        content.index("ADC_GetCalibrationStatus(ADC1)"),
    ]
    assert pos == sorted(pos)


def test_calibration_can_be_disabled() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                calibration=False,
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            )
        ]
    )
    content = next(item.content for item in generate(config).files if item.name == "bsp_adc.c")
    assert "ADC_ResetCalibration" not in content


def test_calibration_wait_loops_are_bounded() -> None:
    """code-standards R4.3：等待硬件标志的循环必须有界。"""
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_adc.c"
    )
    assert "adc_wait_loops < 4096u" in content
    assert "while (ADC_GetResetCalibrationStatus" not in content


def test_calibration_status_polarity_is_reset() -> None:
    """状态函数返回 SET 表示"该位还在"，RESET 才是完成。

    这个极性反直觉，写成 == SET 就是死循环。
    """
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_adc.c"
    )
    assert "ADC_GetResetCalibrationStatus(ADC1) == RESET" in content
    assert "ADC_GetCalibrationStatus(ADC1) == RESET" in content
    # 反向断言：写成 == SET 就是死循环
    assert "ADC_GetResetCalibrationStatus(ADC1) == SET" not in content
    assert "ADC_GetCalibrationStatus(ADC1) == SET" not in content


def test_software_trigger_emits_start_command() -> None:
    """ADC_ExternalTrigConv_None 是软件触发，必须再发一次启动。"""
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_adc.c"
    )
    assert "ADC_SoftwareStartConvCmd(ADC1, ENABLE);" in content


def test_watchdog_emits_single_channel_config() -> None:
    """只调 AnalogWatchdogCmd 而不调 SingleChannelConfig 会盯错通道。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                watchdog=True,
                watchdog_low=100,
                watchdog_high=3000,
                channels=[
                    AdcChannelConfig(channel="ADC_Channel_5", port="GPIOA", pin=5)
                ],
            )
        ]
    )
    content = next(item.content for item in generate(config).files if item.name == "bsp_adc.c")
    assert "ADC_AnalogWatchdogThresholdsConfig(ADC1, 3000, 100);" in content
    assert "ADC_AnalogWatchdogSingleChannelConfig(ADC1, ADC_Channel_5);" in content


def test_adcclk_config_emitted_once_for_two_instances() -> None:
    """ADCCLK 分频是全局一份，只能出现一次。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                prescaler="RCC_PCLK2_Div6",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            ),
            AdcConfig(
                instance="ADC2",
                prescaler="RCC_PCLK2_Div6",
                channels=[AdcChannelConfig(channel="ADC_Channel_1")],
            ),
        ]
    )
    content = next(item.content for item in generate(config).files if item.name == "bsp_adc.c")
    assert content.count("RCC_ADCCLKConfig(") == 1


# --------------------------------------------------------------------------
# 生成：共享中断服务函数
# --------------------------------------------------------------------------


def test_two_adc_instances_share_one_handler() -> None:
    """ADC1 与 ADC2 共用 ADC1_2_IRQn，只能生成一个 handler。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            ),
            AdcConfig(
                instance="ADC2",
                channels=[AdcChannelConfig(channel="ADC_Channel_1")],
                interrupts=["ADC_IT_EOC"],
            ),
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    content = next(item.content for item in generate(config).files if item.name == "bsp_it.c")
    assert content.count("void ADC1_2_IRQHandler(void)") == 1
    # 内部必须按实例分派
    assert "ADC_GetITStatus(ADC1, ADC_IT_EOC)" in content
    assert "ADC_GetITStatus(ADC2, ADC_IT_EOC)" in content


def test_single_adc_generates_one_handler() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            )
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    content = next(item.content for item in generate(config).files if item.name == "bsp_it.c")
    assert content.count("void ADC1_2_IRQHandler(void)") == 1


def test_adc_handler_declared_in_it_header() -> None:
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            )
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    content = next(item.content for item in generate(config).files if item.name == "bsp_it.h")
    assert "void ADC1_2_IRQHandler(void);" in content


def test_no_adc_interrupt_means_no_handler() -> None:
    content = next(
        item.content for item in generate(sample_config()).files if item.name == "bsp_it.c"
    )
    assert "ADC1_2_IRQHandler" not in content


# --------------------------------------------------------------------------
# 生成：上下文与符号收集
# --------------------------------------------------------------------------


def test_context_exposes_adc_fields() -> None:
    ctx = build_context(sample_config())
    assert ctx.has_adc
    assert len(ctx.adc_configs) == 1
    assert ctx.adc_prescaler == "RCC_PCLK2_Div6"
    assert ctx.adc_configs[0].instance == "ADC1"


def test_context_has_no_adc_for_empty_config() -> None:
    ctx = build_context(Stm32Config.model_validate({"project_name": "t"}))
    assert not ctx.has_adc
    assert ctx.adc_configs == []
    assert ctx.adc_clock is None


def test_context_has_interrupts_true_for_adc_only() -> None:
    """只有 ADC 中断时 bsp_it.c 也要生成骨架。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
                interrupts=["ADC_IT_EOC"],
            )
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    assert build_context(config).has_interrupts


def test_symbols_used_covers_adc_symbols() -> None:
    """模板里出现的每个库符号都必须登记，否则护栏失去意义。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                watchdog=True,
                watchdog_low=100,
                watchdog_high=3000,
                continuous=True,
                interrupts=["ADC_IT_EOC"],
                channels=[
                    AdcChannelConfig(
                        channel="ADC_Channel_3",
                        sample_time="ADC_SampleTime_28Cycles5",
                        port="GPIOA",
                        pin=3,
                    )
                ],
            )
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    used = generate(config).symbols_used
    expected = {
        "ADC_Init",
        "ADC_InitTypeDef",
        "ADC_Cmd",
        "ADC_RegularChannelConfig",
        "ADC_Channel_3",
        "ADC_SampleTime_28Cycles5",
        "ADC_DataAlign_Right",
        "ADC_Mode_Independent",
        "ADC_ExternalTrigConv_None",
        "ADC_ResetCalibration",
        "ADC_GetResetCalibrationStatus",
        "ADC_StartCalibration",
        "ADC_GetCalibrationStatus",
        "ADC_SoftwareStartConvCmd",
        "ADC_AnalogWatchdogCmd",
        "ADC_AnalogWatchdogThresholdsConfig",
        "ADC_AnalogWatchdogSingleChannelConfig",
        "ADC_AnalogWatchdog_SingleRegEnable",
        "ADC_ITConfig",
        "ADC_IT_EOC",
        "RCC_ADCCLKConfig",
        "RCC_PCLK2_Div6",
        "RCC_APB2Periph_ADC1",
    }
    missing = expected - used
    assert not missing, f"未登记符号：{sorted(missing)}"


def test_watchdog_channel_symbol_registered() -> None:
    """参数位置上的宏同样要登记 —— 漏了会被护栏判成编造符号。"""
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                watchdog=True,
                watchdog_low=10,
                watchdog_high=100,
                watchdog_channel="ADC_Channel_9",
                channels=[AdcChannelConfig(channel="ADC_Channel_0")],
            )
        ]
    )
    used = generate(config).symbols_used
    assert "ADC_Channel_9" in used


def test_generated_code_passes_anti_invention_guard() -> None:
    """输出层面也没有编造符号（含 ST 惯例的结构体变量名 ADC_InitStructure）。"""
    from stm32gen.stdlib_ref import default_index
    from stm32gen.stdlib_ref.guard import find_invented

    index = default_index()
    config = sample_config(
        adc=[
            AdcConfig(
                instance="ADC1",
                watchdog=True,
                watchdog_low=100,
                watchdog_high=3000,
                channels=[
                    AdcChannelConfig(
                        channel="ADC_Channel_3",
                        sample_time="ADC_SampleTime_239Cycles5",
                        port="GPIOA",
                        pin=3,
                    ),
                    AdcChannelConfig(channel="ADC_Channel_16"),
                ],
                interrupts=["ADC_IT_EOC"],
            )
        ],
        nvic=nvic("ADC1_2_IRQn"),
    )
    for item in generate(config).files:
        problems = find_invented(item.content, index, item.name)
        assert not problems, f"{item.name} 有编造符号：{problems}"


# --------------------------------------------------------------------------
# 合并布局
# --------------------------------------------------------------------------


def test_merged_layout_includes_adc() -> None:
    from stm32gen.generator.layout import OutputLayout

    result = generate(sample_config(), layout=OutputLayout.MERGED)
    names = {item.name for item in result.files}
    assert names == {"bsp.h", "bsp.c", "main.c"}
    source = next(item.content for item in result.files if item.name == "bsp.c")
    assert "void bsp_adc_init(void)" in source
    assert "ADC_Init(ADC1" in source
    header = next(item.content for item in result.files if item.name == "bsp.h")
    assert "void bsp_adc_init(void);" in header


def test_merged_layout_adc_section_order() -> None:
    """bsp.c 里 ADC 段排在 TIM 之后、NVIC 之前。"""
    from stm32gen.generator.layout import OutputLayout

    result = generate(sample_config(), layout=OutputLayout.MERGED)
    source = next(item.content for item in result.files if item.name == "bsp.c")
    assert source.index("bsp_tim_init(void)") < source.index("bsp_adc_init(void)")
    assert source.index("bsp_adc_init(void)") < source.index("bsp_nvic_init(void)")
