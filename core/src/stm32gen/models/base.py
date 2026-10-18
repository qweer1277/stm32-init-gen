"""配置模型的根：``Stm32Config``。

这是整个工具的唯一配置真相。GUI 收集表单 → 构造 ``Stm32Config``；
CLI 读 JSON/YAML → 构造 ``Stm32Config``；生成器只接受 ``Stm32Config``。

本模块还负责把「芯片规格」与「外设配置」对齐：型号决定器件宏、
可用端口、可用 USART 实例，这些都不是用户能随意填的自由字段。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stm32gen.models.afio import AfioConfig
from stm32gen.models.chip import CHIPS, ChipId, ChipSpec, get_chip
from stm32gen.models.enums import UsartInstance
from stm32gen.models.exti import ExtiLineConfig
from stm32gen.models.gpio import GpioPinConfig
from stm32gen.models.nvic import NvicChannelConfig, NvicConfig
from stm32gen.models.rcc import RccConfig
from stm32gen.models.tim import TimConfig
from stm32gen.models.usart import UsartConfig

if TYPE_CHECKING:  # 仅类型检查，运行时不导入（避免 models → validate 的硬依赖）
    from stm32gen.validate.baudrate import ClockTree

# 配置文件的格式版本。将来字段有破坏性变更时用它做迁移。
#
# v2（本版）新增 ``rcc`` 字段：时钟树由配置推导，不再假定库的
# SetSysClockTo72() 固定配置。旧配置（v1）加载时 rcc 取默认值，
# 默认值是「HSE×9 / AHB1 / APB1_2 / APB2_1」，与库默认一致，
# 所以旧配置的行为不变。
CONFIG_VERSION = 2


class Stm32Config(BaseModel):
    """一份完整的初始化配置。"""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    version: int = Field(default=CONFIG_VERSION, description="配置格式版本")
    chip: ChipId = Field(default=ChipId.F103C8T6, description="芯片型号")
    hse_hz: int = Field(
        default=8_000_000,
        ge=1_000_000,
        le=25_000_000,
        description="外部晶振频率（Hz）。必须与实际焊接的晶振一致，否则波特率算错",
    )
    project_name: str = Field(
        default="stm32_init",
        min_length=1,
        max_length=64,
        description="工程名，用于生成文件头注释与函数前缀",
    )

    gpio: list[GpioPinConfig] = Field(default_factory=list)
    usart: list[UsartConfig] = Field(default_factory=list)
    nvic: NvicConfig = Field(default_factory=NvicConfig)
    rcc: RccConfig = Field(
        default_factory=RccConfig,
        description="时钟树配置。默认值与库的 SetSysClockTo72() 一致（HSE×9 → 72MHz）",
    )
    afio: AfioConfig = Field(
        default_factory=AfioConfig,
        description="AFIO 配置（SWJ 调试口释放）。各外设的重映射在各外设自己的配置里",
    )
    exti: list[ExtiLineConfig] = Field(
        default_factory=list,
        description="外部中断线配置。每条线对应一个引脚，线号与引脚号相同",
    )
    tim: list[TimConfig] = Field(
        default_factory=list,
        description="定时器配置。可用实例为 TIM1~TIM4（中容量只有这四个）",
    )

    # -- 派生属性 --------------------------------------------------------

    @property
    def spec(self) -> ChipSpec:
        """本配置对应芯片的规格。"""
        return get_chip(self.chip)

    @property
    def clock_tree(self) -> ClockTree:
        """本配置对应的时钟树。

        这是**唯一**的时钟树来源：波特率误差、定时器时钟、Flash 等待
        周期、生成的 RCC 代码全部从这里取，保证四者一致。

        延迟导入 ``ClockTree`` 是因为 ``models`` 不应依赖 ``validate``；
        这里只借用它的纯计算函数（``validate.baudrate`` 不反向导入
        ``models`` 的运行时符号，只在函数内局部导入 RCC 枚举）。
        """
        from stm32gen.validate.baudrate import compute_clock_tree

        return compute_clock_tree(self.hse_hz, self.rcc)

    @property
    def device_macro(self) -> str:
        """器件宏，由芯片型号推导，不由用户直接填。"""
        return self.spec.device_macro

    @property
    def c_flags(self) -> list[str]:
        """编译时需要的预定义宏。

        这三项缺一不可，否则 ``stm32f10x.h`` 会直接 ``#error``：

        - ``device_macro``：如 ``STM32F10X_MD``（依据 ``Start\\stm32f10x.h:91-131``）
        - ``USE_STDPERIPH_DRIVER``：启用标准外设驱动（依据 ``:133-140``）
        - ``HSE_VALUE``：外部晶振频率（依据 ``:149-157``，默认 8000000）
        """
        return [
            self.device_macro,
            "USE_STDPERIPH_DRIVER",
            f"HSE_VALUE={self.hse_hz}",
        ]

    @property
    def used_ports(self) -> list[str]:
        """配置里实际用到的端口字母（含 USART / EXTI / TIM 引脚），已排序去重。

        这个属性决定要生成哪些 ``RCC_APB2Periph_GPIOx`` 时钟使能，
        漏掉任何一个都会让那个端口的 ``GPIO_Init`` 无效 ——
        所以每加一种"会隐式占用引脚"的外设，都必须在这里登记。
        """
        letters = {pin.port.letter for pin in self.gpio}
        for usart in self.usart:
            letters.add(usart.effective_tx_pin[0])
            letters.add(usart.effective_rx_pin[0])
        for line in self.exti:
            letters.add(line.port.letter)
        for timer in self.tim:
            for channel in timer.channels:
                if channel.needs_gpio:
                    letters.add(channel.port.letter)
        # 过滤掉占位符 "?"（引脚未知时可能出现）
        return sorted(letter for letter in letters if letter.isalpha())

    @property
    def usart_instances(self) -> list[UsartInstance]:
        """配置里用到的 USART 实例。"""
        return [item.instance for item in self.usart]

    @property
    def needs_afio_clock(self) -> bool:
        """是否需要使能 AFIO 时钟。

        三类情况会用到 AFIO 寄存器，缺一不可：

        - 有 USART 重映射
        - 有 TIM 重映射
        - 有 EXTI 线（``GPIO_EXTILineConfig`` 写的是 ``AFIO->EXTICR``）
        - 启用了 SWJ 释放

        **EXTI 这条最容易漏**：用户往往以为"外部中断是 EXTI 外设的事"，
        但选择中断线接哪个端口那一步（``GPIO_EXTILineConfig``）实际写的是
        AFIO 的寄存器，不开 AFIO 时钟这一句完全无效、且不报错。
        """
        from stm32gen.models.enums import TimRemap, UsartRemap

        if self.afio.needs_afio_clock:
            return True
        if self.exti:
            return True
        if any(item.remap is not UsartRemap.NONE for item in self.usart):
            return True
        return any(item.remap is not TimRemap.NONE for item in self.tim)

    def port_of(self, letter: str) -> GpioPinConfig | None:
        """取某端口上第一条配置（仅用于 GUI 展示）。"""
        for pin in self.gpio:
            if pin.port.letter == letter:
                return pin
        return None

    def find_pin(self, letter: str, number: int) -> GpioPinConfig | None:
        """按端口与引脚号查找 GPIO 配置。"""
        for pin in self.gpio:
            if pin.port.letter == letter and pin.pin == number:
                return pin
        return None

    def find_usart(self, instance: UsartInstance) -> UsartConfig | None:
        """按实例查找 USART 配置。"""
        for item in self.usart:
            if item.instance is instance:
                return item
        return None

    # -- 校验 ------------------------------------------------------------

    @field_validator("project_name")
    @classmethod
    def _check_project_name(cls, value: str) -> str:
        """工程名要能安全地放进 C 标识符与注释。"""
        text = value.strip()
        if not text:
            raise ValueError("project_name 不能为空")
        if not all(char.isalnum() or char in "_-" for char in text):
            raise ValueError(
                f"project_name「{text}」只能包含字母、数字、下划线和连字符"
            )
        if text[0].isdigit():
            raise ValueError(f"project_name「{text}」不能以数字开头")
        return text

    @field_validator("hse_hz")
    @classmethod
    def _check_hse_range(cls, value: int) -> int:
        """HSE 频率范围按 STM32F103 数据手册的 1~25MHz。"""
        if not 1_000_000 <= value <= 25_000_000:
            raise ValueError(f"hse_hz={value} 超出 STM32F103 允许的 1~25MHz 范围")
        return value

    @model_validator(mode="after")
    def _check_chip_consistency(self) -> Stm32Config:
        """芯片规格与配置内容的一致性。

        这里拦的是「型号与内容自相矛盾」的配置，例如选了 C8T6 却配了
        GPIOE —— 该端口在 LQFP48 封装上根本没引出。
        引脚是否被晶振占用等更细的检查留给校验层，因为那些是「警告」而非「错误」。
        """
        spec = self.spec

        # 端口必须存在
        for pin in self.gpio:
            if not spec.has_port(pin.port.letter):
                raise ValueError(
                    f"配置了 GPIO{pin.port.letter}，但 {spec.name}（{spec.package}）"
                    f"没有引出该端口。可用端口：{''.join(sorted(spec.ports))}"
                )
            if not spec.has_pin(pin.port.letter, pin.pin):
                available = sorted(spec.ports[pin.port.letter])
                raise ValueError(
                    f"配置了 {pin.name}，但 {spec.name}（{spec.package}）"
                    f"的 GPIO{pin.port.letter} 只引出引脚 {available}"
                )

        # USART 实例必须存在
        allowed = set(spec.usart_instances)
        for item in self.usart:
            if item.instance.value not in allowed:
                raise ValueError(
                    f"配置了 {item.instance.value}，但 {spec.name} 只支持 "
                    f"{', '.join(spec.usart_instances)}"
                )

        # USART 引脚所在端口必须存在
        for item in self.usart:
            for role, (letter, number) in (
                ("TX", item.effective_tx_pin),
                ("RX", item.effective_rx_pin),
            ):
                if not spec.has_pin(letter, number):
                    raise ValueError(
                        f"{item.name} 的 {role} 引脚 P{letter}{number} "
                        f"在 {spec.name}（{spec.package}）上不存在"
                    )

        # EXTI 的引脚（P<端口><线号>）必须存在
        for index, line in enumerate(self.exti):
            if not spec.has_port(line.port.letter):
                raise ValueError(
                    f"exti[{index}] 的 {line.name} 选了 GPIO{line.port.letter}，"
                    f"但 {spec.name}（{spec.package}）没有引出该端口。"
                    f"可用端口：{''.join(sorted(spec.ports))}"
                )
            if not spec.has_pin(line.port.letter, line.line):
                raise ValueError(
                    f"exti[{index}] 的 {line.name} 对应引脚 {line.pin_name}，"
                    f"但 {spec.name} 的 GPIO{line.port.letter} 只引出引脚 "
                    f"{sorted(spec.ports[line.port.letter])}。"
                    "线号必须等于引脚号（EXTI3 只能是 Px3）"
                )

        # TIM 的 PWM 输出引脚必须存在
        for index, timer in enumerate(self.tim):
            for channel in timer.channels:
                if not channel.needs_gpio:
                    continue
                if not spec.has_pin(channel.port.letter, channel.pin):
                    raise ValueError(
                        f"tim[{index}] 的 {channel.channel.value} 选了引脚 "
                        f"{channel.pin_name}，但 {spec.name} 的 "
                        f"GPIO{channel.port.letter} 只引出引脚 "
                        f"{sorted(spec.ports.get(channel.port.letter, ()))}"
                    )

        return self

    # -- 序列化 ----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """导出为纯数据字典（枚举转成其符号名）。

        ``mode="json"`` 会把 Enum 转成 ``.value``，正好是标准库符号名，
        配置文件因此是可读、可手工编辑、可版本控制的。
        """
        return self.model_dump(mode="json")

    def to_json(self, indent: int = 2) -> str:
        """导出为 JSON 文本。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Stm32Config:
        """从字典构造。"""
        return cls.model_validate(data)

    def summary(self) -> str:
        """多行摘要，用于 CLI 输出与 GUI 状态栏。"""
        lines = [
            f"芯片      : {self.chip.value} ({self.spec.package}, "
            f"{self.spec.flash_kb}KB Flash / {self.spec.ram_kb}KB RAM)",
            f"器件宏    : {self.device_macro}",
            f"晶振      : {self.hse_hz / 1e6:g} MHz",
            f"GPIO      : {len(self.gpio)} 个引脚，端口 {''.join(self.used_ports) or '无'}",
            f"USART     : {len(self.usart)} 个"
            + (f"（{', '.join(u.name for u in self.usart)}）" if self.usart else ""),
            f"NVIC      : {self.nvic.describe()}",
        ]
        return "\n".join(lines)


# 供 GUI 下拉框使用
def available_chips() -> list[tuple[str, str]]:
    """返回 ``[(型号, 显示名)]`` 列表。"""
    return [(chip.value, f"{chip.value} ({spec.package})") for chip, spec in CHIPS.items()]


def make_demo_config() -> Stm32Config:
    """构造一份能直接生成的示例配置。

    内容对应 STM32F103C8T6 最小系统上最常见的需求：
    PC13 推挽输出接 LED，USART1 用 PA9/PA10 收发并使能 RXNE 中断。
    """
    return Stm32Config(
        chip=ChipId.F103C8T6,
        hse_hz=8_000_000,
        project_name="demo",
        gpio=[
            GpioPinConfig(
                port="C",
                pin=13,
                mode="GPIO_Mode_Out_PP",
                speed="GPIO_Speed_50MHz",
                init_level="Bit_SET",
                label="LED1 低电平点亮",
            )
        ],
        usart=[
            UsartConfig(
                instance="USART1",
                baudrate=115200,
                interrupts=["USART_IT_RXNE"],
                label="调试串口",
            )
        ],
        nvic=NvicConfig(
            priority_group="NVIC_PriorityGroup_2",
            channels=[
                NvicChannelConfig(
                    irq_channel="USART1_IRQn",
                    preemption_priority=1,
                    sub_priority=1,
                    for_peripheral="USART1",
                )
            ],
        ),
    )
