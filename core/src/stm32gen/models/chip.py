"""芯片规格：决定可选端口、可用 USART 实例与器件宏。

器件宏（``STM32F10X_MD``）不是用户随便填的字符串，而是由芯片型号推导，
避免出现「选了 C8T6 却定义 STM32F10X_HD」这种自相矛盾的配置。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


class ChipId(str, Enum):
    """支持的芯片型号。值为完整型号名，写进配置 JSON 的就是它。"""

    F103C8T6 = "STM32F103C8T6"
    F103C6T6 = "STM32F103C6T6"


@dataclass(frozen=True)
class ChipSpec:
    """一款芯片的规格。

    ``ports`` 是「端口字母 -> 该端口在本封装上引出的引脚号集合」。
    封装没引出的端口直接不出现在映射里，校验层据此判定非法引脚。

    ``notes`` 记录使用上的坑（晶振占用、调试口占用等），
    由校验层转成提示，而不是让用户自己去翻数据手册。
    """

    chip_id: ChipId
    device_macro: str
    flash_kb: int
    ram_kb: int
    package: str
    ports: Mapping[str, frozenset[int]]
    usart_instances: tuple[str, ...]
    pclk1_hz: int
    pclk2_hz: int
    hse_range_hz: tuple[int, int] = (1_000_000, 25_000_000)
    # 端口字母 -> 引脚号 -> 占用原因
    reserved: Mapping[str, Mapping[int, str]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.chip_id.value

    def has_port(self, letter: str) -> bool:
        return letter in self.ports

    def has_pin(self, letter: str, number: int) -> bool:
        return number in self.ports.get(letter, frozenset())

    def available_pins(self) -> list[tuple[str, int]]:
        """列出全部可用引脚，按端口、引脚号排序。"""
        result: list[tuple[str, int]] = []
        for letter in sorted(self.ports):
            for number in sorted(self.ports[letter]):
                result.append((letter, number))
        return result

    def pin_count(self) -> int:
        return sum(len(pins) for pins in self.ports.values())

    def reserved_reason(self, letter: str, number: int) -> str:
        """该引脚被占用的原因；未被占用返回空串。"""
        return self.reserved.get(letter, {}).get(number, "")


# --------------------------------------------------------------------------
# LQFP48（STM32F103C8T6 / C6T6）
#
# 引脚分布属于封装事实：LQFP48 引出 PA0~PA15、PB0~PB15、PC13~PC15、PD0~PD1。
# 未引出的端口（GPIOE/F/G）不放进 ports，选了就会被校验层拦下。
#
# PD0/PD1 是 OSC_IN/OSC_OUT。用外部晶振（HSE）时被晶振占用，
# PC14/PC15 是 OSC32_IN/OSC32_OUT，用 32.768kHz 晶振时被占用。
# 这两组在本工具里按「保留」处理，因为 HSE 晶振是 8MHz 时钟方案的前提。
# --------------------------------------------------------------------------

_LQFP48_PORTS: Mapping[str, frozenset[int]] = MappingProxyType(
    {
        "A": frozenset(range(16)),
        "B": frozenset(range(16)),
        "C": frozenset({13, 14, 15}),
        "D": frozenset({0, 1}),
    }
)

_LQFP48_RESERVED: Mapping[str, Mapping[int, str]] = MappingProxyType(
    {
        "D": MappingProxyType(
            {
                0: "OSC_IN：被 HSE 外部晶振占用（本工程默认 8MHz）",
                1: "OSC_OUT：被 HSE 外部晶振占用（本工程默认 8MHz）",
            }
        ),
        "C": MappingProxyType(
            {
                14: "OSC32_IN：通常接 32.768kHz 晶振",
                15: "OSC32_OUT：通常接 32.768kHz 晶振",
            }
        ),
    }
)

_LQFP48_NOTES: tuple[str, ...] = (
    "PA13/PA14 默认是 SWDIO/SWCLK 调试口，配成普通 GPIO 会导致无法再次下载程序；"
    "确需使用请先调用 GPIO_PinRemapConfig(GPIO_Remap_SWJ_JTAGDisable, ENABLE)",
    "PA9/PA10 是 USART1 默认引脚，PA2/PA3 是 USART2 默认引脚，PB10/PB11 是 USART3 默认引脚",
)

# 中容量（Medium-density）：64~128KB Flash 用 STM32F10X_MD。
# 注意 C6T6 只有 32KB Flash，按库的定义属于小容量（LD），
# 但 ST 的 MD 宏覆盖 64~128KB；C6T6 实际应定义 STM32F10X_LD。
CHIPS: Mapping[ChipId, ChipSpec] = MappingProxyType(
    {
        ChipId.F103C8T6: ChipSpec(
            chip_id=ChipId.F103C8T6,
            device_macro="STM32F10X_MD",
            flash_kb=64,
            ram_kb=20,
            package="LQFP48",
            ports=_LQFP48_PORTS,
            usart_instances=("USART1", "USART2", "USART3"),
            pclk1_hz=36_000_000,
            pclk2_hz=72_000_000,
            reserved=_LQFP48_RESERVED,
            notes=_LQFP48_NOTES,
        ),
        ChipId.F103C6T6: ChipSpec(
            chip_id=ChipId.F103C6T6,
            device_macro="STM32F10X_LD",
            flash_kb=32,
            ram_kb=10,
            package="LQFP48",
            ports=_LQFP48_PORTS,
            usart_instances=("USART1", "USART2", "USART3"),
            pclk1_hz=36_000_000,
            pclk2_hz=72_000_000,
            reserved=_LQFP48_RESERVED,
            notes=_LQFP48_NOTES,
        ),
    }
)


def get_chip(chip_id: ChipId) -> ChipSpec:
    """取芯片规格；未登记则抛 KeyError（宁可报错也不给错数据）。"""
    return CHIPS[chip_id]
