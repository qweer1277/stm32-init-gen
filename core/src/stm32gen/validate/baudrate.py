"""USART 波特率的精确复算。

**为什么不能只用公式**：``USART_Init`` 算 BRR 用的是整数运算，
末尾还有四舍五入的取整，实际波特率与「PCLK/16/BRR」的理想值有偏差。
要判断一个波特率能不能用，必须逐位复现库里的算法，否则算出的误差
是假的 —— 会把能用的配置判成不可用，或反过来。

本模块严格对照 ``E:\\stm32初始化\\代码\\Library\\stm32f10x_usart.c``
的 ``USART_Init()``（第 228~267 行）实现。

时钟来源
--------
PCLK 不是自由参数，由 ``Start\\system_stm32f10x.c`` 的 ``SetSysClockTo72()``
与 ``HSE_VALUE`` 共同决定：

- ``RCC_CFGR_PLLMULL9``（第 1125 行）→ SYSCLK = HSE × 9
- ``RCC_CFGR_HPRE_DIV1``（第 1091 行）→ HCLK = SYSCLK
- ``RCC_CFGR_PPRE2_DIV1``（第 1094 行）→ PCLK2 = HCLK
- ``RCC_CFGR_PPRE1_DIV2``（第 1097 行）→ PCLK1 = HCLK / 2

注意第 1096 行的注释写的是「PCLK1 = HCLK」，但代码是 ``PPRE1_DIV2``，
**注释与代码矛盾，以代码为准**。这是 ST 库里的文档笔误。
"""

from __future__ import annotations

from dataclasses import dataclass

from stm32gen.models.enums import UsartInstance

# 库里的波特率上限：IS_USART_BAUDRATE 要求 BAUDRATE < 0x0044AA21
# （依据 Library\stm32f10x_usart.h:335）
BAUDRATE_MAX = 0x0044AA21 - 1  # 4500000

# SetSysClockTo72() 里硬编码的 PLL 倍频
# ── 旧版固定时钟树的常量（保留供「未生成 RCC 代码」时作参照） ──
#
# 这些值来自 system_stm32f10x.c 的 SetSysClockTo72()（PLLMULL9、HPRE_DIV1、
# PPRE2_DIV1、PPRE1_DIV2）。**现在不再用它们计算时钟树**——时钟树改为由
# RccConfig 推导（见 compute_clock_tree），否则用户一改 PLL 倍频或 APB1
# 分频，波特率误差与定时器时钟就会拿错的 PCLK 去算 —— 那种「静默算错」
# 比报错危险得多。
PLL_MULTIPLIER = 9
HCLK_DIVIDER = 1
PCLK1_DIVIDER = 2
PCLK2_DIVIDER = 1

# STM32F103 的最高主频
SYSCLK_MAX_HZ = 72_000_000
# APB1 的最高频率（超过这个值，挂在 APB1 上的外设工作不正常）
PCLK1_MAX_HZ = 36_000_000
# PLL 输入频率范围（依据 STM32F103 数据手册：PLL 输入 4~16MHz）
PLL_INPUT_MIN_HZ = 4_000_000
PLL_INPUT_MAX_HZ = 16_000_000
# HSE 频率范围
HSE_MIN_HZ = 1_000_000
HSE_MAX_HZ = 25_000_000

# 波特率误差的经验阈值。3% 是串口通信的常见容忍上限；
# 超过 5% 基本会出错。这里用两档给出不同严重级别。
BAUD_ERROR_WARN_PCT = 3.0
BAUD_ERROR_FAIL_PCT = 5.0

# HSI 频率（芯片内部 RC，库的 RCC_GetClocksFreq 用 HSI_VALUE）
HSI_HZ = 8_000_000


def recommended_flash_latency(hclk_hz: int) -> int:
    """按时钟推荐 Flash 等待周期。

    依据 STM32F103 数据手册的表：

    - 0 WS：0 < HCLK ≤ 24MHz
    - 1 WS：24 < HCLK ≤ 48MHz
    - 2 WS：48 < HCLK ≤ 72MHz

    这个换算不该让用户自己做 —— 填错的后果是取指不稳、偶发跑飞，
    极难排查。所以模型层自动推导，只在用户显式覆盖时才用他的值。
    """
    if hclk_hz <= 24_000_000:
        return 0
    if hclk_hz <= 48_000_000:
        return 1
    return 2


@dataclass(frozen=True)
class ClockTree:
    """时钟树。

    所有频率都由 ``RccConfig``（或未启用时的库默认配置）推导，
    **不再是硬编码的 PLL×9**。这样波特率误差、定时器时钟、
    Flash 等待周期三者与生成代码用的是同一条时钟树。
    """

    hse_hz: int
    sysclk_hz: int
    hclk_hz: int
    pclk1_hz: int
    pclk2_hz: int
    sysclk_in_spec: bool
    pclk1_in_spec: bool
    # 以下为新增，供校验层做更细的判断
    pll_input_hz: int = 0
    ahb_divider: int = 1
    apb1_divider: int = 2
    apb2_divider: int = 1
    pll_factor: int = 9
    # 定时器时钟：APB 分频系数 != 1 时，定时器时钟是 PCLK 的 2 倍
    tim1_hz: int = 0
    tim234_hz: int = 0
    # 生效的 Flash 等待周期（用户覆盖值或推荐值）
    flash_latency: int = 2
    # **按时钟推荐的**等待周期。与 ``flash_latency`` 分开是有必要的：
    # 校验层要拿用户覆盖值跟推荐值比，若只留生效值就会变成「跟自己比」
    # —— ``0 < 0`` 恒假，偏低的情况永远不报错（初版就犯了这个错）。
    flash_latency_recommended: int = 2

    @property
    def pll_ratio_ok(self) -> bool:
        """SYSCLK 是否等于芯片最高主频 72MHz。

        「是否最优」而不是「是否合法」—— 合法但不满速由校验层给
        INFO 提示，不报错。
        """
        return self.sysclk_hz == SYSCLK_MAX_HZ

    @property
    def pll_input_in_spec(self) -> bool:
        """PLL 输入频率是否在 4~16MHz 范围内。"""
        return PLL_INPUT_MIN_HZ <= self.pll_input_hz <= PLL_INPUT_MAX_HZ

    def pclk_of(self, instance: UsartInstance) -> int:
        """该实例所在总线的时钟频率。"""
        return self.pclk2_hz if instance.is_apb2 else self.pclk1_hz

    def describe(self) -> str:
        """一行描述，用于 CLI 与 GUI 摘要。"""
        return (
            f"HSE {self.hse_hz / 1e6:g}MHz → PLL 输入 {self.pll_input_hz / 1e6:g}MHz"
            f" × {self.pll_factor} → SYSCLK {self.sysclk_hz / 1e6:g}MHz"
            f" → HCLK {self.hclk_hz / 1e6:g}MHz"
            f" → PCLK1 {self.pclk1_hz / 1e6:g}MHz"
            f" / PCLK2 {self.pclk2_hz / 1e6:g}MHz"
        )


def compute_clock_tree(hse_hz: int, rcc=None) -> ClockTree:
    """推导时钟树。

    ``rcc`` 给定时按它计算（这是「自动配置 RCC」的核心）；
    为 None 时沿用 ``system_stm32f10x.c`` 的 ``SetSysClockTo72()``
    默认配置（PLL×9、HPRE_DIV1、PPRE2_DIV1、PPRE1_DIV2），
    对应「不生成 RCC 代码、靠启动文件配时钟」的情形。
    """
    # 局部导入：models 与 validate 之间不建立模块级耦合
    from stm32gen.models.rcc import RccPllSource, RccSysclkSource

    if rcc is None:
        pll_factor = PLL_MULTIPLIER
        pll_input = hse_hz
        ahb_div = HCLK_DIVIDER
        apb1_div = PCLK1_DIVIDER
        apb2_div = PCLK2_DIVIDER
        sysclk = hse_hz * pll_factor
    else:
        pll_factor = rcc.pll_multiplier.factor
        if rcc.pll_source is RccPllSource.HSI_Div2:
            # HSI 固定 8MHz，其 2 分频作 PLL 输入
            # （依据 stm32f10x_rcc.c:925 的 HSI_VALUE >> 1）
            pll_input = HSI_HZ // 2
        else:
            pll_input = hse_hz // rcc.pll_source.divider

        ahb_div = rcc.ahb_prescaler.divider
        apb1_div = rcc.apb1_prescaler.divider
        apb2_div = rcc.apb2_prescaler.divider

        if rcc.sysclk_source is RccSysclkSource.PLLCLK:
            sysclk = pll_input * pll_factor
        elif rcc.sysclk_source is RccSysclkSource.HSE:
            sysclk = hse_hz
        else:
            sysclk = HSI_HZ

    hclk = sysclk // ahb_div
    pclk1 = hclk // apb1_div
    pclk2 = hclk // apb2_div

    # 定时器时钟：APB 分频系数 != 1 时，定时器时钟是 PCLK 的 2 倍
    # （STM32F103 时钟树特性）。这极易算错：36MHz 的 PCLK1 对应
    # 72MHz 的 TIM2 时钟，算定时周期时弄错就差一倍。
    tim234 = pclk1 * (2 if apb1_div != 1 else 1)
    tim1 = pclk2 * (2 if apb2_div != 1 else 1)

    # 推荐值与生效值分开保留：校验层要拿用户覆盖值跟**推荐值**比。
    # 若只留一个字段，覆盖后就成了「跟自己比」，偏低的情况永远不报错
    # （初版就是这个 bug：tree.flash_latency 被覆盖成 0，再判断 0 < 0 恒假）。
    latency_recommended = recommended_flash_latency(hclk)
    latency = latency_recommended
    if rcc is not None and rcc.flash_latency_override is not None:
        latency = rcc.flash_latency_override.wait_states

    return ClockTree(
        hse_hz=hse_hz,
        sysclk_hz=sysclk,
        hclk_hz=hclk,
        pclk1_hz=pclk1,
        pclk2_hz=pclk2,
        sysclk_in_spec=sysclk <= SYSCLK_MAX_HZ,
        pclk1_in_spec=pclk1 <= PCLK1_MAX_HZ,
        pll_input_hz=pll_input,
        ahb_divider=ahb_div,
        apb1_divider=apb1_div,
        apb2_divider=apb2_div,
        pll_factor=pll_factor,
        tim1_hz=tim1,
        tim234_hz=tim234,
        flash_latency=latency,
        flash_latency_recommended=latency_recommended,
    )


@dataclass(frozen=True)
class BaudRateResult:
    """一个波特率配置的复算结果。"""

    requested: int
    actual_exact: float
    brr: int
    pclk_hz: int
    over8: bool

    @property
    def actual(self) -> int:
        """实际波特率（取整），便于显示。"""
        return round(self.actual_exact)

    @property
    def error_pct(self) -> float:
        """相对误差（%），带符号：正数表示实际波特率偏高。

        用精确值算，不用取整后的值 —— 否则在低波特率下会引入
        可观的额外误差，把判断带偏。
        """
        if self.requested == 0:
            return 0.0
        return (self.actual_exact - self.requested) / self.requested * 100.0

    @property
    def abs_error_pct(self) -> float:
        """相对误差的绝对值（%）。"""
        return abs(self.error_pct)

    @property
    def mantissa(self) -> int:
        """BRR 的整数部分（DIV_Mantissa，BRR[15:4]）。"""
        return self.brr >> 4

    @property
    def is_representable(self) -> bool:
        """该 BRR 是否是硬件可用的合法值。

        硬件要求 ``USARTDIV >= 1``，即 16 倍过采样下 ``BRR >= 16``
        （整数部分至少为 1）。波特率过高时库会算出 ``BRR < 16``
        （整数部分为 0），此时分频比小于 1，硬件行为未定义。

        **这是库本身不检查的缺口**：``IS_USART_BAUDRATE`` 只要求
        ``BAUDRATE < 0x0044AA21``（约 4.5Mbps），是个固定上限，
        与当前 PCLK 无关。所以 36MHz 的 USART2 请求 4.5Mbps 时，
        库会算出 BRR=8 而不报错 —— 误差看起来是 0%，实际不能用。

        合法波特率的实际上限是 ``PCLK / 16``：
        72MHz → 4.5Mbps，36MHz → 2.25Mbps。
        """
        return self.mantissa >= 1

    @property
    def max_representable_baudrate(self) -> int:
        """本 PCLK 下能表示的最高波特率（``PCLK / 16``）。"""
        return self.pclk_hz // 16

    @property
    def fraction(self) -> int:
        """BRR 的小数部分。

        OVER8=0 时是 BRR[3:0]（16 分之一），OVER8=1 时是 BRR[2:0]（8 分之一）。
        """
        return (self.brr & 0x07) if self.over8 else (self.brr & 0x0F)

    @property
    def brr_hex(self) -> str:
        """BRR 的十六进制表示，便于与寄存器/示波器对照。"""
        return f"0x{self.brr:04X}"

    def describe(self) -> str:
        """一行描述，供 CLI 与 GUI 显示。"""
        return (
            f"请求 {self.requested} → 实际 {self.actual_exact:.1f} bps"
            f"（误差 {self.error_pct:+.3f}%，BRR={self.brr_hex}"
            f" = {self.mantissa}.{self.fraction}）"
        )


def compute_brr(pclk_hz: int, baudrate: int, over8: bool = False) -> int:
    """复现 ``USART_Init()`` 计算 BRR 的过程。

    对照 ``Library\\stm32f10x_usart.c:240-267`` 逐行实现。
    所有除法都是 C 的整数除法（向零取整），Python 的 ``//`` 对正数等价。

    以 PCLK2=72MHz、115200bps、16 倍过采样为例：
    ``integerdivider = 25×72000000/(4×115200) = 3906``，
    整数部分 39、小数部分 6，取整后 BRR = ``0x0271`` = 625。
    """
    if baudrate <= 0:
        raise ValueError("波特率必须为正数")

    if over8:
        # 第 244 行
        integerdivider = (25 * pclk_hz) // (2 * baudrate)
    else:
        # 第 249 行
        integerdivider = (25 * pclk_hz) // (4 * baudrate)

    # 第 251 行：整数部分左移 4 位
    tmpreg = (integerdivider // 100) << 4

    # 第 254 行：小数部分 = 总余数 - 100 × 已取整数部分
    fractionaldivider = integerdivider - (100 * (tmpreg >> 4))

    # 第 257-264 行：小数部分四舍五入后写入低位（OVER8 时只用低 3 位）
    if over8:
        tmpreg |= (((fractionaldivider * 8) + 50) // 100) & 0x07
    else:
        tmpreg |= (((fractionaldivider * 16) + 50) // 100) & 0x0F

    return tmpreg


def actual_baudrate_exact(pclk_hz: int, brr: int, over8: bool = False) -> float:
    """由 BRR 反算实际波特率（精确值）。

    BRR 的编码不是「波特率分频比的整数倍」，而是 USARTDIV 的定点表示：

    - ``OVER8=0``：``BRR[15:4]`` 是整数部分，``BRR[3:0]`` 是 1/16 小数部分，
      所以 ``USARTDIV = BRR / 16``，而 ``波特率 = PCLK / (16 × USARTDIV)``，
      两个 16 正好约掉，得 ``波特率 = PCLK / BRR``。
    - ``OVER8=1``：小数部分是 1/8，``USARTDIV = BRR[15:4] + BRR[2:0]/8``，
      ``波特率 = PCLK / (8 × USARTDIV)``。

    初版实现写成 ``PCLK / (16 × BRR)``，对 72MHz/115200 会算出 7200bps ——
    差了 16 倍。手工验算 ``72000000 / 625 = 115200`` 即可发现。
    """
    if brr == 0:
        return 0.0
    if over8:
        mantissa = brr >> 4
        fraction = brr & 0x07
        divider = 8.0 * mantissa + fraction
    else:
        divider = float(brr)
    if divider == 0:
        return 0.0
    return pclk_hz / divider


def evaluate_baudrate(
    pclk_hz: int, baudrate: int, over8: bool = False
) -> BaudRateResult:
    """完整复算一个波特率配置。"""
    brr = compute_brr(pclk_hz, baudrate, over8)
    return BaudRateResult(
        requested=baudrate,
        actual_exact=actual_baudrate_exact(pclk_hz, brr, over8),
        brr=brr,
        pclk_hz=pclk_hz,
        over8=over8,
    )


def common_baudrates() -> tuple[int, ...]:
    """常用波特率列表，供 GUI 下拉框使用。"""
    return (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
