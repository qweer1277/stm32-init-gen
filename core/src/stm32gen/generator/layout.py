"""产物布局：按外设分文件，还是合并成少数几个文件。

两种布局的取舍
--------------

``SPLIT``（默认，19 个文件）
    每个外设一对 ``.h``/``.c``，加聚合入口 ``bsp_init``、中断骨架 ``bsp_it``。
    删掉某个外设时，它的头文件、实现、时钟使能一起消失，不会在别的文件里
    留下孤儿代码。代价是文件数多 —— 但小配置下很多文件只有 4~5 行实体代码
    （空函数 + 注释），因为 ``bsp_init()`` 的调用序列在所有配置下都一致。

``MERGED``（3 个文件）
    全部声明并进 ``bsp.h``，全部实现并进 ``bsp.c``，加 ``main.c``。
    适合「不想在工程里加 19 个文件」的场合。代价是删掉外设时
    ``bsp.c`` 里会少一段，但仍是一个文件，需要自己记住改过什么。

**两者由同一套模板产出**：模板内部仍按外设分段，合并只是把各段的
文件级脚手架（include guard、``extern "C"``、``#include "stm32f10x.h"``、
自身头文件 include、版权尾注）省掉。这样加一个外设只改一处，
不会出现「split 模板改了、merged 模板忘了」的漂移。
"""

from __future__ import annotations

from enum import StrEnum


class OutputLayout(StrEnum):
    """产物布局。

    值是小写字符串，便于直接用作 CLI 参数（``--layout merged``）。
    """

    SPLIT = "split"
    MERGED = "merged"

    @property
    def is_merged(self) -> bool:
        return self is OutputLayout.MERGED

    @property
    def description(self) -> str:
        if self.is_merged:
            return "合并为 3 个文件（bsp.h / bsp.c / main.c）"
        return "按外设分文件（19 个文件）"


DEFAULT_LAYOUT = OutputLayout.SPLIT
