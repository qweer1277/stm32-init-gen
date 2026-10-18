"""配置模型层的测试。

重点覆盖三类内容：

1. **枚举引用的符号必须真实存在**（反编造约束在模型层的落地）
2. **字段间的组合约束**（哪些配置组合是自相矛盾的）
3. **芯片规格约束**（LQFP48 没引出的端口不能被配置）
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from stm32gen.models import (
    CONFIG_VERSION,
    ChipId,
    GpioInitLevel,
    GpioMode,
    GpioPinConfig,
    GpioPort,
    GpioSpeed,
    NvicChannelConfig,
    NvicConfig,
    NvicPriorityGroup,
    Stm32Config,
    UsartConfig,
    UsartDirection,
    UsartInstance,
    UsartRemap,
    UsartWordLength,
    available_chips,
    get_chip,
    make_demo_config,
    verify_all,
)
from stm32gen.models.enums import SYMBOL_BACKED_ENUMS

# --------------------------------------------------------------------------
# 反编造：枚举引用的符号必须真实存在
# --------------------------------------------------------------------------


def test_verify_all_passes() -> None:
    """导入时已跑过一次；这里显式再跑，失败信息更清楚。"""
    verify_all()


def test_every_symbol_backed_enum_verifies() -> None:
    """逐个枚举单独校验，定位到具体是哪个类出问题。"""
    for enum_cls in SYMBOL_BACKED_ENUMS:
        enum_cls.verify()


def test_all_enum_values_are_library_symbols() -> None:
    """枚举的值必须能在符号表里查到（UsartRemap.NONE 的空值除外）。"""
    from stm32gen.stdlib_ref import default_index

    index = default_index()
    for enum_cls in SYMBOL_BACKED_ENUMS:
        for member in enum_cls:
            if member.value == "":
                continue  # NONE 表示"不重映射"，本就不该是符号
            assert index.has_any(member.value), (
                f"{enum_cls.__name__}.{member.name} 的值「{member.value}」"
                "在标准库中不存在"
            )


def test_verify_detects_invented_symbol() -> None:
    """校验函数必须真的能抓出编造符号，而不是永远返回成功。"""
    from stm32gen.models.enums import SymbolBackedEnum

    class Bad(SymbolBackedEnum):
        NOPE = "GPIO_Speed_999MHz"

    with pytest.raises(KeyError, match="GPIO_Speed_999MHz"):
        Bad.verify()


def test_verify_accepts_real_symbols() -> None:
    """反向确认：真实符号不应被误判。"""
    from stm32gen.models.enums import SymbolBackedEnum

    class Good(SymbolBackedEnum):
        MODE = "GPIO_Mode_Out_PP"
        PIN = "GPIO_Pin_13"
        FUNC = "GPIO_Init"

    Good.verify()


# --------------------------------------------------------------------------
# 枚举的语义属性
# --------------------------------------------------------------------------


def test_gpio_port_helpers() -> None:
    assert GpioPort.A.letter == "A"
    assert GpioPort.A.clock_macro == "RCC_APB2Periph_GPIOA"
    assert GpioPort.C.clock_macro == "RCC_APB2Periph_GPIOC"


def test_gpio_mode_classification() -> None:
    """输入/输出/复用的分类必须正确——生成器据此决定要不要配速度。"""
    assert GpioMode.AIN.is_input
    assert GpioMode.IN_FLOATING.is_input
    assert GpioMode.IPD.is_input
    assert GpioMode.IPU.is_input

    assert GpioMode.Out_PP.is_output
    assert GpioMode.Out_OD.is_output
    assert GpioMode.AF_PP.is_output
    assert GpioMode.AF_OD.is_output

    assert GpioMode.AF_PP.is_alternate
    assert not GpioMode.Out_PP.is_alternate


def test_gpio_speed_hz() -> None:
    assert GpioSpeed.SPEED_10MHz.hz == 10_000_000
    assert GpioSpeed.SPEED_2MHz.hz == 2_000_000
    assert GpioSpeed.SPEED_50MHz.hz == 50_000_000


def test_usart_instance_helpers() -> None:
    assert UsartInstance.USART1.index == 1
    assert UsartInstance.USART1.irqn == "USART1_IRQn"
    assert UsartInstance.USART1.clock_macro == "RCC_APB2Periph_USART1"
    assert UsartInstance.USART2.clock_macro == "RCC_APB1Periph_USART2"
    assert UsartInstance.USART3.clock_macro == "RCC_APB1Periph_USART3"

    # USART1 挂 APB2，其余挂 APB1 —— 波特率计算的分频基数不同
    assert UsartInstance.USART1.is_apb2
    assert not UsartInstance.USART2.is_apb2
    assert not UsartInstance.USART3.is_apb2


def test_usart_default_pins() -> None:
    """默认引脚必须与数据手册一致，校验层靠它检测冲突。"""
    assert UsartInstance.USART1.default_tx_pin == ("A", 9)
    assert UsartInstance.USART1.default_rx_pin == ("A", 10)
    assert UsartInstance.USART2.default_tx_pin == ("A", 2)
    assert UsartInstance.USART2.default_rx_pin == ("A", 3)
    assert UsartInstance.USART3.default_tx_pin == ("B", 10)
    assert UsartInstance.USART3.default_rx_pin == ("B", 11)


def test_usart_direction_expression() -> None:
    """方向要展开成位或表达式，且只用库里的宏。"""
    assert UsartDirection.RX.expression == "USART_Mode_Rx"
    assert UsartDirection.TX.expression == "USART_Mode_Tx"
    assert UsartDirection.RX_TX.expression == "USART_Mode_Rx | USART_Mode_Tx"
    assert UsartDirection.RX_TX.macros == ("USART_Mode_Rx", "USART_Mode_Tx")


def test_nvic_priority_group_bit_widths() -> None:
    """分组的位宽与上限必须与 misc.h 的注释表一致。

    依据 Library\\misc.h:80-94 的表格：
    Group_0 抢占 0 位/子 4 位 …… Group_4 抢占 4 位/子 0 位。
    """
    assert (NvicPriorityGroup.GROUP0.preempt_bits, NvicPriorityGroup.GROUP0.sub_bits) == (0, 4)
    assert (NvicPriorityGroup.GROUP1.preempt_bits, NvicPriorityGroup.GROUP1.sub_bits) == (1, 3)
    assert (NvicPriorityGroup.GROUP2.preempt_bits, NvicPriorityGroup.GROUP2.sub_bits) == (2, 2)
    assert (NvicPriorityGroup.GROUP3.preempt_bits, NvicPriorityGroup.GROUP3.sub_bits) == (3, 1)
    assert (NvicPriorityGroup.GROUP4.preempt_bits, NvicPriorityGroup.GROUP4.sub_bits) == (4, 0)

    # 位宽互补，总和恒为 4
    for group in NvicPriorityGroup:
        assert group.preempt_bits + group.sub_bits == 4


def test_nvic_priority_limits() -> None:
    """上限 = 2^位宽 - 1。"""
    assert NvicPriorityGroup.GROUP0.max_preempt == 0
    assert NvicPriorityGroup.GROUP0.max_sub == 15
    assert NvicPriorityGroup.GROUP2.max_preempt == 3
    assert NvicPriorityGroup.GROUP2.max_sub == 3
    assert NvicPriorityGroup.GROUP4.max_preempt == 15
    assert NvicPriorityGroup.GROUP4.max_sub == 0


# --------------------------------------------------------------------------
# 枚举别名解析
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GPIOC", GpioPort.C),
        ("C", GpioPort.C),
        ("c", GpioPort.C),
        ("gpioc", GpioPort.C),
    ],
)
def test_port_alias_resolution(raw: str, expected: GpioPort) -> None:
    """用户既能写标准库符号名，也能写简短的端口字母。"""
    assert GpioPort(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GPIO_Mode_Out_PP", GpioMode.Out_PP),
        ("Out_PP", GpioMode.Out_PP),
        ("out_pp", GpioMode.Out_PP),
    ],
)
def test_mode_alias_resolution(raw: str, expected: GpioMode) -> None:
    assert GpioMode(raw) is expected


def test_alias_resolution_rejects_unknown() -> None:
    """别名解析不能变成"什么都接受"。"""
    with pytest.raises(ValueError):
        GpioMode("GPIO_Mode_Teleport")
    with pytest.raises(ValueError):
        GpioPort("Z")


def test_enum_labels_for_gui() -> None:
    """GUI 下拉框要能拿到 (符号名, 显示名) 列表。"""
    labels = GpioMode.labels()
    assert ("GPIO_Mode_Out_PP", "Out_PP") in labels
    assert len(labels) == 8


# --------------------------------------------------------------------------
# GpioPinConfig
# --------------------------------------------------------------------------


def test_gpio_output_requires_init_level() -> None:
    """输出模式必须给初始电平——否则上电瞬间电平不确定。"""
    with pytest.raises(ValidationError, match="init_level"):
        GpioPinConfig(port="A", pin=5, mode="Out_PP", speed="SPEED_50MHz")


def test_gpio_input_rejects_init_level() -> None:
    """输入模式给初始电平说明用户理解有误，直接报错而非静默忽略。"""
    with pytest.raises(ValidationError, match="不应指定 init_level"):
        GpioPinConfig(
            port="A", pin=5, mode="IPU", init_level="Bit_SET"
        )


def test_gpio_input_ok_without_init_level() -> None:
    pin = GpioPinConfig(port="A", pin=5, mode="IPU")
    assert not pin.is_output
    assert pin.init_level is None


def test_gpio_output_ok_with_init_level() -> None:
    pin = GpioPinConfig(
        port="C", pin=13, mode="Out_PP", speed="SPEED_50MHz", init_level="Bit_SET"
    )
    assert pin.is_output
    assert pin.init_level is GpioInitLevel.HIGH


def test_gpio_pin_range() -> None:
    """引脚号必须落在 0~15。"""
    with pytest.raises(ValidationError):
        GpioPinConfig(port="A", pin=16, mode="IPU")
    with pytest.raises(ValidationError):
        GpioPinConfig(port="A", pin=-1, mode="IPU")


def test_gpio_derived_names() -> None:
    pin = GpioPinConfig(
        port="C", pin=13, mode="Out_PP", speed="SPEED_50MHz", init_level="Bit_RESET"
    )
    assert pin.name == "PC13"
    assert pin.pin_macro == "GPIO_Pin_13"
    assert pin.clock_macro == "RCC_APB2Periph_GPIOC"
    assert pin.identifier == ("C", 13)


def test_gpio_label_rejects_newline() -> None:
    """label 会进注释，含换行会破坏生成的 C 代码。"""
    with pytest.raises(ValidationError, match="换行"):
        GpioPinConfig(
            port="A",
            pin=1,
            mode="Out_PP",
            speed="SPEED_2MHz",
            init_level="Bit_RESET",
            label="LED\n/* 注入 */",
        )


def test_gpio_extra_field_rejected() -> None:
    """拼错的字段名要报错，不能静默忽略。"""
    with pytest.raises(ValidationError):
        GpioPinConfig(
            port="A", pin=1, mode="IPU", speed_typo="SPEED_2MHz"
        )


def test_gpio_describe() -> None:
    pin = GpioPinConfig(
        port="C",
        pin=13,
        mode="Out_PP",
        speed="SPEED_50MHz",
        init_level="Bit_SET",
        label="LED1",
    )
    text = pin.describe()
    assert "PC13" in text
    assert "LED1" in text


# --------------------------------------------------------------------------
# UsartConfig
# --------------------------------------------------------------------------


def test_usart_defaults() -> None:
    usart = UsartConfig(instance="USART1")
    assert usart.baudrate == 115200
    assert usart.word_length is UsartWordLength.B8
    assert usart.direction is UsartDirection.RX_TX
    assert not usart.needs_nvic
    assert usart.mode_expression == "USART_Mode_Rx | USART_Mode_Tx"


def test_usart_effective_pins_use_defaults() -> None:
    usart = UsartConfig(instance="USART1")
    assert usart.effective_tx_pin == ("A", 9)
    assert usart.effective_rx_pin == ("A", 10)

    usart3 = UsartConfig(instance="USART3")
    assert usart3.effective_tx_pin == ("B", 10)
    assert usart3.effective_rx_pin == ("B", 11)


def test_usart_explicit_pins_override() -> None:
    usart = UsartConfig(instance="USART1", tx_pin=12, rx_pin=11)
    assert usart.effective_tx_pin == ("A", 12)
    assert usart.effective_rx_pin == ("A", 11)


def test_usart_word_length_parity_8b_with_parity_rejected() -> None:
    """8b + 校验 → 实际只有 7 个数据位，必须拦下并给出修法。"""
    with pytest.raises(ValidationError, match="7 个数据位"):
        UsartConfig(instance="USART1", word_length="B8", parity="EVEN")


def test_usart_word_length_parity_9b_with_parity_allowed() -> None:
    """9b + 校验 = 8 数据位 + 1 校验位，这是最常见的 8E1/8O1，必须放行。

    这条是回归测试：初版实现把 9b+校验 误判为错误，方向正好搞反了。
    """
    usart = UsartConfig(instance="USART1", word_length="B9", parity="EVEN")
    assert usart.word_length is UsartWordLength.B9
    assert usart.parity.value == "USART_Parity_Even"


def test_usart_tx_only_rejects_flow_control() -> None:
    with pytest.raises(ValidationError, match="只发不收"):
        UsartConfig(instance="USART1", direction="TX", flow_control="RTS")


def test_usart_tx_only_rejects_rx_interrupts() -> None:
    with pytest.raises(ValidationError, match="不应使能 RXNE/IDLE"):
        UsartConfig(instance="USART1", direction="TX", interrupts=["RXNE"])


def test_usart_rx_only_rejects_tx_interrupts() -> None:
    with pytest.raises(ValidationError, match="不应使能 TXE/TC"):
        UsartConfig(instance="USART1", direction="RX", interrupts=["TC"])


def test_usart_txe_and_tc_conflict() -> None:
    """TXE 与 TC 语义重叠，同时开没有意义。"""
    with pytest.raises(ValidationError, match="语义重叠"):
        UsartConfig(instance="USART1", interrupts=["TXE", "TC"])


def test_usart_interrupts_deduped() -> None:
    """重复的中断要合并，否则会生成重复的 USART_ITConfig 调用。"""
    usart = UsartConfig(instance="USART1", interrupts=["RXNE", "RXNE", "IDLE", "RXNE"])
    assert [i.name for i in usart.interrupts] == ["RXNE", "IDLE"]
    assert usart.needs_nvic


def test_usart_baudrate_range() -> None:
    with pytest.raises(ValidationError):
        UsartConfig(instance="USART1", baudrate=10)
    with pytest.raises(ValidationError):
        UsartConfig(instance="USART1", baudrate=99_000_000)


def test_usart_remap_none_is_empty_string() -> None:
    """NONE 表示不重映射，值是空串——生成器据此跳过 PinRemapConfig。"""
    usart = UsartConfig(instance="USART1")
    assert usart.remap is UsartRemap.NONE
    assert usart.remap.value == ""


def test_usart_describe() -> None:
    usart = UsartConfig(instance="USART1", baudrate=9600, interrupts=["RXNE"])
    text = usart.describe()
    assert "USART1" in text
    assert "9600" in text
    assert "RXNE" in text


# --------------------------------------------------------------------------
# NvicChannelConfig / NvicConfig
# --------------------------------------------------------------------------


def test_nvic_channel_requires_irqn_suffix() -> None:
    """中断号名字必须遵循标准库 IRQn_Type 的命名约定。"""
    with pytest.raises(ValidationError, match="_IRQn"):
        NvicChannelConfig(irq_channel="USART1")
    with pytest.raises(ValidationError, match="合法标识符"):
        NvicChannelConfig(irq_channel="USART 1_IRQn")


def test_nvic_channel_ok() -> None:
    channel = NvicChannelConfig(irq_channel="USART1_IRQn", preemption_priority=1, sub_priority=1)
    assert channel.irq_number == 1
    assert channel.enabled


def test_nvic_channel_validate_against_group() -> None:
    """按分组检查越界，返回 (实际值, 上限) 而不是抛异常。"""
    channel = NvicChannelConfig(
        irq_channel="USART1_IRQn", preemption_priority=4, sub_priority=0
    )
    # Group_2 抢占上限 3，4 越界
    assert channel.validate_against_group(NvicPriorityGroup.GROUP2) == (4, 3)
    # Group_4 抢占上限 15，通过
    assert channel.validate_against_group(NvicPriorityGroup.GROUP4) is None

    sub_bad = NvicChannelConfig(
        irq_channel="USART1_IRQn", preemption_priority=0, sub_priority=8
    )
    # Group_2 子上限 3，8 越界
    assert sub_bad.validate_against_group(NvicPriorityGroup.GROUP2) == (8, 3)


def test_nvic_config_enabled_channels() -> None:
    config = NvicConfig(
        priority_group="GROUP2",
        channels=[
            NvicChannelConfig(irq_channel="USART1_IRQn"),
            NvicChannelConfig(irq_channel="USART2_IRQn", enabled=False),
        ],
    )
    assert len(config.enabled_channels) == 1
    assert config.find_channel("USART2_IRQn") is not None
    assert config.find_channel("USART3_IRQn") is None


# --------------------------------------------------------------------------
# Stm32Config
# --------------------------------------------------------------------------


def test_chip_specs() -> None:
    c8 = get_chip(ChipId.F103C8T6)
    assert c8.device_macro == "STM32F10X_MD"
    assert c8.flash_kb == 64
    assert c8.ram_kb == 20
    assert c8.package == "LQFP48"

    c6 = get_chip(ChipId.F103C6T6)
    assert c6.device_macro == "STM32F10X_LD"
    assert c6.flash_kb == 32


def test_lqfp48_port_layout() -> None:
    """LQFP48 的端口分布：PA/PB 全 16 脚，PC 只有 13~15，PD 只有 0~1。

    依据 STM32F103C8T6 的 LQFP48 封装引脚定义。
    """
    spec = get_chip(ChipId.F103C8T6)
    assert spec.has_port("A")
    assert spec.has_port("B")
    assert spec.has_port("C")
    assert spec.has_port("D")
    # GPIOE/F/G 在 LQFP48 上不存在
    assert not spec.has_port("E")
    assert not spec.has_port("F")
    assert not spec.has_port("G")

    assert spec.has_pin("A", 0)
    assert spec.has_pin("A", 15)
    assert not spec.has_pin("C", 12)  # PC12 未引出
    assert spec.has_pin("C", 13)
    assert spec.has_pin("C", 15)
    assert not spec.has_pin("D", 2)  # PD2 未引出
    assert spec.has_pin("D", 1)

    # 共 48 个可用引脚（16+16+3+2 = 37 个 GPIO，其余是电源/复位/晶振）
    assert spec.pin_count() == 37


def test_reserved_pins_documented() -> None:
    """被晶振占用的引脚要能查到原因，供校验层转成提示。"""
    spec = get_chip(ChipId.F103C8T6)
    assert "HSE" in spec.reserved_reason("D", 0)
    assert "OSC32" in spec.reserved_reason("C", 14)
    assert spec.reserved_reason("A", 5) == ""


def test_config_rejects_nonexistent_port() -> None:
    """选了 LQFP48 上不存在的 GPIOE 必须报错。"""
    with pytest.raises(ValidationError, match="没有引出该端口"):
        Stm32Config(
            gpio=[
                GpioPinConfig(
                    port="E", pin=5, mode="Out_PP", speed="SPEED_2MHz", init_level="Bit_SET"
                )
            ]
        )


def test_config_rejects_nonexistent_pin() -> None:
    """PC12 在 LQFP48 上未引出。"""
    with pytest.raises(ValidationError, match="只引出引脚"):
        Stm32Config(
            gpio=[
                GpioPinConfig(
                    port="C", pin=12, mode="Out_PP", speed="SPEED_2MHz", init_level="Bit_SET"
                )
            ]
        )


def test_config_rejects_unsupported_usart() -> None:
    """C8T6 只有 USART1/2/3，配 USART4 要报错。

    这里有两道防线，报错来自**更靠前的那道**（枚举本身不含 USART4）：
    1. ``UsartInstance`` 枚举只有 1/2/3，写 "USART4" 连枚举都构造不出来
    2. ``Stm32Config._check_chip_consistency`` 再按芯片规格核对一遍

    第 2 道在枚举覆盖了某实例、但具体型号不支持时才会生效
    （例如将来加入互联型芯片，它才需要 USART4/5）。
    """
    with pytest.raises(ValidationError, match="Input should be"):
        Stm32Config(usart=[UsartConfig(instance="USART4")])


def test_chip_usart_instance_list_is_enforced() -> None:
    """芯片规格里的实例清单必须与实际检查逻辑一致。

    用一个「规格说支持、枚举里也有」的实例反证：正常配置能通过。
    """
    spec = get_chip(ChipId.F103C8T6)
    assert set(spec.usart_instances) == {"USART1", "USART2", "USART3"}
    for name in spec.usart_instances:
        config = Stm32Config(usart=[UsartConfig(instance=name)])
        assert config.usart_instances[0].value == name


def test_config_c_flags() -> None:
    """编译宏三项缺一不可，否则 stm32f10x.h 会 #error。"""
    config = Stm32Config(hse_hz=8_000_000)
    assert config.c_flags == [
        "STM32F10X_MD",
        "USE_STDPERIPH_DRIVER",
        "HSE_VALUE=8000000",
    ]

    custom = Stm32Config(hse_hz=12_000_000)
    assert "HSE_VALUE=12000000" in custom.c_flags


def test_config_device_macro_follows_chip() -> None:
    """器件宏由型号推导，不能各填各的。"""
    assert Stm32Config(chip="STM32F103C8T6").device_macro == "STM32F10X_MD"
    assert Stm32Config(chip="STM32F103C6T6").device_macro == "STM32F10X_LD"


def test_config_hse_range() -> None:
    with pytest.raises(ValidationError):
        Stm32Config(hse_hz=100)
    with pytest.raises(ValidationError):
        Stm32Config(hse_hz=100_000_000)


def test_config_project_name_rules() -> None:
    with pytest.raises(ValidationError):
        Stm32Config(project_name="")
    with pytest.raises(ValidationError, match="只能包含"):
        Stm32Config(project_name="my project")
    with pytest.raises(ValidationError, match="不能以数字开头"):
        Stm32Config(project_name="1demo")
    assert Stm32Config(project_name="my-proj_1").project_name == "my-proj_1"


def test_config_used_ports_includes_usart() -> None:
    """USART 的引脚也算用到的端口，生成器要据此使能时钟。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(
                port="C", pin=13, mode="Out_PP", speed="SPEED_2MHz", init_level="Bit_RESET"
            )
        ],
        usart=[UsartConfig(instance="USART3")],  # PB10/PB11
    )
    assert config.used_ports == ["B", "C"]


def test_config_find_helpers() -> None:
    config = make_demo_config()
    assert config.find_pin("C", 13) is not None
    assert config.find_pin("C", 12) is None
    assert config.find_usart(UsartInstance.USART1) is not None
    assert config.find_usart(UsartInstance.USART2) is None


# --------------------------------------------------------------------------
# 序列化往返
# --------------------------------------------------------------------------


def test_demo_config_roundtrip() -> None:
    """配置 → JSON → 配置，必须完全等价。"""
    original = make_demo_config()
    restored = Stm32Config.from_dict(json.loads(original.to_json()))
    assert restored.to_dict() == original.to_dict()


def test_serialized_values_are_library_symbols() -> None:
    """序列化后写入文件的值必须是标准库符号名，而不是枚举成员名。"""
    data = make_demo_config().to_dict()
    assert data["gpio"][0]["mode"] == "GPIO_Mode_Out_PP"
    assert data["gpio"][0]["speed"] == "GPIO_Speed_50MHz"
    assert data["gpio"][0]["init_level"] == "Bit_SET"
    assert data["usart"][0]["instance"] == "USART1"
    assert data["usart"][0]["word_length"] == "USART_WordLength_8b"
    assert data["nvic"]["priority_group"] == "NVIC_PriorityGroup_2"


def test_config_version_present() -> None:
    assert make_demo_config().to_dict()["version"] == CONFIG_VERSION


def test_handwritten_json_with_short_names() -> None:
    """手写 JSON 时用简短别名也能解析（端口写 "C" 而不是 "GPIOC"）。"""
    data = {
        "chip": "STM32F103C8T6",
        "gpio": [
            {"port": "C", "pin": 13, "mode": "Out_PP", "speed": "SPEED_50MHz",
             "init_level": "Bit_SET", "label": "LED"}
        ],
    }
    config = Stm32Config.from_dict(data)
    assert config.gpio[0].port is GpioPort.C
    assert config.gpio[0].mode is GpioMode.Out_PP


def test_summary_contains_key_facts() -> None:
    text = make_demo_config().summary()
    assert "STM32F103C8T6" in text
    assert "STM32F10X_MD" in text
    assert "USART1" in text


def test_available_chips_for_gui() -> None:
    chips = available_chips()
    assert len(chips) == 2
    assert any(value == "STM32F103C8T6" for value, _ in chips)
