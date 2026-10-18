"""控制台输出工具。

Windows 控制台默认用 GBK 编码，直接 print 中文会变成乱码。
本模块提供一个幂等的编码修正函数，CLI 与工具脚本共用。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """让中文输出在 Windows 控制台（默认 GBK）下不乱码。

    Python 3.7+ 提供 ``reconfigure``；重定向到文件或管道时同样适用。
    失败时静默忽略 —— 编码问题不该让程序崩掉。

    幂等：重复调用无副作用。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
