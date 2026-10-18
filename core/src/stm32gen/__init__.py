"""stm32-init-gen：STM32 标准外设库初始化代码生成器（库 + 命令行）。

依赖只有 Pydantic / Jinja2 / PyYAML，**不含任何 GUI 依赖**。
桌面界面是独立项目 ``stm32-init-gen-gui``（包名 ``stm32gen_gui``），
它以依赖形式引用本包，方向单向：界面 → 本包，本包不反向引用界面。
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

# 标准库代码根目录（唯一事实来源）。
#
# **刻意留空**：硬编码一个开发机上的绝对路径，会让别人克隆后拿到一个指向
# 不存在目录的默认值，而错误要等到编译验证时才以「找不到头文件」的形式
# 暴露出来，很难倒推到这里。现在改为必须显式设置环境变量
# STM32_STDLIB_ROOT，未设置时由调用方给出明确的引导错误
# （见 cli.py 的 stdlib_root() 与 compile_check.py）。
DEFAULT_STDLIB_ROOT = ""

# 默认输出目录的目录名：与源码树同级，见 default_out_dir()
OUTPUT_DIR_NAME = "stm32-init-gen-output"

# 标记源码树根的文件。用标记文件定位而不是数 __file__ 的层数：后者在
# editable 安装、符号链接或测试临时复制源码时会数错，标记的位置是确定的。
PROJECT_MARKER = "pyproject.toml"


def project_root() -> Path | None:
    """定位源码树根（含 ``pyproject.toml`` 的那一层）；装成包后返回 ``None``。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / PROJECT_MARKER).is_file():
            return parent
    return None


def default_out_dir() -> str:
    """默认输出目录：源码树**旁边**的 ``stm32-init-gen-output``。

    工具与成果分开放。生成产物不该落在源码树里 —— 一旦落在仓库根或 ``src``，
    它就被 git 追踪、被 ruff 扫、被 ``rglob`` 撞见，清一次要记得清两处。

    装成包（找不到 ``pyproject.toml``）时退回当前目录下的 ``out``，
    因为那种情况下已没有「源码树旁边」这个概念。
    """
    root = project_root()
    if root is None:
        return "out"
    return str(root.parent / OUTPUT_DIR_NAME)


__all__ = [
    "__version__",
    "DEFAULT_STDLIB_ROOT",
    "OUTPUT_DIR_NAME",
    "default_out_dir",
    "project_root",
]
