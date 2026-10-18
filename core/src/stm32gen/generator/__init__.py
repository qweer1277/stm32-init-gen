"""生成器层。

纯函数式：``Stm32Config -> GenerateResult``。不依赖 GUI、不写文件
（写文件由调用方决定），保证 CLI 与 GUI 走同一条代码路径。

```
context.py  把配置模型转换成模板上下文（所有条件与预计算都在这里）
render.py   加载模板并渲染，返回 GeneratedFile 列表
layout.py   产物形态（分文件 / 合并）
merge.py    渲染后把 21 个文件合成 bsp.h / bsp.c / main.c
types.py    GeneratedFile / GenerateResult 数据结构
```

设计原则：**模板只做排版，不做判断**。模板里只有 ``{{ 变量 }}`` 与
``{% for %}``，没有任何计算。这样生成逻辑可以用 Python 单测覆盖，
模板保持可读，改样式不会碰逻辑。
"""

from __future__ import annotations

from stm32gen.generator.context import (
    ClockEnable,
    GpioConfig,
    InterruptCheck,
    NamedPin,
    NvicChannel,
    PinRemap,
    RccSetup,
    TemplateContext,
    UsartConfig,
    UsartInterruptHandler,
    build_context,
)
from stm32gen.generator.layout import DEFAULT_LAYOUT, OutputLayout
from stm32gen.generator.merge import MergeError, merge_split
from stm32gen.generator.render import (
    OUTPUT_SPECS,
    TEMPLATES_DIR,
    TemplateRenderError,
    generate,
    get_environment,
    list_templates,
    render_template,
)
from stm32gen.generator.types import GeneratedFile, GenerateResult

__all__ = [
    "DEFAULT_LAYOUT",
    "OUTPUT_SPECS",
    "TEMPLATES_DIR",
    "ClockEnable",
    "GenerateResult",
    "GeneratedFile",
    "GpioConfig",
    "InterruptCheck",
    "MergeError",
    "NamedPin",
    "NvicChannel",
    "OutputLayout",
    "PinRemap",
    "RccSetup",
    "TemplateContext",
    "TemplateRenderError",
    "UsartConfig",
    "UsartInterruptHandler",
    "build_context",
    "generate",
    "get_environment",
    "list_templates",
    "merge_split",
    "render_template",
]
