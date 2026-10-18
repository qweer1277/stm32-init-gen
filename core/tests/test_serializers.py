"""序列化层与 CLI 的测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from stm32gen.models import make_demo_config
from stm32gen.serializers import (
    ConfigFormatError,
    detect_format,
    dump_dict,
    load,
    load_dict,
    save,
)

# --------------------------------------------------------------------------
# 格式探测
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.json", "json"),
        ("a.JSON", "json"),
        ("a.yaml", "yaml"),
        ("a.yml", "yaml"),
        ("a.YAML", "yaml"),
        ("a.txt", "json"),  # 未知扩展名按 JSON 处理
        ("a", "json"),
    ],
)
def test_detect_format(filename: str, expected: str) -> None:
    assert detect_format(filename) == expected


# --------------------------------------------------------------------------
# 往返
# --------------------------------------------------------------------------


def test_json_roundtrip(tmp_path: Path) -> None:
    original = make_demo_config()
    target = tmp_path / "cfg.json"
    save(target, original)
    restored = load(target)
    assert restored.to_dict() == original.to_dict()


def test_yaml_roundtrip(tmp_path: Path) -> None:
    original = make_demo_config()
    target = tmp_path / "cfg.yaml"
    save(target, original)
    restored = load(target)
    assert restored.to_dict() == original.to_dict()


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    """父目录不存在时自动创建，省得调用方先 mkdir。"""
    target = tmp_path / "deep" / "nested" / "cfg.json"
    save(target, make_demo_config())
    assert target.is_file()


def test_saved_json_is_utf8_and_readable(tmp_path: Path) -> None:
    """中文备注要原样写入，不能被转义成 \\uXXXX。"""
    target = tmp_path / "cfg.json"
    save(target, make_demo_config())
    text = target.read_text(encoding="utf-8")
    assert "调试串口" in text
    assert "\\u" not in text


def test_saved_yaml_is_utf8_and_readable(tmp_path: Path) -> None:
    target = tmp_path / "cfg.yaml"
    save(target, make_demo_config())
    text = target.read_text(encoding="utf-8")
    assert "调试串口" in text


def test_saved_values_are_library_symbols(tmp_path: Path) -> None:
    """落盘的值必须是标准库符号名，方便人工对照头文件。"""
    target = tmp_path / "cfg.json"
    save(target, make_demo_config())
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["gpio"][0]["mode"] == "GPIO_Mode_Out_PP"
    assert data["usart"][0]["instance"] == "USART1"


def test_explicit_format_overrides_extension(tmp_path: Path) -> None:
    """可以强制指定格式，不受扩展名影响（读写两侧都要支持）。"""
    target = tmp_path / "cfg.txt"
    save(target, make_demo_config(), fmt="yaml")
    text = target.read_text(encoding="utf-8")
    assert "chip:" in text  # YAML 写法，不是 JSON 的 "chip":
    # 读回时同样要显式指定，否则 .txt 会按 JSON 解析而失败
    restored = load(target, fmt="yaml")
    assert restored.chip.value == "STM32F103C8T6"

    # 不指定格式时，按扩展名推断成 JSON，应当报错而不是静默给出错误结果
    with pytest.raises(ConfigFormatError):
        load(target)


# --------------------------------------------------------------------------
# 错误路径
# --------------------------------------------------------------------------


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "nope.json")


def test_load_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "bad.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigFormatError, match="不是合法 JSON"):
        load(target)


def test_load_invalid_yaml(tmp_path: Path) -> None:
    target = tmp_path / "bad.yaml"
    target.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(ConfigFormatError, match="解析失败"):
        load(target)


def test_load_empty_file(tmp_path: Path) -> None:
    """空文件要在解析前就识别出来，错误信息应说明是空文件。"""
    target = tmp_path / "empty.json"
    target.write_text("", encoding="utf-8")
    with pytest.raises(ConfigFormatError, match="空文件"):
        load(target)


def test_load_whitespace_only_file(tmp_path: Path) -> None:
    """只有空白的文件同样算空。"""
    target = tmp_path / "blank.json"
    target.write_text("   \n\t\n", encoding="utf-8")
    with pytest.raises(ConfigFormatError, match="空文件"):
        load(target)


def test_load_yaml_without_content(tmp_path: Path) -> None:
    """YAML 只有注释时会解析成 None，要明确报错。"""
    target = tmp_path / "comment.yaml"
    target.write_text("# 只有注释\n", encoding="utf-8")
    with pytest.raises(ConfigFormatError, match="没有有效内容"):
        load(target)


def test_load_non_mapping_root(tmp_path: Path) -> None:
    """根节点是列表时要明确报错，而不是让后续逻辑莫名崩掉。"""
    target = tmp_path / "list.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigFormatError, match="必须是映射"):
        load(target)


def test_load_dict_returns_raw_data(tmp_path: Path) -> None:
    """load_dict 不做模型校验，原样返回字典。"""
    target = tmp_path / "raw.json"
    target.write_text(json.dumps({"anything": 1}), encoding="utf-8")
    assert load_dict(target) == {"anything": 1}


def test_load_rejects_invalid_config(tmp_path: Path) -> None:
    """模型校验失败要抛 ValidationError，且错误定位到字段。"""
    from pydantic import ValidationError

    target = tmp_path / "bad.json"
    target.write_text(
        json.dumps({"gpio": [{"port": "Z", "pin": 99, "mode": "Nope"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load(target)


def test_load_rejects_unknown_field(tmp_path: Path) -> None:
    """拼错的字段名必须报错，不能静默忽略。"""
    from pydantic import ValidationError

    target = tmp_path / "typo.json"
    target.write_text(json.dumps({"chip_typo": "x"}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load(target)


def test_dump_dict_yaml_sort_keys_false(tmp_path: Path) -> None:
    """YAML 输出保持字段顺序，便于人工阅读与 diff。"""
    target = tmp_path / "ordered.yaml"
    dump_dict(target, {"zebra": 1, "apple": 2}, fmt="yaml")
    text = target.read_text(encoding="utf-8")
    assert text.index("zebra") < text.index("apple")


def test_yaml_is_valid_yaml(tmp_path: Path) -> None:
    """确保输出能被标准 YAML 解析器读回。"""
    target = tmp_path / "cfg.yaml"
    save(target, make_demo_config())
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["chip"] == "STM32F103C8T6"
    assert isinstance(data["gpio"], list)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_load_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from stm32gen.cli import main

    target = tmp_path / "cfg.json"
    save(target, make_demo_config())
    assert main(["load", str(target)]) == 0
    out = capsys.readouterr().out
    assert "STM32F103C8T6" in out
    assert "STM32F10X_MD" in out


def test_cli_load_reports_validation_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """校验失败要返回非零码并逐条打印问题。"""
    from stm32gen.cli import main

    target = tmp_path / "bad.json"
    target.write_text(json.dumps({"hse_hz": 999}), encoding="utf-8")
    code = main(["load", str(target)])
    assert code == 2
    err = capsys.readouterr().err
    assert "hse_hz" in err


def test_cli_load_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from stm32gen.cli import main

    code = main(["load", str(tmp_path / "nope.json")])
    assert code == 1
    assert "不存在" in capsys.readouterr().err


def test_cli_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    from stm32gen.cli import main

    assert main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_cli_selftest(capsys: pytest.CaptureFixture[str]) -> None:
    """自检应全部通过；标准库目录不在时跳过。"""
    from stm32gen.cli import main, stdlib_root

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")
    assert main(["selftest"]) == 0
    out = capsys.readouterr().out
    assert "[ OK ] 版本满足要求" in out
    assert "符号表" in out


def test_cli_selftest_checks_symbol_table(capsys: pytest.CaptureFixture[str]) -> None:
    """自检要覆盖符号表与模型层——它们是最容易出问题的一环。"""
    from stm32gen.cli import main, stdlib_root

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")
    main(["selftest"])
    out = capsys.readouterr().out
    assert "配置模型层" in out
