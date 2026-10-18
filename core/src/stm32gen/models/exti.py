"""EXTI 外部中断配置模型。

EXTI 的配置比 USART 多一层间接：**先有引脚，才有中断线**。

    GPIO_EXTILineConfig(GPIO_PortSourceGPIOA, GPIO_PinSource0);   ← 选端口
    EXTI_Init(&EXTI_InitStructure);                                ← 配边沿

而且硬件约束是硬的：**同一个线号只能接一个端口**。EXTI0 只能选自
PA0/PB0/PC0… 中的一个，因为 ``AFIO->EXTICR`` 每个引脚号只有 4 位
选择字段（依据 ``Library\\stm32f10x_gpio.c`` 的 ``GPIO_EXTILineConfig``：
``tmp = 0x0F << (4 * (PinSource & 3))``，写进 ``EXTICR[PinSource >> 2]``）。
所以 PA0 与 PB0 不能同时做 EXTI0 —— 这是**必检项**，不是提示。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stm32gen.models.enums import (
    EXTI_EXTERNAL_LINE_MAX,
    EXTI_EXTERNAL_LINE_MIN,
    ExtiMode,
    ExtiTrigger,
    GpioMode,
    GpioPort,
)


class ExtiLineConfig(BaseModel):
    """一条 EXTI 线的配置。

    字段名尽量与 ``EXTI_InitTypeDef`` 的成员对齐
    （``line`` ↔ ``EXTI_Line``、``mode`` ↔ ``EXTI_Mode``、
    ``trigger`` ↔ ``EXTI_Trigger``），另外补充结构体之外的端口来源。
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    line: int = Field(
        ge=EXTI_EXTERNAL_LINE_MIN,
        le=EXTI_EXTERNAL_LINE_MAX,
        description="EXTI 线号 0~15（对应引脚号）。16~19 是内部事件线，本工具不支持",
    )

    port: GpioPort = Field(
        description="该中断线接哪个端口。线号与引脚号相同，所以 EXTI0 既可以是 PA0 也可以是 PB0",
    )

    mode: ExtiMode = Field(
        default=ExtiMode.INTERRUPT,
        description="中断模式或事件模式；事件模式不产生中断，也不需要 NVIC",
    )

    trigger: ExtiTrigger = Field(
        default=ExtiTrigger.FALLING,
        description="触发边沿。按键一般用下降沿（按下拉低）",
    )

    label: str = Field(default="", max_length=64, description="用户备注，只进注释")

    pin_mode: GpioMode = Field(
        default=GpioMode.IPU,
        description=(
            "中断引脚的工作模式。默认上拉输入（IPU）—— "
            "按键接 GND 时按下为低电平，配下降沿触发正好；"
            "悬空输入会在引脚未接时产生随机中断，所以不用 IN_FLOATING"
        ),
    )

    configure_pin: bool = Field(
        default=True,
        description="是否生成该引脚的 GPIO_Init。引脚由别处配置时设为 false",
    )

    @property
    def name(self) -> str:
        """人类可读的名字，如 ``EXTI0``。"""
        return f"EXTI{self.line}"

    @property
    def pin_name(self) -> str:
        """引脚的完整名，如 ``PA0``。"""
        return f"P{self.port.letter}{self.line}"

    @property
    def line_macro(self) -> str:
        """标准库的线宏，如 ``EXTI_Line0``。"""
        return f"EXTI_Line{self.line}"

    @property
    def port_source_macro(self) -> str:
        """``GPIO_EXTILineConfig`` 的端口参数，如 ``GPIO_PortSourceGPIOA``。"""
        return f"GPIO_PortSourceGPIO{self.port.letter}"

    @property
    def pin_source_macro(self) -> str:
        """``GPIO_EXTILineConfig`` 的引脚参数，如 ``GPIO_PinSource0``。"""
        return f"GPIO_PinSource{self.line}"

    @property
    def irqn(self) -> str:
        """对应的 NVIC 中断号。

        **EXTI 的中断号是 7 条线共享 3 个通道**（依据 STM32F103 参考手册
        的中断向量表）：

        - 线 0~4：各自独立，``EXTI0_IRQn`` … ``EXTI4_IRQn``
        - 线 5~9：共享 ``EXTI9_5_IRQn``
        - 线 10~15：共享 ``EXTI15_10_IRQn``

        共享的后果：ISR 里必须逐个判标志位才能知道是哪条线触发的 ——
        生成的中断骨架会把这几个线合并进同一个函数。
        """
        if self.line <= 4:
            return f"EXTI{self.line}_IRQn"
        if self.line <= 9:
            return "EXTI9_5_IRQn"
        return "EXTI15_10_IRQn"

    @property
    def needs_nvic(self) -> bool:
        """是否需要配 NVIC 通道。事件模式不产生中断，不需要。"""
        return self.mode.is_interrupt

    @property
    def handler_name(self) -> str:
        """中断服务函数名。

        末尾的 ``_IRQn`` 换成 ``_IRQHandler`` 即可，共享通道也是一样：
        ``EXTI9_5_IRQn`` → ``EXTI9_5_IRQHandler``（实测启动文件
        ``Start\\startup_stm32f10x_md.s`` 里的向量表就是这么写的）。
        """
        return self.irqn.replace("_IRQn", "_IRQHandler")

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("label 不能包含换行符（会破坏生成的注释）")
        return text

    @model_validator(mode="after")
    def _check_consistency(self) -> ExtiLineConfig:
        """输入模式的引脚不该配成输出，反之亦然。

        EXTI 检测的是引脚上的电平变化，所以引脚必须是**输入**模式。
        配成输出时中断永远不触发（输出由自己驱动，不会因外部信号变化），
        而且 ``GPIO_Init`` 也不会把引脚接到外部信号上。
        """
        if self.configure_pin and self.pin_mode.is_output:
            raise ValueError(
                f"{self.name}（{self.pin_name}）的 pin_mode 是输出模式"
                f"（{self.pin_mode.value}），但 EXTI 检测的是引脚上的外部"
                "电平变化，必须配成输入模式（IPU/IPD/IN_FLOATING）"
            )
        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成注释。"""
        parts = [f"{self.name} ← {self.pin_name}", self.trigger.label, self.mode.name]
        if self.label:
            parts.append(f"({self.label})")
        return " ".join(parts)
