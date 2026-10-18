"""提取器与反编造护栏的测试。

这两者是「禁止编造」约束的执行者，本身必须有回归防线：
护栏坏了却没人发现，比没有护栏更危险（会给人虚假的安全感）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_tool(name: str):
    """按路径加载 tools/ 下的脚本为模块。"""
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"_tool_{name}"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def extractor():
    return _load_tool("extract_symbols")


@pytest.fixture(scope="module")
def guard_script():
    """tools/check_no_invented.py（自测样例的持有者）。

    注意 fixture 名不叫 ``guard``：那会与 ``stm32gen.stdlib_ref.guard``
    模块名混淆，之前就因此踩了一个 AttributeError。
    """
    return _load_tool("check_no_invented")


# --------------------------------------------------------------------------
# 提取器：文本预处理
# --------------------------------------------------------------------------


def test_blank_comments_preserves_line_numbers(extractor) -> None:
    """抹注释必须保留换行，否则行号会与原文错位。"""
    text = "a\n/* one\ntwo\nthree */\nb\n"
    blanked = extractor.blank_comments(text)
    assert len(blanked.splitlines()) == len(text.splitlines())
    assert blanked.splitlines()[0] == "a"
    assert blanked.splitlines()[4] == "b"
    assert "one" not in blanked
    assert "three" not in blanked


def test_blank_comments_handles_line_comments(extractor) -> None:
    """行注释同样要抹掉，且不影响后续行。"""
    text = "int a; // 说明\nint b;\n"
    blanked = extractor.blank_comments(text)
    assert "说明" not in blanked
    assert "int b;" in blanked


def test_strip_conditionals_keeps_medium_density(extractor) -> None:
    """条件编译求值：只保留 STM32F10X_MD 分支。"""
    lines = [
        "#ifdef STM32F10X_MD",
        "MD_ONLY",
        "#endif",
        "#ifdef STM32F10X_CL",
        "CL_ONLY",
        "#endif",
        "ALWAYS",
    ]
    entries = extractor.strip_conditionals(lines, {"STM32F10X_MD"})
    kept = [line.strip() for _, line in entries]
    assert "MD_ONLY" in kept
    assert "CL_ONLY" not in kept
    assert "ALWAYS" in kept


def test_strip_conditionals_handles_nested(extractor) -> None:
    """嵌套条件编译要正确处理（内层不活跃时外层 else 仍可生效）。"""
    lines = [
        "#ifdef STM32F10X_MD",
        "#ifdef USE_STDPERIPH_DRIVER",
        "INNER_BOTH",
        "#else",
        "INNER_MD_ONLY",
        "#endif",
        "#else",
        "OUTER_ELSE",
        "#endif",
    ]
    entries = extractor.strip_conditionals(lines, {"STM32F10X_MD"})
    kept = [line.strip() for _, line in entries]
    assert kept == ["INNER_MD_ONLY"]


def test_join_continuations(extractor) -> None:
    """续行拼接后行号取首行。"""
    lines = [(1, "a \\"), (2, "  b \\"), (3, "  c"), (4, "d")]
    joined = extractor.join_continuations(lines)
    assert joined[0][0] == 1
    assert joined[0][1].split() == ["a", "b", "c"]
    assert joined[1] == (4, "d")


# --------------------------------------------------------------------------
# 提取器：表达式求值
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("((uint16_t)0x2000)", 0x2000),
        ("((uint32_t)0x00000004)", 4),
        ("0x0525", 0x0525),
        ("1UL", 1),
        ("42U", 42),
        ("(1 << 13)", 8192),
        ("~0", -1),
    ],
)
def test_try_eval_int(extractor, expr: str, expected: int) -> None:
    """强转与整数后缀都要能剥掉后求值。"""
    assert extractor.try_eval_int(expr) == expected


def test_try_eval_int_c_logical_not(extractor) -> None:
    """C 的逻辑非 ``!`` 必须转成 Python 的 ``not``。

    这是 ``ENABLE = !DISABLE`` 能被解析出来的前提，
    不转换就会静默丢掉 ``ENABLE`` 这个符号。
    """
    assert extractor.try_eval_int("!DISABLE", {"DISABLE": 0}) == 1
    assert extractor.try_eval_int("!ENABLE", {"ENABLE": 1}) == 0


def test_try_eval_int_rejects_comma(extractor) -> None:
    """含逗号的表达式判为不可求值，避免求值出元组并触发 SyntaxWarning。"""
    assert extractor.try_eval_int("a, b") is None


def test_normalize_expr_operators(extractor) -> None:
    """``&&`` / ``||`` 要转成 Python 写法。"""
    assert extractor.normalize_expr("a && b") == "a  and  b"
    assert extractor.normalize_expr("a || b") == "a  or  b"


def test_resolve_macro_values_chain(extractor) -> None:
    """宏的数值依赖链要能解析（GPIOA_BASE 依赖 APB2PERIPH_BASE）。"""
    raw = {
        "PERIPH_BASE": "0x40000000",
        "APB2PERIPH_BASE": "(PERIPH_BASE + 0x10000)",
        "GPIOA_BASE": "(APB2PERIPH_BASE + 0x0800)",
    }
    resolved = extractor.resolve_macro_values(raw)
    assert resolved["PERIPH_BASE"] == 0x40000000
    assert resolved["APB2PERIPH_BASE"] == 0x40010000
    assert resolved["GPIOA_BASE"] == 0x40010800


def test_split_params(extractor) -> None:
    """函数参数拆分要能处理指针与多参数。"""
    params = extractor.split_params("GPIO_TypeDef* GPIOx, uint16_t GPIO_Pin")
    assert params == [
        {"type": "GPIO_TypeDef*", "name": "GPIOx"},
        {"type": "uint16_t", "name": "GPIO_Pin"},
    ]
    assert extractor.split_params("void") == []


# --------------------------------------------------------------------------
# 提取器：真实头文件上的端到端断言
# --------------------------------------------------------------------------


def test_probes_all_pass(extractor) -> None:
    """完整跑一遍提取，58 项关键符号自检必须全通过。"""
    from stm32gen import configured_stdlib_root

    # 必须用 configured_stdlib_root() 而不是 Path(DEFAULT_STDLIB_ROOT)：
    # 后者在未配置时得到 Path("")，即当前目录 "."，is_dir() 恒为真，
    # 于是「没配置就跳过」会静默失效，改成拿当前目录当标准库根去扫。
    root = configured_stdlib_root()
    if root is None:
        pytest.skip("标准库目录未配置（设置 STM32_STDLIB_ROOT 后才跑这条）")

    data = extractor.extract(root, set(extractor.DEFAULT_DEFINES))
    problems = extractor.run_probes(data)
    assert not problems, "自检失败：\n" + "\n".join(problems)


# --------------------------------------------------------------------------
# 反编造护栏
#
# 注意：第 6 步把护栏的核心逻辑从 tools/check_no_invented.py 抽到了
# src/stm32gen/stdlib_ref/guard.py（随包发布，供 CLI 与 GUI 复用）。
# tools/ 里的版本现在是薄包装。这里测的是**包内实现**，
# 即真正被 CLI 与 GUI 调用的那一份。
# --------------------------------------------------------------------------


def test_guard_detects_invented_symbols(guard_script) -> None:
    """护栏必须抓到全部编造符号。"""
    from stm32gen.stdlib_ref import default_index as index
    from stm32gen.stdlib_ref import find_invented

    problems = find_invented(guard_script.SELFTEST_BAD, index(), "t")
    found = {item.name for item in problems}
    assert found == guard_script.SELFTEST_BAD_EXPECTED


def test_guard_accepts_valid_code(guard_script) -> None:
    """合法代码不能有误报——误报会让护栏被绕过。"""
    from stm32gen.stdlib_ref import default_index as index
    from stm32gen.stdlib_ref import find_invented

    assert find_invented(guard_script.SELFTEST_GOOD, index(), "t") == []


def test_guard_ignores_struct_members() -> None:
    """``cfg.GPIO_Pin`` 里的成员名不是库符号，不能误报。

    结构体成员名与库宏同名（都叫 ``GPIO_Pin`` 前缀），
    不排除成员访问会大量误报。
    """
    from stm32gen.stdlib_ref import default_index as index
    from stm32gen.stdlib_ref import find_invented

    text = (
        "GPIO_InitTypeDef cfg;\n"
        "cfg.GPIO_Pin = GPIO_Pin_13;\n"
        "ptr->USART_BaudRate = 9600;\n"
    )
    # 用真实索引：GPIO_InitTypeDef 是库类型，
    # 但 GPIO_Pin / USART_BaudRate 只是结构体成员名
    assert find_invented(text, index(), "t") == []


def test_guard_catches_invented_type() -> None:
    """库前缀的类型名同样要校验，不能因为不是函数就放过。"""
    from stm32gen.stdlib_ref import SymbolIndex, find_invented

    empty = SymbolIndex({})
    problems = find_invented("GPIO_FakeTypeDef cfg;\n", empty, "t")
    assert len(problems) == 1
    assert problems[0].name == "GPIO_FakeTypeDef"


def test_guard_ignores_comments_and_strings() -> None:
    """注释与字符串里的符号名不算引用。"""
    from stm32gen.stdlib_ref import SymbolIndex, find_invented

    empty = SymbolIndex({})
    text = '// GPIO_FakeThing\n/* USART_Nope */\nconst char *s = "RCC_APB2Periph_Fake";\n'
    assert find_invented(text, empty, "t") == []


def test_guard_honours_noqa() -> None:
    """显式豁免注释要生效，且只对所在行生效。"""
    from stm32gen.stdlib_ref import SymbolIndex, find_invented

    empty = SymbolIndex({})
    text = "GPIO_Invented();  /* noqa: invented-symbol */\nGPIO_Other();\n"
    problems = find_invented(text, empty, "t")
    assert len(problems) == 1
    assert problems[0].name == "GPIO_Other"
    assert problems[0].line == 2


def test_guard_only_checks_lib_prefixes() -> None:
    """非标准库前缀的标识符不参与校验。"""
    from stm32gen.stdlib_ref import SymbolIndex, find_invented

    empty = SymbolIndex({})
    assert find_invented("my_own_function();\nstruct foo bar;\n", empty, "t") == []


def test_guard_module_is_packaged() -> None:
    """护栏必须在包内可用——CLI 与 GUI 依赖它，不能只存在于 tools/。"""
    from stm32gen.stdlib_ref import (
        collect_c_files,
        find_invented,
        find_invented_in_files,
        is_lib_symbol,
        strip_c_comments_and_strings,
    )

    assert callable(find_invented)
    assert callable(find_invented_in_files)
    assert callable(collect_c_files)
    assert callable(strip_c_comments_and_strings)
    assert is_lib_symbol("GPIO_Init")
    assert not is_lib_symbol("my_function")


def test_collect_c_files(tmp_path) -> None:
    """目录要递归展开，且只取 .c/.h。"""
    from stm32gen.stdlib_ref import collect_c_files

    (tmp_path / "a.c").write_text("int a;", encoding="utf-8")
    (tmp_path / "b.h").write_text("int b;", encoding="utf-8")
    (tmp_path / "c.txt").write_text("not code", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.c").write_text("int d;", encoding="utf-8")

    names = sorted(path.name for path in collect_c_files([tmp_path]))
    assert names == ["a.c", "b.h", "d.c"]


def test_find_invented_in_files(tmp_path) -> None:
    from stm32gen.stdlib_ref import SymbolIndex, find_invented_in_files

    bad = tmp_path / "bad.c"
    bad.write_text("GPIO_FakeThing();\n", encoding="utf-8")
    problems = find_invented_in_files([bad], SymbolIndex({}))
    assert len(problems) == 1
    assert problems[0].name == "GPIO_FakeThing"


# --------------------------------------------------------------------------
# ST 惯例结构体变量名不能被误报
# --------------------------------------------------------------------------


def test_st_convention_struct_names_are_exempt() -> None:
    """ST 例程惯例的结构体变量名必须放行，不能报成编造符号。

    这三个名字以库前缀开头（``GPIO_`` / ``USART_`` / ``NVIC_``），
    ``is_lib_symbol`` 会选中它们，而符号表里当然查不到（库不会定义
    叫这名儿的宏）—— 不豁免就会对**完全正确的 ST 惯例写法**误报。

    这条测试的价值在于：误报会让人绕过护栏，比没有护栏更糟。
    """
    from stm32gen.stdlib_ref import (
        STRUCT_VAR_NAMES,
        default_index,
        find_invented,
    )

    # 必须用**真实符号表**：本测试要证明的是「正确的 ST 惯例写法在真实
    # 符号表下不误报」。空索引会让 GPIO_InitTypeDef 这类真符号也报错，
    # 那是另一回事（测的是索引为空时的行为）。
    index = default_index()
    code = (
        "void f(void)\n"
        "{\n"
        "    GPIO_InitTypeDef GPIO_InitStructure;\n"
        "    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_0 | GPIO_Pin_1;\n"
        "    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;\n"
        "    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IPU;\n"
        "    GPIO_Init(GPIOB, &GPIO_InitStructure);\n"
        "\n"
        "    USART_InitTypeDef USART_InitStructure;\n"
        "    USART_Init(USART1, &USART_InitStructure);\n"
        "\n"
        "    NVIC_InitTypeDef NVIC_InitStructure;\n"
        "    NVIC_Init(&NVIC_InitStructure);\n"
        "}\n"
    )
    assert find_invented(code, index, "t") == []
    assert "GPIO_InitStructure" in STRUCT_VAR_NAMES


def test_struct_name_exemption_is_a_closed_list() -> None:
    """豁免是**闭合白名单**，不是"``*_InitStructure`` 结尾都放行"。

    若改成后缀正则，``GPIO_WhateverInitStructure`` 这类真正的编造符号
    就能混过去 —— 护栏会被绕过。这里断言一个相似但不在白名单里的名字
    仍然被抓到。
    """
    from stm32gen.stdlib_ref import STRUCT_VAR_NAMES, SymbolIndex, find_invented

    assert "GPIO_WhateverInitStructure" not in STRUCT_VAR_NAMES

    # 用空索引：本测试只关心"名字本身是否被豁免"，不关心符号表内容
    problems = find_invented(
        "GPIO_WhateverInitStructure.GPIO_Pin = 1;\n", SymbolIndex({}), "t"
    )
    assert len(problems) == 1
    assert problems[0].name == "GPIO_WhateverInitStructure"

