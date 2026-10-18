"""GUI 层。

分层约束：**本层只做「显示」与「收集输入」，不含任何生成或校验逻辑**。
所有校验调用 ``stm32gen.validate``，所有生成调用 ``stm32gen.generator``，
表单与模型之间的转换调用 ``stm32gen_gui.bridge``。

```
bridge.py       表单数据 ↔ 配置模型（**不依赖 Qt**，可完整单测）
highlighter.py  C 代码语法高亮
main_window.py  主窗口：菜单、工具栏、四个配置页、代码预览、校验面板
```

### 为什么桥接层单独拆出来，且不在这里急切导入

GUI 里最容易出错的不是控件摆放，而是「表格里的一行 ↔ 模型里的一条配置」
的双向转换：控件存字符串，模型要枚举与整数。把这段逻辑隔离到
``bridge.py`` 后，它能在无显示器、无事件循环的环境里被完整覆盖 ——
``tests/test_gui.py`` 里大部分断言都是针对它的。

因此本模块**只急切导出 bridge**；``CHighlighter`` 与 ``MainWindow`` 走
``__getattr__`` 惰性导入。否则 ``from stm32gen_gui import bridge`` 会连带
把 PySide6 拉进来，bridge 的那点「无需 Qt 即可测试」的好处就没了。

### 预览与 CLI 同源

预览调用的就是 ``generator.generate()``，与 ``python -m stm32gen generate``
完全同一条代码路径，所以界面上看到的内容与命令行产出的文件逐字节一致。

### 表单非法时的行为

用户输入过程中字段必然暂时非法，所以 ``refresh_preview`` **不弹窗**，
只在状态栏与「校验问题」面板里说明。弹窗会打断输入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stm32gen_gui.bridge import (
    ConfigForm,
    ConfigResult,
    FieldError,
    GpioRow,
    NvicRow,
    UsartForm,
)

if TYPE_CHECKING:  # 仅供类型检查，运行时不导入 PySide6
    from stm32gen.stdlib_ref import SymbolIndex  # noqa: F401

    from stm32gen_gui.highlighter import CHighlighter
    from stm32gen_gui.main_window import MainWindow

# 惰性导出的名字 -> (模块名, 属性名)
_LAZY: dict[str, tuple[str, str]] = {
    "CHighlighter": ("stm32gen_gui.highlighter", "CHighlighter"),
    "MainWindow": ("stm32gen_gui.main_window", "MainWindow"),
    "run": ("stm32gen_gui.main_window", "run"),
}


def __getattr__(name: str) -> Any:
    """按需导入依赖 Qt 的部分。"""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_LAZY))


__all__ = [
    "CHighlighter",
    "ConfigForm",
    "ConfigResult",
    "FieldError",
    "GpioRow",
    "MainWindow",
    "NvicRow",
    "UsartForm",
    "run",
]
