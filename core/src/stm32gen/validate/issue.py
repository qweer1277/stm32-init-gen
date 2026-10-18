"""校验问题的数据结构。

校验层不抛异常，而是**收集全部问题**一次性返回。理由：GUI 要一次把所有
问题标红给用户看，逐个抛异常会让用户改一处、再跑一次、再改一处。

严重级别
--------
- ``ERROR``   配置必然产出错误代码，或硬件上不可能工作，必须修
- ``WARNING`` 能工作但有明确风险（上电抖一下、误差偏大、调试口被占）
- ``INFO``    提示性信息，不影响生成
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    """问题严重级别。"""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def label(self) -> str:
        """中文标签，供 CLI 与 GUI 显示。"""
        return {"error": "错误", "warning": "警告", "info": "提示"}[self.value]

    @property
    def is_blocking(self) -> bool:
        """是否阻断生成。只有 ERROR 阻断。"""
        return self is Severity.ERROR


@dataclass(frozen=True)
class Issue:
    """一条校验结果。"""

    severity: Severity
    code: str
    message: str
    # 定位信息：形如 "gpio[0]" / "usart[0].baudrate" / "nvic.channels[1]"
    location: str = ""
    # 修法建议。好的错误信息要告诉用户怎么改，而不只是说哪里错了
    hint: str = ""
    # 涉及的引脚或符号，供 GUI 高亮
    subjects: tuple[str, ...] = field(default_factory=tuple)

    def format(self, with_hint: bool = True) -> str:
        """格式化成一行文本。"""
        parts = [f"[{self.severity.label}]"]
        if self.location:
            parts.append(f"{self.location}:")
        parts.append(self.message)
        text = " ".join(parts)
        if with_hint and self.hint:
            text += f"\n         修法：{self.hint}"
        return text

    def __str__(self) -> str:
        return self.format()


def error(code: str, message: str, **kwargs) -> Issue:
    """构造一条 ERROR。"""
    return Issue(Severity.ERROR, code, message, **kwargs)


def warning(code: str, message: str, **kwargs) -> Issue:
    """构造一条 WARNING。"""
    return Issue(Severity.WARNING, code, message, **kwargs)


def info(code: str, message: str, **kwargs) -> Issue:
    """构造一条 INFO。"""
    return Issue(Severity.INFO, code, message, **kwargs)


def summarize(issues: list[Issue]) -> str:
    """汇总成一行统计文本。"""
    errors = sum(1 for item in issues if item.severity is Severity.ERROR)
    warnings = sum(1 for item in issues if item.severity is Severity.WARNING)
    infos = sum(1 for item in issues if item.severity is Severity.INFO)
    if not issues:
        return "校验通过，无问题"
    return f"错误 {errors} / 警告 {warnings} / 提示 {infos}"


def has_blocking(issues: list[Issue]) -> bool:
    """是否存在阻断性问题。"""
    return any(item.severity.is_blocking for item in issues)
