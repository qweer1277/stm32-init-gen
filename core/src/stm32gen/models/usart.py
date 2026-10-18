"""USART 配置模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stm32gen.models.enums import (
    UsartDirection,
    UsartFlowControl,
    UsartInstance,
    UsartInterrupt,
    UsartParity,
    UsartRemap,
    UsartStopBits,
    UsartWordLength,
)

# 波特率下限与上限。
#
# 上限按 STM32F103 数据手册的最大 4.5Mbps 取。注意库的
# ``IS_USART_BAUDRATE``（``Library\\stm32f10x_usart.h:335``）要求
# ``BAUDRATE < 0x0044AA21``，即略小于 4.5Mbps —— 但真正的限制是
# ``PCLK / 16``（硬件要求 USARTDIV >= 1），那条由校验层负责。
# 这里只做「单字段合法范围」的粗筛。
#
# 常量放在 models 层而非 validate 层：依赖方向是 validate → models，
# 模型不能反向依赖校验层。校验层自己有一份 ``BAUDRATE_MAX``，
# 两者语义不同（这里是输入粗筛，那里是库约束）。
BAUDRATE_MIN_HINT = 1200
BAUDRATE_MAX_HINT = 4_500_000


class UsartConfig(BaseModel):
    """单个 USART 实例的配置。

    字段名与 ``USART_InitTypeDef`` 的成员一一对应，
    另外补充了该结构体之外的配置（中断、重映射、引脚）。
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    instance: UsartInstance = Field(description="实例，如 UsartInstance.USART1")

    baudrate: int = Field(
        default=115200,
        ge=BAUDRATE_MIN_HINT,
        le=BAUDRATE_MAX_HINT,
        description="波特率。上限按 4.5Mbps 取（STM32F103 数据手册的最大值）",
    )

    word_length: UsartWordLength = Field(default=UsartWordLength.B8)
    stop_bits: UsartStopBits = Field(default=UsartStopBits.B1)
    parity: UsartParity = Field(default=UsartParity.NONE)
    direction: UsartDirection = Field(
        default=UsartDirection.RX_TX,
        description="收发方向；生成时展开为 USART_Mode_Rx | USART_Mode_Tx",
    )
    flow_control: UsartFlowControl = Field(default=UsartFlowControl.NONE)

    # 中断：选中的才生成 USART_ITConfig 调用，并据此决定要不要配 NVIC
    interrupts: list[UsartInterrupt] = Field(
        default_factory=list,
        description="需要使能的中断。非空时应在 nvic 里配对应通道",
    )

    remap: UsartRemap = Field(
        default=UsartRemap.NONE,
        description="引脚重映射；NONE 表示用默认引脚",
    )

    # 显式指定引脚。留空则按实例默认引脚自动推导。
    tx_pin: int | None = Field(default=None, ge=0, le=15, description="TX 引脚号")
    rx_pin: int | None = Field(default=None, ge=0, le=15, description="RX 引脚号")

    label: str = Field(default="", max_length=64, description="用户备注，只进注释")

    @property
    def name(self) -> str:
        """实例名，如 ``USART1``。"""
        return self.instance.value

    @property
    def irqn(self) -> str:
        """中断号枚举名，如 ``USART1_IRQn``。"""
        return self.instance.irqn

    @property
    def clock_macro(self) -> str:
        """时钟使能宏。"""
        return self.instance.clock_macro

    @property
    def needs_nvic(self) -> bool:
        """是否需要配 NVIC 通道。"""
        return bool(self.interrupts)

    @property
    def effective_tx_pin(self) -> tuple[str, int]:
        """生效的 TX 引脚 ``(端口字母, 引脚号)``。

        未显式指定时取该实例的默认引脚。
        """
        if self.tx_pin is None:
            return self.instance.default_tx_pin
        return (self.instance.default_tx_pin[0], self.tx_pin)

    @property
    def effective_rx_pin(self) -> tuple[str, int]:
        """生效的 RX 引脚 ``(端口字母, 引脚号)``。"""
        if self.rx_pin is None:
            return self.instance.default_rx_pin
        return (self.instance.default_rx_pin[0], self.rx_pin)

    @property
    def mode_expression(self) -> str:
        """``USART_InitTypeDef.USART_Mode`` 的 C 表达式。"""
        return self.direction.expression

    @field_validator("interrupts")
    @classmethod
    def _dedupe_interrupts(cls, value: list[UsartInterrupt]) -> list[UsartInterrupt]:
        """去重并保持稳定顺序，避免生成重复的 USART_ITConfig 调用。"""
        seen: set[UsartInterrupt] = set()
        result: list[UsartInterrupt] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("label 不能包含换行符（会破坏生成的注释）")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> UsartConfig:
        """参数组合的一致性检查。

        这里只拦「组合起来没有意义」的配置，不拦需要知道 PCLK 才能判断的
        波特率精度问题 —— 那属于校验层的活。
        """
        # 单向配置下选流控没有意义
        if self.direction is UsartDirection.TX and self.flow_control is not UsartFlowControl.NONE:
            raise ValueError(
                f"{self.name} 只发不收，硬件流控（{self.flow_control.value}）无意义"
            )

        # 校验位与字长。关键事实：库里的 USART_WordLength 指的是
        # **含校验位的帧位数**，不是纯数据位数（对应 USART_CR1 的 M 位）：
        #   M=0 (8b) + 无校验 → 8 数据位
        #   M=0 (8b) + 有校验 → 校验位占用最高位，实际只剩 7 数据位
        #   M=1 (9b) + 无校验 → 9 数据位
        #   M=1 (9b) + 有校验 → 8 数据位 + 1 校验位  ← 最常用的 8E1/8O1
        # 所以「9b + 校验」是**完全正常**的组合，绝不能拦；
        # 要拦的是「8b + 校验」—— 用户多半想要 8 数据位带校验，
        # 却因此只拿到 7 个数据位。
        if self.parity is not UsartParity.NONE and self.word_length is UsartWordLength.B8:
            raise ValueError(
                f"{self.name} 选了 8 位字长 + {self.parity.name} 校验："
                "字长含校验位，此时实际只有 7 个数据位。"
                "若要 8 数据位带校验，请把 word_length 改为 B9（9b），"
                "对应常见的 8E1/8O1 配置"
            )

        # TXE 与 TC 同时开没有意义：TC 已包含 TXE 的语义
        if UsartInterrupt.TXE in self.interrupts and UsartInterrupt.TC in self.interrupts:
            raise ValueError(
                f"{self.name} 同时使能了 TXE 和 TC 中断，二者语义重叠，请只选一个"
            )

        # RX 方向却使能 TXE/TC，或 TX 方向却使能 RXNE/IDLE
        if self.direction is UsartDirection.TX and (
            UsartInterrupt.RXNE in self.interrupts or UsartInterrupt.IDLE in self.interrupts
        ):
            raise ValueError(f"{self.name} 只发不收，不应使能 RXNE/IDLE 中断")
        if self.direction is UsartDirection.RX and (
            UsartInterrupt.TXE in self.interrupts or UsartInterrupt.TC in self.interrupts
        ):
            raise ValueError(f"{self.name} 只收不发，不应使能 TXE/TC 中断")

        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成的注释。"""
        parts = [f"{self.name} {self.baudrate}bps"]
        parts.append(f"{self.word_length.name[1:]}位")
        if self.parity is not UsartParity.NONE:
            parts.append(self.parity.name)
        parts.append(f"停止位{self.stop_bits.name[1:]}")
        parts.append(self.direction.name)
        if self.interrupts:
            parts.append("中断:" + "+".join(i.name for i in self.interrupts))
        if self.label:
            parts.append(f"({self.label})")
        return " ".join(parts)
