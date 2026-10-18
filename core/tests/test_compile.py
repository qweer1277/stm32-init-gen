"""编译验证的测试。

覆盖三层：

1. **诊断解析** —— 纯函数，输入编译器输出文本，输出结构化诊断
2. **命令构造** —— ARM 与宿主工具链的参数差异（这是关键正确性点）
3. **真实执行** —— 用本机可用编译器跑一遍生成的代码，并验证
   「注入错误能被抓到」

第 3 项依赖本机有编译器。没有编译器时相关测试会 ``skip``，
但**不会**用「没跑就算通过」蒙混过去 —— ``CompileResult.ok`` 要求
``ran`` 为真。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stm32gen.compile_check import (
    ARM_TARGET_FLAGS,
    STDLIB_SUBDIRS,
    CompileResult,
    Diagnostic,
    Severity,
    Toolchain,
    ToolchainKind,
    build_command,
    build_include_paths,
    check_sources,
    find_toolchain,
    is_available,
    is_native_available,
    parse_diagnostics,
)
from stm32gen.models import make_demo_config


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    found = find_toolchain()
    if found is None:
        pytest.skip("本机没有可用编译器")
    return found


@pytest.fixture(scope="module")
def generated_dir(tmp_path_factory) -> Path:
    """真的生成一份代码到临时目录（不是用仓库里的 out/）。"""
    from stm32gen.generator import generate

    directory = tmp_path_factory.mktemp("gen")
    generate(make_demo_config()).write_all(directory)
    return directory


# --------------------------------------------------------------------------
# 诊断解析
# --------------------------------------------------------------------------


SAMPLE_OUTPUT = """\
bsp_init.c:42:5: error: 'GPIO_FakeMacro' undeclared (first use in this function)
bsp_init.c:43:9: warning: unused variable 'tmp' [-Wunused-variable]
In file included from bsp_init.c:14:
stm32f10x.h:100:2: note: previous declaration was here
bsp_init.c:50:1: fatal error: stm32f10x_wrong.h: No such file or directory
gcc.exe: warning: '-mcpu=' is deprecated
some random line without a diagnostic
   |> 缩进的行
"""


def test_parse_diagnostics_counts() -> None:
    items = parse_diagnostics(SAMPLE_OUTPUT)
    assert len(items) == 4


def test_parse_diagnostics_fields() -> None:
    items = parse_diagnostics(SAMPLE_OUTPUT)
    first = items[0]
    assert first.severity is Severity.ERROR
    assert first.file == "bsp_init.c"
    assert first.line == 42
    assert first.column == 5
    assert "GPIO_FakeMacro" in first.message

    # 警告
    assert items[1].severity is Severity.WARNING
    assert items[1].line == 43

    # note
    assert items[2].severity is Severity.NOTE
    assert items[2].file == "stm32f10x.h"

    # fatal error 归为 ERROR
    assert items[3].severity is Severity.ERROR
    assert "No such file" in items[3].message


def test_parse_diagnostics_ignores_non_diagnostics() -> None:
    """命令行回显、缩进行、纯文本都不该被当成诊断。"""
    items = parse_diagnostics(SAMPLE_OUTPUT)
    assert all("some random line" not in item.message for item in items)
    assert all("deprecated" not in item.message for item in items)


def test_parse_diagnostics_without_column() -> None:
    """有些诊断不给列号，也要能解析。"""
    items = parse_diagnostics("main.c:10: error: expected ';'\n")
    assert len(items) == 1
    assert items[0].column == 0
    assert items[0].line == 10
    # 无列号时 location 只有行号
    assert items[0].location == "main.c:10"


def test_diagnostic_location_uses_basename() -> None:
    """location 只保留文件名，避免长路径撑爆界面。"""
    item = Diagnostic(
        file=r"D:\long\path\to\bsp_init.c",
        line=42,
        column=5,
        severity=Severity.ERROR,
        message="x",
    )
    assert item.location == "bsp_init.c:42:5"


def test_severity_blocking() -> None:
    assert Severity.ERROR.is_blocking
    assert not Severity.WARNING.is_blocking
    assert not Severity.NOTE.is_blocking


# --------------------------------------------------------------------------
# 工具链
# --------------------------------------------------------------------------


def test_toolchain_kind_native_flag() -> None:
    """``is_native`` 决定措辞 —— 宿主 gcc 的结果不能称为「编译通过」。"""
    assert ToolchainKind.ARMCLANG.is_native
    assert not ToolchainKind.HOST.is_native


def test_toolchain_labels() -> None:
    assert ToolchainKind.ARMCLANG.label.startswith("armclang")
    assert "宿主" in ToolchainKind.HOST.label


def test_find_toolchain_returns_or_none() -> None:
    found = find_toolchain()
    assert found is None or found.path
    if found is not None:
        assert Path(found.path).is_file()


def test_is_available_consistent() -> None:
    assert is_available() == (find_toolchain() is not None)
    assert is_native_available() == (
        find_toolchain() is not None and find_toolchain().kind.is_native
    )


def test_host_fallback_can_be_disabled(monkeypatch) -> None:
    """``STM32_NO_HOST_FALLBACK=1`` 时不该降级到宿主 gcc。

    这个开关的意义：CI 里想强制要求真实 ARM 工具链，不希望
    宿主 gcc 的「降级通过」掩盖工具链缺失。
    """
    monkeypatch.setenv("STM32_NO_HOST_FALLBACK", "1")
    monkeypatch.delenv("ARM_GCC", raising=False)
    found = find_toolchain()
    # 本机没装 ARM 工具链，所以应当返回 None
    if not is_native_available():
        assert found is None


# --------------------------------------------------------------------------
# 命令构造（关键正确性点）
# --------------------------------------------------------------------------


def test_arm_command_has_cpu_flags() -> None:
    """ARM 工具链必须带 CPU 目标参数。"""
    config = make_demo_config()
    tc = Toolchain(path="armclang", kind=ToolchainKind.ARMCLANG)
    cmd = build_command(tc, config, "/tmp/gen", ["bsp_init.c"])
    assert "-mcpu=cortex-m3" in cmd
    assert "-mthumb" in cmd
    # armclang 还需要 --target 才是完整的交叉编译目标
    assert "--target=arm-arm-none-eabi" in cmd


def test_arm_gcc_command_has_cpu_flags() -> None:
    """arm-none-eabi-gcc 也要带 CPU 参数，但不需要 --target。"""
    config = make_demo_config()
    tc = Toolchain(path="arm-none-eabi-gcc", kind=ToolchainKind.ARM_GCC)
    cmd = build_command(tc, config, "/tmp/gen", ["bsp_init.c"])
    assert "-mcpu=cortex-m3" in cmd
    assert "-mthumb" in cmd
    # --target 是 clang 系专有参数，gcc 不认
    assert "--target=arm-arm-none-eabi" not in cmd


def test_armcc_command_uses_its_own_style() -> None:
    """ARMCC (AC5) 的参数风格与 GCC 系完全不同。

    它用 ``--cpu=Cortex-M3`` 而不是 ``-mcpu=cortex-m3``；
    没有 ``-Wall``/``-Wextra``（实测 ``-Wall`` 会被当成 ``-all``
    报 ``C3900U: Unrecognized option``），严格诊断用 ``--strict``；
    也不支持 ``-fsyntax-only``，只能 ``-c`` 编译出目标文件。
    """
    config = make_demo_config()
    tc = Toolchain(path="armcc", kind=ToolchainKind.ARMCC)
    cmd = build_command(tc, config, "/tmp/gen", ["bsp_init.c"])

    assert "--cpu=Cortex-M3" in cmd
    assert "--c99" in cmd
    assert "--strict" in cmd
    assert "-c" in cmd

    # 不能出现 GCC 风格的参数
    assert "-mcpu=cortex-m3" not in cmd
    assert "-mthumb" not in cmd
    assert "-fsyntax-only" not in cmd
    assert "-Wall" not in cmd
    assert "-Wextra" not in cmd
    # ARMCC 需要显式指定目标文件输出位置
    assert any(arg.startswith("-o") for arg in cmd)


def test_host_command_has_no_arm_flags() -> None:
    """宿主 gcc **不能**带 ARM 专属参数。

    实测：``x86_64-w64-mingw32-gcc`` 收到 ``-mthumb`` 会直接报
    ``unrecognized command-line option '-mthumb'`` 并以退出码 1 失败。
    那不是「代码有问题」，是参数不适用 —— 必须区分开。
    """
    config = make_demo_config()
    tc = Toolchain(path="gcc", kind=ToolchainKind.HOST)
    cmd = build_command(tc, config, "/tmp/gen", ["bsp_init.c"])
    for flag in ARM_TARGET_FLAGS:
        assert flag not in cmd
    assert "--target=arm-arm-none-eabi" not in cmd


def test_command_has_required_defines() -> None:
    """三个编译宏缺一不可，否则 stm32f10x.h 会 #error。"""
    config = make_demo_config()
    tc = Toolchain(path="gcc", kind=ToolchainKind.HOST)
    cmd = build_command(tc, config, "/tmp/gen", ["bsp_init.c"])
    assert "-DSTM32F10X_MD" in cmd
    assert "-DUSE_STDPERIPH_DRIVER" in cmd
    assert "-DHSE_VALUE=8000000" in cmd


def test_command_defines_follow_config() -> None:
    """编译宏要跟着配置走（换 HSE 或换芯片）。"""
    from stm32gen.models import ChipId, Stm32Config

    tc = Toolchain(path="gcc", kind=ToolchainKind.HOST)
    c6 = Stm32Config(chip=ChipId.F103C6T6, hse_hz=12_000_000)
    cmd = build_command(tc, c6, "/tmp/gen", ["bsp_init.c"])
    assert "-DSTM32F10X_LD" in cmd
    assert "-DHSE_VALUE=12000000" in cmd


def test_command_has_warning_flags() -> None:
    """-Wall -Wextra 是 code-standards 的要求。"""
    config = make_demo_config()
    tc = Toolchain(path="gcc", kind=ToolchainKind.HOST)
    cmd = build_command(tc, config, "/tmp/gen", ["x.c"])
    assert "-Wall" in cmd
    assert "-Wextra" in cmd


def test_command_syntax_only_mode() -> None:
    """syntax_only 用 -fsyntax-only，不产生目标文件、不需要链接脚本。"""
    config = make_demo_config()
    tc = Toolchain(path="gcc", kind=ToolchainKind.HOST)
    assert "-fsyntax-only" in build_command(tc, config, "/tmp", ["a.c"])
    assert "-fsyntax-only" not in build_command(
        tc, config, "/tmp", ["a.c"], syntax_only=False
    )
    assert "-c" in build_command(tc, config, "/tmp", ["a.c"], syntax_only=False)


def test_command_source_dir_is_absolute() -> None:
    """源码目录必须用绝对路径。

    执行时 cwd 会切到源码目录，相对路径会变成 ``out/out`` 这种
    不存在的路径 —— gcc 只是忽略它而不报错，包含路径就静默失效了
    （初版写的就是相对路径，命令行里能看到 ``-Iout``）。
    """
    config = make_demo_config()
    tc = Toolchain(path="gcc", kind=ToolchainKind.HOST)
    cmd = build_command(tc, config, "out", ["x.c"])
    include_args = [arg for arg in cmd if arg.startswith("-I") and "out" in arg]
    assert include_args
    for arg in include_args:
        if arg.endswith("out"):
            assert Path(arg[2:]).is_absolute(), f"{arg} 不是绝对路径"


def test_build_include_paths() -> None:
    """包含路径要含 Start/Library/User 三层。"""
    from stm32gen import DEFAULT_STDLIB_ROOT

    root = Path(DEFAULT_STDLIB_ROOT)
    if not root.is_dir():
        pytest.skip(f"标准库目录不可用：{root}")

    paths = build_include_paths()
    assert len(paths) == len(STDLIB_SUBDIRS)
    for path, sub in zip(paths, STDLIB_SUBDIRS, strict=True):
        assert path.endswith(sub)
        assert Path(path).is_dir()


# --------------------------------------------------------------------------
# 真实执行
# --------------------------------------------------------------------------


def test_check_generated_code_passes(toolchain, generated_dir) -> None:
    """生成的代码应当通过编译验证（零错误零警告）。

    这是本步最关键的测试：它验证「生成的代码真的能编译」，
    而不只是大括号配平。
    """
    config = make_demo_config()
    result = check_sources(config, generated_dir)

    assert result.ran, result.execution_error
    assert result.files  # 收到了文件列表
    # 拆开断言，失败时能看出是错在错误还是警告
    assert not result.errors, "编译报错：\n" + result.format_diagnostics()
    assert not result.warnings, "-Wall -Wextra 有告警：\n" + result.format_diagnostics()
    assert result.ok


def test_check_detects_invented_function(toolchain, generated_dir) -> None:
    """编译器必须能抓到编造的库函数。

    这条与 ``test_generated_code_passes_anti_invention_guard``（文本层护栏）
    互补：护栏查「符号是否存在」，编译器查「用对没有」。
    两者都通过才说明生成代码真的可用。
    """
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        for item in generated_dir.iterdir():
            shutil.copy(item, target / item.name)

        source = target / "bsp_gpio.c"
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

        result = check_sources(make_demo_config(), target, ["bsp_gpio.c"])
        assert not result.ok
        assert result.errors
        assert any("GPIO_FakeMacro" in item.message for item in result.errors)


def test_check_detects_type_error(toolchain, generated_dir) -> None:
    """类型错误也要被抓到（说明不是只做了预处理）。"""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        for item in generated_dir.iterdir():
            shutil.copy(item, target / item.name)

        source = target / "bsp_gpio.c"
        text = source.read_text(encoding="utf-8")
        assert "GPIO_InitStructure.GPIO_Pin   = GPIO_Pin_13;" in text
        source.write_text(
            text.replace(
                "GPIO_InitStructure.GPIO_Pin   = GPIO_Pin_13;",
                'GPIO_InitStructure.GPIO_Pin   = "not an int";',
                1,
            ),
            encoding="utf-8",
        )

        result = check_sources(make_demo_config(), target, ["bsp_gpio.c"])
        assert not result.ok
        assert result.errors


def test_check_detects_warning(toolchain, generated_dir) -> None:
    """警告要与错误区分开：警告存在但整体仍算通过。"""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        for item in generated_dir.iterdir():
            shutil.copy(item, target / item.name)

        # 加一个未使用的变量，制造 -Wunused-variable
        source = target / "bsp_gpio.c"
        text = source.read_text(encoding="utf-8")
        source.write_text(
            text.replace(
                "void bsp_gpio_init(void)\n{",
                "void bsp_gpio_init(void)\n{\n    int unused_probe_var = 0;",
                1,
            ),
            encoding="utf-8",
        )

        result = check_sources(make_demo_config(), target, ["bsp_gpio.c"])
        assert result.warnings
        assert not result.errors
        # 有警告但无错误 → 整体仍算通过（返回码 0）
        assert result.ok


def test_check_no_c_files(tmp_path) -> None:
    """目录里没有 .c 文件时要明确报「执行错误」，而不是当成通过。"""
    result = check_sources(make_demo_config(), tmp_path)
    assert not result.ran
    assert not result.ok
    assert "没有 .c 文件" in result.execution_error


def test_check_missing_toolchain_reports_not_ran(monkeypatch, generated_dir) -> None:
    """编译器缺失是「没做」而不是「失败」。

    ``ok`` 必须为 False（不能凭没跑过就说通过），
    但 ``execution_error`` 为空，调用方靠 ``ran`` 区分。
    """
    monkeypatch.setattr("stm32gen.compile_check.find_toolchain", lambda: None)
    result = check_sources(make_demo_config(), generated_dir)
    assert not result.ran
    assert not result.ok
    assert result.execution_error == ""
    assert "不可用" in result.summary()


def test_compile_result_ok_requires_ran() -> None:
    """``ok`` 要求真的跑过编译器 —— 防止「不可用」被误读为成功。"""
    blank = CompileResult()
    assert not blank.ran
    assert not blank.ok

    failed = CompileResult(returncode=1)
    assert not failed.ok  # 没有 toolchain 也算没跑

    ran_ok = CompileResult(
        toolchain=Toolchain(path="gcc", kind=ToolchainKind.HOST), returncode=0
    )
    assert ran_ok.ok


def test_compile_result_summary_wording() -> None:
    """措辞必须区分真实编译与降级语法检查。"""
    arm = CompileResult(
        toolchain=Toolchain(path="arm-none-eabi-gcc", kind=ToolchainKind.ARMCLANG),
        returncode=0,
        files=("a.c",),
    )
    assert "编译通过" in arm.summary()
    assert "宿主" not in arm.summary()

    host = CompileResult(
        toolchain=Toolchain(path="gcc", kind=ToolchainKind.HOST),
        returncode=0,
        files=("a.c",),
    )
    assert "语法检查通过" in host.summary()
    # 绝不能把降级结果说成「编译通过」
    assert "宿主 gcc " in host.summary()


def test_compile_result_is_native() -> None:
    arm = CompileResult(toolchain=Toolchain(path="x", kind=ToolchainKind.ARMCLANG))
    host = CompileResult(toolchain=Toolchain(path="x", kind=ToolchainKind.HOST))
    none = CompileResult()
    assert arm.is_native
    assert not host.is_native
    assert not none.is_native


def test_format_diagnostics_truncates() -> None:
    result = CompileResult(
        diagnostics=[
            Diagnostic(file="a.c", line=i, column=0, severity=Severity.ERROR, message="x")
            for i in range(1, 30)
        ]
    )
    text = result.format_diagnostics(limit=5)
    assert "另有" in text
    assert text.count("\n") == 5  # 5 条 + 截断提示


def test_command_line_join() -> None:
    result = CompileResult(command=("gcc", "-Wall", "a.c"))
    assert result.command_line() == "gcc -Wall a.c"


# --------------------------------------------------------------------------
# 集成：所有示例配置都能编译
# --------------------------------------------------------------------------


def _empty_config():
    """一个什么都不配的配置，用于验证空骨架也能编译。"""
    from stm32gen.models import Stm32Config

    return Stm32Config()


@pytest.mark.parametrize(
    "config_factory",
    [
        pytest.param(lambda: make_demo_config(), id="demo"),
        pytest.param(_empty_config, id="empty"),
    ],
)
def test_example_configs_compile(toolchain, config_factory, tmp_path) -> None:
    """不同配置生成的代码都要能编译。"""
    from stm32gen.generator import generate

    config = config_factory()
    target = tmp_path / "gen"
    generate(config).write_all(target)

    result = check_sources(config, target)
    assert result.ran, result.execution_error
    assert not result.errors, result.format_diagnostics()


def test_multi_usart_config_compiles(toolchain, tmp_path) -> None:
    """多 USART + 多中断 + 重映射的组合也要能编译。"""
    from stm32gen.generator import generate
    from stm32gen.models import NvicChannelConfig, NvicConfig, Stm32Config, UsartConfig

    config = Stm32Config(
        usart=[
            UsartConfig(
                instance="USART1",
                baudrate=9600,
                remap="USART1",
                interrupts=["RXNE", "IDLE", "PE"],
            ),
            UsartConfig(instance="USART3", baudrate=57600, direction="TX", interrupts=["TC"]),
        ],
        nvic=NvicConfig(
            priority_group="NVIC_PriorityGroup_3",
            channels=[
                NvicChannelConfig(irq_channel="USART1_IRQn", preemption_priority=1),
                NvicChannelConfig(irq_channel="USART3_IRQn", preemption_priority=2),
            ],
        ),
    )
    target = tmp_path / "gen"
    generate(config).write_all(target)

    result = check_sources(config, target)
    assert result.ran, result.execution_error
    assert not result.errors, result.format_diagnostics()
    assert not result.warnings, result.format_diagnostics()
