"""GPIO 引脚配置模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stm32gen.models.enums import GpioInitLevel, GpioMode, GpioPort, GpioSpeed


class GpioPinConfig(BaseModel):
    """单个 GPIO 引脚的配置。

    字段名与 ``GPIO_InitTypeDef`` 的成员一一对应（``pin`` ↔ ``GPIO_Pin``、
    ``mode`` ↔ ``GPIO_Mode``、``speed`` ↔ ``GPIO_Speed``），
    生成器转换时不需要做名字映射，减少出错面。
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    port: GpioPort = Field(description="端口，如 GpioPort.A")
    pin: int = Field(ge=0, le=15, description="引脚号 0~15")

    mode: GpioMode = Field(
        default=GpioMode.Out_PP,
        description="工作模式，取值对应标准库 GPIOMode_TypeDef",
    )
    speed: GpioSpeed = Field(
        default=GpioSpeed.SPEED_50MHz,
        description="输出速度，取值对应标准库 GPIOSpeed_TypeDef",
    )

    # 输出模式才有意义的字段。输入模式下留空。
    init_level: GpioInitLevel | None = Field(
        default=None,
        description="输出引脚的初始电平；输入模式应为 None",
    )

    label: str = Field(
        default="",
        max_length=64,
        description="用户备注，如 LED1 / KEY_UP。只进注释，不进代码",
    )

    @property
    def name(self) -> str:
        """人类可读的引脚名，如 ``PA13``。"""
        return f"P{self.port.letter}{self.pin}"

    @property
    def pin_macro(self) -> str:
        """标准库引脚掩码宏，如 ``GPIO_Pin_13``。"""
        return f"GPIO_Pin_{self.pin}"

    @property
    def clock_macro(self) -> str:
        """该引脚所属端口的时钟使能宏。"""
        return self.port.clock_macro

    @property
    def identifier(self) -> tuple[str, int]:
        """用于查重的键。"""
        return (self.port.letter, self.pin)

    @property
    def is_output(self) -> bool:
        return self.mode.is_output

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        """去掉首尾空白；注释里不允许出现换行，否则会破坏生成的 C 代码。"""
        text = value.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("label 不能包含换行符（会破坏生成的注释）")
        return text

    @model_validator(mode="after")
    def _check_init_level(self) -> GpioPinConfig:
        """初始电平与模式的一致性。

        输出模式建议显式给初始电平 —— 否则上电瞬间引脚电平不确定，
        这是嵌入式里典型的「上电抖一下」来源。输入模式下给了则说明
        用户理解有误，直接报错而不是静默忽略。
        """
        if self.mode.is_output and self.init_level is None:
            raise ValueError(
                f"{self.name} 是输出模式（{self.mode.value}），必须指定 init_level"
                "（LOW 或 HIGH），否则上电瞬间电平不确定"
            )
        if self.mode.is_input and self.init_level is not None:
            raise ValueError(
                f"{self.name} 是输入模式（{self.mode.value}），不应指定 init_level"
            )
        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成的注释。"""
        parts = [f"{self.name} {self.mode.name}"]
        if self.mode.is_output:
            parts.append(self.speed.name)
            if self.init_level is not None:
                parts.append(f"初始 {self.init_level.name}")
        if self.label:
            parts.append(f"({self.label})")
        return " ".join(parts)
