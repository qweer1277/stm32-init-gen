"""标准库符号索引。

加载 ``symbols.json``，为模型层与校验层提供「这个符号是否真实存在」的查询。
这是"禁止编造函数/宏/枚举"约束在**运行时**的落地：

- ``has_macro`` / ``has_enum`` / ``has_function`` / ``has_type``：精确查询
- ``require``：查不到就抛 ``InventedSymbolError``，让错误在开发期暴露
- ``enum_values`` / ``macros_with_prefix``：给 GUI 下拉框填选项

数据来源是 ``tools/extract_symbols.py`` 的实测产物，不是手抄。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# symbols.json 与本模块同目录
DEFAULT_SYMBOLS_PATH = Path(__file__).with_name("symbols.json")


class InventedSymbolError(KeyError):
    """引用了标准库中不存在的符号。"""

    def __init__(self, kind: str, name: str) -> None:
        super().__init__(name)
        self.kind = kind
        self.name = name

    def __str__(self) -> str:
        return (
            f"标准库中不存在 {self.kind} 「{self.name}」。"
            f"请核对 E:\\stm32初始化\\代码 下的头文件，或重新运行 "
            f"tools\\extract_symbols.py。"
        )


@dataclass(frozen=True)
class SymbolInfo:
    """一个符号的元信息。"""

    name: str
    kind: str
    file: str
    line: int
    value: str = ""
    int_value: int | None = None
    desc: str = ""
    group: str = ""
    enum_type: str = ""
    function_like: bool = False
    params: tuple[str, ...] = ()
    return_type: str = ""
    members: tuple[dict, ...] = ()
    peripheral_type: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> SymbolInfo:
        """从 symbols.json 的一条记录构造。"""
        return cls(
            name=name,
            kind=data.get("kind", ""),
            file=data.get("file", ""),
            line=data.get("line", 0),
            value=data.get("value", ""),
            int_value=data.get("int_value"),
            desc=data.get("desc", ""),
            group=data.get("group", ""),
            enum_type=data.get("enum_type", ""),
            function_like=bool(data.get("function_like", False)),
            params=tuple(data.get("params", ())),
            return_type=data.get("return_type", ""),
            members=tuple(data.get("members", ())),
            peripheral_type=data.get("peripheral_type", ""),
        )

    @property
    def location(self) -> str:
        """形如 ``Library/stm32f10x_gpio.h:123``，便于跳转与追溯。"""
        return f"{self.file}:{self.line}" if self.file else ""


class SymbolIndex:
    """标准库符号索引。"""

    def __init__(self, data: dict) -> None:
        self._meta: dict = data.get("meta", {})
        self._stats: dict = data.get("stats", {})
        # 保留原始记录，供 as_buckets() 与需要完整字段的工具复用
        self._raw_macros: dict[str, dict] = dict(data.get("macros", {}))
        self._raw_enums: dict[str, dict] = dict(data.get("enums", {}))
        self._raw_functions: dict[str, dict] = dict(data.get("functions", {}))
        self._raw_types: dict[str, dict] = dict(data.get("types", {}))
        self._macros = {
            n: SymbolInfo.from_dict(n, d) for n, d in self._raw_macros.items()
        }
        self._enums = {
            n: SymbolInfo.from_dict(n, d) for n, d in self._raw_enums.items()
        }
        self._functions = {
            n: SymbolInfo.from_dict(n, d) for n, d in self._raw_functions.items()
        }
        self._types = {
            n: SymbolInfo.from_dict(n, d) for n, d in self._raw_types.items()
        }
        self._enum_groups: dict[str, list[str]] = {
            n: list(d.get("members", [])) for n, d in data.get("enum_groups", {}).items()
        }

    # -- 构造 ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path | None = None) -> SymbolIndex:
        """从 JSON 文件加载索引。"""
        target = Path(path) if path else DEFAULT_SYMBOLS_PATH
        if not target.is_file():
            raise FileNotFoundError(
                f"找不到符号表 {target}。请先运行：python tools\\extract_symbols.py"
            )
        return cls(json.loads(target.read_text(encoding="utf-8")))

    # -- 属性 ------------------------------------------------------------

    @property
    def meta(self) -> dict:
        """提取时的元信息（标准库路径、预定义宏、头文件清单）。"""
        return dict(self._meta)

    @property
    def stats(self) -> dict:
        """各类符号的数量统计。"""
        return dict(self._stats)

    # -- 查询 ------------------------------------------------------------

    def has_macro(self, name: str) -> bool:
        return name in self._macros

    def has_enum(self, name: str) -> bool:
        return name in self._enums

    def has_function(self, name: str) -> bool:
        return name in self._functions

    def has_type(self, name: str) -> bool:
        return name in self._types

    def has_any(self, name: str) -> bool:
        """名字是否存在于任意一类符号中。"""
        return (
            name in self._macros
            or name in self._enums
            or name in self._functions
            or name in self._types
        )

    def get(self, name: str) -> SymbolInfo | None:
        """按名字取符号，四类里找到哪个算哪个。"""
        for bucket in (self._macros, self._enums, self._functions, self._types):
            found = bucket.get(name)
            if found is not None:
                return found
        return None

    def require_macro(self, name: str) -> SymbolInfo:
        """取宏；不存在则抛 ``InventedSymbolError``。"""
        return self._require(self._macros, "宏", name)

    def require_enum(self, name: str) -> SymbolInfo:
        """取枚举值；不存在则抛 ``InventedSymbolError``。"""
        return self._require(self._enums, "枚举值", name)

    def require_function(self, name: str) -> SymbolInfo:
        """取函数；不存在则抛 ``InventedSymbolError``。"""
        return self._require(self._functions, "函数", name)

    def require_type(self, name: str) -> SymbolInfo:
        """取类型；不存在则抛 ``InventedSymbolError``。"""
        return self._require(self._types, "类型", name)

    @staticmethod
    def _require(bucket: dict[str, SymbolInfo], kind: str, name: str) -> SymbolInfo:
        found = bucket.get(name)
        if found is None:
            raise InventedSymbolError(kind, name)
        return found

    # -- 批量查询（供 GUI 与校验层用） -----------------------------------

    def macro_value(self, name: str) -> int | None:
        """取宏的数值；非数值宏或不存在返回 None。"""
        found = self._macros.get(name)
        return found.int_value if found else None

    def enum_value(self, name: str) -> int | None:
        """取枚举值的数值；不存在返回 None。"""
        found = self._enums.get(name)
        return found.int_value if found else None

    def enum_values(self, enum_type: str) -> list[str]:
        """取某个枚举类型（如 ``GPIOMode_TypeDef``）的全部成员名。"""
        return list(self._enum_groups.get(enum_type, ()))

    def macros_with_prefix(self, prefix: str) -> list[str]:
        """取以某前缀开头的全部宏名（已排序）。"""
        return sorted(n for n in self._macros if n.startswith(prefix))

    def enums_with_prefix(self, prefix: str) -> list[str]:
        """取以某前缀开头的全部枚举值名（已排序）。"""
        return sorted(n for n in self._enums if n.startswith(prefix))

    def functions_with_prefix(self, prefix: str) -> list[str]:
        """取以某前缀开头的全部函数名（已排序）。"""
        return sorted(n for n in self._functions if n.startswith(prefix))

    def struct_members(self, type_name: str) -> list[dict]:
        """取结构体的成员列表（``[{"type":..., "name":...}]``）。"""
        found = self._types.get(type_name)
        return list(found.members) if found else []

    def all_names(self) -> set[str]:
        """全部已知符号名的并集，供反编造护栏构造白名单。"""
        return set(self._macros) | set(self._enums) | set(self._functions) | set(self._types)

    def as_buckets(self) -> dict[str, dict[str, dict]]:
        """按桶导出原始记录，格式与 ``symbols.json`` 一致。

        供反编造护栏等外部工具复用，避免它们伸手去摸私有属性。
        """
        return {
            "macros": dict(self._raw_macros),
            "enums": dict(self._raw_enums),
            "functions": dict(self._raw_functions),
            "types": dict(self._raw_types),
        }

    def describe(self, name: str) -> str:
        """给出一句话描述，用于 GUI 提示或错误信息。"""
        found = self.get(name)
        if found is None:
            return f"{name}（标准库中不存在）"
        parts = [f"{name} = {found.value}" if found.value else name]
        if found.desc:
            parts.append(found.desc)
        if found.location:
            parts.append(f"@ {found.location}")
        return "  ".join(parts)


@lru_cache(maxsize=1)
def default_index() -> SymbolIndex:
    """进程内共享的默认索引（避免 GUI 反复读 1.7MB JSON）。"""
    return SymbolIndex.load()
