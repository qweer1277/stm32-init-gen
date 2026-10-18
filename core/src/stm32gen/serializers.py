"""配置序列化层：JSON / YAML 存读。

约定：磁盘上的配置文件是**纯数据**，值用标准库符号名
（``"GPIO_Mode_Out_PP"``），不携带 Python 类路径，以便手工编辑与版本控制。

读入时做两件事：
1. 按扩展名选解析器（``.json`` / ``.yaml`` / ``.yml``）
2. 交给 ``Stm32Config`` 做完整校验，字段拼错或取值非法都会明确报错

写入时先确保父目录存在，再以 UTF-8 落盘（``ensure_ascii=False``，
中文备注可读）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stm32gen.models import Stm32Config

FORMAT_JSON = "json"
FORMAT_YAML = "yaml"

# 各格式的默认扩展名
DEFAULT_SUFFIX = {FORMAT_JSON: ".json", FORMAT_YAML: ".yaml"}


class ConfigFormatError(ValueError):
    """配置文件格式错误（无法解析、根节点类型不对等）。"""


def detect_format(path: str | Path) -> str:
    """按扩展名推断格式；无法识别时按 JSON 处理。"""
    suffix = Path(path).suffix.lower()
    if suffix in (".yaml", ".yml"):
        return FORMAT_YAML
    return FORMAT_JSON


def load_dict(path: str | Path, fmt: str | None = None) -> dict[str, Any]:
    """读取配置文件为字典（不做模型校验）。

    ``fmt`` 可显式指定格式，覆盖按扩展名的推断；与 ``dump_dict`` 对称，
    便于处理扩展名不可靠的场景（如 ``.txt`` 里放 YAML）。
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"配置文件不存在：{target}")

    text = target.read_text(encoding="utf-8")
    if not text.strip():
        raise ConfigFormatError(f"{target} 是空文件")

    resolved = fmt or detect_format(target)
    try:
        if resolved == FORMAT_YAML:
            import yaml

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigFormatError(f"{target} 不是合法 JSON：{exc}") from exc
    except Exception as exc:  # yaml.YAMLError 等
        raise ConfigFormatError(f"{target} 解析失败：{exc}") from exc

    if data is None:
        # YAML 里只有注释或只有 "---" 时会解析成 None
        raise ConfigFormatError(f"{target} 没有有效内容")
    if not isinstance(data, dict):
        raise ConfigFormatError(
            f"{target} 的根节点必须是映射（mapping），实际是 {type(data).__name__}"
        )
    return data


def load(path: str | Path, fmt: str | None = None) -> Stm32Config:
    """读取配置文件并构造 ``Stm32Config``（含完整校验）。"""
    return Stm32Config.from_dict(load_dict(path, fmt))


def dump_dict(path: str | Path, data: dict[str, Any], fmt: str | None = None) -> None:
    """把字典写入文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved = fmt or detect_format(target)

    if resolved == FORMAT_YAML:
        import yaml

        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, indent=2)
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    target.write_text(text, encoding="utf-8")


def save(path: str | Path, config: Stm32Config, fmt: str | None = None) -> None:
    """把配置写入文件。

    值统一用标准库符号名（``model_dump(mode="json")`` 的天然行为），
    因此配置文件与标准库符号一一对应，可读可查。
    """
    dump_dict(path, config.to_dict(), fmt)
