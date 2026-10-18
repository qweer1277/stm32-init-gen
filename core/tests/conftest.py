"""tests 层共享配置。

开源后最常见的情形：克隆者机器上**没有** ST 标准外设库源码。那时所有需要
标准库头文件的测试（编译验证、工具链探测、selftest）都无从做起 —— 应当
skip 而不是 fail。fail 会让人以为项目本身坏了，而实际上缺的只是外部依赖。

判据是环境变量 ``STM32_STDLIB_ROOT`` 指向一个真实目录。设了它这些测试照常
跑，没设就统一跳过。生成与校验的测试**不依赖**它（符号表已固化在
``stdlib_ref/symbols.json`` 里），所以绝大多数测试在任何机器上都能跑。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# 整个文件都依赖标准库源码的测试模块
STDLIB_DEPENDENT_MODULES = frozenset(
    {
        "test_compile.py",
        "test_keil.py",
    }
)

# 文件名里带这些词的用例也依赖标准库（编译验证与自检都要读库源码）
STDLIB_DEPENDENT_KEYWORDS = ("compile", "selftest", "toolchain")


def _stdlib_root() -> Path | None:
    """标准库根目录；未设置或不存在时返回 ``None``。"""
    configured = os.environ.get("STM32_STDLIB_ROOT", "").strip()
    if not configured:
        return None
    path = Path(configured)
    return path if path.is_dir() else None


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """缺少标准库源码时，跳过依赖它的测试而不是让它们失败。"""
    if _stdlib_root() is not None:
        return

    skip = pytest.mark.skip(
        reason=(
            "未设置 STM32_STDLIB_ROOT（或该目录不存在），"
            "跳过需要 ST 标准外设库源码的测试；"
            "生成与校验测试不受影响"
        )
    )
    for item in items:
        module = Path(str(item.fspath)).name
        if module in STDLIB_DEPENDENT_MODULES:
            item.add_marker(skip)
            continue
        if any(word in item.name.lower() for word in STDLIB_DEPENDENT_KEYWORDS):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def stdlib_root() -> Path:
    """需要标准库源码的测试用这个 fixture 拿目录；缺了就 skip。"""
    root = _stdlib_root()
    if root is None:
        pytest.skip("未设置 STM32_STDLIB_ROOT，跳过需要标准库源码的测试")
    return root
