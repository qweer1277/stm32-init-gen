"""AFIO 配置模型。

AFIO（Alternate Function I/O）在标准库里不是一个"外设"，而是**两个功能**
的载体，两类配置都在本模型里：

1. **引脚重映射**（``AFIO->MAPR``）—— 把外设的引脚挪到别的脚上。
   本工具在**各外设自己的配置里**提供重映射选项（USART 在 ``UsartConfig``、
   TIM 在 ``TimConfig``），因为"这个外设映射到哪"属于该外设的语义。
2. **SWJ 调试口释放**（``AFIO->MAPR`` 的高位）—— 把 PA13/PA14/PA15/PB3/PB4
   从调试口手里抢回来当普通 GPIO。这是 AFIO 独有的功能，所以放在这里。

**为什么不需要单独开关**：``GPIO_PinRemapConfig`` 与 ``GPIO_EXTILineConfig``
都是直接写 ``AFIO->MAPR`` / ``AFIO->EXTICR`` 寄存器（依据
``Library\\stm32f10x_gpio.c`` 里这两个函数的实现），**没使能 AFIO 时钟时
写入无效**。所以 AFIO 时钟由生成器在用到时自动使能（某个外设选了重映射，
或本模型启用了 SWJ 释放），不交给用户勾选 —— 让用户手动维护一句
"别忘了开 AFIO 时钟"是必然会漏的坑。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stm32gen.models.enums import AfioSwjRemap


class AfioConfig(BaseModel):
    """AFIO 里与具体外设无关的那部分配置。"""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    swj_remap: AfioSwjRemap = Field(
        default=AfioSwjRemap.NONE,
        description="SWJ 调试口释放选项；NONE 表示保持调试口默认（最安全）",
    )

    enabled: bool = Field(
        default=False,
        description="是否为此配置生成 AFIO 初始化。仅在用了 SWJ 释放时才有意义",
    )

    def __init__(self, **data) -> None:
        """``enabled`` 默认跟随 ``swj_remap``：选了释放就自动启用。

        这样用户只需回答"要不要释放调试口引脚"，不用再回答第二个
        "那要不要启用这个配置" —— 后者的答案永远由前者决定。
        """
        if "enabled" not in data and data.get("swj_remap"):
            raw = data["swj_remap"]
            value = raw.value if isinstance(raw, AfioSwjRemap) else str(raw)
            data["enabled"] = bool(value)
        super().__init__(**data)

    @property
    def has_swj(self) -> bool:
        """是否配置了 SWJ 释放。"""
        return self.swj_remap is not AfioSwjRemap.NONE

    @property
    def needs_afio_clock(self) -> bool:
        """本配置是否需要 AFIO 时钟。

        只有 SWJ 释放需要；各外设的重映射由生成器按该外设的情况另行判断。
        """
        return self.enabled and self.has_swj

    @property
    def releases_pins(self) -> tuple[str, ...]:
        """释放出来的引脚名（供校验层判断这些引脚是否真被用上）。"""
        return self.swj_remap.releases if self.has_swj else ()

    @property
    def bricks_debugger(self) -> bool:
        """该配置是否会让调试器无法再连接。

        ``GPIO_Remap_SWJ_Disable`` 把 SWD 一起关掉，之后下载程序只能靠
        BOOT0 跳线或整片擦除 —— 这是**不可逆的操作层面**风险，
        校验层必须给 ERROR 而不是提示。
        """
        return self.enabled and self.has_swj and not self.swj_remap.keeps_swd

    @model_validator(mode="after")
    def _check_consistency(self) -> AfioConfig:
        """拦下自相矛盾的组合。

        ``enabled=False`` 却选了 SWJ 释放是矛盾配置：结果是生成代码里
        根本没有那段释放调用，而用户以为自己已经释放了。这种"静默不生效"
        比报错危险得多，所以直接拦。
        """
        if not self.enabled and self.swj_remap is not AfioSwjRemap.NONE:
            raise ValueError(
                "afio.enabled 为 false 时不应设置 swj_remap —— "
                "那样生成的代码不会释放调试口引脚，但配置看起来像已经释放了"
            )
        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 与生成注释。"""
        if not self.enabled or not self.has_swj:
            return "AFIO 无独立配置"
        parts = [f"释放 {self.swj_remap.name}"]
        if self.releases_pins:
            parts.append("引脚 " + "/".join(self.releases_pins))
        if not self.swj_remap.keeps_swd:
            parts.append("危险：会关闭 SWD（之后无法再下载）")
        return " ".join(parts)
