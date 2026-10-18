"""引脚占用分析。

把「谁用了哪个引脚」集中算清楚，供冲突检测与提示共用。
关键点：USART / EXTI / TIM / ADC 的引脚是**隐式**占用的 —— 用户只在表单里填了
「用 USART1」「EXTI0 接 PB0」「TIM3_CH1 输出」「ADC1 采通道 3」，但
PA9/PA10、PB0、PA6、PA3 就实际被占用了。不把它们显式建模出来，就会出现
「USART1 和 GPIO PA9 同时配置」这种冲突漏检。

每加一种"会隐式占用引脚"的外设，都必须在这里登记 ——
否则它的引脚会与其他配置冲突而不被发现。

**冲突不是一个档位。** 一个引脚同一时刻只能有一个**驱动程序**（两个外设
同时推挽输出就是 MOS 管对顶），但可以被任意多个外设当**输入**读：F1 的
外设输入硬连线到引脚，GPIO 配成输入后各输入通道都能读到同一电平。
所以这里给每个占用标注方向，由 ``classify_conflict`` 分级，而不是见重就报。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from stm32gen.models import Stm32Config
from stm32gen.models.enums import GpioMode, UsartRemap


class PinDirection(StrEnum):
    """这个占用是往引脚推电平，还是从引脚读电平。

    ``ANALOG`` 单独成一档是因为它独占：配成模拟输入后 GPIO 的数字输入
    缓冲被关闭，此时任何数字外设都读不到电平，不只是"多一个占用"。
    """

    INPUT = "input"
    OUTPUT = "output"
    ANALOG = "analog"


class ConflictKind(StrEnum):
    """引脚复用的冲突种类，决定报成 ERROR 还是 WARNING。"""

    #: 两个以上输出驱动同一引脚 —— 电气冲突，必须改。
    MULTIPLE_OUTPUTS = "multiple_outputs"
    #: 模拟功能与数字功能落在同一引脚 —— 模拟独占，必须改。
    ANALOG_MIXED = "analog_mixed"
    #: 一个输出加若干输入 —— 能跑，但输入读到的是本引脚自己的输出波形。
    OUTPUT_SHADOWS_INPUT = "output_shadows_input"


@dataclass(frozen=True)
class PinUsage:
    """某个引脚被谁占用。"""

    letter: str
    number: int
    # 占用者描述，如 "gpio[0] LED1" 或 "usart[0] USART1 TX"
    owner: str
    # 配置中的定位路径，如 "gpio[0]" / "usart[0]"
    location: str
    # 该引脚在此处的作用，如 "TX" / "RX" / "推挽输出"
    role: str
    # 这里是驱动引脚还是读引脚，冲突分级用
    direction: PinDirection

    @property
    def name(self) -> str:
        """引脚名，如 ``PA9``。"""
        return f"P{self.letter}{self.number}"

    @property
    def key(self) -> tuple[str, int]:
        return (self.letter, self.number)


def gpio_direction(mode: GpioMode) -> PinDirection:
    """把 GPIO 模式映射成引脚方向。

    ``AF_PP`` / ``AF_OD`` 归为输出：复用模式下引脚由外设驱动，用户选这
    两个模式就是为了让外设推电平。``AIN`` 单独判，不能用 ``is_output``
    反推 —— ``GpioMode.is_input`` 把 ``AIN`` 也算作输入。
    """
    if mode is GpioMode.AIN:
        return PinDirection.ANALOG
    if mode.is_input:
        return PinDirection.INPUT
    return PinDirection.OUTPUT


def remapped_pins(
    instance_value: str, remap: UsartRemap
) -> tuple[tuple[str, int], tuple[str, int]]:
    """返回重映射后该 USART 实例的 ``(TX, RX)`` 引脚。

    依据 ``Library\\stm32f10x_gpio.h:170-173`` 的重映射宏语义：

    - ``GPIO_Remap_USART1``：TX/RX 移到 PB6/PB7
    - ``GPIO_Remap_USART2``：TX/RX 移到 PD5/PD6
    - ``GPIO_PartialRemap_USART3``：TX/RX 移到 PC10/PC11
    - ``GPIO_FullRemap_USART3``：TX/RX 移到 PD8/PD9

    注意这些是重映射后**生效**的引脚，不是额外增加的引脚。
    """
    table: dict[tuple[str, UsartRemap], tuple[tuple[str, int], tuple[str, int]]] = {
        ("USART1", UsartRemap.USART1): (("B", 6), ("B", 7)),
        ("USART2", UsartRemap.USART2): (("D", 5), ("D", 6)),
        ("USART3", UsartRemap.USART3_PARTIAL): (("C", 10), ("C", 11)),
        ("USART3", UsartRemap.USART3_FULL): (("D", 8), ("D", 9)),
    }
    return table.get((instance_value, remap), (("?", -1), ("?", -1)))


def effective_usart_pins(
    instance_value: str,
    remap: UsartRemap,
    tx_pin: int | None,
    rx_pin: int | None,
    default_tx: tuple[str, int],
    default_rx: tuple[str, int],
) -> tuple[tuple[str, int], tuple[str, int]]:
    """算出 USART 最终生效的 ``(TX, RX)`` 引脚。

    优先级：显式指定的引脚号 > 重映射后的引脚 > 默认引脚。
    重映射与显式指定同时存在时，以显式指定为准（用户明确写了就听他的），
    但校验层会就这种「重映射了却指定默认端口引脚」的搭配给出提示。
    """
    if remap is not UsartRemap.NONE:
        base_tx, base_rx = remapped_pins(instance_value, remap)
    else:
        base_tx, base_rx = default_tx, default_rx

    tx = (base_tx[0], tx_pin) if tx_pin is not None else base_tx
    rx = (base_rx[0], rx_pin) if rx_pin is not None else base_rx
    return tx, rx


def collect_pin_usages(config: Stm32Config) -> list[PinUsage]:
    """列出配置里全部引脚占用，按占用者顺序。

    GPIO 与 USART 分别处理，USART 的引脚是推导出来的而不是用户直接填的。
    """
    usages: list[PinUsage] = []

    for index, pin in enumerate(config.gpio):
        role = pin.mode.name
        if pin.label:
            role = f"{role} {pin.label}"
        usages.append(
            PinUsage(
                letter=pin.port.letter,
                number=pin.pin,
                owner=f"GPIO {pin.name}",
                location=f"gpio[{index}]",
                role=role,
                direction=gpio_direction(pin.mode),
            )
        )

    for index, usart in enumerate(config.usart):
        tx, rx = effective_usart_pins(
            usart.instance.value,
            usart.remap,
            usart.tx_pin,
            usart.rx_pin,
            usart.instance.default_tx_pin,
            usart.instance.default_rx_pin,
        )
        # TX 是外设驱动引脚，RX 是外设读引脚 —— 这两个方向不同，
        # 所以「USART1_TX 和另一个外设的输入共用引脚」不会被判成输出冲突。
        for role, (letter, number), direction in (
            ("TX", tx, PinDirection.OUTPUT),
            ("RX", rx, PinDirection.INPUT),
        ):
            usages.append(
                PinUsage(
                    letter=letter,
                    number=number,
                    owner=f"{usart.name} {role}",
                    location=f"usart[{index}]",
                    role=role,
                    direction=direction,
                )
            )

    # EXTI：线号即引脚号，端口由用户选。这是"同一线号只能接一个端口"的
    # 依据所在 —— 冲突检测会把两个用同一条线的配置抓出来。
    for index, line in enumerate(config.exti):
        role = f"EXTI{line.line} {line.trigger.label}"
        usages.append(
            PinUsage(
                letter=line.port.letter,
                number=line.line,
                owner=f"{line.name} 输入",
                location=f"exti[{index}]",
                role=role,
                direction=PinDirection.INPUT,
            )
        )

    # TIM：只有用户显式指定了引脚的通道才占用。
    # 没指定引脚时该通道由用户在别处配 GPIO，本工具不该替它声明占用 ——
    # 否则会报出一堆"假冲突"，用户被迫去关掉正确的配置。
    for index, timer in enumerate(config.tim):
        for channel in timer.channels:
            if not channel.needs_gpio:
                continue
            usages.append(
                PinUsage(
                    letter=channel.port.letter,
                    number=channel.pin,
                    owner=f"{timer.name}_{channel.channel.name} 输出",
                    location=f"tim[{index}]",
                    role=f"PWM {channel.mode.name}"
                    + (f" {channel.label}" if channel.label else ""),
                    direction=PinDirection.OUTPUT,
                )
            )

    # ADC：模拟输入，方向标 ANALOG 而不是 INPUT。
    #
    # **这个区分是必须的**：GPIO_Mode_AIN 会把引脚的施密特触发器与
    # 上/下拉一起关掉（CNF=00），此后任何数字外设都读不到该引脚的电平。
    # 若标成 INPUT，`classify_conflict` 会认为「多个输入共用引脚合法」而不报，
    # 用户就会得到一份"ADC 读数永远是 0、串口也收不到数据"的配置。
    #
    # 内部通道（16 = 温度传感器，17 = Vrefint）不接引脚，`needs_gpio`
    # 恒为假，天然被排除 —— 不需要额外的 if。
    for index, adc in enumerate(config.adc):
        for channel in adc.channels:
            if not channel.needs_gpio or channel.port is None:
                continue
            usages.append(
                PinUsage(
                    letter=channel.port.letter,
                    number=channel.pin,
                    owner=f"{adc.name} {channel.channel.name} 模拟输入",
                    location=f"adc[{index}]",
                    role="AIN " + channel.channel.label
                    + (f" {channel.label}" if channel.label else ""),
                    direction=PinDirection.ANALOG,
                )
            )

    return usages


def find_conflicts(usages: list[PinUsage]) -> dict[tuple[str, int], list[PinUsage]]:
    """找出被多处占用的引脚。

    返回 ``{(端口, 引脚号): [占用者...]}``，只保留占用数 > 1 的项。

    注意这里**不判冲突严重程度** —— 多个输入共用引脚是合法的，调用方
    应当再用 ``classify_conflict`` 过滤，否则会逼用户删掉正确配置。
    """
    grouped: dict[tuple[str, int], list[PinUsage]] = {}
    for usage in usages:
        grouped.setdefault(usage.key, []).append(usage)
    return {key: items for key, items in grouped.items() if len(items) > 1}


def classify_conflict(usages: Sequence[PinUsage]) -> ConflictKind | None:
    """判断同一引脚上的这组占用是哪种冲突，合法则返回 ``None``。

    判定顺序按严重程度从高到低，先命中先返回：

    1. 两个以上输出 —— 电气冲突。
    2. 有模拟功能且不止一个占用 —— 模拟独占。
    3. 恰好一个输出加任意输入 —— 输出会盖住输入，多半不是本意。
    4. 全是输入 —— **合法**，返回 ``None``。
    """
    if len(usages) < 2:
        return None

    outputs = sum(1 for item in usages if item.direction is PinDirection.OUTPUT)
    analog = sum(1 for item in usages if item.direction is PinDirection.ANALOG)

    if outputs >= 2:
        return ConflictKind.MULTIPLE_OUTPUTS
    if analog:
        return ConflictKind.ANALOG_MIXED
    if outputs == 1:
        return ConflictKind.OUTPUT_SHADOWS_INPUT
    return None
