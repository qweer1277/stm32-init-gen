"""Jinja2 渲染。

模板从 ``stm32gen/templates/`` 加载。环境配置有两个关键选择：

1. ``trim_blocks=True`` + ``lstrip_blocks=True``：让 ``{% for %}`` 这类
   控制语句独占一行时不留下空行。C 代码对空行敏感（影响可读性），
   不这样配会生成大量空白。
2. ``keep_trailing_newline=True``：文件末尾保留一个换行，
   符合 POSIX 文本文件约定，也避免 git 报「文件末尾无换行」。

生成器**不写文件**，只返回字符串，写盘由调用方决定。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from stm32gen.generator.context import TemplateContext, build_context
from stm32gen.generator.layout import DEFAULT_LAYOUT, OutputLayout
from stm32gen.generator.merge import merge_split
from stm32gen.generator.types import GeneratedFile, GenerateResult
from stm32gen.models import Stm32Config

# 模板目录
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class TemplateRenderError(RuntimeError):
    """模板渲染失败（模板缺失或语法错误）。"""


@lru_cache(maxsize=1)
def get_environment() -> Environment:
    """构造并缓存 Jinja2 环境。

    ``StrictUndefined`` 是刻意的选择：模板里引用不存在的变量会直接报错，
    而不是静默渲染成空字符串。生成 C 代码时静默的空值会产出语法错误的
    代码，而且很难定位 —— 宁可在这里炸掉。
    """
    if not TEMPLATES_DIR.is_dir():
        raise TemplateRenderError(f"模板目录不存在：{TEMPLATES_DIR}")

    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR), encoding="utf-8"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # 生成 C 代码，不需要 HTML 转义
    )


def render_template(template_name: str, context: TemplateContext) -> str:
    """渲染单个模板。"""
    environment = get_environment()
    try:
        template = environment.get_template(template_name)
    except TemplateNotFound as exc:
        raise TemplateRenderError(
            f"找不到模板 {template_name}（模板目录：{TEMPLATES_DIR}）"
        ) from exc

    try:
        return template.render(ctx=context)
    except Exception as exc:
        raise TemplateRenderError(f"渲染模板 {template_name} 失败：{exc}") from exc


# 产物清单：模板名 -> (输出文件名, 说明)
#
# **按外设分文件**：每个外设一个 .h/.c 对，加上聚合入口与中断骨架。
# 这样删掉某个外设时，它的模板、上下文、时钟使能一起消失，
# 不会在别的文件里留下孤儿代码。
OUTPUT_SPECS: tuple[tuple[str, str, str], ...] = (
    ("bsp_init.h.j2", "bsp_init.h", "总入口声明，并包含各外设头文件"),
    ("bsp_init.c.j2", "bsp_init.c", "按顺序调用各外设 init 的编排"),
    ("bsp_rcc.h.j2", "bsp_rcc.h", "系统时钟接口与 BSP_xxx_HZ 频率常量"),
    ("bsp_rcc.c.j2", "bsp_rcc.c", "Flash 等待周期、HSE/PLL/总线分频、SYSCLK 切换"),
    ("bsp_afio.h.j2", "bsp_afio.h", "AFIO 接口（引脚重映射与 SWJ 释放）"),
    ("bsp_afio.c.j2", "bsp_afio.c", "AFIO 时钟、全部引脚重映射、SWJ 调试口释放"),
    ("bsp_gpio.h.j2", "bsp_gpio.h", "GPIO 接口与带标签引脚的端口/引脚宏"),
    ("bsp_gpio.c.j2", "bsp_gpio.c", "端口时钟与全部 GPIO_Init（含 USART/EXTI/TIM 引脚）"),
    ("bsp_exti.h.j2", "bsp_exti.h", "EXTI 接口"),
    ("bsp_exti.c.j2", "bsp_exti.c", "外部中断线选择与边沿配置"),
    ("bsp_usart.h.j2", "bsp_usart.h", "USART 接口"),
    ("bsp_usart.c.j2", "bsp_usart.c", "实例时钟、USART_Init、中断使能、USART_Cmd"),
    ("bsp_tim.h.j2", "bsp_tim.h", "TIM 接口"),
    ("bsp_tim.c.j2", "bsp_tim.c", "时基、输出比较、预装载、主输出、中断使能"),
    ("bsp_nvic.h.j2", "bsp_nvic.h", "NVIC 接口"),
    ("bsp_nvic.c.j2", "bsp_nvic.c", "优先级分组与各通道 NVIC_Init"),
    ("bsp_it.h.j2", "bsp_it.h", "中断服务函数声明"),
    ("bsp_it.c.j2", "bsp_it.c", "USART / EXTI / TIM 中断服务函数骨架"),
    ("main.c.j2", "main.c", "最小可编译的 main，调用 bsp_init()"),
)


def generate(
    config: Stm32Config, *, layout: OutputLayout = DEFAULT_LAYOUT
) -> GenerateResult:
    """由配置生成全部代码文件（纯函数，不写盘）。

    这是生成器的对外入口。CLI 与 GUI 都调用它，保证两者产物完全一致。

    ``layout`` 决定产物形态：``SPLIT``（默认）按外设分文件出 19 个，
    ``MERGED`` 合并成 ``bsp.h`` / ``bsp.c`` / ``main.c`` 3 个。

    合并是**渲染后的纯文本变换**（见 ``merge.py``）：split 路径一行都不经过它，
    所以默认布局的产物逐字节不变。
    """
    context = build_context(config)
    result = GenerateResult(symbols_used=set(context.symbols_used))

    generated: list[GeneratedFile] = []
    for template_name, output_name, description in OUTPUT_SPECS:
        content = render_template(template_name, context)
        generated.append(
            GeneratedFile(
                name=output_name,
                content=content,
                description=description,
                template=template_name,
            )
        )

    if layout.is_merged:
        for item in merge_split(generated):
            result.add(item)
        result.notes.append(
            "已合并为 3 个文件：全部声明在 bsp.h，全部实现在 bsp.c；"
            "各段正文与按外设分文件的产物一致，只是省掉了文件级脚手架"
        )
    else:
        for item in generated:
            result.add(item)

    # 记录生成过程中的取舍，便于用户理解为什么某些内容没生成
    if (
        not context.has_gpio
        and not context.has_usart
        and not context.has_exti
        and not context.has_tim
    ):
        result.notes.append("配置里没有任何 GPIO / USART / EXTI / TIM，初始化函数体为空")
    if context.has_usart and not context.has_nvic:
        result.notes.append("USART 未使能任何中断，因此没有生成 NVIC 配置与中断服务函数")
    if context.has_exti and not context.has_nvic:
        result.notes.append("EXTI 全部为事件模式（不产生中断），因此没有生成 NVIC 配置")
    if context.has_tim and not context.has_nvic:
        result.notes.append("TIM 未使能任何中断，因此没有生成 NVIC 配置")
    if context.has_nvic and not context.has_interrupts:
        result.notes.append(
            "NVIC 配了通道但没有任何外设使能中断，中断服务函数未生成"
        )
    if context.has_tim and not any(item.has_pwm for item in context.tim_configs):
        result.notes.append(
            "TIM 没有 PWM 通道（只做定时），因此没有生成输出比较配置"
        )

    return result


def list_templates() -> list[str]:
    """列出可用模板（供排查与文档）。"""
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(path.name for path in TEMPLATES_DIR.glob("*.j2"))
