"""反编造护栏（命令行入口）：校验 C 代码里的 STM32 符号都真实存在于标准库。

这是「禁止编造函数/宏/枚举」这条硬性约束的**执行者**。核心逻辑在
``src/stm32gen/stdlib_ref/guard.py``（随包发布，供 CLI 的 ``generate --check``
与 GUI 复用），本脚本只做命令行包装，这样能力只有一份实现。

用法::

    # 检查单个文件或目录
    python tools/check_no_invented.py out\\bsp_gpio.c
    python tools/check_no_invented.py out\\

    # 自测（用内置的编造样例，应当报错；用合法样例，应当通过）
    python tools/check_no_invented.py --selftest

退出码：0 = 通过，1 = 符号表缺失，2 = 发现编造符号或自测失败。

判定规则（详见 guard.py）
------------------------
1. 只检查标准库前缀的标识符（GPIO_ / USART_ / NVIC_ / RCC_ / IS_ / …）
2. 标识符必须存在于符号表的 macros / enums / functions / types 任一桶
3. 显式豁免 C 关键字与常见类型（``uint8_t``、``void``、``NULL``…）
4. **排除成员访问**：``cfg.GPIO_Pin`` 里的 ``GPIO_Pin`` 是结构体成员名，
   不是库符号 —— 不排除会大量误报
5. 支持行内豁免：``/* noqa: invented-symbol */``
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 后台脚本直接运行时，把 src 加进 sys.path，无需先 pip install -e .
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stm32gen.console import force_utf8_stdio  # noqa: E402
from stm32gen.stdlib_ref import (  # noqa: E402
    DEFAULT_SYMBOLS_PATH,
    SymbolIndex,
    collect_c_files,
    find_invented,
)

# --------------------------------------------------------------------------
# 自测样例
# --------------------------------------------------------------------------

# 应当全部被判为编造
SELFTEST_BAD = """
#include "stm32f10x.h"

void bad_init(void)
{
    GPIO_InitTypeDef cfg;
    cfg.GPIO_Pin   = GPIO_Pin_13;
    cfg.GPIO_Speed = GPIO_Speed_100MHz;      /* 不存在：库里只有 10/2/50MHz */
    cfg.GPIO_Mode  = GPIO_Mode_Out_PP;
    GPIO_Init(GPIOA, &cfg);

    USART_InitTypeDef uart;                   /* 存在 */
    uart.USART_BaudRate = 115200;
    uart.USART_WordLength = USART_WordLength_12b;   /* 不存在：只有 8b/9b */
    USART_Init(USART1, &uart);

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART9, ENABLE);   /* 不存在：只有 1/2/3 */
    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_9);          /* 不存在：只有 0~4 */
    GPIO_FakeFunction(GPIOA);                                /* 不存在 */
}
"""

# 应当全部通过（这些符号都在库里）
SELFTEST_GOOD = """
#include "stm32f10x.h"

void good_init(void)
{
    GPIO_InitTypeDef gpio;
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA | RCC_APB2Periph_USART1, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART2, ENABLE);

    gpio.GPIO_Pin   = GPIO_Pin_9 | GPIO_Pin_10;
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    gpio.GPIO_Mode  = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOA, &gpio);

    USART_InitTypeDef usart;
    usart.USART_BaudRate            = 115200;
    usart.USART_WordLength          = USART_WordLength_8b;
    usart.USART_StopBits            = USART_StopBits_1;
    usart.USART_Parity              = USART_Parity_No;
    usart.USART_Mode                = USART_Mode_Rx | USART_Mode_Tx;
    usart.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    USART_Init(USART1, &usart);
    USART_ITConfig(USART1, USART_IT_RXNE, ENABLE);
    USART_Cmd(USART1, ENABLE);

    NVIC_InitTypeDef nvic;
    nvic.NVIC_IRQChannel                   = USART1_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 1;
    nvic.NVIC_IRQChannelSubPriority        = 1;
    nvic.NVIC_IRQChannelCmd                = ENABLE;
    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_2);
    NVIC_Init(&nvic);
}
"""

# 注释与字符串里的符号名不算引用（防误报）
SELFTEST_COMMENT_ONLY = """
/* 文档里提到 GPIO_NotARealThing 是说明文字，不是代码引用 */
// 同理 USART_Fake_Too
const char *help = "用 RCC_APB2Periph_Fake 也可以";
"""

SELFTEST_BAD_EXPECTED = {
    "GPIO_Speed_100MHz",
    "USART_WordLength_12b",
    "RCC_APB2Periph_USART9",
    "NVIC_PriorityGroup_9",
    "GPIO_FakeFunction",
}


def selftest(index: SymbolIndex) -> int:
    """用内置样例验证护栏本身有效。

    测两件事：**该报的报了**（反例全覆盖）、**不该报的没报**（正例零误报）。
    只测前者的护栏会越来越吵，最后被用户绕过。
    """
    failures: list[str] = []

    bad_problems = find_invented(SELFTEST_BAD, index, "selftest-bad")
    found = {item.name for item in bad_problems}

    missing = SELFTEST_BAD_EXPECTED - found
    if missing:
        failures.append(f"护栏漏检（应当报错但没报）：{', '.join(sorted(missing))}")

    extra = found - SELFTEST_BAD_EXPECTED
    if extra:
        failures.append(f"护栏误报：{', '.join(sorted(extra))}")

    good_problems = find_invented(SELFTEST_GOOD, index, "selftest-good")
    if good_problems:
        failures.append(
            "合法样例被误判：\n         "
            + "\n         ".join(item.format() for item in good_problems)
        )

    comment_problems = find_invented(SELFTEST_COMMENT_ONLY, index, "selftest-comment")
    if comment_problems:
        failures.append(
            "注释/字符串里的符号被误判为引用：\n         "
            + "\n         ".join(item.format() for item in comment_problems)
        )

    print(f"反例样例应报 {len(SELFTEST_BAD_EXPECTED)} 个，实报 {len(found)} 个")
    for item in sorted(found):
        print(f"    {item}")
    print(f"正例样例应报 0 个，实报 {len(good_problems)} 个")
    print(f"纯注释样例应报 0 个，实报 {len(comment_problems)} 个")

    if failures:
        print("[FAIL] 护栏自测未通过：", file=sys.stderr)
        for item in failures:
            print(f"       - {item}", file=sys.stderr)
        return 2

    print("[ OK ] 护栏自测通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()

    parser = argparse.ArgumentParser(
        description="校验 C 代码中使用的 STM32 符号都真实存在于标准库",
    )
    parser.add_argument("paths", nargs="*", help="要检查的 .c/.h 文件或目录")
    parser.add_argument(
        "--symbols", default=str(DEFAULT_SYMBOLS_PATH), help="symbols.json 路径"
    )
    parser.add_argument("--selftest", action="store_true", help="用内置样例自测护栏")
    args = parser.parse_args(argv)

    symbols_path = Path(args.symbols)
    if not symbols_path.is_file():
        print(
            f"[FAIL] 找不到 {symbols_path}\n"
            "       先运行：python tools\\extract_symbols.py",
            file=sys.stderr,
        )
        return 1

    index = SymbolIndex.load(symbols_path)
    stats = index.stats
    print(
        f"符号表：{symbols_path.name}  "
        f"（宏 {stats['macros']} / 枚举 {stats['enum_values']} / "
        f"函数 {stats['functions']} / 类型 {stats['types']}）"
    )

    if args.selftest:
        return selftest(index)

    if not args.paths:
        parser.print_help()
        return 0

    targets = collect_c_files(args.paths)
    if not targets:
        print("[FAIL] 没有找到任何 .c/.h 文件", file=sys.stderr)
        return 1

    problems = []
    for target in targets:
        text = target.read_text(encoding="utf-8", errors="replace")
        problems.extend(find_invented(text, index, str(target)))

    print(f"检查文件：{len(targets)} 个")
    if problems:
        print(f"[FAIL] 发现 {len(problems)} 个编造符号：", file=sys.stderr)
        for item in problems:
            print(f"       {item.format()}", file=sys.stderr)
        return 2

    print("[ OK ] 未发现编造符号")
    return 0


if __name__ == "__main__":
    sys.exit(main())
