"""ADC 模型层测试。

三块：

- **枚举派生属性** —— 通道/触发源/采样时间/分频的派生值是否与库一致
- **数学** —— ADCCLK 频率、转换周期、采样率
- **组合约束** —— 「看起来配了实际没生成」的组合必须报错；合法组合必须放行

这是「模型层接受的组合必须能生成、拒绝的组合必须报错」的落地。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stm32gen.models import (
    ADC_SEQUENCE_MAX,
    ADC_THRESHOLD_MAX,
    AdcChannel,
    AdcChannelConfig,
    AdcConfig,
    AdcDataAlign,
    AdcInstance,
    AdcInterrupt,
    AdcMode,
    AdcPrescaler,
    AdcSampleTime,
    AdcTrigger,
    ChipId,
    GpioMode,
    Stm32Config,
    adc_default_pin,
    get_chip,
)


def channel(value: str = "ADC_Channel_0", **kwargs) -> AdcChannelConfig:
    return AdcChannelConfig.model_validate({"channel": value, **kwargs})


def adc_config(**kwargs) -> AdcConfig:
    data = {"instance": "ADC1", "channels": [{"channel": "ADC_Channel_0"}]}
    data.update(kwargs)
    return AdcConfig.model_validate(data)


# --------------------------------------------------------------------------
# 枚举派生属性
# --------------------------------------------------------------------------


def test_adc_instances_are_only_adc1_and_adc2() -> None:
    """中容量没有 ADC3 —— ADC3_BASE 在 STM32F10X_HD 分支里。"""
    assert [item.value for item in AdcInstance] == ["ADC1", "ADC2"]


@pytest.mark.parametrize("instance", list(AdcInstance))
def test_both_adc_instances_share_one_irqn(instance: AdcInstance) -> None:
    """ADC1 与 ADC2 共用 ADC1_2_IRQn。

    这**不是**实现选择，是 stm32f10x.h:262-263 的 STM32F10X_MD 分支只有
    ADC1_2_IRQn = 18。若哪天有人把 irqn 改成 f"ADC{index}_IRQn"，
    生成代码会因为找不到 ADC1_IRQn 而编译失败。
    """
    assert instance.irqn == "ADC1_2_IRQn"
    assert instance.handler_name == "ADC1_2_IRQHandler"


def test_adc_irqn_matches_startup_vector() -> None:
    """函数名必须与 startup 向量表一致，否则弱符号覆盖不了。"""
    assert AdcInstance.ADC1.handler_name == "ADC1_2_IRQHandler"


@pytest.mark.parametrize(
    ("instance", "macro"),
    [(AdcInstance.ADC1, "RCC_APB2Periph_ADC1"), (AdcInstance.ADC2, "RCC_APB2Periph_ADC2")],
)
def test_adc_clock_macro(instance: AdcInstance, macro: str) -> None:
    assert instance.clock_macro == macro


def test_adc_channel_numbers_and_internal_split() -> None:
    """0~15 是外部引脚，16/17 是内部通道。"""
    assert AdcChannel.CH0.number == 0
    assert AdcChannel.CH15.number == 15
    assert AdcChannel.CH16.number == 16
    assert AdcChannel.CH17.number == 17
    for number in range(16):
        assert not getattr(AdcChannel, f"CH{number}").is_internal
    assert AdcChannel.CH16.is_internal
    assert AdcChannel.CH17.is_internal


def test_adc_sample_time_cycles() -> None:
    """采样时间就是库里那 8 个宏对应的周期数。"""
    assert AdcSampleTime.CYCLES_1_5.cycles == 1.5
    assert AdcSampleTime.CYCLES_55_5.cycles == 55.5
    assert AdcSampleTime.CYCLES_239_5.cycles == 239.5
    # 8 个档位，单调递增
    values = [item.cycles for item in AdcSampleTime]
    assert len(values) == 8
    assert values == sorted(values)


def test_adc_prescaler_divisors() -> None:
    assert AdcPrescaler.DIV2.divider == 2
    assert AdcPrescaler.DIV4.divider == 4
    assert AdcPrescaler.DIV6.divider == 6
    assert AdcPrescaler.DIV8.divider == 8


def test_software_trigger_uses_the_none_macro() -> None:
    """ADC_ExternalTrigConv_None 的实际语义是软件触发（SWSTART）。

    库的命名有误导性：它表示"由 ADC_SoftwareStartConvCmd 启动"，
    不是"不需要触发"。生成器必须据此再发一次启动命令。
    """
    assert AdcTrigger.SOFTWARE.value == "ADC_ExternalTrigConv_None"
    assert AdcTrigger.SOFTWARE.is_software
    assert not AdcTrigger.T1_CC1.is_software


def test_adc3_only_triggers_are_marked() -> None:
    """6 个 ADC3 专属触发源必须被识别出来（C8T6 上没有 ADC3）。"""
    adc3_only = {item for item in AdcTrigger if item.adc3_only}
    assert adc3_only == {
        AdcTrigger.T3_CC1,
        AdcTrigger.T2_CC3,
        AdcTrigger.T8_CC1,
        AdcTrigger.T8_TRGO,
        AdcTrigger.T5_CC1,
        AdcTrigger.T5_CC3,
    }
    # C8T6 上可用的只有非 ADC3 的那 8 个
    assert len(AdcTrigger.options_for_chip()) == 8
    assert len(AdcTrigger.options_for_chip(has_adc3=True)) == 14


def test_adc_interrupt_flags() -> None:
    """ADC_IT_* 与 ADC_FLAG_* 是两套不同的宏，不能混用。"""
    assert AdcInterrupt.EOC.flag == "ADC_FLAG_EOC"
    assert AdcInterrupt.JEOC.flag == "ADC_FLAG_JEOC"
    assert AdcInterrupt.AWD.flag == "ADC_FLAG_AWD"


def test_adc_mode_independent_vs_dual() -> None:
    assert AdcMode.INDEPENDENT.is_independent
    assert not AdcMode.INDEPENDENT.is_dual
    assert AdcMode.REG_SIMULT.is_dual
    assert AdcMode.FAST_INTERL.is_interleaved
    assert not AdcMode.REG_SIMULT.is_interleaved


def test_adc_data_align_values() -> None:
    assert AdcDataAlign.RIGHT.value == "ADC_DataAlign_Right"
    assert AdcDataAlign.LEFT.value == "ADC_DataAlign_Left"


# --------------------------------------------------------------------------
# 引脚表：只用于提示，内部通道没有默认引脚
# --------------------------------------------------------------------------


def test_default_pin_table_covers_external_channels_only() -> None:
    for number in range(16):
        assert adc_default_pin(getattr(AdcChannel, f"CH{number}")) is not None
    # 内部通道不接引脚，表里必须没有条目
    assert adc_default_pin(AdcChannel.CH16) is None
    assert adc_default_pin(AdcChannel.CH17) is None


def test_default_pin_known_anchors() -> None:
    """几个可靠锚点：PA0 = IN0、PB0 = IN8、PC5 = IN15。"""
    assert adc_default_pin(AdcChannel.CH0) == ("A", 0)
    assert adc_default_pin(AdcChannel.CH8) == ("B", 0)
    assert adc_default_pin(AdcChannel.CH15) == ("C", 5)


# --------------------------------------------------------------------------
# 频率 / 时序数学
# --------------------------------------------------------------------------


def test_conversion_cycles_adds_the_12_5_overhead() -> None:
    """一次转换 = 采样周期 + 12.5 个固定周期（12 位逐次逼近）。"""
    item = channel(sample_time="ADC_SampleTime_55Cycles5")
    assert item.conversion_cycles == 68.0
    fast = channel(sample_time="ADC_SampleTime_1Cycles5")
    assert fast.conversion_cycles == 14.0


def test_adc_clock_is_pclk2_divided() -> None:
    """ADCCLK 由 PCLK2 分频而来，与 PCLK1 无关。"""
    cfg = adc_config(prescaler="RCC_PCLK2_Div6")
    assert cfg.adc_clock_hz(72_000_000) == pytest.approx(12_000_000)
    cfg8 = adc_config(prescaler="RCC_PCLK2_Div8")
    assert cfg8.adc_clock_hz(72_000_000) == pytest.approx(9_000_000)


def test_max_sample_rate_divides_by_total_cycles() -> None:
    """一轮扫描的速率 = ADCCLK / 总周期。"""
    cfg = adc_config(
        prescaler="RCC_PCLK2_Div6",
        channels=[{"channel": "ADC_Channel_0", "sample_time": "ADC_SampleTime_1Cycles5"}],
    )
    # 14 周期，12MHz → 12000000 / 14
    assert cfg.total_conversion_cycles == 14.0
    assert cfg.max_sample_rate_hz(72_000_000) == pytest.approx(12_000_000 / 14)


def test_multi_channel_cycles_accumulate() -> None:
    cfg = adc_config(
        channels=[
            {"channel": "ADC_Channel_0", "sample_time": "ADC_SampleTime_55Cycles5"},
            {"channel": "ADC_Channel_1", "sample_time": "ADC_SampleTime_55Cycles5"},
        ]
    )
    assert cfg.total_conversion_cycles == 136.0


def test_sequence_length_equals_channel_count() -> None:
    """序列长度就是通道条数 —— 不设独立字段，避免两个真相源。"""
    assert adc_config(instance="ADC1").sequence_length == 1
    cfg = adc_config(
        channels=[
            {"channel": "ADC_Channel_0"},
            {"channel": "ADC_Channel_1"},
            {"channel": "ADC_Channel_2"},
        ]
    )
    assert cfg.sequence_length == 3


def test_scan_mode_inferred_from_multiple_channels() -> None:
    """多通道必须扫描，不需要用户额外勾选。"""
    single = adc_config(scan_mode=False)
    assert not single.uses_scan_mode
    multi = adc_config(
        channels=[{"channel": "ADC_Channel_0"}, {"channel": "ADC_Channel_1"}],
        scan_mode=False,
    )
    assert multi.uses_scan_mode


def test_describe_clock_mentions_adcclk_and_rate() -> None:
    cfg = adc_config()
    text = cfg.describe_clock(72_000_000)
    assert "ADCCLK" in text
    assert "12MHz" in text
    assert "kSPS" in text


# --------------------------------------------------------------------------
# 派生属性
# --------------------------------------------------------------------------


def test_adc_derived_properties() -> None:
    cfg = adc_config(interrupts=["ADC_IT_EOC"])
    assert cfg.name == "ADC1"
    assert cfg.clock_macro == "RCC_APB2Periph_ADC1"
    assert cfg.irqn == "ADC1_2_IRQn"
    assert cfg.handler_name == "ADC1_2_IRQHandler"
    assert cfg.needs_nvic
    assert cfg.has_channels


def test_channel_pin_mode_is_analog_input() -> None:
    """模拟输入必须是 GPIO_Mode_AIN。

    配成任何数字输入模式都会让施密特触发器介入，采样值被钳位，
    现象是"读数永远是 0 或 4095"。
    """
    item = channel(port="GPIOA", pin=3)
    assert item.pin_mode is GpioMode.AIN
    assert item.pin_name == "PA3"
    assert item.needs_gpio


def test_internal_channel_has_no_pin_name() -> None:
    item = channel("ADC_Channel_16")
    assert item.is_internal
    assert not item.needs_gpio
    assert item.pin_name == ""


def test_external_channels_excludes_internal() -> None:
    cfg = adc_config(
        channels=[
            {"channel": "ADC_Channel_0"},
            {"channel": "ADC_Channel_16"},
            {"channel": "ADC_Channel_17"},
        ]
    )
    assert len(cfg.external_channels) == 1


def test_watchdog_channel_defaults_to_first_sequence_entry() -> None:
    """未指定看门狗通道时取序列第一条。

    只调 ADC_AnalogWatchdogCmd 而不调 SingleChannelConfig 时，库里按
    通道 0 处理 —— 盯的是错的通道。所以必须显式给一个通道。
    """
    cfg = adc_config(
        channels=[{"channel": "ADC_Channel_5"}],
        watchdog=True,
        watchdog_low=10,
        watchdog_high=100,
    )
    assert cfg.effective_watchdog_channel is AdcChannel.CH5
    assert cfg.effective_watchdog_channel_macro == "ADC_Channel_5"


def test_watchdog_channel_explicit_wins() -> None:
    cfg = adc_config(
        channels=[{"channel": "ADC_Channel_5"}],
        watchdog=True,
        watchdog_low=10,
        watchdog_high=100,
        watchdog_channel="ADC_Channel_9",
    )
    assert cfg.effective_watchdog_channel is AdcChannel.CH9


# --------------------------------------------------------------------------
# 组合约束：必须报错的情形
# --------------------------------------------------------------------------


def test_rejects_empty_channel_list() -> None:
    """ADC_Init 要求 ADC_NbrOfChannel >= 1，空序列没法生成。"""
    with pytest.raises(ValidationError, match="规则组通道"):
        AdcConfig.model_validate({"instance": "ADC1", "channels": []})


def test_rejects_duplicate_channel_in_sequence() -> None:
    with pytest.raises(ValidationError, match="配了多次"):
        AdcConfig.model_validate(
            {
                "instance": "ADC1",
                "channels": [{"channel": "ADC_Channel_0"}, {"channel": "ADC_Channel_0"}],
            }
        )


def test_rejects_sequence_longer_than_16() -> None:
    channels = [{"channel": f"ADC_Channel_{i}"} for i in range(ADC_SEQUENCE_MAX + 1)]
    with pytest.raises(ValidationError, match=str(ADC_SEQUENCE_MAX)):
        AdcConfig.model_validate({"instance": "ADC1", "channels": channels})


def test_accepts_full_16_channel_sequence() -> None:
    channels = [{"channel": f"ADC_Channel_{i}"} for i in range(ADC_SEQUENCE_MAX)]
    cfg = AdcConfig.model_validate({"instance": "ADC1", "channels": channels})
    assert cfg.sequence_length == ADC_SEQUENCE_MAX


def test_rejects_port_without_pin() -> None:
    """只给端口不给引脚 = 没填完，GPIO 会被静默跳过。"""
    with pytest.raises(ValidationError, match="必须同时指定"):
        AdcChannelConfig.model_validate(
            {"channel": "ADC_Channel_0", "port": "GPIOA"}
        )


def test_rejects_pin_without_port() -> None:
    with pytest.raises(ValidationError, match="必须同时指定"):
        AdcChannelConfig.model_validate({"channel": "ADC_Channel_0", "pin": 3})


def test_rejects_internal_channel_with_pin() -> None:
    """内部通道不接引脚，配了就是配了个不生效的东西。"""
    with pytest.raises(ValidationError, match="内部通道"):
        AdcChannelConfig.model_validate(
            {"channel": "ADC_Channel_16", "port": "GPIOA", "pin": 1}
        )


def test_rejects_adc3_only_trigger() -> None:
    """ADC3 专属触发源在 C8T6 上等于不触发。"""
    with pytest.raises(ValidationError, match="ADC3"):
        adc_config(trigger="ADC_ExternalTrigConv_T5_CC1")


def test_rejects_inverted_watchdog_window() -> None:
    with pytest.raises(ValidationError, match="窗口区间为空"):
        adc_config(watchdog=True, watchdog_low=3000, watchdog_high=1000)


def test_rejects_watchdog_thresholds_without_watchdog() -> None:
    """未开看门狗却填阈值 —— 静默丢弃比报错更糟。"""
    with pytest.raises(ValidationError, match="没有启用模拟看门狗"):
        adc_config(watchdog=False, watchdog_high=2000)


def test_rejects_watchdog_channel_without_watchdog() -> None:
    with pytest.raises(ValidationError, match="没有启用模拟看门狗"):
        adc_config(watchdog=False, watchdog_channel="ADC_Channel_3")


def test_rejects_awd_interrupt_without_watchdog() -> None:
    """没有看门狗就没有 AWD 事件，中断永远不触发。"""
    with pytest.raises(ValidationError, match="AWD"):
        adc_config(watchdog=False, interrupts=["ADC_IT_AWD"])


def test_rejects_watchdog_threshold_over_12bit() -> None:
    with pytest.raises(ValidationError):
        adc_config(watchdog=True, watchdog_high=ADC_THRESHOLD_MAX + 1)


def test_rejects_label_with_newline() -> None:
    with pytest.raises(ValidationError, match="换行"):
        adc_config(label="第一行\n第二行")


# --------------------------------------------------------------------------
# 合法组合必须放行（防"规则越加越吵"）
# --------------------------------------------------------------------------


def test_accepts_minimal_config() -> None:
    cfg = adc_config()
    assert cfg.sequence_length == 1
    assert cfg.prescaler is AdcPrescaler.DIV6


def test_accepts_internal_channel_without_pin() -> None:
    cfg = adc_config(channels=[{"channel": "ADC_Channel_17"}])
    assert not cfg.needs_gpio
    assert cfg.sequence_length == 1


def test_accepts_watchdog_with_valid_window() -> None:
    cfg = adc_config(
        watchdog=True, watchdog_low=100, watchdog_high=3000, interrupts=["ADC_IT_AWD"]
    )
    assert cfg.watchdog
    assert AdcInterrupt.AWD in cfg.interrupts


def test_accepts_adc3_trigger_on_triggers_that_are_not_adc3_only() -> None:
    for trigger in AdcTrigger.options_for_chip():
        cfg = adc_config(trigger=trigger)
        assert cfg.trigger is trigger


def test_interrupts_are_deduped() -> None:
    cfg = adc_config(
        interrupts=["ADC_IT_EOC", "ADC_IT_EOC", "ADC_IT_JEOC"],
    )
    assert [item.name for item in cfg.interrupts] == ["EOC", "JEOC"]


# --------------------------------------------------------------------------
# 芯片一致性
# --------------------------------------------------------------------------


def test_chip_spec_lists_adc_instances() -> None:
    spec = get_chip(ChipId.F103C8T6)
    assert spec.adc_instances == ("ADC1", "ADC2")
    assert spec.has_adc("ADC1")
    assert not spec.has_adc("ADC3")


def test_config_used_ports_includes_adc_pins() -> None:
    """ADC 引脚必须计入 used_ports，否则那个端口的时钟不会使能，
    GPIO_Init 静默无效。

    用 PB0（通道 8）而不是 PC0：LQFP48 的 GPIOC 只引出 13~15，
    PC0 会被芯片一致性校验拦下。
    """
    config = Stm32Config.model_validate(
        {
            "project_name": "t",
            "adc": [
                {
                    "instance": "ADC1",
                    "channels": [
                        {"channel": "ADC_Channel_8", "port": "GPIOB", "pin": 0},
                        {"channel": "ADC_Channel_16"},
                    ],
                }
            ],
        }
    )
    assert "B" in config.used_ports
    assert "C" not in config.used_ports


def test_config_used_ports_excludes_internal_channels() -> None:
    """内部通道不占引脚，不该把某个端口算成"用到了"。"""
    config = Stm32Config.model_validate(
        {
            "project_name": "t",
            "adc": [{"instance": "ADC1", "channels": [{"channel": "ADC_Channel_16"}]}],
        }
    )
    assert config.used_ports == []


def test_config_rejects_pin_not_on_package() -> None:
    """PC12 在 LQFP48 上没引出。"""
    with pytest.raises(ValidationError, match="只引出引脚"):
        Stm32Config.model_validate(
            {
                "project_name": "t",
                "adc": [
                    {
                        "instance": "ADC1",
                        "channels": [
                            {"channel": "ADC_Channel_0", "port": "GPIOC", "pin": 12}
                        ],
                    }
                ],
            }
        )


def test_config_rejects_adc3_instance() -> None:
    with pytest.raises(ValidationError):
        Stm32Config.model_validate(
            {
                "project_name": "t",
                "adc": [{"instance": "ADC3", "channels": [{"channel": "ADC_Channel_0"}]}],
            }
        )


def test_config_rejects_gpio_port_not_on_package() -> None:
    """GPIOE 在 LQFP48 上不存在 —— 与 ADC 引脚走同一条校验。"""
    with pytest.raises(ValidationError):
        Stm32Config.model_validate(
            {
                "project_name": "t",
                "adc": [
                    {
                        "instance": "ADC1",
                        "channels": [
                            {"channel": "ADC_Channel_0", "port": "GPIOE", "pin": 0}
                        ],
                    }
                ],
            }
        )
