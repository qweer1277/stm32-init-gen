"""校验规则的测试。

每条规则都要验两件事：**该报的报了**、**不该报的没报**。
只测前者会让规则越加越吵，用户最后干脆忽略全部警告。
"""

from __future__ import annotations

import pytest

from stm32gen.models import (
    ChipId,
    ExtiLineConfig,
    FlashLatency,
    GpioPinConfig,
    NvicChannelConfig,
    NvicConfig,
    RccConfig,
    Stm32Config,
    UsartConfig,
    make_demo_config,
)
from stm32gen.models.enums import GpioMode
from stm32gen.validate import (
    Severity,
    has_blocking,
    summarize,
    validate,
)
from stm32gen.validate.issue import error, info, warning
from stm32gen.validate.pins import (
    ConflictKind,
    PinDirection,
    PinUsage,
    classify_conflict,
    collect_pin_usages,
    find_conflicts,
    gpio_direction,
)


def codes(issues, severity: Severity | None = None) -> set[str]:
    """取出问题代码集合，便于断言。"""
    return {
        item.code
        for item in issues
        if severity is None or item.severity is severity
    }


def pin_config(letter: str, number: int, **kwargs) -> GpioPinConfig:
    """构造一条输出引脚配置的快捷方式。"""
    data = {
        "port": letter,
        "pin": number,
        "mode": "Out_PP",
        "speed": "SPEED_2MHz",
        "init_level": "Bit_SET",
    }
    data.update(kwargs)
    return GpioPinConfig(**data)


# --------------------------------------------------------------------------
# Issue 数据结构
# --------------------------------------------------------------------------


def test_issue_format_includes_location_and_hint() -> None:
    item = error("x.y", "出问题了", location="gpio[0]", hint="这样改")
    text = item.format()
    assert "[错误]" in text
    assert "gpio[0]" in text
    assert "出问题了" in text
    assert "这样改" in text


def test_issue_format_without_hint() -> None:
    item = warning("x.y", "注意", location="a")
    assert "修法" not in item.format()


def test_severity_blocking() -> None:
    """只有 ERROR 阻断生成。"""
    assert Severity.ERROR.is_blocking
    assert not Severity.WARNING.is_blocking
    assert not Severity.INFO.is_blocking


def test_summarize_empty() -> None:
    assert "通过" in summarize([])


def test_summarize_counts() -> None:
    items = [error("a", "1"), warning("b", "2"), warning("c", "3"), info("d", "4")]
    text = summarize(items)
    assert "错误 1" in text
    assert "警告 2" in text
    assert "提示 1" in text


def test_has_blocking() -> None:
    assert has_blocking([error("a", "x")])
    assert not has_blocking([warning("a", "x"), info("b", "y")])


# --------------------------------------------------------------------------
# 引脚占用分析
# --------------------------------------------------------------------------


def test_collect_pin_usages_includes_usart_pins() -> None:
    """USART 的引脚是隐式占用的，必须被收集到。

    用户只在表单里填了「用 USART1」，但 PA9/PA10 实际被占用了。
    不建模就会漏掉「USART1 与 GPIO PA9 冲突」。
    """
    config = Stm32Config(usart=[UsartConfig(instance="USART1")])
    usages = collect_pin_usages(config)
    keys = {usage.key for usage in usages}
    assert ("A", 9) in keys  # TX
    assert ("A", 10) in keys  # RX


def test_collect_pin_usages_remap() -> None:
    """重映射后生效的是新引脚，不是默认引脚。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART1", remap="USART1")])
    usages = collect_pin_usages(config)
    keys = {usage.key for usage in usages}
    assert ("B", 6) in keys  # 重映射后的 TX
    assert ("B", 7) in keys  # 重映射后的 RX
    assert ("A", 9) not in keys  # 默认引脚不再占用


def test_find_conflicts_detects_double_use() -> None:
    config = Stm32Config(
        gpio=[pin_config("A", 9)],
        usart=[UsartConfig(instance="USART1")],
    )
    conflicts = find_conflicts(collect_pin_usages(config))
    assert ("A", 9) in conflicts
    assert len(conflicts[("A", 9)]) == 2


def test_find_conflicts_no_false_positive() -> None:
    """不同引脚不应被报成冲突。"""
    config = make_demo_config()  # PC13 + USART1(PA9/PA10)
    assert find_conflicts(collect_pin_usages(config)) == {}


# --------------------------------------------------------------------------
# check_pins
# --------------------------------------------------------------------------


def test_pin_conflict_is_error() -> None:
    """同一引脚被 GPIO 与 USART 同时占用 → ERROR。"""
    config = Stm32Config(
        gpio=[pin_config("A", 9)],
        usart=[UsartConfig(instance="USART1")],
    )
    issues = validate(config)
    assert "pin.conflict" in codes(issues, Severity.ERROR)
    conflict = next(i for i in issues if i.code == "pin.conflict")
    assert "PA9" in conflict.message
    assert "USART1 TX" in conflict.message
    assert conflict.hint  # 必须给修法


def test_pin_conflict_message_names_both_outputs() -> None:
    """两个输出撞一个引脚时，文案要说清是「多个输出驱动源」。"""
    config = Stm32Config(
        gpio=[pin_config("A", 9)],
        usart=[UsartConfig(instance="USART1")],
    )
    conflict = next(i for i in validate(config) if i.code == "pin.conflict")
    assert "多个输出驱动源" in conflict.message
    assert "GPIO PA9" in conflict.message
    assert "USART1 TX" in conflict.message


def test_two_inputs_on_one_pin_are_legal() -> None:
    """USART1_RX(PA10) 和 EXTI10(PA10) 都是输入，可以共用 —— 不该报冲突。

    这是分级判定的核心用例：一刀切按引脚名分组会把这种正确配置报成错误，
    用户被迫删掉合法的外设。
    """
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1")],
        exti=[ExtiLineConfig(line=10, port="A")],
    )
    issues = validate(config)
    assert "pin.conflict" not in codes(issues)
    assert "pin.direction_conflict" not in codes(issues)


def test_output_shadowing_input_warns() -> None:
    """PA9 既做 GPIO 推挽输出又做 EXTI9 输入 → WARNING，不是错误。"""
    config = Stm32Config(
        gpio=[pin_config("A", 9)],
        exti=[ExtiLineConfig(line=9, port="A")],
    )
    issues = validate(config)
    assert "pin.direction_conflict" in codes(issues, Severity.WARNING)
    item = next(i for i in issues if i.code == "pin.direction_conflict")
    assert "PA9" in item.message
    assert "多个输出驱动源" not in item.message


def test_analog_with_digital_is_error() -> None:
    """模拟输入独占：PA0 配成 AIN 又拿来做 EXTI0 → ERROR。"""
    config = Stm32Config(
        gpio=[GpioPinConfig(port="A", pin=0, mode="AIN")],
        exti=[ExtiLineConfig(line=0, port="A")],
    )
    assert "pin.analog_conflict" in codes(validate(config), Severity.ERROR)


def test_gpio_direction_splits_analog_from_input() -> None:
    """AIN 必须单独判成模拟 —— 不能用 is_output 反推，它把 AIN 算作输入。"""
    assert gpio_direction(GpioMode.AIN) is PinDirection.ANALOG
    assert gpio_direction(GpioMode.IPU) is PinDirection.INPUT
    assert gpio_direction(GpioMode.Out_PP) is PinDirection.OUTPUT
    assert gpio_direction(GpioMode.AF_PP) is PinDirection.OUTPUT


def test_classify_conflict_levels() -> None:
    """分级函数本身：输入共享返回 None，输出撞输出最严重。"""

    def pin(direction: PinDirection) -> PinUsage:
        return PinUsage(
            letter="A",
            number=9,
            owner="x",
            location="x",
            role="x",
            direction=direction,
        )

    assert classify_conflict([pin(PinDirection.INPUT)]) is None
    assert classify_conflict(
        [pin(PinDirection.INPUT), pin(PinDirection.INPUT)]
    ) is None
    assert (
        classify_conflict([pin(PinDirection.OUTPUT), pin(PinDirection.INPUT)])
        is ConflictKind.OUTPUT_SHADOWS_INPUT
    )
    assert (
        classify_conflict([pin(PinDirection.OUTPUT), pin(PinDirection.OUTPUT)])
        is ConflictKind.MULTIPLE_OUTPUTS
    )
    assert (
        classify_conflict([pin(PinDirection.ANALOG), pin(PinDirection.INPUT)])
        is ConflictKind.ANALOG_MIXED
    )


def test_duplicate_gpio_pin_is_error() -> None:
    """同一引脚在 gpio 列表里配两次 → ERROR。"""
    config = Stm32Config(
        gpio=[
            pin_config("C", 13),
            GpioPinConfig(port="C", pin=13, mode="IPU"),  # 输入模式不带 init_level
        ],
    )
    issues = validate(config)
    assert "gpio.duplicate_pin" in codes(issues, Severity.ERROR)


def test_duplicate_usart_instance_is_error() -> None:
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1"), UsartConfig(instance="USART1")]
    )
    issues = validate(config)
    assert "usart.duplicate_instance" in codes(issues, Severity.ERROR)


def test_reserved_pin_warns() -> None:
    """PD0 是 OSC_IN，被 HSE 晶振占用 → WARNING。"""
    config = Stm32Config(
        chip=ChipId.F103C8T6,
        gpio=[pin_config("D", 0)],
    )
    issues = validate(config)
    assert "pin.reserved" in codes(issues, Severity.WARNING)
    reserved = next(i for i in issues if i.code == "pin.reserved")
    assert "HSE" in reserved.message


def test_debug_pin_warns() -> None:
    """PA13 是 SWDIO，配成 GPIO 会导致下载失效 → WARNING。"""
    config = Stm32Config(gpio=[pin_config("A", 13)])
    issues = validate(config)
    assert "pin.debug_port" in codes(issues, Severity.WARNING)
    item = next(i for i in issues if i.code == "pin.debug_port")
    assert "SWDIO" in item.message
    assert "SWJ_JTAGDisable" in item.hint


def test_normal_pin_no_warning() -> None:
    """普通引脚（PC13）不该被报保留或调试口。"""
    config = Stm32Config(gpio=[pin_config("C", 13)])
    issues = validate(config)
    assert "pin.reserved" not in codes(issues)
    assert "pin.debug_port" not in codes(issues)


def test_usart1_remap_to_pb6_pb7_is_ok() -> None:
    """USART1 重映射到 PB6/PB7 在 LQFP48 上可行。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART1", remap="USART1")])
    issues = validate(config)
    assert "usart.remap_pin_missing" not in codes(issues)


def test_usart3_partial_remap_is_error() -> None:
    """USART3 部分重映射要 PC10/PC11，LQFP48 上不存在 → ERROR。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART3", remap="USART3_PARTIAL")])
    issues = validate(config)
    assert "usart.remap_pin_missing" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "usart.remap_pin_missing")
    assert "PC10" in item.message
    assert "PB6/PB7" in item.hint  # 提示唯一可用的重映射


def test_usart2_remap_is_error_on_lqfp48() -> None:
    """USART2 重映射要 PD5/PD6，LQFP48 上不存在。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART2", remap="USART2")])
    issues = validate(config)
    assert "usart.remap_pin_missing" in codes(issues, Severity.ERROR)


# --------------------------------------------------------------------------
# check_chip
# --------------------------------------------------------------------------


def test_default_hse_no_clock_error() -> None:
    """8MHz 是标准配置，不该报时钟错误。"""
    issues = validate(Stm32Config(hse_hz=8_000_000))
    assert "rcc.sysclk_over_spec" not in codes(issues)
    assert "rcc.pclk1_over_spec" not in codes(issues)
    # 也不该报"与默认不同"
    assert "chip.hse_not_default" not in codes(issues)


def test_12mhz_hse_overclocks() -> None:
    """12MHz × 9 = 108MHz 超频 → ERROR。

    规则代码在生成器自配 RCC 之后由 ``chip.sysclk_over_spec`` 改名为
    ``rcc.sysclk_over_spec``（现在校验的是"这套 rcc 配置能不能工作"，
    而不只是"库默认配置的后果"）。
    """
    issues = validate(Stm32Config(hse_hz=12_000_000))
    assert "rcc.sysclk_over_spec" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "rcc.sysclk_over_spec")
    assert "108MHz" in item.message
    # hint 必须给出可执行的修法：降 PLL 倍频（12MHz 下倍频 6 才是 72MHz）
    assert "倍频" in item.hint
    assert "6" in item.hint


def test_default_hse_no_pclk_error() -> None:
    """8MHz 默认配置的 PCLK1 = 36MHz，正好在上限，不该报超频。"""
    issues = validate(Stm32Config(hse_hz=8_000_000))
    assert "rcc.pclk1_over_spec" not in codes(issues)
    assert "rcc.sysclk_over_spec" not in codes(issues)


def test_non_default_hse_info() -> None:
    """非 8MHz 但没超频（如 4MHz）→ INFO 提示。"""
    issues = validate(Stm32Config(hse_hz=4_000_000))
    assert "chip.hse_not_default" in codes(issues, Severity.INFO)
    assert "rcc.sysclk_over_spec" not in codes(issues)


# --------------------------------------------------------------------------
# check_chip：Flash 等待周期（曾经整块是死代码）
# --------------------------------------------------------------------------


def test_flash_latency_override_too_low_is_error() -> None:
    """用户把等待周期覆盖成低于推荐值 → ERROR。

    这条规则的原实现是 ``actual_ws < tree.flash_latency``，但
    ``tree.flash_latency`` 已经是覆盖后的生效值 —— 于是变成
    ``0 < 0`` 恒假，偏低与偏高的两条分支都永远不进，整块是死代码。
    修复后必须比 ``tree.flash_latency_recommended``。

    72MHz 的 HCLK 需要 2 个等待周期，覆盖成 0 是典型的取指不稳配置。
    """
    config = Stm32Config(
        hse_hz=8_000_000,
        rcc=RccConfig(flash_latency_override=FlashLatency.WS0),
    )
    issues = validate(config)
    assert "rcc.flash_latency_too_low" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "rcc.flash_latency_too_low")
    # 消息里的参照值必须是**推荐值 2**，不能是被覆盖后的 0
    assert "需要至少 2" in item.message
    assert item.hint


def test_flash_latency_override_too_high_warns() -> None:
    """覆盖成高于推荐值 → WARNING（只降性能，不危险）。"""
    config = Stm32Config(
        hse_hz=8_000_000,
        rcc=RccConfig(flash_latency_override=FlashLatency.WS2),
    )
    issues = validate(config)
    assert "rcc.flash_latency_high" not in codes(issues)  # 2 正是推荐值


def test_flash_latency_override_high_on_low_clock() -> None:
    """4MHz 的 HCLK 推荐 0 个等待周期，覆盖成 2 → WARNING。"""
    config = Stm32Config(
        hse_hz=8_000_000,
        rcc=RccConfig(
            pll_multiplier="RCC_PLLMul_2",  # 8MHz × 2 = 16MHz ≤ 24MHz → 0 WS
            flash_latency_override=FlashLatency.WS2,
        ),
    )
    issues = validate(config)
    assert "rcc.flash_latency_high" in codes(issues, Severity.WARNING)
    assert "rcc.flash_latency_too_low" not in codes(issues)


def test_flash_latency_no_override_no_issue() -> None:
    """不覆盖时不报等待周期的问题——自动推导总是对的。"""
    issues = validate(Stm32Config(hse_hz=8_000_000))
    assert "rcc.flash_latency_too_low" not in codes(issues)
    assert "rcc.flash_latency_high" not in codes(issues)


# --------------------------------------------------------------------------
# check_usart
# --------------------------------------------------------------------------


def test_baudrate_exact_reports_info() -> None:
    """能整除的波特率应报 INFO 且说明误差为 0。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART1", baudrate=115200)])
    issues = validate(config)
    assert "usart.baudrate_ok" in codes(issues, Severity.INFO)
    assert "usart.baudrate_error_large" not in codes(issues)
    assert "usart.baudrate_error_too_large" not in codes(issues)


def test_baudrate_on_apb1_has_error() -> None:
    """USART2 在 APB1（36MHz），921600 的误差会超阈值。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART2", baudrate=921600)])
    issues = validate(config)
    # 36MHz/921600：BRR=39，实际 923076.9，误差 +0.16%，仍在阈值内
    # 这里主要验证它不会误报成 ERROR
    assert "usart.baudrate_error_too_large" not in codes(issues)


def test_baudrate_extreme_error() -> None:
    """波特率超出 PCLK 能表示的上限 → ERROR。

    36MHz 的 USART2 请求 4.5Mbps：库算出 BRR=8（整数部分 0），
    分频比小于 1，硬件行为未定义。**库自己的 IS_USART_BAUDRATE
    不会拦这个** —— 它只查固定上限 0x0044AA21，与 PCLK 无关。
    所以这是校验层必须补上的缺口。
    """
    config = Stm32Config(usart=[UsartConfig(instance="USART2", baudrate=4_500_000)])
    issues = validate(config)
    assert "usart.baudrate_over_pclk_limit" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "usart.baudrate_over_pclk_limit")
    assert "2250000" in item.message or "2250000" in item.hint


def test_baudrate_just_under_pclk_limit_ok() -> None:
    """36MHz 下 2.25Mbps 正好是上限，应当通过。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART2", baudrate=2_250_000)])
    issues = validate(config)
    assert "usart.baudrate_over_pclk_limit" not in codes(issues)
    assert "usart.baudrate_unrepresentable" not in codes(issues)


def test_baudrate_4_5m_ok_on_usart1() -> None:
    """72MHz 的 USART1 能表示到 4.5Mbps，应当通过。

    这是与 USART2 的对照：同样的波特率，挂不同总线结论不同。
    """
    config = Stm32Config(usart=[UsartConfig(instance="USART1", baudrate=4_500_000)])
    issues = validate(config)
    assert "usart.baudrate_over_pclk_limit" not in codes(issues)


def test_irq_without_nvic_channel_is_error() -> None:
    """使能了中断但没配 NVIC 通道 → ERROR（中断触发了也没人响应）。"""
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1", interrupts=["RXNE"])],
    )
    issues = validate(config)
    assert "usart.irq_without_nvic" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "usart.irq_without_nvic")
    assert "USART1_IRQn" in item.message


def test_irq_with_nvic_channel_ok() -> None:
    """中断与 NVIC 通道都配了 → 不报错。"""
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1", interrupts=["RXNE"])],
        nvic=NvicConfig(
            channels=[NvicChannelConfig(irq_channel="USART1_IRQn")]
        ),
    )
    issues = validate(config)
    assert "usart.irq_without_nvic" not in codes(issues)
    assert "usart.nvic_without_irq" not in codes(issues)


def test_nvic_channel_without_irq_warns() -> None:
    """配了 NVIC 通道但 USART 没使能中断 → WARNING（两步只做了一步）。"""
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1")],
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="USART1_IRQn")]),
    )
    issues = validate(config)
    assert "usart.nvic_without_irq" in codes(issues, Severity.WARNING)
    item = next(i for i in issues if i.code == "usart.nvic_without_irq")
    assert "USART_ITConfig" in item.hint  # 说清两步是哪两步


def test_irq_with_disabled_nvic_channel_warns() -> None:
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1", interrupts=["RXNE"])],
        nvic=NvicConfig(
            channels=[NvicChannelConfig(irq_channel="USART1_IRQn", enabled=False)]
        ),
    )
    issues = validate(config)
    assert "usart.irq_nvic_disabled" in codes(issues, Severity.WARNING)


def test_flow_control_reports_info() -> None:
    config = Stm32Config(
        usart=[UsartConfig(instance="USART1", flow_control="RTS_CTS")]
    )
    issues = validate(config)
    assert "usart.flow_control_pins" in codes(issues, Severity.INFO)


def test_usart3_on_lqfp48_info() -> None:
    config = Stm32Config(usart=[UsartConfig(instance="USART3")])
    issues = validate(config)
    assert "usart.usart3_on_lqfp48" in codes(issues, Severity.INFO)


# --------------------------------------------------------------------------
# check_nvic
# --------------------------------------------------------------------------


def test_preempt_priority_out_of_range() -> None:
    """Group_2 抢占上限 3，配 4 → ERROR。"""
    config = Stm32Config(
        nvic=NvicConfig(
            priority_group="GROUP2",
            channels=[
                NvicChannelConfig(irq_channel="USART1_IRQn", preemption_priority=4)
            ],
        )
    )
    issues = validate(config)
    assert "nvic.preempt_out_of_range" in codes(issues, Severity.ERROR)
    item = next(i for i in issues if i.code == "nvic.preempt_out_of_range")
    assert "最大 3" in item.message


def test_sub_priority_out_of_range() -> None:
    """Group_4 子优先级上限 0，配 1 → ERROR。"""
    config = Stm32Config(
        nvic=NvicConfig(
            priority_group="GROUP4",
            channels=[NvicChannelConfig(irq_channel="USART1_IRQn", sub_priority=1)],
        )
    )
    issues = validate(config)
    assert "nvic.sub_out_of_range" in codes(issues, Severity.ERROR)


def test_priority_in_range_ok() -> None:
    config = Stm32Config(
        nvic=NvicConfig(
            priority_group="GROUP2",
            channels=[
                NvicChannelConfig(
                    irq_channel="USART1_IRQn", preemption_priority=3, sub_priority=3
                )
            ],
        )
    )
    issues = validate(config)
    assert "nvic.preempt_out_of_range" not in codes(issues)
    assert "nvic.sub_out_of_range" not in codes(issues)


def test_unknown_irqn_is_error() -> None:
    """中断号不在标准库 IRQn_Type 里 → ERROR。"""
    config = Stm32Config(
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="USART9_IRQn")])
    )
    issues = validate(config)
    assert "nvic.unknown_irqn" in codes(issues, Severity.ERROR)


def test_known_irqn_no_error() -> None:
    """真实存在的中断号不该被误报。"""
    config = Stm32Config(
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="EXTI0_IRQn")])
    )
    issues = validate(config)
    assert "nvic.unknown_irqn" not in codes(issues)


def test_duplicate_nvic_channel_is_error() -> None:
    config = Stm32Config(
        nvic=NvicConfig(
            channels=[
                NvicChannelConfig(irq_channel="USART1_IRQn"),
                NvicChannelConfig(irq_channel="USART1_IRQn", sub_priority=1),
            ]
        )
    )
    issues = validate(config)
    assert "nvic.duplicate_channel" in codes(issues, Severity.ERROR)


def test_all_same_preempt_warns() -> None:
    """多个中断抢占优先级全 0 → WARNING（无法互相抢占）。"""
    config = Stm32Config(
        nvic=NvicConfig(
            channels=[
                NvicChannelConfig(irq_channel="USART1_IRQn", preemption_priority=0),
                NvicChannelConfig(irq_channel="USART2_IRQn", preemption_priority=0),
            ]
        )
    )
    issues = validate(config)
    assert "nvic.all_same_preempt" in codes(issues, Severity.WARNING)


def test_single_channel_no_same_preempt_warning() -> None:
    """只有一个通道时不报"抢占全 0"——那是正常的。"""
    config = Stm32Config(
        nvic=NvicConfig(channels=[NvicChannelConfig(irq_channel="USART1_IRQn")])
    )
    issues = validate(config)
    assert "nvic.all_same_preempt" not in codes(issues)


def test_nvic_group_note_always_present() -> None:
    """分组说明是 INFO，始终给出位宽与调用一次的限制。"""
    issues = validate(Stm32Config())
    assert "nvic.group_note" in codes(issues, Severity.INFO)
    item = next(i for i in issues if i.code == "nvic.group_note")
    assert "只能调用一次" in item.hint


# --------------------------------------------------------------------------
# check_gpio
# --------------------------------------------------------------------------


def test_floating_input_warns() -> None:
    """浮空输入在引脚悬空时电平不确定 → WARNING。"""
    config = Stm32Config(
        gpio=[GpioPinConfig(port="A", pin=1, mode="IN_FLOATING")]
    )
    issues = validate(config)
    assert "gpio.floating_input" in codes(issues, Severity.WARNING)


def test_pullup_input_no_warning() -> None:
    config = Stm32Config(gpio=[GpioPinConfig(port="A", pin=1, mode="IPU")])
    issues = validate(config)
    assert "gpio.floating_input" not in codes(issues)


def test_analog_mode_info() -> None:
    config = Stm32Config(gpio=[GpioPinConfig(port="A", pin=1, mode="AIN")])
    issues = validate(config)
    assert "gpio.analog_mode" in codes(issues, Severity.INFO)


def test_low_speed_output_info() -> None:
    config = Stm32Config(gpio=[pin_config("C", 13, speed="SPEED_2MHz")])
    issues = validate(config)
    assert "gpio.low_speed_output" in codes(issues, Severity.INFO)


def test_alternate_mode_info() -> None:
    """复用模式提示用户不需要在 gpio 列表里重复配置。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(
                port="A", pin=9, mode="AF_PP", speed="SPEED_50MHz", init_level="Bit_SET"
            )
        ]
    )
    issues = validate(config)
    assert "gpio.alternate_mode" in codes(issues, Severity.INFO)


def test_empty_config_info() -> None:
    issues = validate(Stm32Config())
    assert "config.empty" in codes(issues, Severity.INFO)


# --------------------------------------------------------------------------
# validate() 整体行为
# --------------------------------------------------------------------------


def test_demo_config_has_no_errors() -> None:
    """示例配置应当零错误——它是用户的第一印象。"""
    issues = validate(make_demo_config())
    assert not has_blocking(issues), (
        "示例配置不该有错误：\n"
        + "\n".join(i.format() for i in issues if i.severity is Severity.ERROR)
    )


def test_issues_sorted_by_severity() -> None:
    """结果按 ERROR → WARNING → INFO 排序。"""
    config = Stm32Config(
        gpio=[pin_config("A", 9), pin_config("A", 13)],  # 冲突 + 调试口
        usart=[UsartConfig(instance="USART1")],
    )
    issues = validate(config)
    order = {"error": 0, "warning": 1, "info": 2}
    values = [order[item.severity.value] for item in issues]
    assert values == sorted(values)


def test_validate_dedupes_issues() -> None:
    """重复的问题应被去重，避免刷屏。"""
    config = Stm32Config(gpio=[pin_config("A", 9)], usart=[UsartConfig(instance="USART1")])
    issues = validate(config)
    keys = [(item.code, item.location, item.message) for item in issues]
    assert len(keys) == len(set(keys))


def test_include_info_false_filters() -> None:
    """include_info=False 时只剩错误与警告。"""
    issues = validate(make_demo_config(), include_info=False)
    assert all(item.severity is not Severity.INFO for item in issues)


def test_rule_exception_does_not_break_validation(monkeypatch) -> None:
    """单条规则抛异常不该让整次校验失败，而应报成内部错误。"""
    import stm32gen.validate as validate_module
    import stm32gen.validate.rules as rules_module

    def boom(_config):
        raise RuntimeError("模拟规则崩溃")

    monkeypatch.setattr(rules_module, "check_gpio", boom)
    monkeypatch.setattr(
        validate_module,
        "ALL_RULES",
        (*rules_module.ALL_RULES[:-1], boom),
    )
    issues = validate_module.validate(Stm32Config())
    assert "internal.rule_failed" in codes(issues, Severity.ERROR)


def test_validate_never_raises_on_valid_config() -> None:
    """校验层不抛异常，只返回问题列表。"""
    for hse in (4_000_000, 8_000_000, 12_000_000):
        issues = validate(Stm32Config(hse_hz=hse))
        assert isinstance(issues, list)


@pytest.mark.parametrize("baud", [9600, 19200, 38400, 57600, 115200])
def test_common_baudrates_on_usart1_no_error(baud: int) -> None:
    """常用波特率在 USART1（72MHz）下都不该报错。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART1", baudrate=baud)])
    issues = validate(config)
    assert "usart.baudrate_error_too_large" not in codes(issues)
    assert "usart.baudrate_error_large" not in codes(issues)
