"""编译验证：把生成的代码真的交给编译器过一遍。

思路：只编译生成的 ``.c`` 文件（不链接）—— 链接需要完整的启动文件与
链接脚本，而本工具的职责只是验证生成代码本身的语法与类型正确性。

四种工具链，能力不同，必须区分
------------------------------
1. **armclang**（Keil MDK 的 Arm Compiler 6，基于 Clang）—— 真实目标编译
2. **armcc**（Keil MDK 的 Arm Compiler 5）—— 真实目标编译
3. **arm-none-eabi-gcc**（GNU Arm Embedded）—— 真实目标编译
4. **宿主 gcc**（降级）—— **仅**预处理、语法、架构无关的类型检查

前三者是真正的目标编译器，结果可以称为「编译通过」；宿主 gcc 的降级结果
**不能**这么说 —— 它发现不了 ARM 专属问题（内联汇编约束、寄存器类型、
intrinsic 用法）。``CompileResult.is_native`` 决定措辞，调用方据此调整。

宿主 gcc 不认 ``-mthumb``（实测报 ``unrecognized command-line option``），
所以 ARM 专属参数只给 ARM 编译器。

工具链查找
----------
按 ``ARMCLANG`` / ``ARMCC`` / ``ARM_GCC`` / ``HOST_CC`` 环境变量 →
PATH → **常见安装目录**的顺序查找。

之所以要搜安装目录：Keil 安装后通常只把路径加到**持久化的**用户 PATH，
已经在跑的进程（包括本工具的宿主进程）看不到。实测本机 Keil 装在
``E:\\download\\keil5``，持久化 PATH 里有但当前会话 PATH 里没有 ——
只查 PATH 会漏掉它能提供的真实编译验证。

用 ``KEIL_ROOT`` 环境变量可指定 Keil 安装根目录。
``STM32_NO_HOST_FALLBACK=1`` 可禁用最后的宿主 gcc 降级。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from stm32gen import DEFAULT_STDLIB_ROOT
from stm32gen.models import Stm32Config

# Cortex-M3 的编译参数（按工具链区分，见 build_command）
CPU = "cortex-m3"

# GCC 系（含 armclang）的告警开关。
# 与 code-standards 的要求一致：-Wall -Wextra 必须清零。
GCC_WARNING_FLAGS = ("-Wall", "-Wextra")

# ARMCC 用的是它自己的参数风格：它没有 -Wall/-Wextra，
# 用 --strict 打开严格诊断（实测 -Wall 会被当成 '-all' 而报 C3900U）。
ARMCC_WARNING_FLAGS = ("--strict",)

# 所有 ARM 编译器的目标参数（用于对外展示「这是目标编译」）
ARM_TARGET_FLAGS = ("-mcpu=cortex-m3", "-mthumb")

# 默认超时（秒）。生成代码很小，正常 1~5 秒即可完成。
DEFAULT_TIMEOUT = 180

# 标准库子目录（相对 STDLIB_ROOT）
STDLIB_SUBDIRS = ("Start", "Library", "User")

# Keil 安装目录的候选相对路径（用于在常见根目录下找编译器）
_KEIL_SUBPATHS = {
    "armclang": (
        Path("ARM") / "ARMCLANG" / "Bin" / "armclang.exe",
        Path("ARM") / "ARMCLANG" / "Bin" / "armclang",
    ),
    "armcc": (
        Path("ARM") / "ARMCC" / "Bin" / "armcc.exe",
        Path("ARM") / "ARMCC" / "Bin" / "armcc",
    ),
}

# Keil 安装根目录的候选名（会在各盘根与常见父目录下探测）
_KEIL_ROOT_NAMES = ("Keil_v5", "Keil_v5_ARM", "keil5", "Keil", "MDK")

# 扫描 Keil 安装时的根目录（顺序即优先级）
_KEIL_SEARCH_PARENTS = (
    Path(r"E:\download"),
    Path(r"D:\download"),
    Path(r"C:\download"),
)


def _keil_root_candidates() -> list[Path]:
    """列出可能的 Keil 安装根目录。

    来源三处：``KEIL_ROOT`` 环境变量（最高优先）、常见父目录下的常见名、
    各盘根下的常见名。
    """
    candidates: list[Path] = []

    explicit = os.environ.get("KEIL_ROOT")
    if explicit:
        candidates.append(Path(explicit))

    for parent in _KEIL_SEARCH_PARENTS:
        if not parent.is_dir():
            continue
        for name in _KEIL_ROOT_NAMES:
            candidates.append(parent / name)

    for drive in ("C:", "D:", "E:"):
        for name in _KEIL_ROOT_NAMES:
            candidates.append(Path(f"{drive}\\{name}"))

    # 去重且保持顺序
    seen: set[str] = set()
    result: list[Path] = []
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _find_in_keil(tool: str) -> str | None:
    """在 Keil 安装目录里找编译器可执行文件。"""
    subpaths = _KEIL_SUBPATHS.get(tool, ())
    for root in _keil_root_candidates():
        for sub in subpaths:
            candidate = root / sub
            if candidate.is_file():
                return str(candidate)
    return None


def _find_keil_lib_root() -> Path | None:
    """找 Keil 的 ARM 目录（含 ARMCLANG/ARMCC 与各类库）。

    用于给 armclang 提供它自带的头文件（如果将来需要）。
    当前生成代码只用 CMSIS 与标准外设库，不需要额外包含路径，
    所以这个函数暂时只为排查与展示保留。
    """
    for root in _keil_root_candidates():
        arm_dir = root / "ARM"
        if arm_dir.is_dir():
            return arm_dir
    return None


# gcc 系诊断行：``file:line:col: severity: message``
# 列号可选（有些诊断不给列）。armclang 与 GCC 格式一致。
_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)

# ARMCC (AC5) 诊断行：``"file", line 42: Error: #35: message``
_ARMCC_DIAG_RE = re.compile(
    r'^"(?P<file>[^"]+)"\s*,\s*line\s*(?P<line>\d+)\s*:\s*'
    r"(?P<severity>Error|Warning|Fatal Error|Info)\s*:?\s*"
    r"(?:#\d+\s*:\s*)?(?P<message>.*)$",
    re.IGNORECASE,
)

# ARMCC 链接器诊断：``Error: L6218E: Undefined symbol X (referred from Y.o).``
_ARMCC_LINK_RE = re.compile(
    r"^(?P<severity>Error|Warning|Fatal)\s*:\s*(?P<message>.*)$",
    re.IGNORECASE,
)


class Severity(str, Enum):
    """诊断严重级别。"""

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"

    @property
    def label(self) -> str:
        return {"error": "错误", "warning": "警告", "note": "附注"}[self.value]

    @property
    def is_blocking(self) -> bool:
        """只有 error 阻断。警告本身不阻断，但按 code-standards 应清零。"""
        return self is Severity.ERROR


@dataclass(frozen=True)
class Diagnostic:
    """一条编译器诊断。"""

    file: str
    line: int
    column: int
    severity: Severity
    message: str

    @property
    def location(self) -> str:
        """形如 ``bsp_init.c:42:5``。"""
        if self.column:
            return f"{Path(self.file).name}:{self.line}:{self.column}"
        if self.file:
            return f"{Path(self.file).name}:{self.line}"
        return "<无位置>"

    def format(self) -> str:
        return f"[{self.severity.label}] {self.location}: {self.message}"


class ToolchainKind(str, Enum):
    """工具链类型。决定参数风格与结果措辞。"""

    ARMCLANG = "armclang"
    ARMCC = "armcc"
    ARM_GCC = "arm-gcc"
    HOST = "host"

    @property
    def label(self) -> str:
        return {
            "armclang": "armclang（Keil Arm Compiler 6）",
            "armcc": "armcc（Keil Arm Compiler 5）",
            "arm-gcc": "arm-none-eabi-gcc",
            "host": "宿主 gcc（仅语法检查）",
        }[self.value]

    @property
    def is_native(self) -> bool:
        """是否真正的目标编译器。宿主 gcc 不是。"""
        return self is not ToolchainKind.HOST

    @property
    def is_keil(self) -> bool:
        return self in (ToolchainKind.ARMCLANG, ToolchainKind.ARMCC)

    @property
    def uses_gcc_syntax(self) -> bool:
        """是否用 GCC/Clang 风格的参数（``-I`` / ``-D`` / ``-fsyntax-only``）。

        ARMCC (AC5) 是独立风格的编译器，参数写法不同。
        """
        return self in (ToolchainKind.ARMCLANG, ToolchainKind.ARM_GCC, ToolchainKind.HOST)

    @property
    def supports_syntax_only(self) -> bool:
        """是否支持 ``-fsyntax-only``。

        ARMCC 不支持，只能用 ``-c`` 编译出目标文件（然后删掉）。
        """
        return self is not ToolchainKind.ARMCC


@dataclass(frozen=True)
class Toolchain:
    """一个可用的编译器。"""

    path: str
    kind: ToolchainKind
    version: str = ""

    @property
    def label(self) -> str:
        return self.kind.label


@dataclass
class CompileResult:
    """编译验证结果。"""

    # 工具链缺失时为 None（不是失败，是「没做」）
    toolchain: Toolchain | None = None
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    # 实际执行的命令，便于排查与手工复现
    command: tuple[str, ...] = ()
    # 编译的文件
    files: tuple[str, ...] = ()
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # 超时或无法启动等执行层面的问题
    execution_error: str = ""

    @property
    def ran(self) -> bool:
        """是否真的跑了编译器。"""
        return self.toolchain is not None and not self.execution_error

    @property
    def ok(self) -> bool:
        """是否通过。没跑过不算通过（避免「不可用」被误读为成功）。"""
        return self.ran and self.returncode == 0

    @property
    def is_native(self) -> bool:
        """是否为真实目标编译器的结果。"""
        return self.toolchain is not None and self.toolchain.kind.is_native

    @property
    def errors(self) -> list[Diagnostic]:
        return [item for item in self.diagnostics if item.severity.is_blocking]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [item for item in self.diagnostics if item.severity is Severity.WARNING]

    def command_line(self) -> str:
        """完整命令行，便于用户手工复现。"""
        return " ".join(self.command)

    def summary(self) -> str:
        """一行结论。措辞区分「真实编译」与「降级语法检查」。"""
        if self.execution_error:
            return f"编译验证未能执行：{self.execution_error}"
        if self.toolchain is None:
            return "未找到编译器，编译验证不可用"

        kind = "编译" if self.is_native else "语法检查"
        prefix = "" if self.is_native else "宿主 gcc "
        if self.returncode == 0:
            detail = f"{len(self.warnings)} 个警告" if self.warnings else "无警告"
            return f"{prefix}{kind}通过（{len(self.files)} 个文件，{detail}）"
        return (
            f"{prefix}{kind}失败：{len(self.errors)} 个错误"
            f" / {len(self.warnings)} 个警告"
        )

    def format_diagnostics(self, limit: int = 20) -> str:
        """把诊断格式化成多行文本。"""
        if not self.diagnostics:
            return ""
        lines = [item.format() for item in self.diagnostics[:limit]]
        if len(self.diagnostics) > limit:
            lines.append(f"… 另有 {len(self.diagnostics) - limit} 条诊断")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# 工具链查找
# --------------------------------------------------------------------------


def _probe_version(path: str, kind: ToolchainKind) -> str:
    """取编译器版本（失败返回空串，不影响可用性判断）。

    两套工具链的版本参数不同：GCC/Clang 系用 ``--version``，
    ARMCC 直接跑不带参数就会打印版本（它不认 ``--version``，
    实测报 ``C3900U: Unrecognized option '--version'``）。
    """
    args = [path] if kind is ToolchainKind.ARMCC else [path, "--version"]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    text = (proc.stdout or "") + (proc.stderr or "")
    for line in text.splitlines():
        stripped = line.strip()
        # 取第一行有信息量的：跳过空行与纯分隔线
        if stripped and not set(stripped) <= set("-="):
            return stripped
    return ""


def _make_toolchain(path: str, kind: ToolchainKind) -> Toolchain:
    return Toolchain(path=path, kind=kind, version=_probe_version(path, kind))


def find_toolchain(prefer: ToolchainKind | None = None) -> Toolchain | None:
    """查找可用编译器。

    默认优先级：**armclang → armcc → arm-none-eabi-gcc → 宿主 gcc**。

    排在前面的理由是检查能力：三款 ARM 编译器都能做真实目标编译，
    其中 armclang（AC6）基于 Clang，对标准的遵循最严、诊断最清晰，
    所以排在首位。宿主 gcc 垫底，只在完全没有 ARM 工具链时兜底。

    ``prefer`` 可强制指定某一类（找不到该类则返回 None，不降级）——
    用于 CI 里「必须用真实工具链」的场景。
    """
    explicit_map = {
        ToolchainKind.ARMCLANG: "ARMCLANG",
        ToolchainKind.ARMCC: "ARMCC",
        ToolchainKind.ARM_GCC: "ARM_GCC",
        ToolchainKind.HOST: "HOST_CC",
    }

    order = (
        (ToolchainKind.ARMCLANG, "armclang"),
        (ToolchainKind.ARMCC, "armcc"),
        (ToolchainKind.ARM_GCC, "arm-none-eabi-gcc"),
        (ToolchainKind.HOST, "gcc"),
    )
    if prefer is not None:
        order = tuple(item for item in order if item[0] is prefer)

    for kind, exe_name in order:
        # 1) 环境变量显式指定
        env_name = explicit_map[kind]
        explicit = os.environ.get(env_name)
        if explicit and Path(explicit).is_file():
            return _make_toolchain(explicit, kind)

        # 2) PATH
        found = shutil.which(exe_name)
        if found:
            return _make_toolchain(found, kind)

        # 3) Keil 安装目录（PATH 里通常没有，见模块 docstring 的说明）
        if kind.is_keil:
            from_keil = _find_in_keil(exe_name)
            if from_keil:
                return _make_toolchain(from_keil, kind)

        # 4) 宿主 gcc 的备用名
        if kind is ToolchainKind.HOST:
            for alt in ("cc", "clang"):
                if shutil.which(alt):
                    return _make_toolchain(shutil.which(alt), kind)

    return None


def is_available() -> bool:
    """是否有可用编译器（GUI 据此决定是否启用「编译验证」按钮）。"""
    return find_toolchain() is not None


def is_native_available() -> bool:
    """是否有真实的 ARM 工具链。"""
    toolchain = find_toolchain()
    return toolchain is not None and toolchain.kind.is_native


def host_fallback_disabled() -> bool:
    """是否禁用了宿主 gcc 降级。"""
    return os.environ.get("STM32_NO_HOST_FALLBACK") == "1"


# --------------------------------------------------------------------------
# 编译命令构造
# --------------------------------------------------------------------------


def build_include_paths(stdlib_root: str | Path | None = None) -> list[str]:
    """构造包含路径：标准库的 Start/Library/User 三层。

    显式传了 ``stdlib_root`` 就用它；否则读环境变量 ``STM32_STDLIB_ROOT``。
    两者都没有时返回空列表 —— 调用方（编译验证）随后会因为找不到
    ``stm32f10x.h`` 而报错，那条错误里已经提示要设这个环境变量。
    """
    configured = stdlib_root or os.environ.get(
        "STM32_STDLIB_ROOT", DEFAULT_STDLIB_ROOT
    )
    if not configured:
        return []
    root = Path(configured)
    paths: list[str] = []
    for sub in STDLIB_SUBDIRS:
        directory = root / sub
        if directory.is_dir():
            paths.append(str(directory))
    return paths


def build_command(
    toolchain: Toolchain,
    config: Stm32Config,
    source_dir: str | Path,
    files: list[str],
    *,
    syntax_only: bool = True,
    extra_includes: list[str] | None = None,
    object_dir: str | Path | None = None,
    single_file: str | None = None,
) -> list[str]:
    """构造编译命令。

    按工具链风格分两条路径：

    - **ARMCC (AC5)**：``--cpu=Cortex-M3 --c99 --strict -c -o <obj>``。
      它不支持 ``-fsyntax-only``，只能用 ``-c`` 编译出目标文件
      （调用方负责删掉）。它用 ``--cpu=`` 而不是 ``-mcpu=``。
      **它一次只能编一个文件**——多文件时单个 ``-o`` 会被忽略，
      目标文件会按默认名落在当前目录。所以走它时要逐文件调本函数，
      用 ``single_file`` 指定。
    - **其余（armclang / arm-gcc / 宿主 gcc）**：GCC 风格参数，
      ``syntax_only`` 时用 ``-fsyntax-only``，可一次编多个文件。

    ``syntax_only=True`` 不产生目标文件，因此不需要链接脚本，也不需要
    工具链的运行时库 —— 这正是「只验证生成代码」所需的最小检查。

    ARM 专属参数（``-mcpu``/``-mthumb``/``--cpu=``）**只给 ARM 编译器**：
    宿主 x86 gcc 收到 ``-mthumb`` 会直接报 ``unrecognized command-line
    option`` 并失败，那不是「代码有问题」。
    """
    cmd: list[str] = [toolchain.path]

    if toolchain.kind.uses_gcc_syntax:
        # ARM 目标参数（宿主 gcc 不给）
        if toolchain.kind.is_native:
            cmd.extend(("--target=arm-arm-none-eabi", "-mcpu=cortex-m3", "-mthumb")
                       if toolchain.kind is ToolchainKind.ARMCLANG
                       else ("-mcpu=cortex-m3", "-mthumb"))
        cmd.extend(GCC_WARNING_FLAGS)
        if syntax_only and toolchain.kind.supports_syntax_only:
            cmd.append("-fsyntax-only")
        else:
            cmd.append("-c")
    else:
        # ARMCC (AC5) 风格
        cmd.append("--cpu=Cortex-M3")
        cmd.append("--c99")
        cmd.extend(ARMCC_WARNING_FLAGS)
        cmd.append("-c")

    # 编译宏：芯片型号宏、标准库开关、晶振频率。
    # 缺任何一个，stm32f10x.h 都会直接 #error。
    for flag in config.c_flags:
        cmd.append(f"-D{flag}")

    # 包含路径：生成代码所在目录排最前（优先用本地产物）。
    #
    # 必须用**绝对路径**：执行时 cwd 会切到源码目录，此时相对路径
    # 会变成 ``out\out`` 这种不存在的路径 —— 编译器只是忽略它而不报错，
    # 包含路径就静默失效了（初版写的就是相对路径）。
    cmd.append(f"-I{Path(source_dir).resolve()}")
    for path in extra_includes or []:
        cmd.append(f"-I{Path(path).resolve()}")
    for path in build_include_paths():
        cmd.append(f"-I{path}")

    # ARMCC 需要显式指定目标文件输出位置。
    #
    # 关键：ARMCC 给**多个**输入文件配单个 ``-o`` 时会忽略它，把 .o 按
    # 默认名丢在当前工作目录（实测产物落在源码目录里污染用户文件）。
    # 所以调用方必须逐文件编译，这里按 single_file 命名目标文件。
    if not toolchain.kind.uses_gcc_syntax:
        out_dir = Path(object_dir) if object_dir else Path(source_dir)
        if single_file:
            name = Path(single_file).stem + ".o"
        else:
            name = "stm32gen_check.o"
        cmd.append(f"-o{out_dir / name}")

    cmd.extend([single_file] if single_file else files)
    return cmd


# --------------------------------------------------------------------------
# 输出解析
# --------------------------------------------------------------------------


def _normalize_severity(text: str) -> Severity | None:
    lowered = text.strip().lower()
    if lowered in ("error", "fatal error", "fatal"):
        return Severity.ERROR
    if lowered == "warning":
        return Severity.WARNING
    if lowered in ("note", "info"):
        return Severity.NOTE
    return None


def parse_diagnostics(text: str) -> list[Diagnostic]:
    """从编译器输出里解析诊断。

    兼容两种格式：

    - GCC / Clang（含 armclang）：``file:line:col: severity: message``
    - ARMCC (AC5)：``"file", line 42: Error: #35: message``

    其他行（命令行回显、``1 error generated.`` 这类总结行）忽略。
    """
    diagnostics: list[Diagnostic] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        match = _DIAG_RE.match(line)
        if match:
            severity = _normalize_severity(match.group("severity"))
            if severity is not None:
                diagnostics.append(
                    Diagnostic(
                        file=match.group("file").strip(),
                        line=int(match.group("line")),
                        column=int(match.group("col") or 0),
                        severity=severity,
                        message=match.group("message").strip(),
                    )
                )
            continue

        match = _ARMCC_DIAG_RE.match(line)
        if match:
            severity = _normalize_severity(match.group("severity"))
            if severity is not None:
                diagnostics.append(
                    Diagnostic(
                        file=match.group("file").strip(),
                        line=int(match.group("line")),
                        column=0,
                        severity=severity,
                        message=match.group("message").strip(),
                    )
                )
            continue

        # ARMCC 的链接器/编译器级错误（没有文件位置）：
        # ``Error: L6218E: Undefined symbol ...``
        match = _ARMCC_LINK_RE.match(line)
        if match and match.group("severity").lower() in ("error", "fatal"):
            message = match.group("message").strip()
            # 跳过总结行（``0 errors`` 之类没有实际内容的）
            if message and not re.match(r"^\d+\s+(error|warning)", message, re.IGNORECASE):
                diagnostics.append(
                    Diagnostic(
                        file="",
                        line=0,
                        column=0,
                        severity=Severity.ERROR,
                        message=message,
                    )
                )

    return diagnostics


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def check_sources(
    config: Stm32Config,
    source_dir: str | Path,
    files: list[str] | None = None,
    *,
    syntax_only: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    extra_includes: list[str] | None = None,
    toolchain: Toolchain | None = None,
) -> CompileResult:
    """编译（或语法检查）给定目录下的源文件。

    ``files`` 为空时自动收集 ``source_dir`` 下的全部 ``.c``。
    找不到编译器时返回一个 ``toolchain=None`` 的结果 —— 那是
    「没做」而不是「失败」，调用方要靠 ``result.ran`` 区分。
    """
    directory = Path(source_dir)
    if files is None:
        files = sorted(path.name for path in directory.glob("*.c"))

    if not files:
        return CompileResult(
            toolchain=toolchain,
            execution_error=f"{directory} 下没有 .c 文件",
        )

    resolved = toolchain or find_toolchain()
    if resolved is None:
        return CompileResult(execution_error="")

    # ARMCC 需要写目标文件，用临时目录收着，跑完即删
    with tempfile.TemporaryDirectory(prefix="stm32gen_obj_") as object_dir:
        # ARMCC 一次只能编一个文件：多文件时单个 -o 会被忽略，
        # 目标文件按默认名落在当前工作目录（实测污染源码目录）。
        # 所以对走专有风格的工具链逐文件编译，再合并诊断。
        if resolved.kind.uses_gcc_syntax:
            commands = [
                build_command(
                    resolved,
                    config,
                    directory,
                    files,
                    syntax_only=syntax_only,
                    extra_includes=extra_includes,
                    object_dir=object_dir,
                )
            ]
        else:
            commands = [
                build_command(
                    resolved,
                    config,
                    directory,
                    files,
                    syntax_only=syntax_only,
                    extra_includes=extra_includes,
                    object_dir=object_dir,
                    single_file=name,
                )
                for name in files
            ]

        collected: list[Diagnostic] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        returncode = 0

        for command in commands:
            try:
                proc = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    # 在源码目录里跑，诊断里的相对路径更干净
                    cwd=str(directory),
                )
            except subprocess.TimeoutExpired:
                return CompileResult(
                    toolchain=resolved,
                    command=tuple(command),
                    files=tuple(files),
                    execution_error=f"编译超时（超过 {timeout} 秒）",
                )
            except OSError as exc:
                return CompileResult(
                    toolchain=resolved,
                    command=tuple(command),
                    files=tuple(files),
                    execution_error=f"无法启动编译器：{exc}",
                )

            if proc.stdout:
                stdout_parts.append(proc.stdout)
            if proc.stderr:
                stderr_parts.append(proc.stderr)
            collected.extend(
                parse_diagnostics("\n".join(filter(None, (proc.stdout, proc.stderr))))
            )
            # 只要有一个文件失败，整体就算失败
            if proc.returncode != 0:
                returncode = proc.returncode

    return CompileResult(
        toolchain=resolved,
        returncode=returncode,
        stdout="".join(stdout_parts),
        stderr="".join(stderr_parts),
        command=tuple(commands[0]),
        files=tuple(files),
        diagnostics=collected,
    )


def check_generated(
    config: Stm32Config,
    output_dir: str | Path,
    *,
    syntax_only: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    toolchain: Toolchain | None = None,
) -> CompileResult:
    """校验一个输出目录里由本工具生成的代码。

    这是对外入口：先跑一次 ``generate`` 写盘，再调本函数。
    """
    return check_sources(
        config,
        output_dir,
        syntax_only=syntax_only,
        timeout=timeout,
        toolchain=toolchain,
    )
