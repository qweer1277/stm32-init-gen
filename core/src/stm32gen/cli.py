"""命令行入口。

子命令
------
- ``selftest``  环境自检：Python 版本、依赖、标准库目录、符号表、模型层
- ``load``      读取并校验配置文件，打印摘要
- ``validate``  跑校验层，逐条打印问题
- ``generate``  生成代码（含反编造自检），可写盘或打印到 stdout

退出码约定（便于 CI 当门禁）
----------------------------
- ``0``  成功
- ``1``  文件不存在 / 格式错误 / I/O 失败
- ``2``  配置校验有阻断性错误，或生成代码里发现编造符号

``generate`` 默认先跑校验层：存在阻断性错误时**拒绝生成**，因为那些错误
（引脚冲突、NVIC 越界）必然产出不能用的代码。确需强行生成用 ``--force``。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stm32gen import (
    OUTPUT_DIR_NAME,
    __version__,
    configured_stdlib_root,
    default_out_dir,
)
from stm32gen.console import force_utf8_stdio
from stm32gen.models import Stm32Config

# 生成器要求的最低 Python 版本
MIN_PYTHON = (3, 10)

# 退出码
EXIT_OK = 0
EXIT_IO = 1
EXIT_BLOCKING = 2


def stdlib_root() -> Path:
    """返回标准库代码根目录：环境变量优先，未设置时给一个明确的引导错误。

    为什么不在缺环境变量时退回某个默认路径：默认值只对开发者的机器成立，
    换台机器就指向不存在的目录，而错误要等到编译验证阶段才以「找不到
    stm32f10x.h」的形式冒出来 —— 那时已经很难联想到是环境变量没设。
    在这里直接失败，错误信息里带着该怎么修。
    """
    root = configured_stdlib_root()
    if root is None:
        raise RuntimeError(
            "未设置标准库代码根目录。请设置环境变量 STM32_STDLIB_ROOT 指向\n"
            "STM32F10x 标准外设库的根目录（其中应包含 Start/ 与 Library/ 两个\n"
            "子目录），例如：\n"
            r"    PowerShell:  $env:STM32_STDLIB_ROOT = 'D:\path\to\STM32F10x_StdPeriph_Lib'"
            "\n"
            r"    cmd:         set STM32_STDLIB_ROOT=D:\path\to\STM32F10x_StdPeriph_Lib"
            "\n"
            "该目录仅用于编译验证与符号表提取，生成代码本身不需要它。"
        )
    return root


# --------------------------------------------------------------------------
# 公共辅助
# --------------------------------------------------------------------------


def _load_config(path: str) -> tuple[Stm32Config | None, int]:
    """读取配置，失败时打印原因并返回退出码。

    三种失败分开处理：文件/格式问题（EXIT_IO）与模型校验问题（EXIT_BLOCKING）
    对用户的意义不同，退出码也应当不同。
    """
    from pydantic import ValidationError

    from stm32gen.serializers import ConfigFormatError, load

    try:
        return load(path), EXIT_OK
    except (ConfigFormatError, FileNotFoundError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return None, EXIT_IO
    except ValidationError as exc:
        print(f"[FAIL] 配置校验未通过（{exc.error_count()} 处问题）：", file=sys.stderr)
        for error_item in exc.errors():
            location = ".".join(str(item) for item in error_item["loc"]) or "<根>"
            print(f"       {location}: {error_item['msg']}", file=sys.stderr)
        return None, EXIT_BLOCKING


def _print_issues(issues, *, show_info: bool) -> None:
    """逐条打印校验结果。"""
    for item in issues:
        if not show_info and item.severity.value == "info":
            continue
        print(f"  {item.format()}")


def _report_symbols(names: set[str], limit: int = 12) -> str:
    """把符号集合格式化成一行摘要（过长时截断）。"""
    ordered = sorted(names)
    if len(ordered) <= limit:
        return ", ".join(ordered)
    head = ", ".join(ordered[:limit])
    return f"{head} … 等 {len(ordered)} 个"


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------


def cmd_selftest(_args: argparse.Namespace) -> int:
    """环境自检。返回 0 表示全部通过，1 表示有硬性缺失。"""
    ok = True

    print(f"Python      : {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < MIN_PYTHON:
        print(f"  [FAIL] 需要 Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")
        ok = False
    else:
        print("  [ OK ] 版本满足要求")

    # 只列核心库真正依赖的三个。PySide6 不在这里 —— 它是桌面界面那个项目的
    # 依赖，装不装都不影响本包能干什么；检查它会让「只装了核心」的环境
    # 自检失败，那恰恰是要支持的用法。
    for module_name, pip_name in (
        ("pydantic", "pydantic>=2.5"),
        ("jinja2", "Jinja2>=3.1"),
        ("yaml", "PyYAML>=6.0"),
    ):
        try:
            module = __import__(module_name)
        except ImportError:
            print(f'  [FAIL] 缺少依赖 {pip_name} -> python -m pip install "{pip_name}"')
            ok = False
        else:
            version = getattr(module, "__version__", "?")
            print(f"  [ OK ] {module_name} {version}")

    # 标准库目录是可选的：只有编译验证与符号提取需要它。缺了照常报告，
    # 但不算致命 —— 生成代码本身不依赖它。
    try:
        root = stdlib_root()
    except RuntimeError as exc:
        print(f"  [ -- ] 标准库目录未配置（不影响生成）：{exc}")
    else:
        if root.is_dir():
            print(f"  [ OK ] 标准库目录 {root}")
        else:
            print(f"  [FAIL] 找不到标准库目录 {root}")
            print("         请设置环境变量 STM32_STDLIB_ROOT 指向你的标准库代码目录")
            ok = False

    # 符号表：模型层与校验层的事实来源
    try:
        from stm32gen.stdlib_ref import DEFAULT_SYMBOLS_PATH, default_index
    except Exception as exc:
        print(f"  [FAIL] 符号索引层加载失败：{exc}")
        ok = False
    else:
        if not DEFAULT_SYMBOLS_PATH.is_file():
            print(f"  [FAIL] 缺少符号表 {DEFAULT_SYMBOLS_PATH}")
            print("         请运行：python tools\\extract_symbols.py")
            ok = False
        else:
            index = default_index()
            stats = index.stats
            size_kb = DEFAULT_SYMBOLS_PATH.stat().st_size / 1024
            print(
                f"  [ OK ] 符号表 {DEFAULT_SYMBOLS_PATH.name}（{size_kb:.0f} KB）"
                f" 宏 {stats['macros']} / 枚举 {stats['enum_values']}"
                f" / 函数 {stats['functions']} / 类型 {stats['types']}"
            )

    # 模型层：导入即校验枚举引用的符号
    try:
        from stm32gen.models import make_demo_config
    except Exception as exc:
        print(f"  [FAIL] 配置模型层加载失败：{exc}")
        ok = False
    else:
        config = make_demo_config()
        print(f"  [ OK ] 配置模型层（示例配置：{config.chip.value}，{len(config.gpio)} 引脚）")

    # 生成器：能生成且通过反编造自检，才说明整条链路是通的
    try:
        from stm32gen.generator import generate
        from stm32gen.stdlib_ref import default_index
    except Exception as exc:
        print(f"  [FAIL] 生成器层加载失败：{exc}")
        ok = False
    else:
        try:
            result = generate(make_demo_config())
        except Exception as exc:
            print(f"  [FAIL] 生成器执行失败：{exc}")
            ok = False
        else:
            index = default_index()
            from stm32gen.stdlib_ref import find_invented

            invented = []
            for item in result.files:
                invented.extend(find_invented(item.content, index, item.name))
            if invented:
                print(f"  [FAIL] 生成代码含 {len(invented)} 个编造符号")
                for problem in invented[:5]:
                    print(f"         {problem.format()}")
                ok = False
            else:
                print(
                    f"  [ OK ] 生成器层（示例生成 {len(result.files)} 文件"
                    f" / {result.total_lines} 行，反编造自检通过）"
                )

    # GUI 不在这里自检：自 2026-02 起桌面界面是独立项目（stm32-init-gen-gui），
    # 核心库不该为了自检去导入 PySide6 —— 那会让「只装了核心」的环境自检失败。
    # GUI 侧自带 ``python -m stm32gen_gui --selfcheck``。

    # 编译工具链（可选，不影响自检结论）
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    toolchain = find_toolchain()
    if toolchain is None:
        print("  [ -- ] 未找到编译器，编译验证不可用（可选）")
        print("         装 Keil MDK 或 arm-none-eabi-gcc 可做真实目标编译，")
        print("         或设 HOST_CC 指定宿主编译器做降级语法检查")
    elif toolchain.kind.is_native:
        # 真实目标编译。顺带列出其它可用的 ARM 工具链，便于切换。
        print(f"  [ OK ] 编译工具链 {toolchain.label}")
        print(f"         {toolchain.path}")
        print(f"         {toolchain.version}")
        alternatives = []
        for kind in (
            ToolchainKind.ARMCLANG,
            ToolchainKind.ARMCC,
            ToolchainKind.ARM_GCC,
        ):
            if kind is toolchain.kind:
                continue
            other = find_toolchain(prefer=kind)
            if other is not None:
                alternatives.append(other.label)
        if alternatives:
            print(f"         其它可用：{'、'.join(alternatives)}")
    else:
        # 降级路径要明确标注，不能让用户以为做的是真实编译
        print("  [ -- ] 无 ARM 工具链，降级为宿主 gcc 语法检查")
        print(f"         {toolchain.path}")
        print(f"         {toolchain.version}")
        print("         注意：宿主 gcc 发现不了 ARM 专属问题")

    return EXIT_OK if ok else EXIT_IO


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------


def cmd_load(args: argparse.Namespace) -> int:
    """读取配置文件，校验通过后打印摘要。"""
    config, code = _load_config(args.config)
    if config is None:
        return code

    print(f"配置文件  : {args.config}")
    print(config.summary())
    print(f"编译宏    : {' '.join('-D' + flag for flag in config.c_flags)}")
    return EXIT_OK


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    """跑校验层，逐条打印问题。"""
    from stm32gen.validate import has_blocking, summarize, validate

    config, code = _load_config(args.config)
    if config is None:
        return code

    issues = validate(config, include_info=not args.no_info)
    print(f"配置文件  : {args.config}")
    print(config.summary())
    print(f"校验结果  : {summarize(issues)}")
    if issues:
        print()
        _print_issues(issues, show_info=not args.no_info)
        print()

    if has_blocking(issues):
        blocking = [item for item in issues if item.severity.is_blocking]
        print(
            f"[FAIL] 存在 {len(blocking)} 个阻断性问题，无法生成代码",
            file=sys.stderr,
        )
        return EXIT_BLOCKING

    print("[ OK ] 校验通过")
    return EXIT_OK


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    """生成代码。

    流程：读配置 → 校验（阻断则拒绝）→ 生成 → 反编造自检 → 输出。
    反编造自检**默认开启**：它是本项目「禁止编造」约束的最后一道防线，
    关掉它等于放弃核心保证。
    """
    from stm32gen.generator import generate
    from stm32gen.generator.layout import OutputLayout
    from stm32gen.stdlib_ref import default_index, find_invented
    from stm32gen.validate import has_blocking, summarize, validate

    config, code = _load_config(args.config)
    if config is None:
        return code

    print(f"配置文件  : {args.config}")
    print(config.summary())
    print()

    # 1) 校验层
    issues = validate(config, include_info=not args.no_info)
    print(f"校验结果  : {summarize(issues)}")
    if issues:
        _print_issues(issues, show_info=not args.no_info)
    print()

    if has_blocking(issues) and not args.force:
        blocking = [item for item in issues if item.severity.is_blocking]
        print(
            f"[FAIL] 存在 {len(blocking)} 个阻断性错误，已拒绝生成。\n"
            "       这些错误必然产出不能用的代码，请先修复。\n"
            "       确需强行生成请加 --force（不推荐）。",
            file=sys.stderr,
        )
        return EXIT_BLOCKING

    if has_blocking(issues) and args.force:
        blocking = [item for item in issues if item.severity.is_blocking]
        print(
            f"[WARN] --force 已指定：忽略 {len(blocking)} 个阻断性错误继续生成",
            file=sys.stderr,
        )

    # 2) 生成
    result = generate(config, layout=OutputLayout(args.layout))
    print(result.summary())
    print()

    # 3) 反编造自检（默认开启）
    if not args.no_check:
        index = default_index()
        invented = []
        for item in result.files:
            invented.extend(find_invented(item.content, index, item.name))

        if invented:
            print(f"[FAIL] 生成代码含 {len(invented)} 个编造符号：", file=sys.stderr)
            for problem in invented:
                print(f"       {problem.format()}", file=sys.stderr)
            print(
                "       这是工具缺陷：模板或上下文引用了标准库里不存在的符号。\n"
                "       请核对 E:\\stm32初始化\\代码 下的头文件。",
                file=sys.stderr,
            )
            return EXIT_BLOCKING

        print(
            f"[ OK ] 反编造自检通过"
            f"（引用 {len(result.symbols_used)} 个标准库符号："
            f"{_report_symbols(result.symbols_used)}）"
        )

    # 4) 输出
    if args.print or args.dry_run:
        for item in result.files:
            print()
            print("=" * 78)
            print(f"// {item.name}  — {item.description}")
            print("=" * 78)
            # end="" 避免在文件内容末尾再加一个换行
            print(item.content, end="")

    if args.dry_run:
        print()
        print("[ OK ] --dry-run：未写入任何文件")
        return EXIT_OK

    try:
        written, skipped = result.write_all(
            args.out, skip_existing=args.no_overwrite
        )
    except FileExistsError as exc:
        # 只有 skip_existing=False 且文件已存在时才会走到这里（内部一致性校验用）
        print(f"[FAIL] {exc}", file=sys.stderr)
        return EXIT_IO

    print()
    if skipped:
        print(f"[ OK ] 已写入 {len(written)} 个文件到 {Path(args.out).resolve()}")
        print(f"       跳过 {len(skipped)} 个已存在文件：{'、'.join(skipped)}")
    else:
        print(f"[ OK ] 已写入 {len(written)} 个文件到 {Path(args.out).resolve()}")
    for path in written:
        print(f"       {path.name}  ({path.stat().st_size} 字节)")

    # 5) 编译验证（可选）
    if args.compile or args.compile_only:
        print()
        code = _run_compile_check(config, args.out, mode=args.compile_mode)
        if code != EXIT_OK:
            return code

    return EXIT_OK


# --------------------------------------------------------------------------
# 编译验证
# --------------------------------------------------------------------------


def _run_compile_check(
    config: Stm32Config, output_dir: str | Path, *, mode: str = "auto"
) -> int:
    """跑编译验证并打印结果。返回退出码。

    ``mode`` 语义:

    - ``auto``     按优先级自动挑：armclang → armcc → arm-none-eabi-gcc → 宿主 gcc
    - ``keil``     只用 Keil 编译器（armclang 或 armcc）
    - ``arm``      只用真实 ARM 工具链（含 Keil 与 GCC），不用宿主降级
    - ``armclang`` 只用 ARMCLANG (AC6)
    - ``armcc``    只用 ARMCC (AC5)
    - ``gcc``      只用 arm-none-eabi-gcc
    - ``host``     只用宿主编译器做语法检查

    降级结果**不会**被当成「编译通过」—— ``CompileResult.is_native``
    决定措辞，避免给人虚假的信心。
    """
    from stm32gen.compile_check import check_sources

    chosen = _select_toolchain(mode)
    if chosen is None and mode == "auto":
        print("[ -- ] 未找到任何编译器，跳过编译验证")
        print("       装 Keil MDK 或 arm-none-eabi-gcc 可做真实目标编译；")
        print("       或设 HOST_CC 指定宿主编译器做降级语法检查。")
        return EXIT_OK

    if chosen is None:
        print(f"[FAIL] 未找到 {mode} 模式的编译器", file=sys.stderr)
        print(
            "       可用模式：auto / keil / arm / armclang / armcc / gcc / host",
            file=sys.stderr,
        )
        return EXIT_IO

    # 真实目标编译必须先有标准库头文件。不提前拦下的话，编译器会为每个
    # 头文件各报一条「'stm32f10x.h' file not found」，11 条错误把真正的
    # 原因（环境变量没设）埋掉，用户会去查自己生成的代码。
    # 宿主 gcc 降级不检查：它只做语法检查，本来就不需要标准库头文件。
    #
    # 提示全部走 stdout 并先于调用打印：混用 stderr 会让它和前面已缓冲的
    # stdout 内容在终端里交错成乱序（实测提示会夹在「配置文件」与「输出目录」
    # 之间），用户读起来像输出错乱。
    if chosen.kind.is_native and configured_stdlib_root() is None:
        print("[ -- ] 未设置标准库代码根目录，无法做真实目标编译")
        print(
            "       请设置环境变量 STM32_STDLIB_ROOT 指向 STM32F10x 标准外设库根目录"
        )
        print(
            r"       例：$env:STM32_STDLIB_ROOT = 'D:\path\to\STM32F10x_StdPeriph_Lib'"
        )
        print("       该目录只用于编译验证，生成代码本身不需要它。")
        return EXIT_IO

    result = check_sources(config, output_dir, toolchain=chosen)

    print(f"编译验证  : {result.toolchain.label}")
    print(f"            {result.toolchain.version}")
    if result.command:
        print(f"命令行    : {result.command_line()}")

    if result.execution_error:
        print(f"[FAIL] {result.execution_error}", file=sys.stderr)
        return EXIT_IO

    if result.diagnostics:
        print()
        for item in result.diagnostics:
            stream = sys.stderr if item.severity.is_blocking else None
            print(f"  {item.format()}", file=stream)

    print()
    print(f"结论      : {result.summary()}")

    if not result.is_native:
        print(
            "           注意：宿主 gcc 只能做语法与架构无关的类型检查，"
            "发现不了 ARM 专属问题。\n"
            "           要真正的目标编译验证，请安装 Keil MDK 或 "
            "arm-none-eabi-gcc。"
        )

    if result.returncode != 0:
        print(f"[FAIL] 编译验证未通过（{len(result.errors)} 个错误）", file=sys.stderr)
        return EXIT_BLOCKING

    if result.warnings:
        # 警告不阻断，但要提醒：code-standards 要求告警清零
        print(f"[WARN] 有 {len(result.warnings)} 个警告，按规范应清零")
    return EXIT_OK


def _select_toolchain(mode: str):
    """按模式挑编译器。

    ``keil`` 与 ``arm`` 是**组合**模式：按优先级在若干类型里挑第一个可用的。
    ``armclang`` / ``armcc`` / ``gcc`` / ``host`` 是精确模式，找不到就返回
    None（不降级），便于 CI 里强制要求某种工具链。
    """
    from stm32gen.compile_check import ToolchainKind, find_toolchain

    if mode == "auto":
        return find_toolchain()

    if mode == "keil":
        # Keil 的两套编译器，armclang（AC6）优先
        for kind in (ToolchainKind.ARMCLANG, ToolchainKind.ARMCC):
            found = find_toolchain(prefer=kind)
            if found is not None:
                return found
        return None

    if mode == "arm":
        # 任意真实 ARM 工具链：两套 Keil + arm-none-eabi-gcc
        for kind in (
            ToolchainKind.ARMCLANG,
            ToolchainKind.ARMCC,
            ToolchainKind.ARM_GCC,
        ):
            found = find_toolchain(prefer=kind)
            if found is not None:
                return found
        return None

    mapping = {
        "armclang": ToolchainKind.ARMCLANG,
        "armcc": ToolchainKind.ARMCC,
        "gcc": ToolchainKind.ARM_GCC,
        "host": ToolchainKind.HOST,
    }
    kind = mapping.get(mode)
    if kind is None:
        return None
    return find_toolchain(prefer=kind)


def cmd_compile(args: argparse.Namespace) -> int:
    """校验一个已存在的输出目录里的生成代码能否编译。"""
    config, code = _load_config(args.config)
    if config is None:
        return code

    directory = Path(args.out)
    if not directory.is_dir():
        print(f"[FAIL] 输出目录不存在：{directory}", file=sys.stderr)
        print("       先运行：python -m stm32gen generate <配置> -o <目录>", file=sys.stderr)
        return EXIT_IO

    print(f"配置文件  : {args.config}")
    print(f"输出目录  : {directory.resolve()}")
    print(config.summary())
    print()
    return _run_compile_check(config, directory, mode=args.mode)


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stm32gen",
        description="STM32 标准外设库初始化代码生成器（STM32F103C8T6 / GPIO / USART / NVIC）",
    )
    parser.add_argument("--version", action="version", version=f"stm32-init-gen {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("selftest", help="检查 Python、依赖、标准库目录、符号表与生成链路")

    load_parser = subparsers.add_parser("load", help="读取并校验配置文件，打印摘要")
    load_parser.add_argument("config", help="配置文件路径（.json / .yaml）")

    validate_parser = subparsers.add_parser(
        "validate", help="跑校验层，检查引脚冲突、波特率误差、NVIC 优先级等"
    )
    validate_parser.add_argument("config", help="配置文件路径（.json / .yaml）")
    validate_parser.add_argument(
        "--no-info", action="store_true", help="只显示错误与警告，隐藏提示"
    )

    generate_parser = subparsers.add_parser(
        "generate", help="生成初始化代码（含反编造自检）"
    )
    generate_parser.add_argument("config", help="配置文件路径（.json / .yaml）")
    generate_parser.add_argument(
        "--out",
        "-o",
        default=default_out_dir(),
        help=f"输出目录（默认 {OUTPUT_DIR_NAME}，在源码树旁边）",
    )
    generate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成并打印到 stdout，不写文件",
    )
    generate_parser.add_argument(
        "--print",
        dest="print",
        action="store_true",
        help="除写文件外，同时把内容打印到 stdout（便于复制）",
    )
    generate_parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="跳过已存在的文件（不是报错终止），只写新文件",
    )
    generate_parser.add_argument(
        "--no-check",
        action="store_true",
        help="跳过生成后的反编造自检（不推荐，会放弃核心保证）",
    )
    generate_parser.add_argument(
        "--no-info", action="store_true", help="校验输出只显示错误与警告"
    )
    generate_parser.add_argument(
        "--force",
        action="store_true",
        help="即使校验存在阻断性错误也继续生成（不推荐）",
    )
    generate_parser.add_argument(
        "--layout",
        choices=("split", "merged"),
        default="split",
        help=(
            "产物形态：split=按外设分文件（默认，19 个）；"
            "merged=合并成 bsp.h/bsp.c/main.c（3 个）"
        ),
    )
    generate_parser.add_argument(
        "--compile",
        action="store_true",
        help="生成后顺带做编译验证（并入 --compile-mode 选择编译器）",
    )
    generate_parser.add_argument(
        "--compile-only",
        action="store_true",
        help="只做编译验证，不打印生成摘要（仍会写文件）",
    )
    generate_parser.add_argument(
        "--compile-mode",
        choices=("auto", "keil", "arm", "armclang", "armcc", "gcc", "host"),
        default="auto",
        help=(
            "编译器选择：auto=按优先级自动；keil=只用 Keil；"
            "arm=只用真实 ARM 工具链；host=只用宿主 gcc 降级"
        ),
    )

    compile_parser = subparsers.add_parser(
        "compile", help="校验已生成目录里的代码能否编译"
    )
    compile_parser.add_argument("config", help="配置文件路径（.json / .yaml）")
    compile_parser.add_argument(
        "--out",
        "-o",
        default=default_out_dir(),
        help=f"已生成代码的目录（默认 {OUTPUT_DIR_NAME}，在源码树旁边）",
    )
    compile_parser.add_argument(
        "--mode",
        choices=("auto", "keil", "arm", "armclang", "armcc", "gcc", "host"),
        default="auto",
        help=(
            "编译器选择：auto=按优先级自动；keil=只用 Keil；"
            "arm=只用真实 ARM 工具链；host=只用宿主 gcc 降级"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "selftest":
        return cmd_selftest(args)
    if args.command == "load":
        return cmd_load(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "generate":
        return cmd_generate(args)
    if args.command == "compile":
        return cmd_compile(args)

    parser.print_help()
    return EXIT_OK
