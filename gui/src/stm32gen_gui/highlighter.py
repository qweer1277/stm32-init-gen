"""C 代码语法高亮。

PySide6 自带的 ``QSyntaxHighlighter`` 只做规则匹配，不解析语义。
对预览而言足够：用户要看的是关键字、注释、宏、字符串是否可读。

高亮规则按**优先级从低到高**应用，后应用的会覆盖前面的 ——
这个顺序很重要：先按通用 C 语法着色，再单独标出 STM32 标准库符号，
这样库符号不会被 ``\\w+`` 规则盖掉。
"""

from __future__ import annotations

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

# 配色：深色主题下的可读性优先，避免纯饱和色（长时间看刺眼）
COLOR_KEYWORD = QColor("#569CD6")     # C 关键字
COLOR_TYPE = QColor("#4EC9B0")        # 类型名
COLOR_COMMENT = QColor("#6A9955")     # 注释
COLOR_STRING = QColor("#CE9178")      # 字符串/字符字面量
COLOR_NUMBER = QColor("#B5CEA8")      # 数字
COLOR_PREPROCESSOR = QColor("#C586C0")  # 预处理指令
COLOR_LIB_SYMBOL = QColor("#DCDCAA")  # 标准外设库符号
COLOR_LIB_FUNC = QColor("#FFD700")    # 库函数名（更亮，便于扫读）

# C89/C99 关键字（生成代码会用到的主要子集）
KEYWORDS = (
    "auto", "break", "case", "const", "continue", "default", "do", "else",
    "enum", "extern", "for", "goto", "if", "inline", "register", "restrict",
    "return", "sizeof", "static", "struct", "switch", "typedef", "union",
    "volatile", "while",
)

# 类型名（含 CMSIS 与标准库的 typedef）
TYPES = (
    "void", "char", "short", "int", "long", "float", "double", "signed",
    "unsigned", "_Bool",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "u8", "u16", "u32", "s8", "s16", "s32", "vu32", "vu16", "vu8",
    "__IO", "__I", "__O",
    "GPIO_InitTypeDef", "USART_InitTypeDef", "NVIC_InitTypeDef",
    "FunctionalState", "ITStatus", "FlagStatus", "BitAction",
    "GPIO_TypeDef", "USART_TypeDef",
)


def _format(color: QColor, *, bold: bool = False) -> QTextCharFormat:
    """构造一个字符格式。"""
    fmt = QTextCharFormat()
    fmt.setForeground(color)
    if bold:
        fmt.setFontWeight(QFont.Weight.Bold)
    return fmt


class CHighlighter(QSyntaxHighlighter):
    """C 代码高亮器。"""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self._build_rules()

    def _build_rules(self) -> None:
        """构造规则列表。顺序即应用顺序，后面的覆盖前面的。"""
        # 1) 库符号（先加，让后面的关键字/注释规则能覆盖它）
        #    这样注释里的 GPIO_Init 仍显示为注释色而不是库符号色
        lib_fmt = _format(COLOR_LIB_SYMBOL)
        self._rules.append(
            (
                QRegularExpression(
                    r"\b(?:GPIO|USART|RCC|NVIC|AFIO|EXTI)_[A-Za-z0-9_]+\b"
                ),
                lib_fmt,
            )
        )

        # 2) 数字（含 0x 十六进制与 U/L 后缀）
        number_fmt = _format(COLOR_NUMBER)
        self._rules.append(
            (
                QRegularExpression(r"\b(?:0[xX][0-9a-fA-F]+|\d+)(?:[uU][lL]{0,2}|[lL]{1,2})?\b"),
                number_fmt,
            )
        )

        # 3) 关键字
        keyword_fmt = _format(COLOR_KEYWORD, bold=True)
        for word in KEYWORDS:
            self._rules.append(
                (QRegularExpression(rf"\b{word}\b"), keyword_fmt)
            )

        # 4) 类型
        type_fmt = _format(COLOR_TYPE)
        for word in TYPES:
            self._rules.append((QRegularExpression(rf"\b{word}\b"), type_fmt))

        # 5) 预处理指令（#include / #define / #ifndef …）
        preproc_fmt = _format(COLOR_PREPROCESSOR)
        self._rules.append(
            (QRegularExpression(r"^\s*#\s*\w+"), preproc_fmt)
        )

        # 6) 字符串与字符字面量
        string_fmt = _format(COLOR_STRING)
        self._rules.append((QRegularExpression(r'"(?:[^"\\]|\\.)*"'), string_fmt))
        self._rules.append((QRegularExpression(r"'(?:[^'\\]|\\.)*'"), string_fmt))

        # 7) 注释放最后，优先级最高 —— 注释里的任何内容都不该被当成代码着色
        comment_fmt = _format(COLOR_COMMENT)
        self._comment_fmt = comment_fmt
        # 块注释需要跨行处理，见 highlightBlock

    def highlightBlock(self, text: str) -> None:
        """对一行文本应用高亮。

        块注释跨行的处理方式是 Qt 的经典做法：用 ``previousBlockState``
        记录「上一行结束时是否还在块注释里」，本行结束时再写回状态。
        """
        # 1) 先按常规规则着色
        for regex, fmt in self._rules:
            iterator = regex.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)

        # 2) 处理块注释（跨行）
        self._highlight_block_comments(text)

        # 3) 行注释（覆盖本行剩余部分）
        line_comment = QRegularExpression(r"//.*$")
        iterator = line_comment.globalMatch(text)
        while iterator.hasNext():
            match = iterator.next()
            self.setFormat(match.capturedStart(), match.capturedLength(), self._comment_fmt)

    def _highlight_block_comments(self, text: str) -> None:
        """处理 ``/* ... */``，含跨行情况。

        状态机：``1`` 表示本行结束时仍在块注释内。
        """
        start_expr = QRegularExpression(r"/\*")
        end_expr = QRegularExpression(r"\*/")
        in_comment = self.previousBlockState() == 1
        start = 0

        if in_comment:
            end_match = end_expr.match(text)
            if not end_match.hasMatch():
                # 整行都在注释里
                self.setFormat(0, len(text), self._comment_fmt)
                self.setCurrentBlockState(1)
                return
            end = end_match.capturedEnd()
            self.setFormat(0, end, self._comment_fmt)
            start = end
            in_comment = False

        while start >= 0:
            start_match = start_expr.match(text, start)
            if not start_match.hasMatch():
                break
            comment_start = start_match.capturedStart()
            end_match = end_expr.match(text, start_match.capturedEnd())
            if not end_match.hasMatch():
                # 块注释在本行未闭合，延续到下一行
                self.setFormat(comment_start, len(text) - comment_start, self._comment_fmt)
                self.setCurrentBlockState(1)
                return
            comment_end = end_match.capturedEnd()
            self.setFormat(
                comment_start, comment_end - comment_start, self._comment_fmt
            )
            start = comment_end

        self.setCurrentBlockState(0)
