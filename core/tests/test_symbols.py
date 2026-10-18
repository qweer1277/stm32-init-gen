"""符号索引层的测试。

这些测试是「禁止编造」约束的回归防线：期望值全部来自对
``E:\\stm32初始化\\代码`` 的实地核对，不是从文档抄的。
"""

from __future__ import annotations

import pytest

from stm32gen.stdlib_ref import (
    DEFAULT_SYMBOLS_PATH,
    InventedSymbolError,
    SymbolIndex,
    default_index,
)


@pytest.fixture(scope="module")
def index() -> SymbolIndex:
    return default_index()


def test_symbols_json_exists() -> None:
    """symbols.json 必须存在且非空——它是所有校验的事实来源。"""
    assert DEFAULT_SYMBOLS_PATH.is_file(), (
        "缺少 symbols.json，请先运行：python tools\\extract_symbols.py"
    )
    assert DEFAULT_SYMBOLS_PATH.stat().st_size > 100_000


def test_stats_are_plausible(index: SymbolIndex) -> None:
    """符号数量应落在合理区间，突然暴跌说明解析器坏了。"""
    stats = index.stats
    assert stats["macros"] > 5000
    assert stats["functions"] > 400
    assert stats["types"] > 50
    assert stats["enum_values"] > 50


def test_meta_records_defines(index: SymbolIndex) -> None:
    """元信息必须记录目标器件宏，否则条件编译走向无法复现。"""
    defines = index.meta["defines"]
    assert "STM32F10X_MD" in defines
    assert "USE_STDPERIPH_DRIVER" in defines


# --------------------------------------------------------------------------
# 数值精确性：这些数字是标准库头文件里的真实定义，不能变
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("GPIO_Pin_0", 0x0001),
        ("GPIO_Pin_13", 0x2000),
        ("GPIO_Pin_15", 0x8000),
        ("GPIO_Pin_All", 0xFFFF),
        ("RCC_APB2Periph_GPIOA", 0x00000004),
        ("RCC_APB2Periph_GPIOB", 0x00000008),
        ("RCC_APB2Periph_GPIOC", 0x00000010),
        ("RCC_APB2Periph_AFIO", 0x00000001),
        ("RCC_APB2Periph_USART1", 0x00004000),
        ("RCC_APB1Periph_USART2", 0x00020000),
        ("RCC_APB1Periph_USART3", 0x00040000),
        ("USART_IT_RXNE", 0x0525),
        ("USART_IT_IDLE", 0x0424),
        ("USART_IT_TXE", 0x0727),
        ("USART_IT_TC", 0x0626),
        ("USART_WordLength_8b", 0x0000),
        ("USART_WordLength_9b", 0x1000),
        ("USART_StopBits_1", 0x0000),
        ("USART_StopBits_2", 0x2000),
        ("USART_Parity_No", 0x0000),
        ("USART_Parity_Even", 0x0400),
        ("USART_Parity_Odd", 0x0600),
        ("USART_Mode_Rx", 0x0004),
        ("USART_Mode_Tx", 0x0008),
        ("USART_HardwareFlowControl_None", 0x0000),
        ("NVIC_PriorityGroup_0", 0x700),
        ("NVIC_PriorityGroup_1", 0x600),
        ("NVIC_PriorityGroup_2", 0x500),
        ("NVIC_PriorityGroup_3", 0x400),
        ("NVIC_PriorityGroup_4", 0x300),
    ],
)
def test_macro_values(index: SymbolIndex, name: str, expected: int) -> None:
    """宏的数值必须与头文件一致。"""
    assert index.has_macro(name), f"缺少宏 {name}"
    assert index.macro_value(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("GPIO_Mode_AIN", 0x0),
        ("GPIO_Mode_IN_FLOATING", 0x04),
        ("GPIO_Mode_IPD", 0x28),
        ("GPIO_Mode_IPU", 0x48),
        ("GPIO_Mode_Out_OD", 0x14),
        ("GPIO_Mode_Out_PP", 0x10),
        ("GPIO_Mode_AF_OD", 0x1C),
        ("GPIO_Mode_AF_PP", 0x18),
        ("GPIO_Speed_10MHz", 1),
        ("GPIO_Speed_2MHz", 2),
        ("GPIO_Speed_50MHz", 3),
        ("Bit_RESET", 0),
        ("Bit_SET", 1),
        ("DISABLE", 0),
        ("ENABLE", 1),
        ("USART1_IRQn", 37),
        ("USART2_IRQn", 38),
        ("USART3_IRQn", 39),
    ],
)
def test_enum_values(index: SymbolIndex, name: str, expected: int) -> None:
    """枚举值的数值必须正确。

    这组测试专门覆盖两个易错点：

    - ``GPIO_Speed_*`` 是**递增省略**写法（``= 1`` 后不再写值）
    - ``ENABLE = !DISABLE`` 是 **C 逻辑非**，Python 里需转换
    """
    assert index.has_enum(name), f"缺少枚举值 {name}"
    assert index.enum_value(name) == expected


def test_enum_type_membership(index: SymbolIndex) -> None:
    """枚举类型必须完整收录其成员。"""
    modes = index.enum_values("GPIOMode_TypeDef")
    assert len(modes) == 8
    assert "GPIO_Mode_AF_PP" in modes

    speeds = index.enum_values("GPIOSpeed_TypeDef")
    assert speeds == ["GPIO_Speed_10MHz", "GPIO_Speed_2MHz", "GPIO_Speed_50MHz"]


def test_irqn_only_medium_density(index: SymbolIndex) -> None:
    """条件编译必须只保留中容量分支。

    ``IRQn_Type`` 里嵌着 LD/MD/HD/CL 等 8 个 ``#ifdef`` 分支，
    若不求值就会混入别的器件的中断号（如 ``OTG_FS_IRQn = 67`` 属于互联型）。
    """
    assert index.has_enum("USART1_IRQn")
    assert not index.has_enum("OTG_FS_IRQn"), "混入了互联型（CL）的中断号"


# --------------------------------------------------------------------------
# 函数与类型
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "GPIO_Init",
        "GPIO_SetBits",
        "GPIO_ResetBits",
        "GPIO_WriteBit",
        "GPIO_ReadInputDataBit",
        "USART_Init",
        "USART_Cmd",
        "USART_ITConfig",
        "NVIC_Init",
        "NVIC_PriorityGroupConfig",
        "RCC_APB2PeriphClockCmd",
        "RCC_APB1PeriphClockCmd",
    ],
)
def test_functions_exist(index: SymbolIndex, name: str) -> None:
    """生成器会用到的函数必须都在索引里。"""
    assert index.has_function(name), f"缺少函数 {name}"


def test_function_signature(index: SymbolIndex) -> None:
    """函数签名要能取到返回类型与参数，生成器据此做参数校验。"""
    info = index.require_function("GPIO_Init")
    assert info.return_type == "void"
    assert len(info.params) == 2
    assert info.params[0]["name"] == "GPIOx"
    assert info.params[1]["name"] == "GPIO_InitStruct"

    usart = index.require_function("USART_ITConfig")
    assert len(usart.params) == 3
    assert usart.params[1]["type"] == "uint16_t"


def test_struct_members(index: SymbolIndex) -> None:
    """结构体成员名必须解析正确。

    库里头文件的结构体成员普遍带多行注释，逐行删注释会把注释文字
    误当成员名，所以这里严格校验成员名与数量。
    """
    gpio_members = [m["name"] for m in index.struct_members("GPIO_InitTypeDef")]
    assert gpio_members == ["GPIO_Pin", "GPIO_Speed", "GPIO_Mode"]

    usart_members = [m["name"] for m in index.struct_members("USART_InitTypeDef")]
    assert usart_members == [
        "USART_BaudRate",
        "USART_WordLength",
        "USART_StopBits",
        "USART_Parity",
        "USART_Mode",
        "USART_HardwareFlowControl",
    ]

    nvic_members = [m["name"] for m in index.struct_members("NVIC_InitTypeDef")]
    assert nvic_members == [
        "NVIC_IRQChannel",
        "NVIC_IRQChannelPreemptionPriority",
        "NVIC_IRQChannelSubPriority",
        "NVIC_IRQChannelCmd",
    ]


def test_struct_members_have_no_comment_text(index: SymbolIndex) -> None:
    """成员名里不能混入注释文字。

    这是回归测试：曾经因为逐行删注释，``GPIO_Pin`` 被解析成
    ``GPIO 引脚。`` 之类的碎片。
    """
    for type_name in ("GPIO_InitTypeDef", "USART_InitTypeDef", "NVIC_InitTypeDef"):
        for member in index.struct_members(type_name):
            assert member["name"].isidentifier(), (
                f"{type_name}.{member['name']} 不是合法标识符（混入了注释文字）"
            )


# --------------------------------------------------------------------------
# 反编造：查不到必须抛异常
# --------------------------------------------------------------------------


def test_invented_symbol_raises(index: SymbolIndex) -> None:
    """编造的符号必须抛异常，而不是静默返回 None。"""
    with pytest.raises(InventedSymbolError):
        index.require_macro("GPIO_Speed_100MHz")
    with pytest.raises(InventedSymbolError):
        index.require_enum("GPIO_Mode_SuperFast")
    with pytest.raises(InventedSymbolError):
        index.require_function("GPIO_FakeFunction")
    with pytest.raises(InventedSymbolError):
        index.require_type("Fake_TypeDef")


def test_error_message_names_the_symbol(index: SymbolIndex) -> None:
    """错误信息里必须点名符号，便于定位。"""
    with pytest.raises(InventedSymbolError) as excinfo:
        index.require_enum("USART_WordLength_12b")
    assert "USART_WordLength_12b" in str(excinfo.value)


def test_has_checks_do_not_raise(index: SymbolIndex) -> None:
    """``has_*`` 是探测接口，不该抛异常。"""
    assert index.has_macro("GPIO_Pin_13") is True
    assert index.has_macro("GPIO_Pin_99") is False
    assert index.has_function("GPIO_Init") is True
    assert index.has_function("GPIO_Nope") is False
    assert index.get("NoSuchSymbol") is None


def test_prefix_queries(index: SymbolIndex) -> None:
    """前缀查询给 GUI 下拉框用，结果必须非空且有序。"""
    pins = index.macros_with_prefix("GPIO_Pin_")
    assert "GPIO_Pin_13" in pins
    assert pins == sorted(pins)

    modes = index.enums_with_prefix("GPIO_Mode_")
    assert "GPIO_Mode_AF_PP" in modes

    funcs = index.functions_with_prefix("GPIO_")
    assert "GPIO_Init" in funcs


def test_describe_reports_location(index: SymbolIndex) -> None:
    """describe 要带上出处，方便 GUI 显示与人工核对。"""
    text = index.describe("GPIO_Pin_13")
    assert "GPIO_Pin_13" in text
    assert "stm32f10x_gpio.h" in text

    missing = index.describe("GPIO_Pin_99")
    assert "不存在" in missing
