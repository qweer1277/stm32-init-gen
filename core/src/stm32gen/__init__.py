"""stm32-init-gen：STM32 标准外设库初始化代码生成器（库 + 命令行）。

依赖只有 Pydantic / Jinja2 / PyYAML，**不含任何 GUI 依赖**。
桌面界面是独立项目 ``stm32-init-gen-gui``（包名 ``stm32gen_gui``），
它以依赖形式引用本包，方向单向：界面 → 本包，本包不反向引用界面。
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

# 标准库代码根目录（唯一事实来源）。
#
# **刻意留空**：硬编码一个开发机上的绝对路径，会让别人克隆后拿到一个指向
# 不存在目录的默认值，而错误要等到编译验证时才以「找不到头文件」的形式
# 暴露出来，很难倒推到这里。现在改为必须显式设置环境变量
# STM32_STDLIB_ROOT，未设置时由调用方给出明确的引导错误
# （见 cli.py 的 stdlib_root() 与 compile_check.py）。
#
# 取值是空字符串而不是 None：它同时被 argparse 的 default= 与
# os.environ.get 的默认值使用，两处都要求 str。判断「有没有配置」请一律走
# configured_stdlib_root()，不要自己 Path(DEFAULT_STDLIB_ROOT).is_dir() ——
# Path("") 会解析成当前目录 "."，那个目录永远存在，于是「未配置」会被
# 误判成「配置好了」，然后拿当前目录当标准库根去扫。
DEFAULT_STDLIB_ROOT = ""

# 环境变量名：覆盖 DEFAULT_STDLIB_ROOT 的唯一途径
STDLIB_ROOT_ENV = "STM32_STDLIB_ROOT"

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


def configured_stdlib_root() -> Path | None:
    """返回已配置且**确实存在**的标准库根目录；未配置或不存在时返回 ``None``。

    这是判断「标准库可用吗」的唯一入口。返回值是 ``None`` 而不是 ``Path("")``，
    因为后者会解析成当前目录 ``.`` —— 一个永远 ``is_dir()`` 为真的路径，
    会让所有「没配置就跳过 / 就报引导错误」的分支静默失效。
    """
    configured = os.environ.get(STDLIB_ROOT_ENV) or DEFAULT_STDLIB_ROOT
    if not configured:
        return None
    root = Path(configured)
    return root if root.is_dir() else None


__all__ = [
    "__version__",
    "DEFAULT_STDLIB_ROOT",
    "OUTPUT_DIR_NAME",
    "STDLIB_ROOT_ENV",
    "configured_stdlib_root",
    "default_out_dir",
    "project_root",
]
