"""标准库符号索引层。

从 ``E:\\stm32初始化\\代码`` 的标准库头文件中提取真实存在的符号，
生成 ``symbols.json`` 供模型枚举与校验层使用 —— 这是"禁止编造函数/宏/枚举"
这条硬性约束的落地机制。

- ``symbols.json`` 由 ``tools/extract_symbols.py`` 生成（实测产物，非手抄）
- ``index.py`` 提供查询接口，查不到会抛 ``InventedSymbolError``
- ``guard.py`` 提供反编造护栏，校验任意 C 代码里的标准库符号
"""

from __future__ import annotations

from stm32gen.stdlib_ref.guard import (
    EXEMPT,
    LIB_PREFIXES,
    PERIPHERAL_INSTANCES,
    STRUCT_VAR_NAMES,
    InventedSymbol,
    collect_c_files,
    find_invented,
    find_invented_in_files,
    is_lib_symbol,
    strip_c_comments_and_strings,
)
from stm32gen.stdlib_ref.index import (
    DEFAULT_SYMBOLS_PATH,
    InventedSymbolError,
    SymbolIndex,
    SymbolInfo,
    default_index,
)

__all__ = [
    "DEFAULT_SYMBOLS_PATH",
    "EXEMPT",
    "LIB_PREFIXES",
    "PERIPHERAL_INSTANCES",
    "STRUCT_VAR_NAMES",
    "InventedSymbol",
    "InventedSymbolError",
    "SymbolIndex",
    "SymbolInfo",
    "collect_c_files",
    "default_index",
    "find_invented",
    "find_invented_in_files",
    "is_lib_symbol",
    "strip_c_comments_and_strings",
]
