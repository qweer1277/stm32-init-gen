"""波特率与时钟树计算的测试。

**这组测试的期望值全部来自对标准库源码的实地核对**，
不是从数据手册或公式记忆里抄的：

- BRR 算法依据 ``Library\\stm32f10x_usart.c:228-267``
- 时钟树依据 ``Start\\system_stm32f10x.c:1091-1097`` 与 ``:1125``

其中 ``0x0271`` 与 ``0x1D4C`` 两个 BRR 值是 ST 官方例程里的已知结果，
用来交叉验证实现是否与库一致。
"""

from __future__ import annotations

import pytest

from stm32gen.models.enums import UsartInstance
from stm32gen.validate.baudrate import (
    BAUDRATE_MAX,
    HCLK_DIVIDER,
    PCLK1_DIVIDER,
    PCLK2_DIVIDER,
    PLL_MULTIPLIER,
    actual_baudrate_exact,
    common_baudrates,
    compute_brr,
    compute_clock_tree,
    evaluate_baudrate,
)

# --------------------------------------------------------------------------
# 时钟树
# --------------------------------------------------------------------------


def test_clock_tree_default_8mhz() -> None:
    """8MHz HSE × 9 = 72MHz，PCLK2 = 72MHz，PCLK1 = 36MHz。

    依据 system_stm32f10x.c：
    - :1125 PLLMULL9 → SYSCLK = HSE × 9
    - :1091 HPRE_DIV1 → HCLK = SYSCLK
    - :1094 PPRE2_DIV1 → PCLK2 = HCLK
    - :1097 PPRE1_DIV2 → PCLK1 = HCLK / 2
    """
    tree = compute_clock_tree(8_000_000)
    assert tree.sysclk_hz == 72_000_000
    assert tree.hclk_hz == 72_000_000
    assert tree.pclk2_hz == 72_000_000
    assert tree.pclk1_hz == 36_000_000
    assert tree.sysclk_in_spec
    assert tree.pclk1_in_spec
    assert tree.pll_ratio_ok


def test_clock_tree_12mhz_overclocks() -> None:
    """12MHz × 9 = 108MHz，超过 72MHz 上限，必须被判为超频。

    这是真实的坑：库的 SetSysClockTo72() 硬编码 PLLMULL9，
    换 12MHz 晶振而不改倍频系数会直接超频。
    """
    tree = compute_clock_tree(12_000_000)
    assert tree.sysclk_hz == 108_000_000
    assert not tree.sysclk_in_spec
    assert not tree.pll_ratio_ok
    # PCLK1 = 54MHz 也超了 APB1 的 36MHz
    assert tree.pclk1_hz == 54_000_000
    assert not tree.pclk1_in_spec


def test_clock_tree_dividers_match_source() -> None:
    """分频系数常量必须与源码一致。"""
    assert PLL_MULTIPLIER == 9
    assert HCLK_DIVIDER == 1
    assert PCLK2_DIVIDER == 1
    assert PCLK1_DIVIDER == 2


def test_pclk_of_instance() -> None:
    """USART1 挂 APB2，USART2/3 挂 APB1。"""
    tree = compute_clock_tree(8_000_000)
    assert tree.pclk_of(UsartInstance.USART1) == 72_000_000
    assert tree.pclk_of(UsartInstance.USART2) == 36_000_000
    assert tree.pclk_of(UsartInstance.USART3) == 36_000_000


# --------------------------------------------------------------------------
# BRR 计算：与库逐行对照
# --------------------------------------------------------------------------


def test_compute_brr_known_values() -> None:
    """与 ST 官方例程的已知 BRR 值交叉验证。

    72MHz / 115200 → BRR = 0x0271 = 625
    72MHz / 9600   → BRR = 0x1D4C = 7500

    这两个值在 ST 的标准库例程与无数教程里出现过，
    对不上就说明复算实现与库不一致。
    """
    assert compute_brr(72_000_000, 115200) == 0x0271
    assert compute_brr(72_000_000, 115200) == 625
    assert compute_brr(72_000_000, 9600) == 0x1D4C
    assert compute_brr(72_000_000, 9600) == 7500


def test_compute_brr_uses_integer_division() -> None:
    """库用的是整数运算，复算必须一致。

    25 × 72000000 / (4 × 115200) = 3906.25，C 的整数除法得 3906。
    若用浮点会得到 3906.25，后续取整结果可能不同。
    """
    # 3906 // 100 = 39（整数部分）
    # 3906 - 100×39 = 6（小数部分）
    # (6×16 + 50) // 100 = 1
    # BRR = (39 << 4) | 1 = 625
    assert compute_brr(72_000_000, 115200) == (39 << 4) | 1


def test_compute_brr_over8_mode() -> None:
    """OVER8=1 时 BRR 约为 16 倍采样时的两倍。

    容易想反：OVER8=1 的波特率是 ``PCLK/(8×USARTDIV)``，
    所以同样的波特率下 USARTDIV 是 16 倍采样时的两倍
    （115200 在 72MHz 下：39.0625 vs 78.125），BRR 也就更大。

    实测：OVER8=0 → 0x271 = 625；OVER8=1 → 0x4E1 = 1249。
    """
    brr8 = compute_brr(72_000_000, 115200, over8=True)
    brr16 = compute_brr(72_000_000, 115200, over8=False)
    assert brr8 > brr16
    assert brr8 == 0x04E1  # 1249
    assert brr16 == 0x0271  # 625
    # OVER8 时小数部分只用低 3 位，bit3 必须是 0
    assert brr8 & 0x08 == 0


def test_actual_baudrate_over8_roundtrip() -> None:
    """OVER8 模式下 BRR 与反算公式必须自洽。

    这是回归测试：OVER8 的 BRR 编码是 ``USARTDIV × 16``
    但小数部分只有 1/8 精度，两处很容易写得不一致。
    """
    for baud in (9600, 115200, 921600):
        result = evaluate_baudrate(72_000_000, baud, over8=True)
        assert result.abs_error_pct < 1.0, f"OVER8 {baud} 误差过大：{result.describe()}"


def test_compute_brr_rejects_zero_baudrate() -> None:
    with pytest.raises(ValueError):
        compute_brr(72_000_000, 0)


# --------------------------------------------------------------------------
# 实际波特率反算
# --------------------------------------------------------------------------


def test_actual_baudrate_formula() -> None:
    """OVER8=0 时 ``波特率 = PCLK / BRR``。

    这不是笔误：BRR 的编码是 ``USARTDIV × 16``，而
    ``波特率 = PCLK / (16 × USARTDIV)``，两个 16 约掉。

    初版实现写成 ``PCLK / (16 × BRR)``，对 72MHz/115200 会算出
    7200bps —— 差了 16 倍。手工验算 ``72000000 / 625 = 115200`` 即可发现。
    """
    assert actual_baudrate_exact(72_000_000, 625) == 115200.0
    assert actual_baudrate_exact(72_000_000, 7500) == 9600.0
    # 反向确认不是 PCLK/(16×BRR)
    assert actual_baudrate_exact(72_000_000, 625) != 7200.0


def test_actual_baudrate_over8() -> None:
    """OVER8=1 时 ``波特率 = PCLK / (8 × USARTDIV)``，USARTDIV 带 1/8 小数。"""
    # mantissa=9, fraction=0 → divider = 8×9 + 0 = 72 → 72MHz/72 = 1Mbps
    brr = (9 << 4) | 0
    assert actual_baudrate_exact(72_000_000, brr, over8=True) == 1_000_000.0


def test_actual_baudrate_zero_brr() -> None:
    assert actual_baudrate_exact(72_000_000, 0) == 0.0


# --------------------------------------------------------------------------
# 完整复算
# --------------------------------------------------------------------------


def test_evaluate_baudrate_exact_division() -> None:
    """能整除的波特率误差应为 0。

    72MHz 与 115200 的关系：72000000 / 115200 = 625，正好整除。
    """
    for baud in (9600, 19200, 38400, 57600, 115200):
        result = evaluate_baudrate(72_000_000, baud)
        assert result.abs_error_pct < 1e-9, f"{baud} 应能整除"


def test_evaluate_baudrate_reports_small_error() -> None:
    """230400 在 72MHz 下不能整除，会有小误差但可接受。

    72000000 / 230400 = 312.5，BRR 取 312 或 313 都不精确。
    """
    result = evaluate_baudrate(72_000_000, 230400)
    assert result.brr > 0
    assert 0 < result.abs_error_pct < 1.0


def test_evaluate_baudrate_brr_fields() -> None:
    """BRR 的整数/小数部分拆分要正确。"""
    result = evaluate_baudrate(72_000_000, 115200)
    assert result.brr == 0x0271
    assert result.mantissa == 0x27  # 39
    assert result.fraction == 0x01  # 1
    assert result.brr_hex == "0x0271"


def test_baudrate_result_describe() -> None:
    text = evaluate_baudrate(72_000_000, 115200).describe()
    assert "115200" in text
    assert "0x0271" in text


def test_evaluate_baudrate_apb1_clock() -> None:
    """挂在 APB1 的 USART2/3 用 36MHz 算，误差比 72MHz 大。"""
    r72 = evaluate_baudrate(72_000_000, 115200)
    r36 = evaluate_baudrate(36_000_000, 115200)
    assert r72.abs_error_pct < r36.abs_error_pct
    assert r36.brr == 0x0138  # 312


def test_baudrate_max_matches_library() -> None:
    """波特率上限来自 IS_USART_BAUDRATE 的 0x0044AA21。"""
    assert BAUDRATE_MAX == 0x0044AA21 - 1
    assert BAUDRATE_MAX == 4_500_000


def test_common_baudrates_sorted() -> None:
    values = common_baudrates()
    assert values == tuple(sorted(values))
    assert 115200 in values


# --------------------------------------------------------------------------
# 可表示性（USARTDIV >= 1）
# --------------------------------------------------------------------------


def test_is_representable_requires_mantissa_at_least_one() -> None:
    """硬件要求 USARTDIV >= 1，即 BRR 整数部分至少为 1。

    这是库**不检查**的约束：``IS_USART_BAUDRATE`` 只要求
    ``BAUDRATE < 0x0044AA21``（约 4.5Mbps），是固定上限，与 PCLK 无关。
    """
    # 36MHz / 4.5Mbps → BRR=8，整数部分 0，不可用
    bad = evaluate_baudrate(36_000_000, 4_500_000)
    assert bad.mantissa == 0
    assert not bad.is_representable
    assert bad.max_representable_baudrate == 36_000_000 // 16  # 2250000

    # 72MHz / 4.5Mbps → BRR=16，整数部分 1，刚好可用
    good = evaluate_baudrate(72_000_000, 4_500_000)
    assert good.mantissa >= 1
    assert good.is_representable


def test_max_representable_baudrate_is_pclk_over_16() -> None:
    """上限 = PCLK / 16。"""
    assert evaluate_baudrate(72_000_000, 115200).max_representable_baudrate == 4_500_000
    assert evaluate_baudrate(36_000_000, 115200).max_representable_baudrate == 2_250_000


def test_common_baudrates_always_representable() -> None:
    """常用波特率在两种 PCLK 下都必须可用。"""
    for pclk in (36_000_000, 72_000_000):
        for baud in common_baudrates():
            result = evaluate_baudrate(pclk, baud)
            assert result.is_representable, f"PCLK={pclk} baud={baud} 不可表示"


@pytest.mark.parametrize("pclk", [36_000_000, 72_000_000])
@pytest.mark.parametrize("baud", [9600, 19200, 115200])
def test_baudrate_roundtrip_stability(pclk: int, baud: int) -> None:
    """复算出的 BRR 反算回去，误差应在合理范围。

    这是自洽性检查：如果 BRR 计算与反算用的模型不一致，
    这里会出现巨大误差。
    """
    result = evaluate_baudrate(pclk, baud)
    assert result.abs_error_pct < 5.0, f"PCLK={pclk} baud={baud} 误差过大"
