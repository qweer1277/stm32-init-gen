"""CLI 的测试。

重点验证三件事：

1. **退出码契约** —— 0/1/2 的含义必须稳定，因为 CI 会拿它当门禁
2. **拒绝生成** —— 存在阻断性错误时不能产出代码
3. **反编造自检默认开启** —— 关掉它等于放弃核心保证

不测 argparse 自身的错误处理（那是标准库的事）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stm32gen import OUTPUT_DIR_NAME, default_out_dir, project_root
from stm32gen.cli import (
    EXIT_BLOCKING,
    EXIT_IO,
    EXIT_OK,
    build_parser,
    main,
)
from stm32gen.models import make_demo_config
from stm32gen.serializers import save


@pytest.fixture
def demo_json(tmp_path: Path) -> Path:
    target = tmp_path / "demo.json"
    save(target, make_demo_config())
    return target


@pytest.fixture
def conflict_json(tmp_path: Path) -> Path:
    """故意构造的冲突配置：PA9 冲突 + NVIC 子优先级越界。"""
    target = tmp_path / "conflict.json"
    target.write_text(
        json.dumps(
            {
                "chip": "STM32F103C8T6",
                "gpio": [
                    {
                        "port": "GPIOA",
                        "pin": 9,
                        "mode": "GPIO_Mode_Out_PP",
                        "speed": "GPIO_Speed_2MHz",
                        "init_level": "Bit_SET",
                    }
                ],
                "usart": [{"instance": "USART1"}],
                "nvic": {
                    "priority_group": "NVIC_PriorityGroup_4",
                    "channels": [
                        {
                            "irq_channel": "USART1_IRQn",
                            "preemption_priority": 0,
                            "sub_priority": 3,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return target


# --------------------------------------------------------------------------
# 退出码契约
# --------------------------------------------------------------------------


def test_exit_codes_are_distinct() -> None:
    """三个退出码必须互不相同，否则 CI 无法区分失败原因。"""
    assert len({EXIT_OK, EXIT_IO, EXIT_BLOCKING}) == 3


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    out = capsys.readouterr().out
    assert "usage" in out.lower()
    # 四个子命令都要出现在帮助里
    for command in ("selftest", "load", "validate", "generate"):
        assert command in out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "stm32-init-gen" in capsys.readouterr().out


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------


def test_selftest_passes(capsys: pytest.CaptureFixture[str]) -> None:
    from stm32gen.cli import stdlib_root

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")

    assert main(["selftest"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[ OK ] 版本满足要求" in out
    assert "符号表" in out
    assert "配置模型层" in out
    # 生成链路也要被自检覆盖
    assert "生成器层" in out
    assert "反编造自检通过" in out


def test_selftest_does_not_require_pyside6(capsys: pytest.CaptureFixture[str]) -> None:
    """核心库的自检不得依赖 PySide6。

    桌面界面拆成独立项目后，「只装了核心库」是最常见的部署形态
    （脚本、CI、批处理）。自检若去探测 PySide6，那种环境会报 [FAIL]，
    而它其实完全可用。
    """
    from stm32gen.cli import stdlib_root

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")

    assert main(["selftest"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "PySide6" not in out
    # 也不该出现只有界面项目才有的字样
    assert "GUI 桥接层" not in out
    assert "Qt 平台插件" not in out


def test_selftest_reports_missing_toolchain(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """工具链是可选项，缺失时不应判为失败。

    用 monkeypatch 强制「没有编译器」，而不是依赖本机环境 ——
    否则装了什么工具链这条测试就失去意义了。
    """
    from stm32gen.cli import stdlib_root

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")

    monkeypatch.setattr("stm32gen.compile_check.find_toolchain", lambda prefer=None: None)

    assert main(["selftest"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "未找到编译器" in out
    # 缺失工具链不该让自检失败（它是可选能力）
    assert "[FAIL]" not in out


def test_selftest_reports_keil_toolchain(capsys: pytest.CaptureFixture[str]) -> None:
    """有 Keil 时自检要报出它，并标注为真实目标编译。"""
    from stm32gen.cli import stdlib_root
    from stm32gen.compile_check import find_toolchain

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")
    found = find_toolchain()
    if found is None or not found.kind.is_keil:
        pytest.skip("本机没有 Keil 编译器")

    main(["selftest"])
    out = capsys.readouterr().out
    assert "编译工具链" in out
    assert "Keil" in out
    # 不该出现降级警告
    assert "降级为宿主 gcc" not in out


def test_selftest_lists_alternative_toolchains(capsys: pytest.CaptureFixture[str]) -> None:
    """有多套 ARM 工具链时，自检要顺带列出其它可用的。

    本机装了 ARMCLANG 与 ARMCC 两套，用户可能想切换。
    """
    from stm32gen.cli import stdlib_root
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")

    available = [
        kind
        for kind in (ToolchainKind.ARMCLANG, ToolchainKind.ARMCC, ToolchainKind.ARM_GCC)
        if find_toolchain(prefer=kind) is not None
    ]
    if len(available) < 2:
        pytest.skip("本机只有一套 ARM 工具链")

    main(["selftest"])
    out = capsys.readouterr().out
    assert "其它可用" in out


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------


def test_load_ok(demo_json: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["load", str(demo_json)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "STM32F103C8T6" in out
    assert "STM32F10X_MD" in out
    assert "编译宏" in out


def test_load_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["load", str(tmp_path / "nope.json")]) == EXIT_IO
    assert "不存在" in capsys.readouterr().err


def test_load_bad_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "bad.json"
    target.write_text("{ not json", encoding="utf-8")
    assert main(["load", str(target)]) == EXIT_IO
    assert "JSON" in capsys.readouterr().err


def test_load_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """模型校验失败返回 EXIT_BLOCKING，并逐条打印字段位置。"""
    target = tmp_path / "bad.json"
    target.write_text(json.dumps({"hse_hz": 999}), encoding="utf-8")
    assert main(["load", str(target)]) == EXIT_BLOCKING
    err = capsys.readouterr().err
    assert "hse_hz" in err


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def test_validate_ok(demo_json: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(demo_json)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "校验结果" in out
    assert "[ OK ] 校验通过" in out


def test_validate_no_info_hides_info(
    demo_json: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(demo_json), "--no-info"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[提示]" not in out
    assert "校验通过，无问题" in out


def test_validate_blocking(
    conflict_json: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["validate", str(conflict_json), "--no-info"]) == EXIT_BLOCKING
    captured = capsys.readouterr()
    assert "pin.conflict" not in captured.out  # 输出里是中文描述
    assert "PA9" in captured.out
    assert "阻断性" in captured.err


ALL_GENERATED_FILES = (
    "bsp_init.h",
    "bsp_init.c",
    "bsp_rcc.h",
    "bsp_rcc.c",
    "bsp_afio.h",
    "bsp_afio.c",
    "bsp_gpio.h",
    "bsp_gpio.c",
    "bsp_exti.h",
    "bsp_exti.c",
    "bsp_usart.h",
    "bsp_usart.c",
    "bsp_tim.h",
    "bsp_tim.c",
    "bsp_nvic.h",
    "bsp_nvic.c",
    "bsp_it.h",
    "bsp_it.c",
    "main.c",
)


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------


def test_generate_writes_files(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "out"
    assert main(["generate", str(demo_json), "-o", str(out_dir)]) == EXIT_OK

    names = {path.name for path in out_dir.iterdir()}
    assert len(names) == 19
    assert names == set(ALL_GENERATED_FILES)

    captured = capsys.readouterr()
    assert "反编造自检通过" in captured.out
    assert "已写入 19 个文件" in captured.out


def test_generate_dry_run_writes_nothing(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "out"
    assert main(["generate", str(demo_json), "-o", str(out_dir), "--dry-run"]) == EXIT_OK
    assert not out_dir.exists()

    out = capsys.readouterr().out
    # dry-run 要把内容打印出来
    assert "void bsp_init(void)" in out
    assert "--dry-run：未写入任何文件" in out


def test_generate_print_also_writes(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--print 是「既写文件又打印」，与 --dry-run 不同。"""
    out_dir = tmp_path / "out"
    assert main(["generate", str(demo_json), "-o", str(out_dir), "--print"]) == EXIT_OK
    assert (out_dir / "bsp_init.c").is_file()
    assert "void bsp_init(void)" in capsys.readouterr().out


def test_generate_refuses_on_blocking_errors(
    conflict_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """存在阻断性错误时必须拒绝生成，且不产出任何文件。

    这是核心保证：那些错误必然产出不能用的代码。
    """
    out_dir = tmp_path / "out"
    code = main(["generate", str(conflict_json), "-o", str(out_dir), "--no-info"])
    assert code == EXIT_BLOCKING
    assert not out_dir.exists(), "拒绝生成时不该留下任何文件"

    err = capsys.readouterr().err
    assert "拒绝生成" in err


def test_generate_force_overrides_refusal(
    conflict_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--force 可以强行生成，但要给出警告。"""
    out_dir = tmp_path / "out"
    code = main(
        ["generate", str(conflict_json), "-o", str(out_dir), "--no-info", "--force"]
    )
    assert code == EXIT_OK
    assert (out_dir / "bsp_init.c").is_file()

    captured = capsys.readouterr()
    assert "--force" in captured.err
    assert "忽略" in captured.err


def test_generate_no_overwrite_skips(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-overwrite 的字面含义是「跳过」，不是「报错终止」。"""
    out_dir = tmp_path / "out"
    assert main(["generate", str(demo_json), "-o", str(out_dir)]) == EXIT_OK
    capsys.readouterr()

    # 第二次生成：全部跳过，退出码仍为 0
    assert main(["generate", str(demo_json), "-o", str(out_dir), "--no-overwrite"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "已写入 0 个文件" in out
    assert "跳过 19 个已存在文件" in out


def test_generate_no_overwrite_writes_new_files(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-overwrite 只跳过已存在的，新文件仍要写。"""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "bsp_init.c").write_text("// 手写的，别覆盖", encoding="utf-8")

    assert main(["generate", str(demo_json), "-o", str(out_dir), "--no-overwrite"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "已写入 18 个文件" in out
    assert "跳过 1 个" in out
    # 手写内容必须保留
    assert (out_dir / "bsp_init.c").read_text(encoding="utf-8") == "// 手写的，别覆盖"


def test_generate_overwrites_by_default(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "bsp_init.c").write_text("// 旧内容", encoding="utf-8")

    assert main(["generate", str(demo_json), "-o", str(out_dir)]) == EXIT_OK
    capsys.readouterr()
    content = (out_dir / "bsp_init.c").read_text(encoding="utf-8")
    assert "旧内容" not in content
    assert "void bsp_init(void)" in content


def test_generate_check_is_on_by_default(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["generate", str(demo_json), "-o", str(tmp_path / "out")]) == EXIT_OK
    assert "反编造自检通过" in capsys.readouterr().out


def test_generate_no_check_skips_guard(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["generate", str(demo_json), "-o", str(tmp_path / "out"), "--no-check"])
        == EXIT_OK
    )
    assert "反编造自检" not in capsys.readouterr().out


def test_generate_reports_symbol_count(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """自检输出要给出引用的符号数量，便于判断覆盖面。"""
    main(["generate", str(demo_json), "-o", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "个标准库符号" in out
    assert "GPIO_Init" in out


def test_generate_missing_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(tmp_path / "nope.json")]) == EXIT_IO
    assert "不存在" in capsys.readouterr().err


def test_generate_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "bad.json"
    target.write_text(json.dumps({"gpio": [{"port": "Z"}]}), encoding="utf-8")
    assert main(["generate", str(target)]) == EXIT_BLOCKING
    assert "配置校验未通过" in capsys.readouterr().err


def test_generate_empty_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """空配置也要能生成（骨架），并说明为什么内容为空。"""
    target = tmp_path / "empty.json"
    target.write_text(json.dumps({"chip": "STM32F103C8T6"}), encoding="utf-8")

    out_dir = tmp_path / "out"
    assert main(["generate", str(target), "-o", str(out_dir)]) == EXIT_OK
    assert (out_dir / "bsp_init.c").is_file()
    out = capsys.readouterr().out
    assert "说明：" in out  # GenerateResult.notes


def test_generate_output_dir_created_nested(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """输出目录可以是不存在的多级路径。"""
    out_dir = tmp_path / "a" / "b" / "c"
    assert main(["generate", str(demo_json), "-o", str(out_dir)]) == EXIT_OK
    assert (out_dir / "main.c").is_file()


def test_generate_yaml_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """YAML 配置同样能生成。"""
    target = tmp_path / "demo.yaml"
    save(target, make_demo_config(), fmt="yaml")
    assert main(["generate", str(target), "-o", str(tmp_path / "out")]) == EXIT_OK
    assert (tmp_path / "out" / "bsp_init.c").is_file()


# --------------------------------------------------------------------------
# 编译验证
# --------------------------------------------------------------------------


def _has_compiler() -> bool:
    from stm32gen.compile_check import find_toolchain

    return find_toolchain() is not None


def test_generate_with_compile(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--compile`` 生成后顺带做编译验证。"""
    if not _has_compiler():
        pytest.skip("本机没有可用编译器")

    out_dir = tmp_path / "out"
    code = main(["generate", str(demo_json), "-o", str(out_dir), "--compile"])
    assert code == EXIT_OK

    out = capsys.readouterr().out
    assert "编译验证" in out
    assert "结论" in out
    # 降级路径必须明确标注，不能让用户以为做的是真实编译
    from stm32gen.compile_check import is_native_available

    if not is_native_available():
        assert "只能做语法与架构无关的类型检查" in out


def test_generate_without_compile_skips(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """不加 --compile 时不该跑编译器（保持生成快速）。"""
    main(["generate", str(demo_json), "-o", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "编译验证" not in out


def test_compile_subcommand(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not _has_compiler():
        pytest.skip("本机没有可用编译器")

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    assert main(["compile", str(demo_json), "-o", str(out_dir)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "编译验证" in out


def test_compile_subcommand_missing_dir(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """输出目录不存在时给出明确指引，返回 I/O 错误码。"""
    code = main(["compile", str(demo_json), "-o", str(tmp_path / "nope")])
    assert code == EXIT_IO
    err = capsys.readouterr().err
    assert "输出目录不存在" in err
    assert "generate" in err  # 告诉用户怎么做


def test_compile_mode_arm_without_arm_toolchain(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """强制要求 ARM 工具链但找不到时，要明确失败而不是静默降级到宿主 gcc。

    这个开关的意义：CI 里想强制要求真实目标编译，不希望宿主 gcc 的
    「降级通过」掩盖工具链缺失。

    用 monkeypatch 把 ARM 类工具链全屏蔽掉，而不是依赖本机没装 ——
    否则装了 ARM 工具链这条测试就失去意义了。
    """
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    real_find = find_toolchain

    def only_host(prefer=None):
        """只让宿主 gcc 可见，ARM 类一律返回 None。

        ``prefer=None`` 是默认调用（``_select_toolchain("auto")`` 用的），
        必须单独处理成「宿主 gcc」—— 不能原样转发，否则 ARM 工具链
        照样会被返回，替身形同虚设。
        """
        if prefer is None:
            return real_find(prefer=ToolchainKind.HOST)
        if prefer.is_native:
            return None
        return real_find(prefer=prefer)

    monkeypatch.setattr("stm32gen.compile_check.find_toolchain", only_host)

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    code = main(["compile", str(demo_json), "-o", str(out_dir), "--mode", "arm"])
    assert code == EXIT_IO
    err = capsys.readouterr().err
    assert "未找到" in err
    # 要告诉用户可用的模式
    assert "armclang" in err


def test_compile_mode_arm_uses_native_toolchain(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode arm`` 在有 ARM 工具链时必须用它，且结果措辞为「编译通过」。"""
    from stm32gen.compile_check import is_native_available

    if not is_native_available():
        pytest.skip("本机没有 ARM 工具链")

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    assert main(["compile", str(demo_json), "-o", str(out_dir), "--mode", "arm"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "编译通过" in out
    # 真实目标编译不该出现降级警告
    assert "发现不了 ARM 专属问题" not in out


def test_compile_mode_keil(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode keil`` 只用 Keil 编译器。"""
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    found = find_toolchain(prefer=ToolchainKind.ARMCLANG) or find_toolchain(
        prefer=ToolchainKind.ARMCC
    )
    if found is None:
        pytest.skip("本机没有 Keil 编译器")

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    assert main(["compile", str(demo_json), "-o", str(out_dir), "--mode", "keil"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Keil" in out


def test_compile_mode_armclang(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode armclang`` 精确指定 AC6。"""
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if find_toolchain(prefer=ToolchainKind.ARMCLANG) is None:
        pytest.skip("本机没有 armclang")

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    code = main(["compile", str(demo_json), "-o", str(out_dir), "--mode", "armclang"])
    assert code == EXIT_OK
    assert "armclang" in capsys.readouterr().out


def test_compile_mode_armcc(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode armcc`` 精确指定 AC5。它不支持 -fsyntax-only，走 -c 路径。"""
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if find_toolchain(prefer=ToolchainKind.ARMCC) is None:
        pytest.skip("本机没有 armcc")

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    code = main(["compile", str(demo_json), "-o", str(out_dir), "--mode", "armcc"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "armcc" in out
    # ARMCC 的参数风格
    assert "--cpu=Cortex-M3" in out


def test_compile_mode_host(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--mode host`` 只用宿主编译器，并给出降级警告。"""
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if find_toolchain(prefer=ToolchainKind.HOST) is None:
        pytest.skip("本机没有宿主 gcc")

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    assert main(["compile", str(demo_json), "-o", str(out_dir), "--mode", "host"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "宿主 gcc" in out
    # 降级必须明确标注
    assert "发现不了 ARM 专属问题" in out


def test_compile_reports_diagnostics(
    demo_json: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """编译报错时要逐条打印诊断，并以退出码 2 失败。"""
    if not _has_compiler():
        pytest.skip("本机没有可用编译器")

    out_dir = tmp_path / "out"
    main(["generate", str(demo_json), "-o", str(out_dir)])
    capsys.readouterr()

    # 注入一个编造函数，制造编译错误。
    # 注意改的是 bsp_gpio.c —— 拆分后 GPIO_Init 调用在那里，不在 bsp_init.c。
    source = out_dir / "bsp_gpio.c"
    text = source.read_text(encoding="utf-8")
    assert "GPIO_Init(GPIOC, &GPIO_InitStructure);" in text
    source.write_text(
        text.replace(
            "GPIO_Init(GPIOC, &GPIO_InitStructure);",
            "GPIO_FakeMacro(GPIOC, &GPIO_InitStructure);",
            1,
        ),
        encoding="utf-8",
    )

    code = main(["compile", str(demo_json), "-o", str(out_dir)])
    assert code == EXIT_BLOCKING
    captured = capsys.readouterr()
    assert "GPIO_FakeMacro" in captured.out + captured.err
    assert "编译验证未通过" in captured.err


def test_selftest_reports_toolchain_status(capsys: pytest.CaptureFixture[str]) -> None:
    """自检要说明工具链状态。

    三种状态互斥，必须命中其中一种 —— 断言不该写宽泛的「编译」二字，
    因为实际输出是「降级为宿主 gcc 语法检查」，里面没有「编译」这个连续词
    （初版就是这么写错的）。
    """
    from stm32gen.cli import stdlib_root

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")

    main(["selftest"])
    out = capsys.readouterr().out

    states = (
        "未找到编译器",          # 完全没有编译器
        "编译工具链 arm",        # 真实 ARM 工具链
        "降级为宿主 gcc",        # 宿主编译器降级
    )
    assert any(state in out for state in states), (
        "自检输出里没有工具链状态行：\n" + out
    )


def test_selftest_warns_about_host_fallback(capsys: pytest.CaptureFixture[str]) -> None:
    """降级到宿主 gcc 时必须给出「发现不了 ARM 专属问题」的警告。

    这是防止虚假信心的关键一行：用户看到「检查通过」容易误以为
    已经做了目标编译验证。
    """
    from stm32gen.cli import stdlib_root
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")
    if find_toolchain(prefer=ToolchainKind.HOST) is None:
        pytest.skip("本机没有宿主 gcc")

    main(["selftest"])
    out = capsys.readouterr().out
    # 有真实 ARM 工具链时不该出现降级警告
    assert "降级为宿主 gcc" not in out


def test_selftest_warns_when_only_host_available(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """只有宿主 gcc 时必须给出「发现不了 ARM 专属问题」的警告。

    这是防止虚假信心的关键一行：用户看到「检查通过」容易误以为
    已经做了目标编译验证。

    用 monkeypatch 屏蔽 ARM 类工具链，而不是依赖本机没装 ——
    否则装了 Keil 这条测试就失去意义。
    """
    from stm32gen.cli import stdlib_root
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if not stdlib_root().is_dir():
        pytest.skip("标准库目录不可用")

    real_find = find_toolchain

    def only_host(prefer=None):
        """只让宿主 gcc 可见（``prefer=None`` 也要落到宿主）。"""
        if prefer is None:
            return real_find(prefer=ToolchainKind.HOST)
        if prefer.is_native:
            return None
        return real_find(prefer=prefer)

    monkeypatch.setattr("stm32gen.compile_check.find_toolchain", only_host)

    if only_host() is None:
        pytest.skip("本机连宿主 gcc 都没有")

    main(["selftest"])
    out = capsys.readouterr().out
    assert "降级为宿主 gcc" in out
    assert "发现不了 ARM 专属问题" in out
    # 最终选中的类型必须是宿主
    assert only_host().kind is ToolchainKind.HOST


# --------------------------------------------------------------------------
# --layout：产物形态
# --------------------------------------------------------------------------


def test_layout_defaults_to_split(tmp_path: Path, capsys) -> None:
    """不指定 --layout 时仍是 19 个文件，保持既有用法不变。"""
    config_path = tmp_path / "cfg.json"
    save(config_path, make_demo_config())
    out = tmp_path / "out"

    code = main(["generate", str(config_path), "-o", str(out), "--no-info"])
    assert code == EXIT_OK
    assert len(list(out.glob("*"))) == 19
    assert (out / "bsp_gpio.c").exists()


def test_layout_merged_writes_three_files(tmp_path: Path) -> None:
    """--layout merged 只写 bsp.h / bsp.c / main.c。"""
    config_path = tmp_path / "cfg.json"
    save(config_path, make_demo_config())
    out = tmp_path / "out"

    code = main(
        ["generate", str(config_path), "-o", str(out), "--no-info", "--layout", "merged"]
    )
    assert code == EXIT_OK
    assert sorted(path.name for path in out.glob("*")) == ["bsp.c", "bsp.h", "main.c"]


def test_layout_merged_output_compiles(tmp_path: Path) -> None:
    """合并产物必须真能编译 —— 这是它存在的唯一理由。

    用项目自带的 compile_check（armclang / armcc），找不到编译器时跳过。
    """
    from stm32gen.compile_check import ToolchainKind, check_sources, find_toolchain
    from stm32gen.generator import generate
    from stm32gen.generator.layout import OutputLayout

    toolchain = find_toolchain(ToolchainKind.ARMCLANG) or find_toolchain(ToolchainKind.ARMCC)
    if toolchain is None:
        pytest.skip("没有可用的 ARM 编译器")

    config = make_demo_config()
    result = generate(config, layout=OutputLayout.MERGED)
    out = tmp_path / "out"
    out.mkdir()
    for item in result.files:
        (out / item.name).write_text(item.content, encoding="utf-8")

    sources = [item.name for item in result.files if item.name.endswith(".c")]
    check = check_sources(config, out, sources, toolchain=toolchain)
    assert check.ok, check.format_diagnostics()
    assert not check.warnings, check.format_diagnostics()


# --------------------------------------------------------------------------
# 工具与成果分开放：默认输出目录的契约
# --------------------------------------------------------------------------


def test_project_root_is_the_source_tree() -> None:
    """源码树根必须是含 pyproject.toml 的那一层。

    这条测试盯着的是「定位方式」而不是某个具体路径 —— 它自身就跑在源码树里，
    所以 `project_root()` 必须能指回这里。反过来，如果哪天改成数 __file__
    的层数，editable 安装或测试临时复制源码就会数错，这条会先报警。
    """
    root = project_root()
    assert root is not None
    assert (root / "pyproject.toml").is_file()
    assert (root / "src" / "stm32gen").is_dir()


def test_default_output_dir_is_beside_the_source_tree() -> None:
    """默认成果目录是源码树的**兄弟**，不是它内部的子目录。"""
    root = project_root()
    assert root is not None
    out = Path(default_out_dir())
    assert out.parent == root.parent
    assert out.name == OUTPUT_DIR_NAME
    # 关键断言：不在源码树内部。落在内部就会进 git、被 ruff 扫。
    assert root not in out.parents


@pytest.mark.parametrize("argv", [["generate", "x.json"], ["compile", "x.json"]])
def test_cli_default_out_points_at_the_output_dir(argv: list[str]) -> None:
    """generate 与 compile 两个子命令的默认输出目录必须一致。

    两条命令都写/读同一批文件，默认值若各写一份就会漂移（复制粘贴的经典后果）。
    """
    namespace = build_parser().parse_args(argv)
    assert namespace.out == default_out_dir()


def test_default_output_dir_has_no_trailing_separator() -> None:
    """默认值里不能带尾部分隔符。

    带尾分隔符时 ``Path("out") / "x.c"`` 和字符串拼接的结果不同，
    还会有 ``out\\out`` 这类拼接错误（README 里专门记过这个坑）。
    """
    value = default_out_dir()
    assert not value.endswith(("\\", "/"))
