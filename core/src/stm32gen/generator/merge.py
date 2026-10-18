"""把按外设拆分的产物合并成 bsp.h / bsp.c / main.c。

为什么用「渲染后合并」而不是「模板里加 if」
------------------------------------------

两者都能出结果，但代价不同：

* 模板里加 ``{% if not ctx.merged %}`` 需要给 18 个模板各包 2~3 段脚手架，
  开标签和闭标签必须成对、区间数必须和预期一致。这是**结构容易失衡**的改动 ——
  实际尝试时正是这一步把模板改坏过（孤立 guard、区间数不配平）。
* 渲染后合并只依赖 ``split`` 产物的**固定形态**：9 个 ``.h`` 各有且只有
  7 个条件编译行（``#ifndef`` / ``#ifdef`` ×2 / ``extern "C" {`` / ``#endif`` ×3），
  9 个 ``.c`` 各有 0 个。这套形态由模板的常量前缀保证，与具体配置无关。

因此这里选择后者：``split`` 的输出**一行都不经过本模块**，它的逐字节稳定性
由结构保证，不需要靠「改完再比对」来验证。合并只是一层纯函数据变换，
输入是 19 个文件的文本，输出是 3 个文件的文本。

合并丢掉的东西
--------------

每个文件级的：头注释、include guard、``extern "C"`` 包裹、
``#include "stm32f10x.h"``、``#include "bsp_xxx.h"``、版权尾注。
正文一行不动 —— 特别是**不重写正文注释**。各段注释里「见 bsp_init.c」
这类跨文件指引在合并后指向的是同一文件里的对应段，为此在文件头声明了
这一点，而不是去替换正文里的短语：替换要靠字符串匹配，模板一改就静默失效。
"""

from __future__ import annotations

from stm32gen.generator.types import GeneratedFile

# 合并顺序。必须与 bsp_init() 的调用序列一致：
# 时钟 → 重映射 → GPIO → EXTI → USART → TIM → NVIC → 中断服务函数。
# bsp_init.c 排在最前，因为它是编排入口，读代码时先看到「按什么顺序做什么」。
SOURCE_ORDER: tuple[str, ...] = (
    "bsp_init.c",
    "bsp_rcc.c",
    "bsp_afio.c",
    "bsp_gpio.c",
    "bsp_exti.c",
    "bsp_usart.c",
    "bsp_tim.c",
    "bsp_nvic.c",
    "bsp_it.c",
)

HEADER_ORDER: tuple[str, ...] = (
    "bsp_init.h",
    "bsp_rcc.h",
    "bsp_afio.h",
    "bsp_gpio.h",
    "bsp_exti.h",
    "bsp_usart.h",
    "bsp_tim.h",
    "bsp_nvic.h",
    "bsp_it.h",
)

MERGED_HEADER = "bsp.h"
MERGED_SOURCE = "bsp.c"

COPYRIGHT = (
    "/******************* (C) COPYRIGHT 2011 STMicroelectronics "
    "*****END OF FILE****/"
)


# ST 模板在头文件里用这四行横幅划分「类型 / 常量 / 宏 / 函数」区段。
# 单文件里它们帮读者定位；9 个头文件合并后变成 36 行重复的同类横幅，
# 读者看到的全是分隔线而不是声明，所以在合并时整行丢掉。
_SECTION_BANNERS = frozenset(
    {
        "/* Exported types ------------------------------------------------------------*/",
        "/* Exported constants --------------------------------------------------------*/",
        "/* Exported macro ------------------------------------------------------------*/",
        "/* Exported functions ------------------------------------------------------- */",
    }
)


class MergeError(RuntimeError):
    """split 产物的形态与预期不符（模板被改动时在这里炸掉，而不是产出坏文件）。"""


def _rstrip_blank(lines: list[str]) -> list[str]:
    """去掉尾部空行，避免拼接时出现连续多行空白。"""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _body_of_header(name: str, content: str) -> list[str]:
    """取出一个 ``.h`` 的正文：去掉头注释、guard、extern "C"、include 段。

    同时丢掉 ST 模板里那四行 ``/* Exported types/constants/macro/functions */``
    分隔栏。单文件里它们是给读者划段用的；9 个文件合并后会出现 36 行重复
    横幅，反而盖过了真正的内容。
    """
    lines = content.splitlines()

    marks = [
        index
        for index, line in enumerate(lines)
        if line.strip().startswith(
            ("#ifndef __BSP_", "#ifdef __cplusplus", 'extern "C"', "#endif")
        )
    ]
    # 每个 .h 恰好这 7 行：guard 开、extern 开、extern "#endif"、
    # extern 收尾的 #ifdef / #endif、guard 收尾的 #endif。
    if len(marks) != 7:
        raise MergeError(
            f"{name} 的条件编译行数为 {len(marks)}，预期 7 —— "
            f"模板脚手架形态变了，合并规则需要同步更新"
        )

    _, _, _, extern_open_end, extern_close_start, _, _ = marks

    # 正文起点：extern "C" 开块之后，跳过空行 / /* Includes */ / #include
    start = extern_open_end + 1
    while start < extern_close_start:
        text = lines[start].strip()
        if text and not text.startswith(("#include", "/* Includes")):
            break
        start += 1

    body = [
        line
        for line in lines[start:extern_close_start]
        if line.strip() not in _SECTION_BANNERS
    ]
    return _rstrip_blank(body)


def _body_of_source(name: str, content: str) -> list[str]:
    """取出一个 ``.c`` 的正文：去掉头注释与自身的 ``#include``。"""
    lines = content.splitlines()

    # 头注释终点：第一个以 (*) 收尾的注释行。模板统一写成 "  */"。
    try:
        end_of_banner = next(
            index for index, line in enumerate(lines) if line.strip() == "*/"
        )
    except StopIteration as exc:
        raise MergeError(f"{name} 找不到头注释结束行 `*/`") from exc

    start = end_of_banner + 1
    while start < len(lines):
        text = lines[start].strip()
        if text and not text.startswith("#include"):
            break
        start += 1

    return _rstrip_blank(lines[start:])


def _collapse_blank(lines: list[str]) -> list[str]:
    """把连续空行压成一个。

    丢横幅会留下它前后的空行：``/* Exported functions */`` 两侧各有一个空行，
    横幅一走就变成两个连续空行，段内于是出现「三换行」。
    """
    out: list[str] = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return out


def _join_sections(sections: list[tuple[str, list[str]]]) -> list[str]:
    """把各段正文拼成一个整体，段间恰好留一个空行。

    段首段尾的空行都要去掉，否则去掉 ``/* Exported functions */`` 横幅后
    原本夹在横幅和内容之间的空行会叠成两三个空行。
    """
    out: list[str] = []
    for _, body in sections:
        start = 0
        while start < len(body) and not body[start].strip():
            start += 1
        trimmed = _collapse_blank(_rstrip_blank(body[start:]))
        if not trimmed:
            continue
        if out:
            out.append("")
        out.extend(trimmed)
    return out


def _banner(title: str, note: str, extra: tuple[str, ...] = ()) -> list[str]:
    """合并文件的头注释。"""
    lines = [
        "/**",
        "  ******************************************************************************",
        f"  * @file    {title}",
        f"  * @brief   {note}",
        "  *          本文件由 stm32-init-gen 自动生成，请勿手工修改 —— 重新生成会覆盖。",
        "  *",
    ]
    lines.extend(f"  * {text}" for text in extra)
    lines.extend(
        [
            "  ******************************************************************************",
            "  */",
        ]
    )
    return lines


def merge_split(files: list[GeneratedFile]) -> list[GeneratedFile]:
    """把 split 布局的 19 个产物合并成 3 个。

    输入必须是 ``generate()`` 在 split 布局下的原始产物；本函数不写盘。
    """
    by_name = {item.name: item for item in files}

    missing = [
        name
        for name in (*HEADER_ORDER, *SOURCE_ORDER, "main.c")
        if name not in by_name
    ]
    if missing:
        raise MergeError(f"合并所需的文件缺失：{'、'.join(missing)}")

    header_bodies = [(name, _body_of_header(name, by_name[name].content)) for name in HEADER_ORDER]
    source_bodies = [(name, _body_of_source(name, by_name[name].content)) for name in SOURCE_ORDER]

    header_lines = [
        *_banner(
            MERGED_HEADER,
            "合并后的外设声明（由按外设拆分的各头文件合并而成）。",
            extra=(
                "本文件把 bsp_init / bsp_rcc / bsp_afio / bsp_gpio / bsp_exti /",
                "bsp_usart / bsp_tim / bsp_nvic / bsp_it 的声明合在一起。",
                "各段注释里提到的 bsp_xxx.c / bsp_xxx.h 指的是同一套模板的",
                "分文件产物；在本布局下，对应的就是 bsp.c / 本文件里的同名声明的段。",
            ),
        ),
        "#ifndef __BSP_H",
        "#define __BSP_H",
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
        "/* Includes ------------------------------------------------------------------*/",
        '#include "stm32f10x.h"',
        "",
        *_join_sections(header_bodies),
        "",
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
        "#endif /* __BSP_H */",
        "",
        COPYRIGHT,
    ]

    source_lines = [
        *_banner(
            MERGED_SOURCE,
            "合并后的外设初始化实现（由按外设拆分的各源文件合并而成）。",
            extra=(
                "段顺序与 bsp_init() 的调用序列一致：",
                "时钟 → 重映射 → GPIO → EXTI → USART → TIM → NVIC → 中断服务函数。",
            ),
        ),
        f'#include "{MERGED_HEADER}"',
        "",
        *_join_sections(source_bodies),
        "",
        COPYRIGHT,
    ]

    # main.c：头文件从两个变成一个，正文不动。
    main_text = by_name["main.c"].content
    old_includes = '#include "bsp_init.h"\n#include "bsp_it.h"'
    if old_includes not in main_text:
        raise MergeError(
            "main.c 里没有找到预期的 include 组合（`bsp_init.h` + `bsp_it.h`），"
            "模板改动后合并规则需要同步更新"
        )
    main_body = main_text.replace(old_includes, f'#include "{MERGED_HEADER}"')

    return [
        GeneratedFile(
            name=MERGED_HEADER,
            content="\n".join(header_lines) + "\n",
            description="合并后的全部外设声明与带标签引脚宏",
            template="(merged)",
        ),
        GeneratedFile(
            name=MERGED_SOURCE,
            content="\n".join(source_lines) + "\n",
            description="合并后的全部外设初始化实现与中断服务函数",
            template="(merged)",
        ),
        GeneratedFile(
            name="main.c",
            content=main_body,
            description="最小可编译的 main，调用 bsp_init()",
            template=by_name["main.c"].template,
        ),
    ]
