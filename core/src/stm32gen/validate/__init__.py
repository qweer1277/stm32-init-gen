"""校验层。

与模型层的分工：

- **模型层**拦「单条配置自相矛盾」——字段取值非法、组合起来没有意义
- **校验层**拦「多条配置之间冲突」——引脚被两处占用、
  波特率相对 PCLK 的误差、中断使能与 NVIC 通道不匹配

校验层**不抛异常**，而是收集全部问题一次性返回。理由：GUI 要一次把
所有问题标红给用户看，逐个抛异常会让用户改一处、跑一次、再改一处。

典型用法::

    from stm32gen.validate import validate, has_blocking, summarize

    issues = validate(config)
    if has_blocking(issues):
        print(summarize(issues))
        for item in issues:
            print(item.format())
"""

from __future__ import annotations

from stm32gen.models import Stm32Config
from stm32gen.validate.baudrate import (
    BAUD_ERROR_FAIL_PCT,
    BAUD_ERROR_WARN_PCT,
    BAUDRATE_MAX,
    ClockTree,
    common_baudrates,
    compute_clock_tree,
    evaluate_baudrate,
)
from stm32gen.validate.issue import (
    Issue,
    Severity,
    error,
    has_blocking,
    info,
    summarize,
    warning,
)
from stm32gen.validate.pins import (
    ConflictKind,
    PinDirection,
    PinUsage,
    classify_conflict,
    collect_pin_usages,
    effective_usart_pins,
    find_conflicts,
)
from stm32gen.validate.rules import (
    ALL_RULES,
    check_chip,
    check_gpio,
    check_nvic,
    check_pins,
    check_usart,
    sort_issues,
)


def validate(config: Stm32Config, *, include_info: bool = True) -> list[Issue]:
    """跑全部校验规则。

    ``include_info=False`` 时过滤掉 INFO 级提示，只留错误与警告
    （CLI 的默认输出更干净，GUI 则保留全部供用户查看）。
    """
    issues: list[Issue] = []
    for rule in ALL_RULES:
        try:
            issues.extend(rule(config))
        except Exception as exc:  # 单条规则出错不该让整次校验失败
            issues.append(
                error(
                    "internal.rule_failed",
                    f"校验规则 {rule.__name__} 执行失败：{exc}",
                    location="<内部>",
                    hint="这是工具缺陷，请把这条信息反馈给开发者",
                )
            )

    from stm32gen.validate.rules import _dedupe

    issues = _dedupe(issues)
    issues = sort_issues(issues)
    if not include_info:
        issues = [item for item in issues if item.severity is not Severity.INFO]
    return issues


__all__ = [
    "ALL_RULES",
    "BAUDRATE_MAX",
    "BAUD_ERROR_FAIL_PCT",
    "BAUD_ERROR_WARN_PCT",
    "ClockTree",
    "ConflictKind",
    "Issue",
    "PinDirection",
    "PinUsage",
    "Severity",
    "check_chip",
    "check_gpio",
    "check_nvic",
    "check_pins",
    "check_usart",
    "classify_conflict",
    "collect_pin_usages",
    "common_baudrates",
    "compute_clock_tree",
    "effective_usart_pins",
    "error",
    "evaluate_baudrate",
    "find_conflicts",
    "has_blocking",
    "info",
    "sort_issues",
    "summarize",
    "validate",
    "warning",
]
