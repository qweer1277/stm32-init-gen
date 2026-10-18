"""TIM 通道的默认复用引脚表。

**关于本表可信度的重要说明**（code-standards R9.4 要求引用前确认来源）：

本环境的 ``E:\\stm32初始化\\代码`` 只有标准外设库源码，**没有任何数据手册**；
``stm32f10x_gpio.h`` 里只给了重映射宏的数值与中文名（如
``GPIO_PartialRemap_TIM3 ((uint32_t)0x001A0800) /* TIM3 部分重映射 */``），
**不包含引脚对应关系**。本机也无法联网核对。

因此本表的定位被刻意限制为：

- **只用于提示，不用于拦截**。校验层拿它对比时只报 WARNING
  （``tim.pin_not_default``），绝不报 ERROR —— 报错会让"用户按数据手册
  选了正确的非默认引脚"这种情况被工具挡下来，那是工具在犯错。
- **只覆盖边沿对齐的常见默认复用功能**，不覆盖重映射后的引脚。
  选了重映射时不查本表（重映射后的引脚同样需要数据手册确认）。
- **GUI 用它做下拉建议值**，用户改了就以用户的为准。

换句话说：**生成代码不依赖本表**。真正决定引脚的是用户的
``TimChannelConfig.port/pin``，本表只在"用户没主动选、我们想给个合理默认"
和"给个提醒"这两个场合出现。这样即使表里某一项记错，后果也只是一句
多余的提示，而不是生成一份引脚错误的代码。
"""

from __future__ import annotations

from stm32gen.models.enums import TimChannel, TimInstance

# 默认（不复用重映射）时各通道的复用引脚：实例 → 通道 → (端口字母, 引脚号)
#
# 这些是 STM32F103 各定时器通道的默认复用功能引脚。**请对照你手上那份
# STM32F103 数据手册的「引脚定义」表确认后再依赖它。**
DEFAULT_TIM_PINS: dict[TimInstance, dict[TimChannel, tuple[str, int]]] = {
    # TIM1（高级定时器）：CH1~CH4 = PA8/PA9/PA10/PA11
    # 另有互补输出 CH1N~CH3N = PB13/PB14/PB15
    TimInstance.TIM1: {
        TimChannel.CH1: ("A", 8),
        TimChannel.CH2: ("A", 9),
        TimChannel.CH3: ("A", 10),
        TimChannel.CH4: ("A", 11),
    },
    # TIM2：CH1~CH4 = PA0/PA1/PA2/PA3
    TimInstance.TIM2: {
        TimChannel.CH1: ("A", 0),
        TimChannel.CH2: ("A", 1),
        TimChannel.CH3: ("A", 2),
        TimChannel.CH4: ("A", 3),
    },
    # TIM3：CH1/CH2 = PA6/PA7，CH3/CH4 = PB0/PB1
    TimInstance.TIM3: {
        TimChannel.CH1: ("A", 6),
        TimChannel.CH2: ("A", 7),
        TimChannel.CH3: ("B", 0),
        TimChannel.CH4: ("B", 1),
    },
    # TIM4：CH1~CH4 = PB6/PB7/PB8/PB9
    TimInstance.TIM4: {
        TimChannel.CH1: ("B", 6),
        TimChannel.CH2: ("B", 7),
        TimChannel.CH3: ("B", 8),
        TimChannel.CH4: ("B", 9),
    },
}

# TIM1 的互补输出引脚（CH1N/CH2N/CH3N）。本工具暂不生成互补输出配置，
# 但保留这张表供 GUI 在用户选了 TIM1 通道时给出"注意相邻引脚是互补输出"的提示。
TIM1_COMPLEMENTARY_PINS: dict[str, tuple[str, int]] = {
    "CH1N": ("B", 13),
    "CH2N": ("B", 14),
    "CH3N": ("B", 15),
}


def default_pin(instance: TimInstance, channel: TimChannel) -> tuple[str, int] | None:
    """该实例该通道的默认复用引脚；无记录返回 ``None``。"""
    return DEFAULT_TIM_PINS.get(instance, {}).get(channel)


def pin_role_hint(instance: TimInstance, channel: TimChannel) -> str:
    """给 GUI 下拉用的引脚角色标注，如 ``TIM2_CH1``。"""
    return f"{instance.value}_{channel.name}"
