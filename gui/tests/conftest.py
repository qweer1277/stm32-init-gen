"""pytest 全局配置。

**关键作用**：在任何 PySide6 模块被导入之前把 Qt 平台设成 ``offscreen``。

不设的话，``QApplication`` 会尝试连接真实显示：
- 有显示器的开发机上会真的弹出窗口，测试期间抢焦点
- 无头环境（CI、容器）上直接构造失败
- 更糟的是弹窗（``QMessageBox``）会进入自己的事件循环等用户点击，
  测试**永久挂住**而不是失败

本步第一次跑 GUI 测试就挂死在 ``QMessageBox.information`` 上，
300 秒超时被移到后台 —— 这个文件的存在的直接原因就是那次事故。

必须在模块级设置（而非 fixture 里）：``PySide6.QtWidgets`` 一旦被导入，
平台插件就已经选定了，之后再改环境变量无效。
"""

from __future__ import annotations

import os
from pathlib import Path

# 必须在 import PySide6 之前
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# offscreen 插件默认去 ``PySide6/lib/fonts`` 找字体，而那里是空的。没有字体时
# Qt 会把所有文字的宽度算成 fallback 值，``minimumSizeHint`` 于是比真实窗口
# 大出一大截 —— 布局类断言（面板最小宽度、分隔条分配）会测出和用户看到的
# 完全不同的数字。指向系统字体目录，让测试和真实运行用同一套度量。
if os.name == "nt":
    _fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if _fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(_fonts))

# 关掉 Qt 的无用警告（字体缺失之类的提示会淹没测试输出）
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false;qt.qpa.*=false")

