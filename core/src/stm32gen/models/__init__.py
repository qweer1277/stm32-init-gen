"""Pydantic 配置模型层。

这是整个工具的**唯一配置真相**：GUI 收集表单 → 构造这里的模型；
CLI 读 JSON/YAML → 构造这里的模型；生成器只接受 ``Stm32Config``。

分层约束：本层不得导入 PySide6。

模块划分
--------
- ``chip.py``   —— 芯片规格（型号 → 器件宏、可用端口、可用 USART 实例）
- ``enums.py``  —— 与标准库宏/枚举一一对应的配置枚举
- ``gpio.py``   —— ``GpioPinConfig``
- ``usart.py``  —— ``UsartConfig``
- ``nvic.py``   —— ``NvicConfig`` / ``NvicChannelConfig``
- ``base.py``   —— ``Stm32Config`` 根模型

反编造约束
----------
``enums.py`` 里每个枚举的**值都是标准库符号名**，导入本包时会逐个校验
这些名字真实存在于 ``symbols.json``（``verify_all()``）。因此模型层不可能
出现库里没有的选项 —— 一旦有人手抖写错名字，``import stm32gen.models``
就会直接失败，而不是等到生成代码时才发现。
"""

from __future__ import annotations

from stm32gen.models.afio import AfioConfig
from stm32gen.models.base import (
    CONFIG_VERSION,
    Stm32Config,
    available_chips,
    make_demo_config,
)
from stm32gen.models.chip import CHIPS, ChipId, ChipSpec, get_chip
from stm32gen.models.enums import (
    EXTI_EXTERNAL_LINE_MAX,
    EXTI_EXTERNAL_LINE_MIN,
    AfioSwjRemap,
    ExtiMode,
    ExtiTrigger,
    GpioInitLevel,
    GpioMode,
    GpioPort,
    GpioSpeed,
    NvicPriorityGroup,
    TimChannel,
    TimClockDivision,
    TimClockSource,
    TimCounterMode,
    TimExtTriggerPolarity,
    TimExtTriggerPrescaler,
    TimInstance,
    TimInterrupt,
    TimOcMode,
    TimOcPolarity,
    TimOcPreload,
    TimOutputState,
    TimRemap,
    UsartDirection,
    UsartFlowControl,
    UsartInstance,
    UsartInterrupt,
    UsartParity,
    UsartRemap,
    UsartStopBits,
    UsartWordLength,
    verify_all,
)
from stm32gen.models.exti import ExtiLineConfig
from stm32gen.models.gpio import GpioPinConfig
from stm32gen.models.nvic import NvicChannelConfig, NvicConfig
from stm32gen.models.rcc import (
    RCC_ENUMS,
    AhbPrescaler,
    ApbPrescaler,
    FlashLatency,
    FlashPrefetch,
    RccConfig,
    RccHseState,
    RccPllMultiplier,
    RccPllSource,
    RccSysclkSource,
)
from stm32gen.models.tim import (
    TIM_EXT_FILTER_MAX,
    TIM_REGISTER_MAX,
    TimChannelConfig,
    TimConfig,
    TimTimingSolution,
    solve_period,
    solve_timing,
)
from stm32gen.models.usart import UsartConfig

# 导入即校验：枚举里出现库里不存在的符号会让本行抛错。
# 这是"禁止编造"约束在模型层的落地，代价是每次导入做一次符号查询
# （符号索引有 lru_cache，实际只读一次 JSON）。
#
# RCC/Flash 的枚举定义在 models/rcc.py（它们需要 enums.py 的基类，
# 不能反过来导入），所以通过 extra 传进来一起校验。
verify_all(extra=RCC_ENUMS)

__all__ = [
    "CHIPS",
    "CONFIG_VERSION",
    "EXTI_EXTERNAL_LINE_MAX",
    "EXTI_EXTERNAL_LINE_MIN",
    "RCC_ENUMS",
    "TIM_EXT_FILTER_MAX",
    "TIM_REGISTER_MAX",
    "TimTimingSolution",
    "solve_period",
    "solve_timing",
    "AfioConfig",
    "AfioSwjRemap",
    "AhbPrescaler",
    "ApbPrescaler",
    "ChipId",
    "ChipSpec",
    "ExtiLineConfig",
    "ExtiMode",
    "ExtiTrigger",
    "FlashLatency",
    "FlashPrefetch",
    "GpioInitLevel",
    "GpioMode",
    "GpioPinConfig",
    "GpioPort",
    "GpioSpeed",
    "NvicChannelConfig",
    "NvicConfig",
    "NvicPriorityGroup",
    "RccConfig",
    "RccHseState",
    "RccPllMultiplier",
    "RccPllSource",
    "RccSysclkSource",
    "Stm32Config",
    "TimChannel",
    "TimChannelConfig",
    "TimClockDivision",
    "TimConfig",
    "TimClockSource",
    "TimCounterMode",
    "TimExtTriggerPolarity",
    "TimExtTriggerPrescaler",
    "TimInstance",
    "TimInterrupt",
    "TimOcMode",
    "TimOcPolarity",
    "TimOcPreload",
    "TimOutputState",
    "TimRemap",
    "UsartConfig",
    "UsartDirection",
    "UsartFlowControl",
    "UsartInstance",
    "UsartInterrupt",
    "UsartParity",
    "UsartRemap",
    "UsartStopBits",
    "UsartWordLength",
    "available_chips",
    "get_chip",
    "make_demo_config",
    "verify_all",
]
