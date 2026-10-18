"""从 STM32 标准外设库头文件提取真实符号，产出 ``symbols.json``。

这是"禁止编造函数/宏/枚举"这条硬性约束的落地机制：模型层与校验层
只允许使用本脚本从标准库源码中实测出来的符号。

用法::

    python tools/extract_symbols.py
    python tools/extract_symbols.py --stdlib-root "E:\\stm32初始化\\代码" --out out.json

设计要点（每一条都对应一个踩过的坑）
------------------------------------
1. **注释必须先整体抹掉，再按行解析**。
   库里头文件的结构体成员普遍写成多行注释，例如::

        uint16_t GPIO_Pin;   /*!< 指定要配置的 GPIO 引脚。
                                  该参数可以取 @ref GPIO_pins_define 中的任意值 */

   逐行删注释时第一行没有 ``*/``，于是 ``GPIO 引脚。`` 被当成成员名。
   正确做法：对整份文件把注释内容替换成空格并**保留换行**，
   这样行号不变，解析又不受注释干扰。

2. **先做条件编译求值**。``stm32f10x.h`` 的 ``IRQn_Type`` 枚举内部嵌着
   ``#ifdef STM32F10X_MD / _CL / _HD`` 等 8 个分支，若不做过滤，
   ``USART1_IRQn`` 会同时拿到中容量与互联型的定义。本脚本按
   ``STM32F10X_MD`` + ``USE_STDPERIPH_DRIVER`` 求值，只保留中容量分支
   （对应 STM32F103C8T6）。

3. **同时处理 ``#define`` 与 ``typedef enum``**。``GPIO_Speed_*`` 与
   ``GPIO_Mode_*`` 是枚举而非宏，只抓宏会漏掉最核心的两组常量。

4. **枚举值支持递增省略与成员互引**。
   ``A = 1, B, C`` 中 B/C 取前一项 +1；``typedef enum {DISABLE = 0,
   ENABLE = !DISABLE} FunctionalState;`` 这种单行定义要能解析，
   且 ``!DISABLE`` 需要把同枚举内已解析的成员代入。

5. **不用回溯型正则**。``(?:[A-Za-z_]\\w*\\s+|\\*\\s*)+?`` 这类嵌套量词
   在长行上会灾难性回溯，实测直接把提取过程挂死。函数原型改为手工定位括号。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

# 目标器件与库配置（决定条件编译走向）
DEFAULT_DEFINES = ("STM32F10X_MD", "USE_STDPERIPH_DRIVER")

# 类型修饰符，解析成员/参数时剥掉
_TYPE_QUALIFIERS = ("__IO", "__I", "__O", "__in", "__out", "volatile", "const")

# 整数强转，求值数值时剥掉
_CAST_RE = re.compile(
    r"\(\s*(?:u?int(?:8|16|32|64)_t|u8|u16|u32|s8|s16|s32|vu32|__IO\s+u?int\d+_t)\s*\)"
)

# 整数后缀（0x1234UL / 42U）
_SUFFIX_RE = re.compile(r"\b(0[xX][0-9a-fA-F]+|\d+)([uU][lL]{0,2}|[lL]{1,2}[uU]?)\b")

# 标识符
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")

# @defgroup 名称与描述
_DEFGROUP_RE = re.compile(r"@defgroup\s+(\w+)\s*(.*?)\s*(?:\*/|$)")

# 条件编译指令
_COND_RE = re.compile(r"#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)")

# 外设指针宏：#define GPIOA ((GPIO_TypeDef *) GPIOA_BASE)
_PERIPH_RE = re.compile(r"\(\s*(\w+)\s*\*\s*\)\s*(\w+)")

# 函数式宏：#define IS_GPIO_PIN(PIN) (((PIN) & 0x00) == 0x00)
_FUNC_MACRO_RE = re.compile(r"^(\w+)\s*\(([^)]*)\)\s*(.*)$", re.DOTALL)

# 结构体/枚举声明头。**不能要求 `{` 与 typedef 同行**：
# 库里的写法普遍是
#     typedef enum
#     {
#       ...
#     }GPIOSpeed_TypeDef;
# 只匹配 ``typedef enum\s*\{`` 会漏掉全部枚举与结构体。
# 这里只认 ``typedef struct|enum [tag]``，花括号交给逐字符扫描定位。
_TYPEDEF_HEAD_RE = re.compile(r"^\s*typedef\s+(struct|enum)\b\s*(\w+)?")


@dataclass
class Symbol:
    """一个符号（宏 / 枚举值 / 函数 / 类型）。"""

    name: str
    kind: str
    file: str
    line: int
    value: str = ""
    int_value: int | None = None
    desc: str = ""
    group: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data: dict = {"kind": self.kind, "file": self.file, "line": self.line}
        if self.value:
            data["value"] = self.value
        if self.int_value is not None:
            data["int_value"] = self.int_value
        if self.desc:
            data["desc"] = self.desc
        if self.group:
            data["group"] = self.group
        data.update(self.extra)
        return data


# --------------------------------------------------------------------------
# 文本预处理
# --------------------------------------------------------------------------


def blank_comments(text: str) -> str:
    """把注释内容替换成空格，**保留换行**。

    保留换行是为了让 ``splitlines()`` 的行号与原文一一对应，
    这样报错与跳转都能指回真实位置。
    """
    out = list(text)
    index = 0
    length = len(out)

    while index < length:
        char = out[index]
        if char == "/" and index + 1 < length:
            nxt = out[index + 1]
            if nxt == "*":
                # 块注释：内部换行保留，其余字符变空格
                end = text.find("*/", index + 2)
                end = length if end < 0 else end + 2
                for pos in range(index, end):
                    if out[pos] != "\n":
                        out[pos] = " "
                index = end
                continue
            if nxt == "/":
                # 行注释：到行尾
                end = text.find("\n", index + 2)
                end = length if end < 0 else end
                for pos in range(index, end):
                    out[pos] = " "
                index = end
                continue
        index += 1

    return "".join(out)


def build_comment_map(text: str) -> dict[int, str]:
    """建立「注释起始行号 -> 注释文本」的映射，供符号说明使用。

    按起始行号记录，多行注释也能拿到完整文本。
    """
    result: dict[int, str] = {}
    for match in re.finditer(r"/\*+!?<?\s*(.*?)\s*\*/", text, re.DOTALL):
        lineno = text.count("\n", 0, match.start()) + 1
        desc = re.sub(r"\s+", " ", match.group(1)).strip()
        desc = re.sub(r"^[<@\\]\s*", "", desc).strip()
        if desc:
            result.setdefault(lineno, desc)

    for match in re.finditer(r"//[!/<]?\s*(.*)$", text, re.MULTILINE):
        lineno = text.count("\n", 0, match.start()) + 1
        desc = match.group(1).strip()
        if desc:
            result.setdefault(lineno, desc)

    return result


def desc_near(comment_map: dict[int, str], lineno: int) -> str:
    """取该行或其后续 3 行内最近的注释文本。

    多行注释的起始行往往在成员声明之前一行，所以要向后看几行。
    """
    for offset in range(0, 4):
        found = comment_map.get(lineno + offset)
        if found:
            return found
    return ""


# --------------------------------------------------------------------------
# 条件编译求值
# --------------------------------------------------------------------------


def eval_condition(expr: str, defines: set[str]) -> bool:
    """把 C 预处理条件表达式翻译成 Python 并求值。

    未定义的标识符按 0 处理（与 C 预处理一致）。表达式无法求值时
    保守返回 False —— 宁可漏掉符号，也不要把别的器件分支混进来。
    """
    text = expr
    text = re.sub(
        r"defined\s*\(\s*(\w+)\s*\)",
        lambda m: "1" if m.group(1) in defines else "0",
        text,
    )
    text = re.sub(
        r"defined\s+(\w+)",
        lambda m: "1" if m.group(1) in defines else "0",
        text,
    )
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)

    def _ident(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in ("and", "or", "not"):
            return token
        if token[0].isdigit():
            return token
        return "1" if token in defines else "0"

    text = re.sub(r"\b(\w+)\b", _ident, text)
    text = _SUFFIX_RE.sub(r"\1", text)

    try:
        return bool(eval(text, {"__builtins__": {}}, {}))  # noqa: S307
    except Exception:
        return False


def strip_conditionals(lines: list[str], defines: set[str]) -> list[tuple[int, str]]:
    """过滤条件编译，返回 ``(原始行号, 行内容)`` 列表。

    条件指令行本身不进入结果；非活跃分支整体丢弃。
    """
    out: list[tuple[int, str]] = []
    # 栈帧：[父分支是否活跃, 当前分支是否活跃, 是否已有分支被选中]
    stack: list[list[bool]] = []
    active = True

    for lineno, raw in enumerate(lines, 1):
        match = _COND_RE.match(raw.strip())
        if not match:
            if active:
                out.append((lineno, raw))
            continue

        kind = match.group(1)
        rest = match.group(2).strip()

        if kind in ("ifdef", "ifndef", "if"):
            parent = active
            if kind == "ifdef":
                tokens = rest.split()
                cond = bool(tokens) and tokens[0] in defines
            elif kind == "ifndef":
                tokens = rest.split()
                cond = bool(tokens) and tokens[0] not in defines
            else:
                cond = eval_condition(rest, defines)
            this = parent and cond
            stack.append([parent, this, this])
            active = this
        elif kind == "elif":
            if stack:
                frame = stack[-1]
                if frame[2]:
                    this = False
                else:
                    this = frame[0] and eval_condition(rest, defines)
                    frame[2] = frame[2] or this
                frame[1] = this
                active = this
        elif kind == "else":
            if stack:
                frame = stack[-1]
                this = frame[0] and not frame[2]
                frame[2] = True
                frame[1] = this
                active = this
        elif kind == "endif":
            if stack:
                frame = stack.pop()
                active = frame[0]

    return out


def join_continuations(entries: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """把以 ``\\`` 结尾的续行拼接成单行，行号取首行。"""
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    start_line = 0

    for lineno, raw in entries:
        stripped = raw.rstrip()
        if not buffer:
            start_line = lineno
        if stripped.endswith("\\"):
            buffer.append(stripped[:-1])
            continue
        if buffer:
            buffer.append(stripped)
            out.append((start_line, " ".join(buffer)))
            buffer = []
        else:
            out.append((lineno, raw))

    if buffer:
        out.append((start_line, " ".join(buffer)))
    return out


# 折行原型最多跨几行。标准库里最长的是 3~4 行，留点余量即可；
# 给太大反而会让「括号一直不配平」的垃圾文本吞掉大段正常内容。
MAX_PROTOTYPE_LINES = 8

# 函数原型可能出现的返回类型。用白名单而不是「任意标识符后跟 (」——
# 后者会把头文件里的 `if (x)`、宏体里的函数式调用一起卷进来，
# 结果是往符号表里塞进一堆根本不存在的「函数」。
_PROTOTYPE_RETURN_RE = re.compile(
    r"^(?:void|static\s+void|inline\s+void"
    r"|uint8_t|uint16_t|uint32_t|uint64_t|int8_t|int16_t|int32_t|int64_t"
    r"|int|unsigned|u8|u16|u32|s8|s16|s32"
    r"|FlagStatus|ITStatus|ErrorStatus|FunctionalState)\b"
)


def looks_like_prototype_start(line: str) -> bool:
    """判断一行是否是「参数列表还没写完」的函数原型开头。"""
    if not line or line.startswith("#") or "{" in line or ";" in line:
        return False
    if "(" not in line or not _PROTOTYPE_RETURN_RE.match(line):
        return False
    return line.count("(") > line.count(")")


def join_prototype(entries: list[tuple[int, str]], index: int) -> tuple[str, int]:
    """从 ``index`` 起把折行原型拼成一行，返回 ``(文本, 消耗行数)``。

    拼不成（括号迟迟不配平、或中途遇到空行 / 预处理行）时返回 ``("", 1)``，
    让调用方按普通行处理 —— 宁可漏解析这一行，也不能吞掉后面的正常内容。
    """
    parts: list[str] = []
    depth = 0

    for offset in range(MAX_PROTOTYPE_LINES):
        position = index + offset
        if position >= len(entries):
            return "", 1
        text = entries[position][1].strip()
        if not text or text.startswith("#"):
            return "", 1
        parts.append(text)
        depth += text.count("(") - text.count(")")
        if depth <= 0:
            joined = " ".join(parts)
            # 括号配平了就必须同时以分号收尾，否则不是函数原型
            if joined.rstrip().endswith(";"):
                return joined, offset + 1
            return "", 1
    return "", 1


# --------------------------------------------------------------------------
# 表达式求值
# --------------------------------------------------------------------------


def normalize_expr(value: str) -> str:
    """把 C 表达式转成可 eval 的 Python 表达式。

    必须处理 C 与 Python 的运算符差异，否则会静默丢符号：

    - ``!x`` 是 C 的逻辑非，Python 里 ``!`` 是语法错误。
      库里 ``typedef enum {DISABLE = 0, ENABLE = !DISABLE} FunctionalState;``
      正是这种写法，不转换就会漏掉 ``ENABLE``。
    - ``&&`` / ``||`` 同理。
    - ``~``、``<<``、``>>``、``|``、``&`` 两种语言一致，无需处理。
    """
    text = _SUFFIX_RE.sub(r"\1", _CAST_RE.sub("", value)).strip()
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    return text.strip()


def _safe_eval(expr: str) -> object:
    """求值一个纯算术表达式。

    带两个防护：

    - 含逗号的表达式直接判为不可求值。库里有些宏是 ``(a, b)`` 形式或
      枚举列表，``eval`` 会把它当元组求值并触发
      ``SyntaxWarning: 'tuple' object is not callable``，污染输出。
    - 屏蔽 ``SyntaxWarning``，避免残缺表达式把告警打到 stderr。
    """
    if "," in expr:
        raise ValueError("表达式含逗号，非单一取值")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return eval(expr, {"__builtins__": {}}, {})  # noqa: S307


def try_eval_int(expr: str, known: dict[str, int] | None = None) -> int | None:
    """尝试把表达式求值为整数；失败返回 None。

    ``known`` 用于代入同枚举内已解析的成员（``ENABLE = !DISABLE``）。
    """
    text = normalize_expr(expr)
    if known:
        text = _IDENT_RE.sub(
            lambda m: str(known[m.group(0)]) if m.group(0) in known else m.group(0),
            text,
        )
    try:
        result = _safe_eval(text)
    except Exception:
        return None
    if isinstance(result, bool):
        return int(result)
    return result if isinstance(result, int) else None


def resolve_macro_values(raw_macros: dict[str, str]) -> dict[str, int]:
    """迭代解析宏的数值。

    需要多轮，因为 ``GPIOA_BASE`` 依赖 ``APB2PERIPH_BASE``，
    而后者可能在后文才定义。

    实现上刻意避开"对每个已知宏做一次 ``re.sub``"的写法：宏数量上千时
    那是 O(轮数 × 宏数²) 次正则扫描，实测会跑不动。这里改用
    **单次标识符扫描 + 回调查表**，每轮只扫一遍文本。
    """
    pending: dict[str, str] = {}
    for name, raw in raw_macros.items():
        expr = normalize_expr(raw)
        if expr:
            pending[name] = expr

    resolved: dict[str, int] = {}

    for _ in range(12):
        if not pending:
            break
        progress = False

        for name in list(pending):
            expr = pending[name]

            def _sub(match: re.Match[str], _resolved: dict[str, int] = resolved) -> str:
                token = match.group(0)
                number = _resolved.get(token)
                return f"({number})" if number is not None else token

            substituted = _IDENT_RE.sub(_sub, expr)
            try:
                result = _safe_eval(substituted)
            except Exception:
                continue
            if isinstance(result, bool) or not isinstance(result, int):
                continue
            resolved[name] = result
            del pending[name]
            progress = True

        if not progress:
            break

    return resolved


def split_params(params: str) -> list[dict[str, str]]:
    """拆分函数参数列表为 ``[{"type": ..., "name": ...}]``。"""
    params = params.strip()
    if not params or params == "void":
        return []

    parts: list[str] = []
    depth = 0
    current = ""
    for char in params:
        if char in "(<[":
            depth += 1
        elif char in ")>]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)

    parsed: list[dict[str, str]] = []
    for item in parts:
        text = item.strip()
        if not text:
            continue
        for qualifier in _TYPE_QUALIFIERS:
            text = re.sub(rf"\b{qualifier}\b", "", text).strip()
        tokens = text.rsplit(" ", 1)
        if len(tokens) == 2:
            type_part, name_part = tokens[0].strip(), tokens[1].strip().lstrip("*")
        else:
            type_part, name_part = text, ""
        parsed.append({"type": type_part, "name": name_part})
    return parsed


# --------------------------------------------------------------------------
# 头文件解析
# --------------------------------------------------------------------------


class HeaderParser:
    """单个头文件的解析器。"""

    def __init__(
        self,
        path: Path,
        rel_path: str,
        defines: set[str],
        comment_map: dict[int, str],
    ) -> None:
        self.path = path
        self.rel_path = rel_path
        self.defines = defines
        self.comments = comment_map
        self.macros: dict[str, Symbol] = {}
        self.enums: dict[str, Symbol] = {}
        self.enum_groups: dict[str, list[str]] = {}
        self.functions: dict[str, Symbol] = {}
        self.types: dict[str, Symbol] = {}
        self._resolved: dict[str, int] = {}

    # -- 主流程 ----------------------------------------------------------

    def parse(self) -> None:
        text = self.path.read_text(encoding="utf-8", errors="replace")
        blanked = blank_comments(text)

        entries = strip_conditionals(blanked.splitlines(), self.defines)
        entries = join_continuations(entries)

        # 第一遍：收集宏的原始值（解析宏数值需要全集）
        raw_macros: dict[str, str] = {}
        for _, line in entries:
            match = re.match(r"\s*#\s*define\s+(\w+)\s*(.*)", line)
            if match and not match.group(1).startswith("_"):
                raw_macros[match.group(1)] = match.group(2).strip()
        self._resolved = resolve_macro_values(raw_macros)

        # 第二遍：正式解析
        self._walk(entries)

    # -- 逐行状态机 ------------------------------------------------------

    def _walk(self, entries: list[tuple[int, str]]) -> None:
        group = ""
        group_desc = ""
        index = 0

        while index < len(entries):
            lineno, line = entries[index]

            defgroup = _DEFGROUP_RE.search(line)
            if defgroup:
                group = defgroup.group(1)
                group_desc = re.sub(r"\s*\*/?\s*$", "", defgroup.group(2)).strip()

            match = re.match(r"\s*#\s*define\s+(\w+)\s*(.*)", line)
            if match:
                name, value = match.group(1), match.group(2).strip()
                if not name.startswith("_") and value:
                    self._add_macro(name, value, lineno, group, group_desc)
                index += 1
                continue

            if _TYPEDEF_HEAD_RE.search(line):
                consumed = self._parse_type(entries, index, group)
                if consumed:
                    index += consumed
                    continue

            cleaned = line.strip()
            if cleaned.endswith(";") and "(" in cleaned and not cleaned.startswith("#"):
                self._add_function(cleaned.rstrip(";").strip(), lineno, group)
            elif looks_like_prototype_start(cleaned):
                # 折行的原型：参数列表被排版到下一行。标准库头文件里这种写法
                # 很常见（``TIM_ETRClockMode1Config``、``DAC_ITConfig`` 都是），
                # 只认「整行以 ; 结尾」会把它们整段漏掉 —— 曾漏掉 13 个函数，
                # 后果是反编造护栏把它们判成「编造符号」，正确代码编译不过。
                joined, consumed = join_prototype(entries, index)
                if consumed > 1:
                    self._add_function(joined, lineno, group)
                    index += consumed - 1

            index += 1

    # -- 宏 --------------------------------------------------------------

    def _add_macro(
        self, name: str, value: str, lineno: int, group: str, group_desc: str
    ) -> None:
        """登记一个宏。区分对象式宏与函数式宏。

        函数式宏（``IS_GPIO_PIN(PIN)``）不能当值用，生成器若把它
        当常量引用会产出错误代码，所以必须单独标注。
        """
        extra: dict = {}
        func_match = _FUNC_MACRO_RE.match(value)
        if func_match:
            extra["function_like"] = True
            extra["params"] = [
                item.strip() for item in func_match.group(2).split(",") if item.strip()
            ]
            body = func_match.group(3).strip()
        else:
            body = value

        periph = _PERIPH_RE.search(body)
        if periph and "*" in body:
            extra["peripheral_type"] = periph.group(1)
            extra["base"] = periph.group(2)

        self.macros[name] = Symbol(
            name=name,
            kind="macro",
            file=self.rel_path,
            line=lineno,
            value=body,
            int_value=self._resolved.get(name),
            desc=desc_near(self.comments, lineno) or group_desc,
            group=group,
            extra=extra,
        )

    # -- 函数 ------------------------------------------------------------

    def _add_function(self, text: str, lineno: int, group: str) -> None:
        """从原型文本中解析函数。手工定位括号，不用回溯型正则。"""
        open_paren = text.find("(")
        if open_paren <= 0:
            return

        depth = 0
        close_paren = -1
        for index in range(open_paren, len(text)):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    close_paren = index
                    break
        if close_paren < 0:
            return

        head = text[:open_paren].strip()
        param_text = text[open_paren + 1 : close_paren].strip()

        name_match = re.search(r"([A-Za-z_]\w*)\s*$", head)
        if not name_match:
            return
        name = name_match.group(1)
        return_type = re.sub(r"\s+", " ", head[: name_match.start()]).strip()
        if not return_type or ";" in return_type:
            return

        self.functions[name] = Symbol(
            name=name,
            kind="function",
            file=self.rel_path,
            line=lineno,
            value=f"{return_type} {name}({param_text})",
            desc=desc_near(self.comments, lineno),
            group=group,
            extra={"return_type": return_type, "params": split_params(param_text)},
        )

    # -- typedef struct / enum -------------------------------------------

    def _parse_type(
        self, entries: list[tuple[int, str]], start: int, group: str
    ) -> int:
        """解析 ``typedef struct/enum {...} Name;``，返回消耗的行数。

        逐字符扫描花括号，因此同一行里既开又闭的写法
        （``typedef enum {DISABLE = 0, ENABLE = !DISABLE} FunctionalState;``）
        也能正确处理。``{`` 之前的内容（typedef 头）丢弃。
        """
        head = entries[start][1]
        head_match = _TYPEDEF_HEAD_RE.search(head)
        if not head_match:
            return 0
        kind = head_match.group(1)

        body: list[tuple[int, str]] = []
        tail = ""
        depth = 0
        index = start
        closed = False

        while index < len(entries) and not closed:
            lineno, line = entries[index]
            buffer = ""
            for pos, char in enumerate(line):
                if char == "{":
                    depth += 1
                    if depth == 1:
                        buffer = ""  # 丢弃 '{' 之前的 typedef 头
                    else:
                        buffer += char
                elif char == "}":
                    depth -= 1
                    if depth <= 0:
                        if buffer.strip():
                            body.append((lineno, buffer))
                        buffer = ""
                        tail = line[pos + 1 :]
                        closed = True
                        break
                    buffer += char
                elif depth >= 1:
                    buffer += char
            else:
                if depth >= 1 and buffer.strip():
                    body.append((lineno, buffer))
                index += 1
                continue
            index += 1

        if not closed:
            return index - start

        # 一个 typedef 可以带多个别名：
        #   typedef enum {RESET = 0, SET = !RESET} FlagStatus, ITStatus;
        # 只取第一个会漏掉 ITStatus（它同样出现在函数签名里）。
        names = [n for n in re.findall(r"\b(\w+)\b", tail.split(";")[0]) if n]
        if not names:
            return index - start

        if kind == "enum":
            members = self._build_enum(names[0], body, entries[start][0], group)
            for alias in names[1:]:
                self.enum_groups[alias] = members
                self.types[alias] = Symbol(
                    name=alias,
                    kind="enum",
                    file=self.rel_path,
                    line=entries[start][0],
                    group=group,
                    extra={"members": members, "alias_of": names[0]},
                )
        else:
            members_struct = self._build_struct(
                names[0], head_match.group(2) or "", body, entries[start][0], group
            )
            for alias in names[1:]:
                self.types[alias] = Symbol(
                    name=alias,
                    kind="struct",
                    file=self.rel_path,
                    line=entries[start][0],
                    group=group,
                    extra={
                        "members": members_struct,
                        "tag": head_match.group(2) or "",
                        "alias_of": names[0],
                    },
                )

        return index - start

    def _build_enum(
        self,
        type_name: str,
        body: list[tuple[int, str]],
        decl_line: int,
        group: str,
    ) -> list[str]:
        """构建枚举，返回成员名列表。支持递增省略与成员互引。"""
        members: list[str] = []
        next_value = 0
        known: dict[str, int] = {}

        for lineno, line in body:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            for item in text.split(","):
                item = item.strip().strip("{}").strip()
                if not item:
                    continue
                if "=" in item:
                    name, expr = item.split("=", 1)
                    name, expr = name.strip(), expr.strip()
                    value = try_eval_int(expr, known)
                    if value is None:
                        continue
                else:
                    name = item
                    value = next_value
                if not re.fullmatch(r"\w+", name):
                    continue
                self.enums[name] = Symbol(
                    name=name,
                    kind="enum_value",
                    file=self.rel_path,
                    line=lineno,
                    value=str(value),
                    int_value=value,
                    desc=desc_near(self.comments, lineno),
                    group=group,
                    extra={"enum_type": type_name},
                )
                members.append(name)
                known[name] = value
                next_value = value + 1

        self.enum_groups[type_name] = members
        self.types[type_name] = Symbol(
            name=type_name,
            kind="enum",
            file=self.rel_path,
            line=decl_line,
            group=group,
            extra={"members": members},
        )
        return members

    def _build_struct(
        self,
        type_name: str,
        tag: str,
        body: list[tuple[int, str]],
        decl_line: int,
        group: str,
    ) -> list[dict[str, str]]:
        """构建结构体，返回成员列表。"""
        members: list[dict[str, str]] = []

        for lineno, line in body:
            text = line.strip()
            if not text or text.startswith("#") or text in ("{", "}"):
                continue
            bits = ""
            if ":" in text:
                text, bits = text.split(":", 1)
                bits = bits.strip().rstrip(";")
            text = text.rstrip(";").strip()
            if not text:
                continue
            for qualifier in _TYPE_QUALIFIERS:
                text = re.sub(rf"\b{qualifier}\b", "", text).strip()
            tokens = text.rsplit(" ", 1)
            if len(tokens) != 2:
                continue
            member_type, member_name = tokens[0].strip(), tokens[1].strip()
            member_name = re.sub(r"\[.*\]$", "", member_name).lstrip("*")
            if not re.fullmatch(r"\w+", member_name):
                continue
            member: dict[str, str] = {"type": member_type, "name": member_name}
            if bits:
                member["bits"] = bits
            member["line"] = str(lineno)
            members.append(member)

        self.types[type_name] = Symbol(
            name=type_name,
            kind="struct",
            file=self.rel_path,
            line=decl_line,
            group=group,
            extra={"members": members, "tag": tag},
        )
        return members


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def collect_headers(stdlib_root: Path) -> list[tuple[Path, str]]:
    """列出要解析的头文件（相对路径用于追溯来源）。"""
    targets: list[tuple[Path, str]] = []
    for sub in ("Start", "Library", "User"):
        directory = stdlib_root / sub
        if not directory.is_dir():
            continue
        for header in sorted(directory.glob("*.h")):
            targets.append((header, f"{sub}/{header.name}"))
    return targets


def extract(stdlib_root: Path, defines: set[str]) -> dict:
    """解析全部头文件并汇总为可序列化的字典。"""
    macros: dict[str, dict] = {}
    enums: dict[str, dict] = {}
    enum_groups: dict[str, dict] = {}
    functions: dict[str, dict] = {}
    types: dict[str, dict] = {}

    headers = collect_headers(stdlib_root)
    for path, rel_path in headers:
        text = path.read_text(encoding="utf-8", errors="replace")
        parser = HeaderParser(path, rel_path, defines, build_comment_map(text))
        parser.parse()

        macros.update({n: s.to_dict() for n, s in parser.macros.items()})
        enums.update({n: s.to_dict() for n, s in parser.enums.items()})
        functions.update({n: s.to_dict() for n, s in parser.functions.items()})
        types.update({n: s.to_dict() for n, s in parser.types.items()})
        for name, members in parser.enum_groups.items():
            enum_groups[name] = {
                "members": members,
                "file": rel_path,
                "line": types.get(name, {}).get("line", 0),
            }

    return {
        "meta": {
            "stdlib_root": str(stdlib_root),
            "defines": sorted(defines),
            "headers": [rel for _, rel in headers],
            "generator": "tools/extract_symbols.py",
        },
        "macros": dict(sorted(macros.items())),
        "enums": dict(sorted(enums.items())),
        "enum_groups": dict(sorted(enum_groups.items())),
        "functions": dict(sorted(functions.items())),
        "types": dict(sorted(types.items())),
        "stats": {
            "macros": len(macros),
            "enum_values": len(enums),
            "enum_types": len(enum_groups),
            "functions": len(functions),
            "types": len(types),
        },
    }


# 关键符号自检表：(桶, 名称, 期望的整数值或 None 表示不检查)
# 这些是 GPIO/USART/NVIC 生成器真正会用到的符号，任何一个缺失
# 或数值不对，都说明解析器出了问题，必须让脚本以非零码退出。
PROBES: tuple[tuple[str, str, int | None], ...] = (
    ("macros", "GPIO_Pin_0", 0x0001),
    ("macros", "GPIO_Pin_13", 0x2000),
    ("macros", "GPIO_Pin_15", 0x8000),
    ("macros", "GPIO_Pin_All", 0xFFFF),
    ("macros", "RCC_APB2Periph_GPIOA", 0x00000004),
    ("macros", "RCC_APB2Periph_GPIOB", 0x00000008),
    ("macros", "RCC_APB2Periph_GPIOC", 0x00000010),
    ("macros", "RCC_APB2Periph_AFIO", 0x00000001),
    ("macros", "RCC_APB2Periph_USART1", 0x00004000),
    ("macros", "RCC_APB1Periph_USART2", 0x00020000),
    ("macros", "RCC_APB1Periph_USART3", 0x00040000),
    ("macros", "USART_IT_RXNE", 0x0525),
    ("macros", "USART_IT_IDLE", 0x0424),
    ("macros", "USART_IT_TXE", 0x0727),
    ("macros", "USART_IT_TC", 0x0626),
    ("macros", "NVIC_PriorityGroup_0", 0x700),
    ("macros", "NVIC_PriorityGroup_2", 0x500),
    ("macros", "NVIC_PriorityGroup_4", 0x300),
    ("macros", "USART_WordLength_8b", 0x0000),
    ("macros", "USART_StopBits_1", 0x0000),
    ("macros", "USART_Parity_No", 0x0000),
    ("macros", "USART_Mode_Rx", 0x0004),
    ("macros", "USART_Mode_Tx", 0x0008),
    ("macros", "USART_HardwareFlowControl_None", 0x0000),
    ("macros", "USART_Clock_Disable", 0x0000),
    ("enums", "GPIO_Mode_AIN", 0x0),
    ("enums", "GPIO_Mode_IN_FLOATING", 0x04),
    ("enums", "GPIO_Mode_IPD", 0x28),
    ("enums", "GPIO_Mode_IPU", 0x48),
    ("enums", "GPIO_Mode_Out_OD", 0x14),
    ("enums", "GPIO_Mode_Out_PP", 0x10),
    ("enums", "GPIO_Mode_AF_OD", 0x1C),
    ("enums", "GPIO_Mode_AF_PP", 0x18),
    ("enums", "GPIO_Speed_10MHz", 1),
    ("enums", "GPIO_Speed_2MHz", 2),
    ("enums", "GPIO_Speed_50MHz", 3),
    ("enums", "Bit_RESET", 0),
    ("enums", "Bit_SET", 1),
    ("enums", "DISABLE", 0),
    ("enums", "ENABLE", 1),
    ("enums", "USART1_IRQn", 37),
    ("enums", "USART2_IRQn", 38),
    ("enums", "USART3_IRQn", 39),
    ("functions", "GPIO_Init", None),
    ("functions", "GPIO_SetBits", None),
    ("functions", "GPIO_ResetBits", None),
    ("functions", "GPIO_WriteBit", None),
    ("functions", "USART_Init", None),
    ("functions", "USART_Cmd", None),
    ("functions", "USART_ITConfig", None),
    ("functions", "NVIC_Init", None),
    ("functions", "NVIC_PriorityGroupConfig", None),
    ("functions", "RCC_APB2PeriphClockCmd", None),
    ("functions", "RCC_APB1PeriphClockCmd", None),
    ("types", "GPIO_InitTypeDef", None),
    ("types", "USART_InitTypeDef", None),
    ("types", "NVIC_InitTypeDef", None),
    ("types", "FunctionalState", None),
)


def run_probes(data: dict) -> list[str]:
    """跑自检，返回问题描述列表（空列表表示全通过）。"""
    problems: list[str] = []
    for bucket, name, expected in PROBES:
        entry = data[bucket].get(name)
        if entry is None:
            problems.append(f"缺失 {bucket}:{name}")
            continue
        if expected is None:
            continue
        actual = entry.get("int_value")
        if actual != expected:
            problems.append(
                f"数值不符 {bucket}:{name} 期望 {expected} 实得 {actual}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    from stm32gen import DEFAULT_STDLIB_ROOT, configured_stdlib_root
    from stm32gen.console import force_utf8_stdio

    force_utf8_stdio()

    parser = argparse.ArgumentParser(
        description="从 STM32 标准外设库头文件提取真实符号表",
    )
    parser.add_argument(
        "--stdlib-root",
        default=DEFAULT_STDLIB_ROOT,
        help="标准库代码根目录（含 Start/Library/User）",
    )
    parser.add_argument(
        "--out",
        default=str(
            Path(__file__).resolve().parents[1]
            / "src"
            / "stm32gen"
            / "stdlib_ref"
            / "symbols.json"
        ),
        help="输出 JSON 路径",
    )
    parser.add_argument(
        "--defines",
        default=",".join(DEFAULT_DEFINES),
        help="预定义宏，逗号分隔（决定条件编译走向）",
    )
    args = parser.parse_args(argv)

    # --stdlib-root 未传时回落到环境变量。不能直接 Path(args.stdlib_root)：
    # 空字符串会解析成当前目录 "."，那个目录永远存在，于是「没配置」会被
    # 误判成「配置好了」，然后拿当前目录当标准库根去扫。
    stdlib_root: Path | None
    if args.stdlib_root:
        stdlib_root = Path(args.stdlib_root)
    else:
        stdlib_root = configured_stdlib_root()

    if stdlib_root is None:
        print(
            "[FAIL] 未指定标准库目录。请传 --stdlib-root 或设置环境变量 STM32_STDLIB_ROOT",
            file=sys.stderr,
        )
        return 1

    if not stdlib_root.is_dir():
        print(f"[FAIL] 找不到标准库目录：{stdlib_root}", file=sys.stderr)
        return 1

    defines = {item.strip() for item in args.defines.split(",") if item.strip()}
    data = extract(stdlib_root, defines)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    stats = data["stats"]
    print(f"标准库根目录 : {stdlib_root}")
    print(f"预定义宏     : {', '.join(sorted(defines))}")
    print(f"解析头文件   : {len(data['meta']['headers'])} 个")
    print(f"宏           : {stats['macros']}")
    print(f"枚举值       : {stats['enum_values']}（{stats['enum_types']} 个枚举类型）")
    print(f"函数原型     : {stats['functions']}")
    print(f"类型定义     : {stats['types']}")
    print(f"输出         : {out_path}  ({out_path.stat().st_size / 1024:.0f} KB)")

    problems = run_probes(data)
    if problems:
        print(f"[FAIL] 自检发现 {len(problems)} 个问题：", file=sys.stderr)
        for item in problems:
            print(f"       - {item}", file=sys.stderr)
        return 2

    print(f"[ OK ] 自检通过（{len(PROBES)} 项关键符号）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
