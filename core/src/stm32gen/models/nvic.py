"""NVIC 配置模型。

STM32F103 的 NVIC 用 4 个位表示优先级，分成「抢占优先级」与「子优先级」
两段，分界线由优先级分组决定（``NVIC_PriorityGroup_x``）。
两者的位宽是**互补**的：抢占占 n 位，子优先级就占 4-n 位。

本模块把「分组 → 位宽 → 取值范围」的换算集中在一处，
避免生成器和 GUI 各算一遍、算法还不一致。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stm32gen.models.enums import NvicPriorityGroup, UsartInstance


class NvicChannelConfig(BaseModel):
    """单个中断通道的配置。

    字段名与 ``NVIC_InitTypeDef`` 的成员一一对应。
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    irq_channel: str = Field(
        min_length=1,
        description="中断号枚举名，如 USART1_IRQn。取值由标准库 IRQn_Type 定义",
    )
    preemption_priority: int = Field(
        default=0,
        ge=0,
        le=15,
        description="抢占优先级。实际上限由优先级分组决定，见 validate_against_group",
    )
    sub_priority: int = Field(
        default=0,
        ge=0,
        le=15,
        description="子优先级。实际上限由优先级分组决定",
    )
    enabled: bool = Field(default=True, description="是否使能该通道")

    # 可选：标明这个通道是给哪个外设用的，供 GUI 关联显示
    for_peripheral: str = Field(
        default="",
        description="该通道服务于哪个外设，如 USART1。仅用于展示与关联",
    )

    @property
    def irq_number(self) -> int:
        """从 ``USART1_IRQn`` 推出实例，或返回 -1。

        中断号本身由标准库 ``IRQn_Type`` 定义（``USART1_IRQn = 37``），
        生成代码时写符号名即可，不需要这个数值。它只用于 GUI 排序。
        """
        for instance in UsartInstance:
            if self.irq_channel == instance.irqn:
                return instance.index
        return -1

    def validate_against_group(
        self, group: NvicPriorityGroup
    ) -> tuple[int, int] | None:
        """按分组检查优先级是否越界。

        返回 ``None`` 表示通过；否则返回 ``(实际值, 上限)`` 供调用方组织错误信息。
        这里不用异常，因为校验层要收集全部问题一次性报给用户。
        """
        if self.preemption_priority > group.max_preempt:
            return (self.preemption_priority, group.max_preempt)
        if self.sub_priority > group.max_sub:
            return (self.sub_priority, group.max_sub)
        return None

    @model_validator(mode="after")
    def _check_irq_channel_name(self) -> NvicChannelConfig:
        """中断号名字必须是合法的 C 标识符。

        真正的存在性校验交给校验层（它持有符号索引），
        这里只拦明显写错的（空格、括号、数字开头等）。
        """
        name = self.irq_channel
        if not name.isidentifier():
            raise ValueError(
                f"irq_channel「{name}」不是合法标识符，应形如 USART1_IRQn"
            )
        if not name.endswith("_IRQn"):
            raise ValueError(
                f"irq_channel「{name}」应以 _IRQn 结尾（标准库 IRQn_Type 的命名约定）"
            )
        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 列表与生成的注释。"""
        state = "使能" if self.enabled else "禁用"
        text = (
            f"{self.irq_channel} 抢占{self.preemption_priority}"
            f"/子{self.sub_priority} {state}"
        )
        if self.for_peripheral:
            text += f" ({self.for_peripheral})"
        return text


class NvicConfig(BaseModel):
    """NVIC 全局配置：一个优先级分组 + 若干通道。"""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    priority_group: NvicPriorityGroup = Field(
        default=NvicPriorityGroup.GROUP2,
        description="优先级分组。整个固件只能调用一次 NVIC_PriorityGroupConfig",
    )
    channels: list[NvicChannelConfig] = Field(default_factory=list)

    @property
    def enabled_channels(self) -> list[NvicChannelConfig]:
        """只取使能了的通道。"""
        return [channel for channel in self.channels if channel.enabled]

    def find_channel(self, irq_channel: str) -> NvicChannelConfig | None:
        """按中断号查找通道。"""
        for channel in self.channels:
            if channel.irq_channel == irq_channel:
                return channel
        return None

    def describe(self) -> str:
        """一句话描述。"""
        return (
            f"{self.priority_group.value}"
            f"（抢占 {self.priority_group.preempt_bits} 位 / "
            f"子 {self.priority_group.sub_bits} 位），"
            f"{len(self.enabled_channels)} 个通道"
        )
