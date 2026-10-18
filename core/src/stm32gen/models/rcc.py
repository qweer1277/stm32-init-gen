"""RCC 时钟配置模型。

与前面几个外设不同，本模块的枚举值**必须分成两类**，因为库里的两套
分频宏名字相近但位域完全不同 —— 用错会静默写坏寄存器位：

===============================  =========================  ==================
配置函数                          该用的宏                    位域
===============================  =========================  ==================
``RCC_HCLKConfig()``             ``RCC_SYSCLK_Div*``        HPRE bit[7:4]
``RCC_PCLK1Config()``            ``RCC_HCLK_Div*``          PPRE1 bit[10:8]
``RCC_PCLK2Config()``            ``RCC_HCLK_Div*``          PPRE2 bit[13:11]
===============================  =========================  ==================

依据 ``Library\\stm32f10x_rcc.c``：

- ``RCC_HCLKConfig()``（:601-613）用 ``CFGR_HPRE_Reset_Mask`` 清理后
  直接 ``tmpreg |= RCC_SYSCLK``，而 ``IS_RCC_HCLK`` 校验的是
  ``RCC_SYSCLK_Div*``（``stm32f10x_rcc.h:308-312``）
- ``RCC_PCLK1Config()``（:626-638）用 ``CFGR_PPRE1_Reset_Mask``，
  校验 ``IS_RCC_PCLK`` 即 ``RCC_HCLK_Div*``（``:326-328``）
- ``RCC_PCLK2Config()``（:651-663）**额外左移 3 位**：``tmpreg |= RCC_HCLK << 3``
  —— 因为 PPRE2 在 PPRE1 上面 3 位，而 ``RCC_HCLK_Div*`` 是按 PPRE1 位置定义的

所以本模块把两套分开成两个枚举类，并在生成时用对应的类，从类型上杜绝混用。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stm32gen.models.enums import SymbolBackedEnum


class RccHseState(SymbolBackedEnum):
    """HSE 外部晶振的启用方式。取自 ``stm32f10x_rcc.h:63-65``。"""

    ON = "RCC_HSE_ON"
    BYPASS = "RCC_HSE_Bypass"
    OFF = "RCC_HSE_OFF"


class RccPllSource(SymbolBackedEnum):
    """PLL 输入源。取自 ``stm32f10x_rcc.h:77-81``。

    - ``HSI_Div2``：HSI(8MHz)/2 = 4MHz，不依赖外部晶振
    - ``HSE_Div1``：HSE 直连（本工程 8MHz）
    - ``HSE_Div2``：HSE/2（HSE 高于 16MHz 时必须用它，否则超出 PLL 输入范围）
    """

    HSI_Div2 = "RCC_PLLSource_HSI_Div2"
    HSE_Div1 = "RCC_PLLSource_HSE_Div1"
    HSE_Div2 = "RCC_PLLSource_HSE_Div2"

    @property
    def is_hse(self) -> bool:
        """是否使用外部晶振作为 PLL 源（决定要不要开 HSE）。"""
        return self in (RccPllSource.HSE_Div1, RccPllSource.HSE_Div2)

    @property
    def divider(self) -> int:
        """对输入时钟的分频系数。"""
        return 2 if self in (RccPllSource.HSI_Div2, RccPllSource.HSE_Div2) else 1


class RccSysclkSource(SymbolBackedEnum):
    """系统时钟源。取自 ``stm32f10x_rcc.h:285-287``。"""

    HSI = "RCC_SYSCLKSource_HSI"
    HSE = "RCC_SYSCLKSource_HSE"
    PLLCLK = "RCC_SYSCLKSource_PLLCLK"

    @property
    def sws_value(self) -> int:
        """切换完成后 SWS 位应有的值，用于生成等待循环。

        依据 ``Library\\stm32f10x_rcc.c:906-914`` 的 ``switch (RCC->CFGR & CFGR_SWS_Mask)``
        分支：``0x00`` = HSI、``0x04`` = HSE、``0x08`` = PLL。
        """
        return {
            RccSysclkSource.HSI: 0x00,
            RccSysclkSource.HSE: 0x04,
            RccSysclkSource.PLLCLK: 0x08,
        }[self]


class RccPllMultiplier(SymbolBackedEnum):
    """PLL 倍频系数。取自 ``stm32f10x_rcc.h:99-113``（非互联型分支）。

    注意值不是连续的位域取值：``RCC_PLLMul_2 = 0x00000000``、
    ``RCC_PLLMul_9 = 0x001C0000``，寄存器里存的是 ``(mul - 2) << 18``。
    库的 ``RCC_GetClocksFreq`` 就是 ``pllmull = (pllmull >> 18) + 2``
    （``stm32f10x_rcc.c:921``），本类如实提取代号供计算用。
    """

    MUL2 = "RCC_PLLMul_2"
    MUL3 = "RCC_PLLMul_3"
    MUL4 = "RCC_PLLMul_4"
    MUL5 = "RCC_PLLMul_5"
    MUL6 = "RCC_PLLMul_6"
    MUL7 = "RCC_PLLMul_7"
    MUL8 = "RCC_PLLMul_8"
    MUL9 = "RCC_PLLMul_9"
    MUL10 = "RCC_PLLMul_10"
    MUL11 = "RCC_PLLMul_11"
    MUL12 = "RCC_PLLMul_12"
    MUL13 = "RCC_PLLMul_13"
    MUL14 = "RCC_PLLMul_14"
    MUL15 = "RCC_PLLMul_15"
    MUL16 = "RCC_PLLMul_16"

    @property
    def factor(self) -> int:
        """倍频系数数值，2~16。"""
        return int(self.name.replace("MUL", ""))


class AhbPrescaler(SymbolBackedEnum):
    """AHB 分频。**用 ``RCC_SYSCLK_Div*`` 宏**（HPRE 位域）。

    喂给 ``RCC_HCLKConfig()`` 的必须是这一套 —— 见模块 docstring 的表格。
    """

    DIV1 = "RCC_SYSCLK_Div1"
    DIV2 = "RCC_SYSCLK_Div2"
    DIV4 = "RCC_SYSCLK_Div4"
    DIV8 = "RCC_SYSCLK_Div8"
    DIV16 = "RCC_SYSCLK_Div16"
    DIV64 = "RCC_SYSCLK_Div64"
    DIV128 = "RCC_SYSCLK_Div128"
    DIV256 = "RCC_SYSCLK_Div256"
    DIV512 = "RCC_SYSCLK_Div512"

    @property
    def divider(self) -> int:
        return int(self.name.replace("DIV", ""))


class ApbPrescaler(SymbolBackedEnum):
    """APB1/APB2 分频。**用 ``RCC_HCLK_Div*`` 宏**（PPRE 位域）。

    注意只有 1/2/4/8/16 五档（不像 AHB 能到 512），因为 PPRE 是 3 位。
    喂给 ``RCC_PCLK1Config()`` / ``RCC_PCLK2Config()``。
    """

    DIV1 = "RCC_HCLK_Div1"
    DIV2 = "RCC_HCLK_Div2"
    DIV4 = "RCC_HCLK_Div4"
    DIV8 = "RCC_HCLK_Div8"
    DIV16 = "RCC_HCLK_Div16"

    @property
    def divider(self) -> int:
        return int(self.name.replace("DIV", ""))


class FlashLatency(SymbolBackedEnum):
    """Flash 等待周期。取自 ``stm32f10x_flash.h:70-72``。

    依据 STM32F103 数据手册的表（HCLK 与等待周期对应关系）：

    - 0 WS：0 < HCLK ≤ 24MHz
    - 1 WS：24 < HCLK ≤ 48MHz
    - 2 WS：48 < HCLK ≤ 72MHz

    这个值**不用用户填**，由时钟树自动推导（见 ``validate/baudrate.py``
    的 ``recommended_flash_latency``）。让用户自己算容易在 48/72MHz 边界
    上填错，而填错的后果是取指不稳、偶发跑飞 —— 极难排查。
    """

    WS0 = "FLASH_Latency_0"
    WS1 = "FLASH_Latency_1"
    WS2 = "FLASH_Latency_2"

    @property
    def wait_states(self) -> int:
        return int(self.name.replace("WS", ""))


class FlashPrefetch(SymbolBackedEnum):
    """Flash 预取缓冲。取自 ``stm32f10x_flash.h:96-97``。

    HCLK 高于 24MHz 时**必须开启**，否则取指成为瓶颈，实测性能损失很大。
    """

    ENABLE = "FLASH_PrefetchBuffer_Enable"
    DISABLE = "FLASH_PrefetchBuffer_Disable"


class RccConfig(BaseModel):
    """时钟树配置。

    这是「自动配置 RCC」的载体：用户选 PLL 源、倍频与各级分频，
    生成器产出真正的 ``RCC_*Config()`` 调用序列。

    **重要**：本配置一旦改动，PCLK1/PCLK2 就会变，而
    ``validate/baudrate.py`` 的波特率误差按 PCLK 计算 ——
    两边必须用同一条时钟树。为此 ``compute_clock_tree()`` 改为接受
    本配置（见该函数），波特率误差与生成代码永远一致。
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    # 是否生成 RCC 初始化代码。
    # False 时沿用启动文件里 SystemInit() 已配好的时钟树（库默认 72MHz），
    # 此时 hse_hz 由 Stm32Config 提供，其余字段仅用于**推演**时钟树供校验。
    enabled: bool = Field(
        default=True,
        description="是否生成 RCC 初始化代码。False 表示沿用 SystemInit() 的配置",
    )

    hse_state: RccHseState = Field(
        default=RccHseState.ON,
        description="HSE 启用方式；用 HSI 作 PLL 源时可设为 OFF",
    )
    pll_source: RccPllSource = Field(
        default=RccPllSource.HSE_Div1,
        description="PLL 输入源。HSE 高于 16MHz 时必须用 HSE_Div2",
    )
    pll_multiplier: RccPllMultiplier = Field(
        default=RccPllMultiplier.MUL9,
        description="PLL 倍频系数 2~16",
    )
    sysclk_source: RccSysclkSource = Field(
        default=RccSysclkSource.PLLCLK,
        description="系统时钟源，通常为 PLLCLK",
    )

    ahb_prescaler: AhbPrescaler = Field(
        default=AhbPrescaler.DIV1,
        description="AHB 分频（HPRE），用 RCC_SYSCLK_Div* 宏",
    )
    apb1_prescaler: ApbPrescaler = Field(
        default=ApbPrescaler.DIV2,
        description="APB1 分频（PPRE1），用 RCC_HCLK_Div* 宏",
    )
    apb2_prescaler: ApbPrescaler = Field(
        default=ApbPrescaler.DIV1,
        description="APB2 分频（PPRE2），用 RCC_HCLK_Div* 宏",
    )

    # Flash 相关：默认自动推导，用户可覆盖
    flash_prefetch: FlashPrefetch = Field(
        default=FlashPrefetch.ENABLE,
        description="Flash 预取缓冲；HCLK>24MHz 必须开启",
    )
    flash_latency_override: FlashLatency | None = Field(
        default=None,
        description="手动指定 Flash 等待周期；None 表示按时钟自动推导（推荐）",
    )

    @property
    def uses_hse(self) -> bool:
        """是否需要开启 HSE。

        两种情况需要：PLL 源用 HSE，或 SYSCLK 直接选 HSE。
        """
        return self.pll_source.is_hse or self.sysclk_source is RccSysclkSource.HSE

    @property
    def needs_pll(self) -> bool:
        """是否用到 PLL。"""
        return self.sysclk_source is RccSysclkSource.PLLCLK

    @model_validator(mode="after")
    def _check_consistency(self) -> RccConfig:
        """组合一致性检查。

        只拦「自相矛盾」的配置，需要知道 HSE 具体频率才能判断的
        （如 PLL 输入频率超范围）留给校验层 —— 那里才拿得到 hse_hz。
        """
        # 用 HSE 作 PLL 源，却不开启 HSE
        if self.pll_source.is_hse and self.hse_state is RccHseState.OFF:
            raise ValueError(
                "PLL 源选了 HSE，但 HSE 状态是 OFF —— 那样 PLL 没有输入时钟，"
                "芯片会卡在 RCC_WaitForHSEStartUp 的等待里"
            )

        # 用 PLLCLK 作 SYSCLK，但 PLL 源是 HSI 且 HSE 关闭
        if (
            self.needs_pll
            and self.pll_source is RccPllSource.HSI_Div2
            and self.hse_state is RccHseState.ON
        ):
            # 这不矛盾（HSE 开着但不用），只是浪费，给个提示而不报错
            pass

        # SYSCLK 选 HSE 但 HSE 关闭
        if self.sysclk_source is RccSysclkSource.HSE and self.hse_state is RccHseState.OFF:
            raise ValueError(
                "SYSCLK 选了 HSE，但 HSE 状态是 OFF —— 没有时钟源可选"
            )

        return self

    def describe(self) -> str:
        """一句话描述，用于 GUI 与摘要。"""
        parts = [f"{self.pll_source.name} × {self.pll_multiplier.factor}"]
        parts.append(f"AHB/{self.ahb_prescaler.divider}")
        parts.append(f"APB1/{self.apb1_prescaler.divider}")
        parts.append(f"APB2/{self.apb2_prescaler.divider}")
        return f"{self.sysclk_source.name} ← " + " ".join(parts)


# 本模块定义的全部符号背书枚举，供 ``models/__init__.py`` 汇总校验
RCC_ENUMS: tuple[type[SymbolBackedEnum], ...] = (
    RccHseState,
    RccPllSource,
    RccSysclkSource,
    RccPllMultiplier,
    AhbPrescaler,
    ApbPrescaler,
    FlashLatency,
    FlashPrefetch,
)
