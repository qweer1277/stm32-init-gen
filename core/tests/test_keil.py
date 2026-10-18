"""Keil 工具链的专项测试（AC5 与 AC6 差异）。

这两套编译器的参数风格、诊断格式、能力边界都不同，混用会出问题 ——
本文件把差异逐条固定下来。所有期望值均来自**实测**（本机 Keil MDK
Professional 5.40，装在 ``E:\\download\\keil5``），不是从文档抄的。

若本机没有 Keil，相关测试会跳过；但**参数构造类的测试不依赖安装**，
它们用构造出来的 ``Toolchain`` 对象验证生成逻辑。
"""

from __future__ import annotations

import pytest

from stm32gen.compile_check import (
    ARMCC_WARNING_FLAGS,
    GCC_WARNING_FLAGS,
    Severity,
    Toolchain,
    ToolchainKind,
    build_command,
    find_toolchain,
    parse_diagnostics,
)
from stm32gen.models import make_demo_config


@pytest.fixture(scope="module")
def armclang() -> Toolchain:
    found = find_toolchain(prefer=ToolchainKind.ARMCLANG)
    if found is None:
        pytest.skip("本机没有 armclang (Keil AC6)")
    return found


@pytest.fixture(scope="module")
def armcc() -> Toolchain:
    found = find_toolchain(prefer=ToolchainKind.ARMCC)
    if found is None:
        pytest.skip("本机没有 armcc (Keil AC5)")
    return found


# --------------------------------------------------------------------------
# 枚举语义
# --------------------------------------------------------------------------


def test_keil_kinds_are_native() -> None:
    """两套 Keil 编译器都是真实目标编译器，结果可称「编译通过」。"""
    assert ToolchainKind.ARMCLANG.is_native
    assert ToolchainKind.ARMCC.is_native
    assert not ToolchainKind.HOST.is_native


def test_keil_kinds_flagged() -> None:
    assert ToolchainKind.ARMCLANG.is_keil
    assert ToolchainKind.ARMCC.is_keil
    assert not ToolchainKind.ARM_GCC.is_keil
    assert not ToolchainKind.HOST.is_keil


def test_parameter_style_split() -> None:
    """参数风格的分野：只有 ARMCC 是专有风格。"""
    assert ToolchainKind.ARMCLANG.uses_gcc_syntax
    assert ToolchainKind.ARM_GCC.uses_gcc_syntax
    assert ToolchainKind.HOST.uses_gcc_syntax
    assert not ToolchainKind.ARMCC.uses_gcc_syntax


def test_syntax_only_support() -> None:
    """只有 ARMCC 不支持 -fsyntax-only。"""
    assert ToolchainKind.ARMCLANG.supports_syntax_only
    assert ToolchainKind.ARM_GCC.supports_syntax_only
    assert ToolchainKind.HOST.supports_syntax_only
    assert not ToolchainKind.ARMCC.supports_syntax_only


def test_labels_mention_keil() -> None:
    assert "Keil" in ToolchainKind.ARMCLANG.label
    assert "Keil" in ToolchainKind.ARMCC.label
    assert "AC6" in ToolchainKind.ARMCLANG.label or "6" in ToolchainKind.ARMCLANG.label
    assert "AC5" in ToolchainKind.ARMCC.label or "5" in ToolchainKind.ARMCC.label


# --------------------------------------------------------------------------
# 命令构造（不依赖本机装了 Keil）
# --------------------------------------------------------------------------


def test_armclang_command_full_shape() -> None:
    """armclang 需要 --target 才是完整的交叉编译目标。"""
    tc = Toolchain(path="armclang", kind=ToolchainKind.ARMCLANG)
    cmd = build_command(tc, make_demo_config(), "/tmp/gen", ["a.c"])

    assert "--target=arm-arm-none-eabi" in cmd
    assert "-mcpu=cortex-m3" in cmd
    assert "-mthumb" in cmd
    assert "-fsyntax-only" in cmd
    for flag in GCC_WARNING_FLAGS:
        assert flag in cmd


def test_armcc_command_full_shape() -> None:
    """ARMCC 用 --cpu= 而不是 -mcpu=，用 --strict 而不是 -Wall/-Wextra。"""
    tc = Toolchain(path="armcc", kind=ToolchainKind.ARMCC)
    cmd = build_command(tc, make_demo_config(), "/tmp/gen", ["a.c"])

    assert "--cpu=Cortex-M3" in cmd
    assert "--c99" in cmd
    for flag in ARMCC_WARNING_FLAGS:
        assert flag in cmd

    # 不该出现 GCC 风格参数
    assert "-mcpu=cortex-m3" not in cmd
    assert "-mthumb" not in cmd
    assert "-fsyntax-only" not in cmd
    assert "-Wall" not in cmd
    assert "-Wextra" not in cmd
    assert "--target=arm-arm-none-eabi" not in cmd


def test_armcc_always_compiles_objects() -> None:
    """ARMCC 不支持 -fsyntax-only，所以即使请求 syntax_only 也要 -c。"""
    tc = Toolchain(path="armcc", kind=ToolchainKind.ARMCC)
    cmd = build_command(
        tc, make_demo_config(), "/tmp/gen", ["a.c"], syntax_only=True
    )
    assert "-c" in cmd
    assert "-fsyntax-only" not in cmd
    # 必须显式指定目标文件输出位置，否则会污染源码目录
    assert any(arg.startswith("-o") for arg in cmd)


def test_armcc_object_output_goes_to_object_dir() -> None:
    """ARMCC 的目标文件要写到指定的临时目录。"""
    tc = Toolchain(path="armcc", kind=ToolchainKind.ARMCC)
    cmd = build_command(
        tc, make_demo_config(), "/tmp/gen", ["a.c"], object_dir="/tmp/objs"
    )
    out_args = [arg for arg in cmd if arg.startswith("-o")]
    assert len(out_args) == 1
    assert "objs" in out_args[0]


def test_both_keil_compilers_get_defines_and_includes() -> None:
    """两套编译器都要拿到编译宏与包含路径 —— 否则头文件根本打不开。"""
    for kind in (ToolchainKind.ARMCLANG, ToolchainKind.ARMCC):
        tc = Toolchain(path="x", kind=kind)
        cmd = build_command(tc, make_demo_config(), "/tmp/gen", ["a.c"])
        assert "-DSTM32F10X_MD" in cmd
        assert "-DUSE_STDPERIPH_DRIVER" in cmd
        assert "-DHSE_VALUE=8000000" in cmd
        # -I 参数（两套写法一致）
        assert any(arg.startswith("-I") for arg in cmd)


# --------------------------------------------------------------------------
# 诊断格式（不依赖本机装了 Keil）
# --------------------------------------------------------------------------


# 实测的 ARMCC (AC5) 输出格式
AC5_OUTPUT = """\
"bsp_init.c", line 42: Error: #35: expected a ";"
"bsp_init.c", line 50: Warning: #223-D: function "foo" declared implicitly
"bsp_init.c", line 55: Info: #177-D: variable "x" was declared but never referenced
Error: L6218E: Undefined symbol GPIO_Init (referred from bsp_init.o).
0 Errors, 1 Warning
"""


def test_parse_armcc_diagnostics() -> None:
    """ARMCC 的诊断格式是 ``"file", line N: Severity: #code: msg``。

    与 GCC 的 ``file:N:C: severity: msg`` 完全不同，必须两套都认。
    """
    items = parse_diagnostics(AC5_OUTPUT)
    assert len(items) == 4  # 3 条带位置的 + 1 条链接错误

    first = items[0]
    assert first.severity is Severity.ERROR
    assert first.file == "bsp_init.c"
    assert first.line == 42
    assert "expected" in first.message
    # ARMCC 不给列号
    assert first.column == 0

    assert items[1].severity is Severity.WARNING
    assert items[1].line == 50
    assert items[2].severity is Severity.NOTE
    assert items[2].line == 55


def test_parse_armcc_strips_error_code() -> None:
    """``#35:`` 这类错误码要从消息里剥掉，否则显示很啰嗦。"""
    items = parse_diagnostics(AC5_OUTPUT)
    assert not items[0].message.startswith("#")
    assert "expected" in items[0].message


def test_parse_armcc_link_error() -> None:
    """没有文件位置的链接器错误也要抓到。

    实测：ARMCC 不加 ``-c`` 时会自动链接，报
    ``Error: L6218E: Undefined symbol GPIO_Init``。那条消息说明编译本身
    过了、失败在链接阶段 —— 正是「只编译不链接」要避免的。
    """
    items = parse_diagnostics(AC5_OUTPUT)
    link_errors = [item for item in items if not item.file]
    assert len(link_errors) == 1
    assert "L6218E" in link_errors[0].message
    assert "GPIO_Init" in link_errors[0].message
    assert link_errors[0].location == "<无位置>"


def test_parse_armcc_skips_summary_line() -> None:
    """``0 Errors, 1 Warning`` 这类总结行不该被当成诊断。"""
    items = parse_diagnostics(AC5_OUTPUT)
    assert all("0 Errors" not in item.message for item in items)


def test_parse_gcc_style_still_works() -> None:
    """加了 ARMCC 支持后，GCC/Clang 格式不能被破坏。"""
    gcc_output = "bsp_init.c:42:5: error: 'x' undeclared\nbsp_init.c:43:1: warning: unused\n"
    items = parse_diagnostics(gcc_output)
    assert len(items) == 2
    assert items[0].column == 5
    assert items[1].severity.value == "warning"


def test_parse_armclang_style() -> None:
    """armclang 是 Clang，格式与 GCC 一致。"""
    clang_output = (
        "bsp_init.c:53:5: error: call to undeclared function 'GPIO_FakeMacro'; "
        "ISO C99 and later do not support implicit function declarations "
        "[-Wimplicit-function-declaration]\n"
        "   53 |     GPIO_FakeMacro(GPIOC, &GPIO_InitStructure);\n"
        "      |     ^\n"
        "1 error generated.\n"
    )
    items = parse_diagnostics(clang_output)
    assert len(items) == 1
    assert items[0].line == 53
    assert items[0].column == 5
    assert "GPIO_FakeMacro" in items[0].message
    # 源码引用行与总结行不该被当成诊断
    assert all("error generated" not in item.message for item in items)


# --------------------------------------------------------------------------
# 真实执行（依赖本机装了 Keil）
# --------------------------------------------------------------------------


def _generate_to(tmp_path) -> object:
    from stm32gen.generator import generate

    directory = tmp_path / "gen"
    generate(make_demo_config()).write_all(directory)
    return directory


def test_armclang_compiles_generated_code(armclang, tmp_path) -> None:
    """armclang（AC6）能把生成的代码编译通过，且零告警。"""
    from stm32gen.compile_check import check_sources

    directory = _generate_to(tmp_path)
    result = check_sources(make_demo_config(), directory, toolchain=armclang)

    assert result.ran, result.execution_error
    assert result.is_native
    assert not result.errors, result.format_diagnostics()
    assert not result.warnings, result.format_diagnostics()
    assert result.ok


def test_armcc_compiles_generated_code(armcc, tmp_path) -> None:
    """armcc（AC5）能把生成的代码编译通过。

    AC5 走的是 ``-c`` 路径（不支持 ``-fsyntax-only``），
    所以这条同时验证「目标文件写到临时目录、不污染源码目录」。
    """
    from stm32gen.compile_check import check_sources

    directory = _generate_to(tmp_path)
    result = check_sources(make_demo_config(), directory, toolchain=armcc)

    assert result.ran, result.execution_error
    assert result.is_native
    assert not result.errors, result.format_diagnostics()
    assert result.ok

    # 编译器在临时目录里产出的 .o 不该留在源码目录
    leftovers = list(directory.glob("*.o"))
    assert not leftovers, f"目标文件污染了源码目录：{leftovers}"


def test_armcc_version_probe_works(armcc) -> None:
    """ARMCC 不认 ``--version``，必须直接用无参调用取版本。

    实测 ``armcc --version`` 报 ``C3900U: Unrecognized option '--version'``。
    """
    assert armcc.version
    assert "Arm Compiler" in armcc.version or "MDK" in armcc.version


def test_armclang_version_probe_works(armclang) -> None:
    assert armclang.version
    assert "MDK" in armclang.version or "Arm Compiler" in armclang.version


@pytest.mark.parametrize("kind", [ToolchainKind.ARMCLANG, ToolchainKind.ARMCC])
def test_both_keil_compilers_detect_invented_symbol(kind, tmp_path) -> None:
    """两套 Keil 编译器都要能抓到编造的函数名。

    这条是编译验证价值的核心证据：如果编译只是走过场，这里会失败。
    """
    from stm32gen.compile_check import check_sources

    toolchain = find_toolchain(prefer=kind)
    if toolchain is None:
        pytest.skip(f"本机没有 {kind.value}")

    directory = _generate_to(tmp_path)
    source = directory / "bsp_gpio.c"
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

    result = check_sources(
        make_demo_config(), directory, ["bsp_gpio.c"], toolchain=toolchain
    )
    assert not result.ok
    assert result.errors
    assert any("GPIO_FakeMacro" in item.message for item in result.errors)


@pytest.mark.parametrize("kind", [ToolchainKind.ARMCLANG, ToolchainKind.ARMCC])
def test_both_keil_compilers_detect_type_error(kind, tmp_path) -> None:
    """两套编译器都要能抓到类型错误（说明不是只做了预处理）。"""
    from stm32gen.compile_check import check_sources

    toolchain = find_toolchain(prefer=kind)
    if toolchain is None:
        pytest.skip(f"本机没有 {kind.value}")

    directory = _generate_to(tmp_path)
    source = directory / "bsp_gpio.c"
    text = source.read_text(encoding="utf-8")
    source.write_text(
        text.replace(
            "GPIO_InitStructure.GPIO_Pin   = GPIO_Pin_13;",
            'GPIO_InitStructure.GPIO_Pin   = "not an int";',
            1,
        ),
        encoding="utf-8",
    )

    result = check_sources(
        make_demo_config(), directory, ["bsp_gpio.c"], toolchain=toolchain
    )
    assert not result.ok
    assert result.errors


def test_keil_found_in_install_dir_not_path() -> None:
    """Keil 检测必须能绕过 PATH，直接搜安装目录。

    实测背景：Keil 装好后只把路径加到**持久化的**用户 PATH，
    已经在跑的进程（包括本工具）看不到 —— 只查 ``shutil.which`` 会漏掉它。

    这条测试验证至少有一套 Keil 能被找到（无论走 PATH 还是安装目录）。
    """
    found = find_toolchain(prefer=ToolchainKind.ARMCLANG) or find_toolchain(
        prefer=ToolchainKind.ARMCC
    )
    if found is None:
        pytest.skip("本机没有 Keil")
    from pathlib import Path

    assert Path(found.path).is_file()
    assert "keil" in str(found.path).lower() or "arm" in str(found.path).lower()


def test_keil_root_env_override(monkeypatch, armclang) -> None:
    """``KEIL_ROOT`` 环境变量可以显式指定安装根目录。

    从编译器路径反推根目录设进去，验证这条路可走通。
    """
    from pathlib import Path

    # E:\download\keil5\ARM\ARMCLANG\Bin\armclang.exe → E:\download\keil5
    path = Path(armclang.path)
    root = path.parents[3]
    monkeypatch.setenv("KEIL_ROOT", str(root))

    found = find_toolchain(prefer=ToolchainKind.ARMCLANG)
    assert found is not None
    assert Path(found.path).is_file()


def test_armcc_severity_mapping() -> None:
    """ARMCC 的四级严重度要正确映射到我们的三级。"""
    items = parse_diagnostics(AC5_OUTPUT)
    assert items[0].severity is Severity.ERROR
    assert items[1].severity is Severity.WARNING
    # Info 映射为 NOTE
    assert items[2].severity is Severity.NOTE
