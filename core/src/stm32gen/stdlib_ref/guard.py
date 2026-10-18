"""反编造护栏：校验 C 代码里用到的 STM32 符号都真实存在于标准库。

这是「禁止编造函数/宏/枚举」这条硬性约束的**执行者**。
生成器产出 C 代码后，本模块扫描其中所有标准库前缀的标识符，
逐一比对符号索引；任何一个查不到就报错。

原先这段逻辑只存在于 ``tools/check_no_invented.py``。搬进包内的原因：
``tools/`` 不随包发布，而 CLI 的 ``generate --check`` 与 GUI 的
「生成后自检」都需要调用它。能力放包内，``tools/`` 只留薄包装。

判定规则
--------
1. 只检查**标准库前缀**的标识符（``GPIO_`` / ``USART_`` / ``NVIC_`` /
   ``RCC_`` / ``IS_`` / …），其他名字（用户自己的函数、局部变量）不管。
2. 标识符必须存在于符号索引的 macros / enums / functions / types 任一桶。
3. 显式豁免一组 C 语言关键字与常见类型（``uint8_t``、``void``、``NULL``…），
   它们不属于标准外设库。
4. **排除成员访问**：``cfg.GPIO_Pin`` 里的 ``GPIO_Pin`` 是结构体成员名，
   不是库符号。结构体成员名与库宏前缀相同，不排除会大量误报。
5. 支持行内豁免注释：``/* noqa: invented-symbol */``，
   用于极少数确实需要引用库外符号的场合，但必须显式写下。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from stm32gen.stdlib_ref.index import SymbolIndex, default_index

# 需要校验的标准库符号前缀
#
# ``FLASH_`` 前缀是本次加 RCC/Flash 初始化代码时补上的：在此之前生成代码
# 从不碰 Flash 库，护栏不查它也没人会发现。现在 bsp_rcc.c 会调用
# ``FLASH_SetLatency`` / ``FLASH_PrefetchBufferCmd``，前缀漏掉就等于这两条
# 调用不受反编造护栏保护。
LIB_PREFIXES = (
    "GPIO_",
    "USART_",
    "UART_",
    "NVIC_",
    "RCC_",
    "FLASH_",
    "AFIO_",
    "EXTI_",
    "IS_",
    "SysTick_",
    "SCB_",
)

# 标准库里的外设实例宏（GPIOA、USART1…），单独列出
PERIPHERAL_INSTANCES = {
    "GPIOA", "GPIOB", "GPIOC", "GPIOD", "GPIOE", "GPIOF", "GPIOG",
    "USART1", "USART2", "USART3", "UART4", "UART5",
    "AFIO", "EXTI", "RCC", "NVIC", "SCB",
}

# C 关键字 / 标准库类型 / 常用宏：不属于标准外设库，直接放行
EXEMPT = {
    # C 关键字
    "void", "int", "char", "long", "short", "float", "double", "unsigned",
    "signed", "const", "volatile", "static", "extern", "inline", "return",
    "if", "else", "while", "for", "do", "switch", "case", "break", "continue",
    "default", "sizeof", "struct", "union", "enum", "typedef", "register",
    "goto", "auto",
    # CMSIS / 标准整数类型
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "u8", "u16", "u32", "s8", "s16", "s32", "vu32", "vu16", "vu8",
    "__IO", "__I", "__O", "__in", "__out",
    "NULL", "true", "false", "bool",
    # 标准库宏
    "assert_param",
    # 常见 C 库函数
    "printf", "sprintf", "snprintf", "memcpy", "memset", "strlen", "strcmp",
    "strcpy", "strncpy", "main",
}

# 标准库约定俗成的**结构体变量名**（不是库符号，是用户变量的命名惯例）。
#
# ST 官方例程与绝大多数中文资料都用这几个名字：
#
#     GPIO_InitTypeDef GPIO_InitStructure;
#     GPIO_InitStructure.GPIO_Pin = GPIO_Pin_13;
#     GPIO_Init(GPIOC, &GPIO_InitStructure);
#
# 它们以 ``GPIO_`` / ``USART_`` / ``NVIC_`` 前缀开头，会被 ``is_lib_symbol``
# 选中，而符号表里当然查不到（库不可能定义一个叫这个名字的宏）——
# 不豁免就会对**完全正确的 ST 惯例写法**误报「编造符号」。
#
# 用显式白名单而不是「``*_InitStructure`` 结尾都放行」的正则：后者等于给
# 任何 ``GPIO_WhateverInitStructure`` 开后门，护栏会被绕过。白名单只有
# 这三个名字，新增要走代码审查。
STRUCT_VAR_NAMES = frozenset(
    {
        "GPIO_InitStructure",
        "USART_InitStructure",
        "NVIC_InitStructure",
        "EXTI_InitStructure",
        "TIM_TimeBaseStructure",
        "TIM_OCInitStructure",
    }
)

# 行内豁免标记
NOQA_RE = re.compile(r"/\*\s*noqa:\s*invented-symbol\s*\*/|//\s*noqa:\s*invented-symbol")

# 标识符
IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")

# 成员访问：``cfg.GPIO_Pin`` / ``p->USART_BaudRate``
_MEMBER_ACCESS_RE = re.compile(r"(?:\.|->)\s*([A-Za-z_]\w*)")


@dataclass(frozen=True)
class InventedSymbol:
    """一处编造符号。"""

    label: str
    line: int
    name: str

    def format(self) -> str:
        return f"{self.label}:{self.line}: 编造符号 {self.name}"


def is_lib_symbol(name: str) -> bool:
    """判断标识符是否属于需要校验的标准库符号。"""
    if name in PERIPHERAL_INSTANCES:
        return True
    return any(name.startswith(prefix) for prefix in LIB_PREFIXES)


def strip_c_comments_and_strings(text: str) -> str:
    """把注释与字符串字面量替换成空格，保留换行以维持行号。

    必须做这一步，否则注释里提到的符号（如文档说「可用 GPIO_SetBits」）
    会被当成代码引用了它，造成误报。
    """
    out = list(text)
    index = 0
    length = len(out)

    while index < length:
        char = out[index]
        if char == "/" and index + 1 < length:
            nxt = out[index + 1]
            if nxt == "*":
                end = text.find("*/", index + 2)
                end = length if end < 0 else end + 2
                for pos in range(index, end):
                    if out[pos] != "\n":
                        out[pos] = " "
                index = end
                continue
            if nxt == "/":
                end = text.find("\n", index + 2)
                end = length if end < 0 else end
                for pos in range(index, end):
                    out[pos] = " "
                index = end
                continue
        elif char == '"':
            end = index + 1
            while end < length:
                if out[end] == "\\":
                    end += 2
                    continue
                if out[end] == '"':
                    end += 1
                    break
                if out[end] == "\n":
                    break
                end += 1
            for pos in range(index, min(end, length)):
                if out[pos] != "\n":
                    out[pos] = " "
            index = end
            continue
        elif char == "'":
            end = index + 1
            while end < length:
                if out[end] == "\\":
                    end += 2
                    continue
                if out[end] == "'":
                    end += 1
                    break
                if out[end] == "\n":
                    break
                end += 1
            for pos in range(index, min(end, length)):
                if out[pos] != "\n":
                    out[pos] = " "
            index = end
            continue
        index += 1

    return "".join(out)


def find_invented(text: str, index: SymbolIndex, label: str = "<code>") -> list[InventedSymbol]:
    """检查一段 C 代码，返回全部编造符号。

    ``IS_`` 前缀的宏是库自带的参数校验宏，但生成代码不会直接调用它们，
    所以不额外豁免 —— 真的调用了也应该查得到。
    """
    problems: list[InventedSymbol] = []
    lines = text.splitlines()
    cleaned = strip_c_comments_and_strings(text).splitlines()

    for lineno, line in enumerate(cleaned, 1):
        original = lines[lineno - 1] if lineno - 1 < len(lines) else ""
        if NOQA_RE.search(original):
            continue

        # 标出成员访问位置，这些标识符不参与校验
        member_spans: set[int] = set()
        for match in _MEMBER_ACCESS_RE.finditer(line):
            member_spans.add(match.start(1))

        for match in IDENT_RE.finditer(line):
            if match.start(1) in member_spans:
                continue
            name = match.group(1)
            if not is_lib_symbol(name):
                continue
            if name in EXEMPT or name in STRUCT_VAR_NAMES or index.has_any(name):
                continue
            problems.append(InventedSymbol(label=label, line=lineno, name=name))

    return problems


def find_invented_in_files(
    paths: list[Path], index: SymbolIndex | None = None
) -> list[InventedSymbol]:
    """批量检查文件。"""
    idx = index or default_index()
    problems: list[InventedSymbol] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        problems.extend(find_invented(text, idx, str(path)))
    return problems


def collect_c_files(paths: list[str | Path]) -> list[Path]:
    """把输入路径展开为 .c/.h 文件列表。"""
    targets: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            targets.extend(sorted(path.rglob("*.c")))
            targets.extend(sorted(path.rglob("*.h")))
        elif path.is_file():
            targets.append(path)
    # 去重并保持顺序
    seen: set[Path] = set()
    result: list[Path] = []
    for path in targets:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result
