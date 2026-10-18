"""ADC 通道的默认模拟输入引脚表。

**关于本表可信度的重要说明**（code-standards R9.4 要求引用前确认来源）：

本环境的 ``E:\\stm32初始化\\代码`` 只有标准外设库源码，**没有任何数据手册**；
``stm32f10x_adc.h`` 里只有通道宏 ``ADC_Channel_0`` … ``ADC_Channel_17`` 的
数值，**不包含通道号与引脚的对应关系**。

因此本表的定位被刻意限制为（与 ``tim_pins.py`` 完全一致）：

- **只用于提示，不用于拦截**。校验层拿它对比时只报 WARNING
  （``adc.pin_not_default``），绝不报 ERROR —— 报错会让"用户按数据手册
  选了正确的非默认引脚"这种情况被工具挡下来，那是工具在犯错。
- **生成代码不依赖本表**。真正决定引脚的是用户的
  ``AdcChannelConfig.port/pin``，本表只在"用户没主动选、我们想给个合理
  默认"和"给个提醒"这两个场合出现。
- **GUI 用它做下拉建议值**，用户改了就以用户的为准。

这样即使表里某一项记错，后果也只是一句多余的提示，而不是生成一份
引脚错误的代码。

本表覆盖的范围
--------------

STM32F103 的 ADC1/ADC2 共用全部 16 个外部通道：``ADC12_IN0``~``ADC12_IN15``
分别落在 PA0~PA7 与 PB0~PB1、PC0~PC5。通道是**按 ADC 实例组共享**的，
所以 ADC1 与 ADC2 的同一通道号对应同一个引脚。

通道 16（温度传感器）与 17（Vrefint）是**内部通道**，不接任何引脚，
本表里不给它们任何条目。
"""

from __future__ import annotations

from stm32gen.models.enums import AdcChannel

# 通道号 → (端口字母, 引脚号)。
#
# **请对照你手上那份 STM32F103 数据手册的「引脚定义」表确认后再依赖它。**
DEFAULT_ADC_PINS: dict[AdcChannel, tuple[str, int]] = {
    # PA0~PA7 = ADC12_IN0~IN7
    AdcChannel.CH0: ("A", 0),
    AdcChannel.CH1: ("A", 1),
    AdcChannel.CH2: ("A", 2),
    AdcChannel.CH3: ("A", 3),
    AdcChannel.CH4: ("A", 4),
    AdcChannel.CH5: ("A", 5),
    AdcChannel.CH6: ("A", 6),
    AdcChannel.CH7: ("A", 7),
    # PB0/PB1 = ADC12_IN8/IN9
    AdcChannel.CH8: ("B", 0),
    AdcChannel.CH9: ("B", 1),
    # PC0~PC5 = ADC12_IN10~IN15
    AdcChannel.CH10: ("C", 0),
    AdcChannel.CH11: ("C", 1),
    AdcChannel.CH12: ("C", 2),
    AdcChannel.CH13: ("C", 3),
    AdcChannel.CH14: ("C", 4),
    AdcChannel.CH15: ("C", 5),
    # CH16/CH17 是内部通道，不占引脚，故意不给条目
}


def default_pin(channel: AdcChannel) -> tuple[str, int] | None:
    """该通道的默认模拟输入引脚；内部通道或无记录返回 ``None``。"""
    return DEFAULT_ADC_PINS.get(channel)


def pin_role_hint(channel: AdcChannel) -> str:
    """给 GUI 下拉用的引脚角色标注，如 ``ADC12_IN3``。"""
    if channel.is_internal:
        return f"{channel.label}（内部）"
    return f"ADC12_IN{channel.number}"
