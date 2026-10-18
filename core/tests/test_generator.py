"""生成器层的测试。

覆盖三层：

1. **上下文构建** —— 时钟分组、GPIO 合并、带标签引脚、中断清标志规则
2. **渲染** —— 产物齐全、语法结构正确、条件分支正确
3. **反编造集成** —— 生成代码里的每个 STM32 符号都必须在标准库里存在

第 3 项是最关键的：它把「禁止编造」从工具约束变成生成器的**输出约束**。
"""

from __future__ import annotations

import re

import pytest

from stm32gen.generator import (
    OUTPUT_SPECS,
    TEMPLATES_DIR,
    GeneratedFile,
    build_context,
    generate,
    list_templates,
)
from stm32gen.generator.layout import OutputLayout
from stm32gen.generator.merge import MergeError, merge_split
from stm32gen.models import (
    GpioPinConfig,
    NvicChannelConfig,
    NvicConfig,
    Stm32Config,
    UsartConfig,
    make_demo_config,
)


@pytest.fixture(scope="module")
def demo_result():
    return generate(make_demo_config())


def _strip_comments_and_strings(text: str) -> str:
    """抹掉注释与字符串，用于「反向提取符号」这类检查。

    直接复用包内护栏的实现（``stdlib_ref.guard``），避免两份逻辑漂移。
    """
    from stm32gen.stdlib_ref import strip_c_comments_and_strings

    return strip_c_comments_and_strings(text)


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
    "bsp_adc.h",
    "bsp_adc.c",
    "bsp_nvic.h",
    "bsp_nvic.c",
    "bsp_it.h",
    "bsp_it.c",
    "main.c",
)


# --------------------------------------------------------------------------
# 模板清单与产物
# --------------------------------------------------------------------------


def test_all_templates_exist() -> None:
    """OUTPUT_SPECS 里声明的模板必须都在磁盘上。"""
    available = set(list_templates())
    for template_name, _, _ in OUTPUT_SPECS:
        assert template_name in available, f"缺少模板 {template_name}"


def test_templates_dir_exists() -> None:
    assert TEMPLATES_DIR.is_dir()


def test_generate_produces_all_files(demo_result) -> None:
    """全部产物一个都不能少（按外设分文件）。"""
    names = {item.name for item in demo_result.files}
    assert names == set(ALL_GENERATED_FILES)


EXPECTED_FILE_COUNT = len(ALL_GENERATED_FILES)


def test_output_specs_are_per_peripheral() -> None:
    """按外设拆分：每个外设一个 .h/.c 对，加上聚合入口、中断骨架与 main。"""
    names = [output for _, output, _ in OUTPUT_SPECS]
    for peripheral in ("rcc", "gpio", "usart", "adc", "nvic"):
        assert f"bsp_{peripheral}.h" in names
        assert f"bsp_{peripheral}.c" in names
    # 聚合入口不再包含外设实现细节，只做顺序编排
    assert names[0] == "bsp_init.h"
    assert names[1] == "bsp_init.c"


def test_each_peripheral_c_is_paired_with_header() -> None:
    """R7.1：每个 .c 配一个同名 .h。

    例外是 main.c —— 它是程序入口，标准做法就是单独一个 .c，
    没有配套头文件（没有任何模块需要 include 它）。
    """
    names = {output for _, output, _ in OUTPUT_SPECS}
    for name in names:
        if name.endswith(".c") and name != "main.c":
            assert name[:-2] + ".h" in names, f"{name} 没有同名头文件"


def test_generate_is_pure_function(demo_result) -> None:
    """生成器不写文件——重复调用应得到相同结果。"""
    again = generate(make_demo_config())
    assert [item.content for item in again.files] == [
        item.content for item in demo_result.files
    ]


def test_output_is_deterministic() -> None:
    """同一配置多次生成，结果必须逐字节相同（便于 diff 与快照测试）。"""
    first = generate(make_demo_config())
    second = generate(make_demo_config())
    for a, b in zip(first.files, second.files, strict=True):
        assert a.content == b.content, f"{a.name} 两次生成结果不一致"


def test_generated_file_helpers(tmp_path) -> None:
    item = GeneratedFile(name="x.c", content="int a;\nint b;\n")
    assert item.lines == 2
    assert item.size_bytes > 0

    path = item.write(tmp_path)
    assert path.read_text(encoding="utf-8") == item.content


def test_write_refuses_overwrite(tmp_path) -> None:
    """overwrite=False 时已存在的文件要报错，供 GUI 做覆盖确认。"""
    item = GeneratedFile(name="x.c", content="int a;\n")
    item.write(tmp_path)
    with pytest.raises(FileExistsError):
        item.write(tmp_path, overwrite=False)


def test_write_all(tmp_path) -> None:
    """``write_all`` 返回 ``(已写入路径, 跳过说明)``。"""
    result = generate(make_demo_config())
    written, skipped = result.write_all(tmp_path)
    assert len(written) == EXPECTED_FILE_COUNT
    assert skipped == []
    assert all(path.is_file() for path in written)


def test_write_all_skip_existing(tmp_path) -> None:
    """``skip_existing=True`` 只跳过已存在文件，其余照常写。"""
    result = generate(make_demo_config())
    result.write_all(tmp_path)

    # 模拟「用户保留了部分文件」：删掉其余，只留手改过的那一个
    keep = tmp_path / "bsp_gpio.c"
    keep.write_text("// 手写内容", encoding="utf-8")
    for item in result.files:
        target = tmp_path / item.name
        if target != keep:
            target.unlink()

    written, skipped = result.write_all(tmp_path, skip_existing=True)
    assert len(written) == EXPECTED_FILE_COUNT - 1
    assert len(skipped) == 1
    assert "bsp_gpio.c" in skipped[0]
    # 手写内容必须原样保留
    assert keep.read_text(encoding="utf-8") == "// 手写内容"


def test_write_all_skip_existing_all_present(tmp_path) -> None:
    """全部已存在时，一个都不写。"""
    result = generate(make_demo_config())
    result.write_all(tmp_path)
    written, skipped = result.write_all(tmp_path, skip_existing=True)
    assert written == []
    assert len(skipped) == EXPECTED_FILE_COUNT


def test_write_all_overwrite_false_raises(tmp_path) -> None:
    """``overwrite=False`` 与 ``skip_existing=True`` 语义不同：前者报错。

    这个区别是有意的：``overwrite=False`` 用于「全有或全无」的场景
    （如 GUI 需要用户确认后才动任何文件），``skip_existing`` 用于
    「增量补齐」的场景（如 CLI 的 --no-overwrite）。
    """
    result = generate(make_demo_config())
    result.write_all(tmp_path)
    with pytest.raises(FileExistsError):
        result.write_all(tmp_path, overwrite=False)


# --------------------------------------------------------------------------
# 上下文：时钟使能
# --------------------------------------------------------------------------


def test_clock_enables_grouped_by_peripheral() -> None:
    """时钟使能按**外设归属**分组，而不是按总线全局合并。

    拆分后每个外设模块管自己的时钟：GPIO 模块（端口 + AFIO）在 bsp_gpio.c，
    USART 模块在 bsp_usart.c。所以同一个 APB2 上会有两条语句 ——
    这是刻意的取舍：删掉某个外设时它的时钟使能跟着同一个文件消失，
    不会在聚合文件里留下孤儿代码。
    """
    config = Stm32Config(
        gpio=[
            GpioPinConfig(
                port="C", pin=13, mode="Out_PP", speed="SPEED_2MHz", init_level="Bit_SET"
            )
        ],
        usart=[UsartConfig(instance="USART1"), UsartConfig(instance="USART2")],
    )
    ctx = build_context(config)

    # GPIO 模块：GPIOC 在 APB2
    assert ctx.gpio_clock is not None
    assert ctx.gpio_clock.bus == "APB2"
    assert "RCC_APB2Periph_GPIOC" in ctx.gpio_clock.macros
    assert not any("USART" in macro for macro in ctx.gpio_clock.macros)

    # USART 模块：USART1 在 APB2、USART2 在 APB1，两条语句各管一个总线
    apb2 = ctx.usart_apb2_clock
    apb1 = ctx.apb1_clock
    assert apb2 is not None and apb2.bus == "APB2"
    assert "RCC_APB2Periph_USART1" in apb2.macros
    assert apb1 is not None and apb1.bus == "APB1"
    assert "RCC_APB1Periph_USART2" in apb1.macros
    # APB1 语句不该混入 APB2 的宏
    assert not any("APB2" in macro for macro in apb1.macros)
    # USART 的时钟宏不该跑到 GPIO 模块里
    assert not any("USART" in macro for macro in ctx.gpio_clock.macros)


def test_clock_enable_function_name() -> None:
    """总线与函数名必须对应。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART1")])
    ctx = build_context(config)
    assert ctx.usart_apb2_clock.function == "RCC_APB2PeriphClockCmd"

    config2 = Stm32Config(usart=[UsartConfig(instance="USART2")])
    ctx2 = build_context(config2)
    assert ctx2.apb1_clock.function == "RCC_APB1PeriphClockCmd"


def test_afio_clock_enabled_for_remap() -> None:
    """用了重映射就必须开 AFIO 时钟，否则 GPIO_PinRemapConfig 无效。

    **AFIO 时钟归 bsp_afio 模块**（不是 GPIO 模块）：重映射改的是
    ``AFIO->MAPR``，而 ``bsp_afio_init()`` 排在 ``bsp_gpio_init()`` 之前
    —— 重映射必须在 GPIO_Init 之前生效。早期版本把 AFIO 时钟放在 GPIO
    模块里，拆出 AFIO 后归位。
    """
    without = build_context(Stm32Config(usart=[UsartConfig(instance="USART1")]))
    # 无重映射时不该有 AFIO 时钟
    assert not any(
        "AFIO" in macro for macro in (without.gpio_clock.macros if without.gpio_clock else ())
    )
    assert not without.afio.has_clock

    with_remap = build_context(
        Stm32Config(usart=[UsartConfig(instance="USART1", remap="USART1")])
    )
    assert with_remap.afio.has_clock
    assert "RCC_APB2Periph_AFIO" in with_remap.afio.clock.macros
    # GPIO 模块只负责端口时钟，不该混入 AFIO
    assert not any("AFIO" in macro for macro in with_remap.gpio_clock.macros)


def test_usart_pins_included_in_used_ports() -> None:
    """USART 的引脚所在端口也要使能时钟（在 GPIO 模块里）。"""
    config = Stm32Config(usart=[UsartConfig(instance="USART3")])  # PB10/PB11
    ctx = build_context(config)
    assert "RCC_APB2Periph_GPIOB" in ctx.gpio_clock.macros


# --------------------------------------------------------------------------
# 上下文：GPIO
# --------------------------------------------------------------------------


def test_gpio_configs_include_usart_pins() -> None:
    """USART 的 TX/RX 引脚要自动生成 GPIO_Init 配置。

    TX 是复用推挽输出，RX 是浮空输入 —— 这是串口的标准配法。
    """
    config = Stm32Config(usart=[UsartConfig(instance="USART1")])
    ctx = build_context(config)
    modes = {item.port: item.mode for item in ctx.gpio_configs}
    assert modes["GPIOA"] in ("GPIO_Mode_AF_PP", "GPIO_Mode_IN_FLOATING")

    tx = next(item for item in ctx.gpio_configs if "TX" in item.comment)
    rx = next(item for item in ctx.gpio_configs if "RX" in item.comment)
    assert tx.mode == "GPIO_Mode_AF_PP"
    assert tx.pin_macro == "GPIO_Pin_9"
    assert rx.mode == "GPIO_Mode_IN_FLOATING"
    assert rx.pin_macro == "GPIO_Pin_10"


def test_gpio_same_config_merged() -> None:
    """同端口 + 同模式 + 同速度的引脚合并成一次 GPIO_Init。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="A", pin=0, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET"),
            GpioPinConfig(port="A", pin=1, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET"),
        ]
    )
    ctx = build_context(config)
    assert len(ctx.gpio_configs) == 1
    assert ctx.gpio_configs[0].pin_macro == "GPIO_Pin_0 | GPIO_Pin_1"


def test_gpio_different_speed_not_merged() -> None:
    """速度不同不能合并——一次 GPIO_Init 只有一个速度字段。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="A", pin=0, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET"),
            GpioPinConfig(port="A", pin=1, mode="Out_PP", speed="SPEED_50MHz",
                          init_level="Bit_SET"),
        ]
    )
    ctx = build_context(config)
    assert len(ctx.gpio_configs) == 2


def test_gpio_init_level_recorded() -> None:
    """输出引脚的初始电平要传下去，模板据此生成 Set/Reset。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="C", pin=13, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_RESET")
        ]
    )
    ctx = build_context(config)
    assert ctx.gpio_configs[0].init_level == "Bit_RESET"
    assert ctx.gpio_configs[0].needs_level_init


def test_gpio_input_has_no_level_init() -> None:
    """输入模式不该有初始电平，模板据此跳过 Set/Reset。"""
    config = Stm32Config(gpio=[GpioPinConfig(port="A", pin=1, mode="IPU")])
    ctx = build_context(config)
    assert ctx.gpio_configs[0].init_level is None
    assert not ctx.gpio_configs[0].needs_level_init


# --------------------------------------------------------------------------
# 上下文：带标签引脚
# --------------------------------------------------------------------------


def test_named_pins_from_labels() -> None:
    """带标签的引脚生成 PORT/PIN 宏。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="C", pin=13, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET", label="LED1")
        ]
    )
    ctx = build_context(config)
    assert len(ctx.named_pins) == 1
    assert ctx.named_pins[0].macro_name == "BSP_LED1"
    assert ctx.named_pins[0].port == "GPIOC"
    assert ctx.named_pins[0].pin_macro == "GPIO_Pin_13"


def test_named_pins_skip_unlabeled() -> None:
    """没标签的引脚不生成宏——否则头文件全是噪音。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="C", pin=13, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET"),
            GpioPinConfig(port="C", pin=14, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET", label="LED2"),
        ]
    )
    ctx = build_context(config)
    assert len(ctx.named_pins) == 1
    assert ctx.named_pins[0].macro_name == "BSP_LED2"


def test_named_pins_sanitize_chinese_label() -> None:
    """中文标签要能转成合法 C 标识符。

    "LED1 低电平点亮" 里的空格和中文会被替换/剔除，
    结果必须是合法标识符。
    """
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="C", pin=13, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET", label="LED1 低电平点亮")
        ]
    )
    ctx = build_context(config)
    macro = ctx.named_pins[0].macro_name
    assert macro.isidentifier(), f"{macro} 不是合法标识符"
    assert macro.startswith("BSP_")


def test_named_pins_deduplicate() -> None:
    """同名标签要加序号后缀，避免宏重定义。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="A", pin=0, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET", label="LED"),
            GpioPinConfig(port="A", pin=1, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET", label="LED"),
        ]
    )
    ctx = build_context(config)
    names = [item.macro_name for item in ctx.named_pins]
    assert len(names) == len(set(names)), f"宏名重复：{names}"


def test_named_pins_numeric_label() -> None:
    """以数字开头的标签要加前缀，否则不是合法标识符。"""
    config = Stm32Config(
        gpio=[
            GpioPinConfig(port="A", pin=0, mode="Out_PP", speed="SPEED_2MHz",
                          init_level="Bit_SET", label="1WIRE")
        ]
    )
    ctx = build_context(config)
    assert ctx.named_pins[0].macro_name.isidentifier()


# --------------------------------------------------------------------------
# 上下文：USART 与波特率
# --------------------------------------------------------------------------


def test_usart_context_has_brr() -> None:
    """上下文要带上复算出的 BRR 与误差，写进生成的注释便于核对。"""
    ctx = build_context(Stm32Config(usart=[UsartConfig(instance="USART1")]))
    item = ctx.usart_configs[0]
    assert item.brr_hex == "0x0271"
    assert item.baud_error == "+0.000"
    assert item.baud_actual == "115200.0"


def test_usart_mode_expression() -> None:
    """方向要展开成位或表达式。"""
    ctx = build_context(
        Stm32Config(usart=[UsartConfig(instance="USART1", direction="RX_TX")])
    )
    assert ctx.usart_configs[0].mode_expression == "USART_Mode_Rx | USART_Mode_Tx"


def test_usart_apb1_uses_36mhz() -> None:
    """USART2 挂 APB1，波特率按 36MHz 复算。"""
    ctx = build_context(Stm32Config(usart=[UsartConfig(instance="USART2")]))
    item = ctx.usart_configs[0]
    assert item.brr_hex == "0x0138"  # 312，对应 36MHz


# --------------------------------------------------------------------------
# 上下文：中断清标志规则（最容易出错的地方）
# --------------------------------------------------------------------------


def test_rxne_cleared_by_receive_data() -> None:
    """RXNE 用读 DR 清除。"""
    ctx = build_context(
        Stm32Config(usart=[UsartConfig(instance="USART1", interrupts=["RXNE"])])
    )
    check = ctx.handlers[0].checks[0]
    assert check.it_macro == "USART_IT_RXNE"
    assert "USART_ReceiveData(USART1)" in check.clear_stmt


def test_idle_does_not_use_clear_it_pending_bit() -> None:
    """IDLE 必须用「先读 SR 再读 DR」清除，**不能**用 USART_ClearITPendingBit。

    这是关键正确性点：库的 ``IS_USART_CLEAR_IT``（stm32f10x_usart.h:256-257）
    只允许 TC / RXNE / LBD / CTS。对 IDLE 调 ClearITPendingBit 会触发
    assert_param 失败，且标志实际清不掉 —— 中断会一直重入。

    依据 stm32f10x_usart.c:1003-1011 的 @note。
    """
    ctx = build_context(
        Stm32Config(usart=[UsartConfig(instance="USART1", interrupts=["IDLE"])])
    )
    check = ctx.handlers[0].checks[0]
    assert check.it_macro == "USART_IT_IDLE"
    assert "ClearITPendingBit" not in check.clear_stmt
    assert "USART_ReceiveData" in check.clear_stmt
    assert "先读 SR 再读 DR" in check.clear_note


def test_pe_does_not_use_clear_it_pending_bit() -> None:
    """PE 同样不在 IS_USART_CLEAR_IT 里。"""
    ctx = build_context(
        Stm32Config(usart=[UsartConfig(instance="USART1", interrupts=["PE"])])
    )
    check = ctx.handlers[0].checks[0]
    assert "ClearITPendingBit" not in check.clear_stmt


def test_tc_uses_clear_it_pending_bit() -> None:
    """TC 在 IS_USART_CLEAR_IT 里，可以用 ClearITPendingBit。"""
    ctx = build_context(
        Stm32Config(usart=[UsartConfig(instance="USART1", interrupts=["TC"])])
    )
    check = ctx.handlers[0].checks[0]
    assert "USART_ClearITPendingBit" in check.clear_stmt


def test_txe_hint_mentions_write_dr() -> None:
    """TXE 只能靠写 DR 清除，提示要说清楚。"""
    ctx = build_context(
        Stm32Config(usart=[UsartConfig(instance="USART1", interrupts=["TXE"])])
    )
    check = ctx.handlers[0].checks[0]
    assert "USART_DR" in check.clear_note


def test_multi_interrupt_handler() -> None:
    """多个中断源要生成多个检查分支。"""
    ctx = build_context(
        Stm32Config(
            usart=[UsartConfig(instance="USART1", interrupts=["RXNE", "IDLE"])]
        )
    )
    handler = ctx.handlers[0]
    assert handler.is_multi
    assert len(handler.checks) == 2
    assert handler.handler == "USART1_IRQHandler"


def test_handler_only_for_interrupt_enabled_usart() -> None:
    """没使能中断的 USART 不生成中断服务函数。"""
    ctx = build_context(Stm32Config(usart=[UsartConfig(instance="USART1")]))
    assert ctx.handlers == []


def test_handler_name_matches_startup_vector() -> None:
    """中断函数名必须与启动文件向量表里的符号一致。

    启动文件 ``Start\\startup_stm32f10x_md.s`` 的向量表与 ``EXPORT ... [WEAK]``
    段里用的是 ``USART1_IRQHandler`` 这种命名。
    """
    ctx = build_context(
        Stm32Config(
            usart=[
                UsartConfig(instance="USART1", interrupts=["RXNE"]),
                UsartConfig(instance="USART2", interrupts=["RXNE"]),
            ]
        )
    )
    names = [item.handler for item in ctx.handlers]
    assert names == ["USART1_IRQHandler", "USART2_IRQHandler"]


# --------------------------------------------------------------------------
# 上下文：NVIC
# --------------------------------------------------------------------------


def test_nvic_channels_skip_disabled() -> None:
    """禁用的通道不生成 NVIC_Init。"""
    config = Stm32Config(
        nvic=NvicConfig(
            channels=[
                NvicChannelConfig(irq_channel="USART1_IRQn"),
                NvicChannelConfig(irq_channel="USART2_IRQn", enabled=False),
            ]
        )
    )
    ctx = build_context(config)
    assert len(ctx.nvic_channels) == 1
    assert ctx.nvic_channels[0].irq_channel == "USART1_IRQn"


def test_nvic_group_recorded() -> None:
    ctx = build_context(
        Stm32Config(nvic=NvicConfig(priority_group="NVIC_PriorityGroup_4"))
    )
    assert ctx.nvic_group == "NVIC_PriorityGroup_4"
    assert "抢占 4 位" in ctx.nvic_group_note


# --------------------------------------------------------------------------
# 渲染：产物内容
# --------------------------------------------------------------------------


def test_generated_code_has_no_template_leftovers(demo_result) -> None:
    """生成结果里不能残留 Jinja2 标记。"""
    for item in demo_result.files:
        assert "{{" not in item.content, f"{item.name} 残留 {{{{"
        assert "{%" not in item.content, f"{item.name} 残留 {{%"
        assert "{#" not in item.content, f"{item.name} 残留 {{#"


def test_generated_code_uses_lf(demo_result) -> None:
    """统一用 LF 换行，避免 CRLF 造成 diff 噪声。"""
    for item in demo_result.files:
        assert "\r" not in item.content, f"{item.name} 含 CR"


def test_generated_code_ends_with_newline(demo_result) -> None:
    for item in demo_result.files:
        assert item.content.endswith("\n"), f"{item.name} 末尾无换行"


def test_include_guards_present(demo_result) -> None:
    """头文件要有 include guard。"""
    for name in ("bsp_init.h", "bsp_it.h"):
        content = demo_result.by_name(name).content
        assert "#ifndef" in content
        assert "#define" in content
        assert "#endif" in content


# 空样板注释：只有分隔线、不含任何信息，属于「代码本身已说明做什么」的
# 典型反例（code-standards R9.1：注释解释为什么，不解释做什么）。
#
# 这些段落在 ST 的模板文件里有意义 —— 它是给人**手写**代码时填空用的骨架。
# 但本工具产出的是成品，每一段都是算好填好的，留一行空标题只是噪音。
_IRRELEVANT_COMMENT_PATTERNS = (
    "Private typedef",
    "Private define",
    "Private macro",
    "Private variables",
    "Private function prototypes",
    "Private functions",
    # 版权行不只是无关，而且是**错的**：这些代码由本工具生成，不是 ST 的。
    "COPYRIGHT 2011 STMicroelectronics",
)


@pytest.mark.parametrize("name", ["bsp_init.c", "bsp_rcc.c", "bsp_gpio.c",
                                  "bsp_usart.c", "bsp_nvic.c", "bsp_it.c", "main.c"])
def test_sources_have_no_boilerplate_comments(name) -> None:
    """``.c`` 里不该有空样板注释段。"""
    content = generate(make_demo_config()).by_name(name).content
    for pattern in _IRRELEVANT_COMMENT_PATTERNS:
        assert pattern not in content, f"{name} 残留样板注释：{pattern}"


def test_no_false_copyright_in_sources() -> None:
    """``.c`` 里不能带 ST 版权行 —— 那是错误的归属。

    这行是从 ST 库的模板文件里抄来的，但生成代码不是 ST 的作品。
    留在文件里会让人误以为需要遵守 ST 的授权条款，属于事实上不正确的信息。

    （本轮只清理 ``.c``；``.h`` 里的同款行与 ``Includes`` / ``Exported xxx``
    分隔线一样，属于库文件沿用的分节惯例，保持与 ST 头文件观感一致。）
    """
    for item in generate(make_demo_config()).files:
        if item.name.endswith(".c"):
            assert "COPYRIGHT" not in item.content, f"{item.name} 带版权行"


def test_source_comments_are_not_all_stripped() -> None:
    """清理样板时不能把**有信息量**的注释一起删掉。

    这条是上一条的反向约束：一味删注释很容易连「为什么」也删了，
    而 code-standards R9.1 要求注释解释为什么。
    """
    content = generate(make_demo_config()).by_name("bsp_gpio.c").content
    # 保留：先写 ODR 再切输出模式的理由
    assert "先写 ODR" in content
    # 保留：USART 引脚为什么配成浮空输入
    assert "GPIO_Mode_IN_FLOATING" in content


def test_init_call_order(demo_result) -> None:
    """bsp_init() 里的调用顺序必须是 时钟 → GPIO → USART → NVIC。

    顺序错了后果具体：时钟没配好时后续寄存器写入无效（最常见的「配了没用」）；
    NVIC 若排在前面，中断通道一开而外设还没配好，会进一次不完整的中断。
    """
    content = demo_result.by_name("bsp_init.c").content
    body = content.split("void bsp_init(void)")[1]
    rcc_pos = body.index("bsp_rcc_init()")
    gpio_pos = body.index("bsp_gpio_init()")
    usart_pos = body.index("bsp_usart_init()")
    nvic_pos = body.index("bsp_nvic_init()")
    assert rcc_pos < gpio_pos < usart_pos < nvic_pos


def test_aggregator_has_no_peripheral_details(demo_result) -> None:
    """聚合入口只做编排，不含外设配置细节。

    这是拆分的核心目的：外设细节都在各自的 bsp_xxx.c 里，
    想只初始化某一部分时直接调对应的 bsp_xxx_init() 即可。
    """
    content = demo_result.by_name("bsp_init.c").content
    for leaked in (
        "GPIO_Init(",
        "USART_Init(",
        "NVIC_Init(",
        "RCC_HSEConfig(",
        "FLASH_SetLatency(",
    ):
        assert leaked not in content, f"bsp_init.c 里不该出现 {leaked}"


def test_gpio_level_set_before_init(demo_result) -> None:
    """初始电平必须在 GPIO_Init 之前设置，否则切到输出模式时会抖一下。"""
    content = demo_result.by_name("bsp_gpio.c").content
    set_pos = content.index("GPIO_SetBits(GPIOC, GPIO_Pin_13)")
    init_pos = content.index("GPIO_Init(GPIOC, &GPIO_InitStructure)")
    assert set_pos < init_pos


def test_gpio_clock_enabled_before_use(demo_result) -> None:
    """时钟使能必须在 GPIO_Init 之前 —— 否则配置不生效。"""
    content = demo_result.by_name("bsp_gpio.c").content
    clock_pos = content.index("RCC_APB2PeriphClockCmd(")
    init_pos = content.index("GPIO_Init(GPIOC, &GPIO_InitStructure)")
    assert clock_pos < init_pos


def test_nvic_group_config_called_once(demo_result) -> None:
    """NVIC_PriorityGroupConfig 整个固件只能调用一次。"""
    content = demo_result.by_name("bsp_nvic.c").content
    assert content.count("NVIC_PriorityGroupConfig(") == 1
    # 且不能别处也调一次
    for name in ("bsp_init.c", "bsp_gpio.c", "bsp_usart.c"):
        assert "NVIC_PriorityGroupConfig(" not in demo_result.by_name(name).content


def test_named_pin_macros_in_gpio_header(demo_result) -> None:
    """带标签引脚的宏在 bsp_gpio.h 里（引脚是 GPIO 的职责）。"""
    content = demo_result.by_name("bsp_gpio.h").content
    assert "#define BSP_LED1_PORT      GPIOC" in content
    assert "#define BSP_LED1_PIN       GPIO_Pin_13" in content


def test_aggregator_header_includes_peripheral_headers(demo_result) -> None:
    """bsp_init.h 要包含各外设头文件，业务代码只 include 一个就够。"""
    content = demo_result.by_name("bsp_init.h").content
    for name in ("bsp_rcc.h", "bsp_gpio.h", "bsp_usart.h", "bsp_nvic.h"):
        assert f'#include "{name}"' in content


def test_brr_documented_in_comment(demo_result) -> None:
    """生成的注释要带上 BRR，便于与寄存器/示波器对照。"""
    content = demo_result.by_name("bsp_usart.c").content
    assert "0x0271" in content


def test_rcc_header_exposes_frequency_constants(demo_result) -> None:
    """bsp_rcc.h 要给出频率常量，业务代码不该写死数字。"""
    content = demo_result.by_name("bsp_rcc.h").content
    assert "#define BSP_SYSCLK_HZ       72000000u" in content
    assert "#define BSP_PCLK1_HZ        36000000u" in content
    # APB1 分频 != 1 时定时器时钟是 PCLK 的 2 倍，极易算错
    assert "#define BSP_TIM234_HZ" in content


def test_rcc_init_sequence_and_guard(demo_result) -> None:
    """bsp_rcc.c 的顺序与超时守卫。

    顺序：Flash 等待周期 → HSE/PLL → 总线分频 → SYSCLK 切换。
    Flash 必须最先：HCLK 拉高前就得把等待周期设好，否则那段窗口取指不稳。
    每处等待硬件标志的循环都必须带上限（code-standards R4.3）——
    硬件坏掉时无限等待会让固件卡在启动阶段。
    """
    content = demo_result.by_name("bsp_rcc.c").content
    flash_pos = content.index("FLASH_SetLatency(")
    hse_pos = content.index("RCC_HSEConfig(")
    bus_pos = content.index("RCC_HCLKConfig(")
    sysclk_pos = content.index("RCC_SYSCLKConfig(")
    assert flash_pos < hse_pos < bus_pos < sysclk_pos

    # 等待标志的循环必须是 for 带上限，不能是 while(1) 式的裸循环
    assert "BSP_RCC_WAIT_LOOPS" in content
    assert "for (guard = 0; guard < BSP_RCC_WAIT_LOOPS; guard++)" in content
    # HSE 用库自带超时的等待函数，而不是裸循环
    assert "RCC_WaitForHSEStartUp()" in content


def test_rcc_disabled_generates_empty_impl() -> None:
    """rcc.enabled=False 时 bsp_rcc_init() 是空实现，但仍存在。

    保留空函数而不是条件性地不调用：函数体在所有配置下都一致，
    「为什么我的时钟没配」这类问题才好定位。
    """
    from stm32gen.models import RccConfig

    result = generate(Stm32Config(rcc=RccConfig(enabled=False)))
    content = result.by_name("bsp_rcc.c").content
    assert "void bsp_rcc_init(void)" in content
    assert "FLASH_SetLatency(" not in content
    assert "RCC_SYSCLKConfig(" not in content
    assert content.count("{") == content.count("}")


def test_main_calls_bsp_init(demo_result) -> None:
    content = demo_result.by_name("main.c").content
    assert "bsp_init();" in content
    assert "while (1)" in content


# --------------------------------------------------------------------------
# 渲染：条件分支
# --------------------------------------------------------------------------


def test_empty_config_generates_compilable_skeleton() -> None:
    """空配置也要能生成结构完整的代码，不能出现语法断裂。

    拆分后每个外设模块都会产出文件（即使配置为空），
    因为「模块存在但内容为空」比「模块时有时无」更好维护 ——
    bsp_init() 的调用序列在所有配置下完全一致。
    """
    result = generate(Stm32Config())
    content = result.by_name("bsp_init.c").content
    assert content.count("void bsp_init(void)") == 1
    # 四个外设的 init 都被调用，函数体各自在对应文件里
    for name in ("bsp_rcc_init()", "bsp_gpio_init()", "bsp_usart_init()", "bsp_nvic_init()"):
        assert name in content
    assert content.count("{") == content.count("}")

    # 每个外设文件都有对应函数定义
    assert "void bsp_rcc_init(void)" in result.by_name("bsp_rcc.c").content
    assert "void bsp_gpio_init(void)" in result.by_name("bsp_gpio.c").content
    assert "void bsp_usart_init(void)" in result.by_name("bsp_usart.c").content
    assert "void bsp_nvic_init(void)" in result.by_name("bsp_nvic.c").content


def test_usart_without_interrupts_skips_it_file_content() -> None:
    """没使能中断时，bsp_it.c 应是空骨架而不是语法错误。"""
    result = generate(Stm32Config(usart=[UsartConfig(instance="USART1")]))
    content = result.by_name("bsp_it.c").content
    assert "IRQHandler" not in content
    assert content.count("{") == content.count("}")


def test_usart_only_config_generates_gpio_init() -> None:
    """只配 USART 不配 GPIO 时，USART 引脚仍需要 GPIO 配置。

    这是容易漏的边界：GPIO 模块的内容必须基于
    「是否真的需要配 GPIO」，而不是「用户有没有显式配 GPIO」。
    """
    result = generate(Stm32Config(usart=[UsartConfig(instance="USART1")]))
    content = result.by_name("bsp_gpio.c").content
    assert "void bsp_gpio_init(void)" in content
    assert "GPIO_Mode_AF_PP" in content  # TX
    assert "GPIO_Mode_IN_FLOATING" in content  # RX
    # USART 的引脚时钟也要在 GPIO 模块里使能
    assert "RCC_APB2Periph_GPIOA" in content


def test_remap_generates_pin_remap_config() -> None:
    """选了重映射要生成 GPIO_PinRemapConfig 调用（在 bsp_afio.c 里）。

    重映射归 AFIO 模块：它改的是 ``AFIO->MAPR``，而且必须早于
    ``GPIO_Init`` 生效 —— ``bsp_afio_init()`` 由 ``bsp_init()``
    排在 ``bsp_gpio_init()`` 之前调用。
    """
    result = generate(
        Stm32Config(usart=[UsartConfig(instance="USART1", remap="USART1")])
    )
    afio = result.by_name("bsp_afio.c").content
    assert "GPIO_PinRemapConfig" in afio
    assert "GPIO_Remap_USART1" in afio
    # AFIO 时钟必须使能，否则重映射不生效
    assert "RCC_APB2Periph_AFIO" in afio

    # 重映射后的引脚在 GPIO 模块里配成复用推挽输出
    gpio = result.by_name("bsp_gpio.c").content
    assert "GPIO_Pin_6" in gpio
    assert "GPIO_Pin_7" in gpio


def test_remap_config_before_gpio_init() -> None:
    """重映射必须在 GPIO_Init 之前生效 —— 靠的是**跨文件的调用顺序**。

    重映射改的是引脚的复用功能来源，配好之后再设引脚模式才有意义。
    本工具把它放在 ``bsp_afio_init()`` 里，并让 ``bsp_init()`` 先调它、
    再调 ``bsp_gpio_init()`` —— 这是唯一可靠的保证方式，
    因为 ``GPIO_PinRemapConfig`` 与 ``GPIO_Init`` 现在不在同一个文件里了。
    """
    result = generate(
        Stm32Config(usart=[UsartConfig(instance="USART1", remap="USART1")])
    )
    init = result.by_name("bsp_init.c").content
    body = init.split("void bsp_init(void)")[1]
    afio_pos = body.index("bsp_afio_init()")
    gpio_pos = body.index("bsp_gpio_init()")
    assert afio_pos < gpio_pos, "bsp_afio_init() 必须排在 bsp_gpio_init() 之前"

    # 且重映射确实在 AFIO 文件、GPIO_Init 确实在 GPIO 文件
    assert "GPIO_PinRemapConfig(" in result.by_name("bsp_afio.c").content
    assert "GPIO_Init(" in result.by_name("bsp_gpio.c").content


def test_no_remap_no_pin_remap_config() -> None:
    """没有重映射时 bsp_afio.c 不该**调用** GPIO_PinRemapConfig。

    注意断言的是调用形式（带左括号）而不是光秃秃的名字：文件顶部的
    说明注释里会提到这个函数名（解释 AFIO 时钟为什么必需），
    只查名字会误判成"生成了调用"。
    """
    result = generate(Stm32Config(usart=[UsartConfig(instance="USART1")]))
    content = result.by_name("bsp_afio.c").content
    assert "GPIO_PinRemapConfig(" not in content


def test_usart_clock_in_usart_module() -> None:
    """USART 的时钟使能在 bsp_usart.c 里，USART_Init 之前。"""
    result = generate(Stm32Config(usart=[UsartConfig(instance="USART2")]))
    content = result.by_name("bsp_usart.c").content
    clock_pos = content.index("RCC_APB1PeriphClockCmd(")
    init_pos = content.index("USART_Init(")
    assert clock_pos < init_pos


def test_usart_uses_both_bus_clock_statements() -> None:
    """同时用 USART1 与 USART2 时要生成两条时钟使能语句。

    USART1 在 APB2、USART2 在 APB1 —— 调试口用 USART1、业务口用 USART2
    是常见组合，只保留一条会让另一个外设没时钟。
    """
    result = generate(
        Stm32Config(
            usart=[UsartConfig(instance="USART1"), UsartConfig(instance="USART2")]
        )
    )
    content = result.by_name("bsp_usart.c").content
    assert "RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1, ENABLE);" in content
    assert "RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART2, ENABLE);" in content


def test_notes_for_empty_config() -> None:
    """生成器要说明为什么某些内容没生成。"""
    result = generate(Stm32Config())
    assert result.notes
    assert any("空" in note for note in result.notes)


# --------------------------------------------------------------------------
# 反编造集成：最关键的一组
# --------------------------------------------------------------------------


def test_symbols_used_are_all_real(demo_result) -> None:
    """生成器记录的每个符号都必须在标准库里存在。"""
    from stm32gen.stdlib_ref import default_index

    index = default_index()
    invented = sorted(name for name in demo_result.symbols_used if not index.has_any(name))
    assert not invented, f"生成器引用了不存在的符号：{invented}"


def test_symbols_used_non_empty(demo_result) -> None:
    assert len(demo_result.symbols_used) > 15


@pytest.mark.parametrize(
    "config_factory",
    [
        pytest.param(lambda: make_demo_config(), id="demo"),
        pytest.param(
            lambda: Stm32Config(usart=[UsartConfig(instance="USART1")]), id="usart-only"
        ),
        pytest.param(
            lambda: Stm32Config(usart=[UsartConfig(instance="USART1", remap="USART1")]),
            id="remap",
        ),
        pytest.param(
            lambda: Stm32Config(
                usart=[
                    UsartConfig(instance="USART1", direction="RX", interrupts=["IDLE"]),
                    UsartConfig(instance="USART2", direction="TX", interrupts=["TC"]),
                ]
            ),
            id="direction-variants",
        ),
    ],
)
def test_symbols_used_covers_generated_code(config_factory) -> None:
    """``symbols_used`` 必须覆盖生成代码里出现的每一个标准库符号。

    这个断言比「symbols_used 里的符号都真实存在」更强：它保证
    符号收集本身没有遗漏。若模板里手写了一个符号而 ``_collect_symbols``
    没记录，这里会失败 —— 那种遗漏会让反编造护栏失去意义。
    """
    from stm32gen.stdlib_ref import default_index, strip_c_comments_and_strings

    index = default_index()
    result = generate(config_factory())

    # 从生成代码里反向提取所有库前缀标识符。
    # 必须先抹掉注释与字符串：文档注释里提到 GPIO_SetBits 是**说明文字**，
    # 不算代码引用了它，否则会误报（第一版就栽在这里）。
    pattern = re.compile(r"\b(GPIO_\w+|USART_\w+|NVIC_\w+|RCC_\w+|FLASH_\w+|Bit_\w+)\b")
    found: set[str] = set()
    for item in result.files:
        cleaned = strip_c_comments_and_strings(item.content)
        for match in pattern.finditer(cleaned):
            name = match.group(1)
            # 结构体成员名（GPIO_Pin / USART_BaudRate 在赋值左边）不是库符号
            if index.has_any(name):
                found.add(name)

    missing = sorted(found - result.symbols_used)
    assert not missing, (
        f"这些符号出现在生成代码里但未记录进 symbols_used：{missing}"
    )


@pytest.mark.parametrize(
    "config_factory",
    [
        pytest.param(lambda: make_demo_config(), id="demo"),
        pytest.param(lambda: Stm32Config(), id="empty"),
        pytest.param(
            lambda: Stm32Config(usart=[UsartConfig(instance="USART1")]), id="usart-only"
        ),
        pytest.param(
            lambda: Stm32Config(usart=[UsartConfig(instance="USART1", remap="USART1")]),
            id="remap",
        ),
        pytest.param(
            lambda: Stm32Config(
                gpio=[
                    GpioPinConfig(port="A", pin=0, mode="Out_OD", speed="SPEED_10MHz",
                                  init_level="Bit_RESET", label="OD pin"),
                    GpioPinConfig(port="B", pin=5, mode="IPU"),
                ]
            ),
            id="mixed-gpio",
        ),
        pytest.param(
            lambda: Stm32Config(
                usart=[
                    UsartConfig(instance="USART1", baudrate=9600,
                                interrupts=["RXNE", "IDLE", "PE"]),
                    UsartConfig(instance="USART3", baudrate=57600, direction="TX",
                                interrupts=["TC"]),
                ],
                nvic=NvicConfig(
                    priority_group="NVIC_PriorityGroup_3",
                    channels=[
                        NvicChannelConfig(irq_channel="USART1_IRQn", preemption_priority=1),
                        NvicChannelConfig(irq_channel="USART3_IRQn", preemption_priority=2),
                    ],
                ),
            ),
            id="multi-usart-multi-irq",
        ),
    ],
)
def test_generated_code_passes_anti_invention_guard(config_factory) -> None:
    """生成的 C 代码必须通过反编造护栏。

    这是本步最关键的测试：它把「禁止编造」从工具约束变成
    生成器的**输出约束**。任何模板里手写错的符号都会在这里被抓到。
    """
    from stm32gen.stdlib_ref import default_index, find_invented

    index = default_index()
    result = generate(config_factory())

    problems = []
    for item in result.files:
        problems.extend(find_invented(item.content, index, item.name))

    assert not problems, "生成的代码含编造符号：\n" + "\n".join(
        problem.format() for problem in problems
    )


def test_all_generated_files_have_matching_braces(demo_result) -> None:
    """大括号必须配平——这是最基本的语法完整性检查。

    不做完整 C 解析（第 8 步交给 arm-none-eabi-gcc），
    但配平检查能抓到模板条件分支写错导致的断裂。
    """
    for item in demo_result.files:
        # 去掉字符串与字符字面量里的花括号（本工程生成代码里没有，保险起见）
        text = re.sub(r'"[^"\n]*"', "", item.content)
        assert text.count("{") == text.count("}"), f"{item.name} 大括号不配平"


def test_all_generated_files_have_matching_parens(demo_result) -> None:
    for item in demo_result.files:
        text = re.sub(r'"[^"\n]*"', "", item.content)
        assert text.count("(") == text.count(")"), f"{item.name} 圆括号不配平"


# --------------------------------------------------------------------------
# 合并布局：21 个文件 → bsp.h / bsp.c / main.c
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def merged_result():
    return generate(make_demo_config(), layout=OutputLayout.MERGED)


def test_merged_layout_produces_three_files(merged_result) -> None:
    """合并布局只出 3 个文件，且名字固定。"""
    assert {item.name for item in merged_result.files} == {"bsp.h", "bsp.c", "main.c"}


def test_split_layout_is_untouched_by_merge(merged_result) -> None:
    """默认布局仍是按外设分文件的布局（21 个）。"""
    split = generate(make_demo_config())
    assert len(split.files) == EXPECTED_FILE_COUNT
    assert len(merged_result.files) == 3


def test_merged_header_has_exactly_one_include_guard(merged_result) -> None:
    """9 个头文件的 guard 必须合成一个，否则会出现 9 层嵌套。"""
    text = {item.name: item.content for item in merged_result.files}["bsp.h"]
    assert text.count("#ifndef __BSP_H") == 1
    assert text.count("#endif /* __BSP_H */") == 1
    assert "#ifndef __BSP_RCC_H" not in text
    assert "#ifndef __BSP_GPIO_H" not in text


def test_merged_header_has_exactly_one_extern_c_block(merged_result) -> None:
    """extern "C" 也只能有一个 —— 9 个会变成 9 层空包裹。"""
    text = {item.name: item.content for item in merged_result.files}["bsp.h"]
    assert text.count('extern "C" {') == 1
    assert text.count("#include \"stm32f10x.h\"") == 1


def test_merged_source_has_no_peripheral_includes(merged_result) -> None:
    """bsp.c 只 include bsp.h，不该再出现 bsp_gpio.h 这类内层头文件。"""
    text = {item.name: item.content for item in merged_result.files}["bsp.c"]
    assert '#include "bsp.h"' in text
    for peripheral in ("rcc", "afio", "gpio", "exti", "usart", "tim", "nvic", "it", "init"):
        assert f'#include "bsp_{peripheral}.h"' not in text, f"bsp.c 仍 include bsp_{peripheral}.h"


def test_merged_declarations_all_present(demo_result, merged_result) -> None:
    """每个外设的 init 声明都要出现在 bsp.h 里。"""
    text = {item.name: item.content for item in merged_result.files}["bsp.h"]
    for function in (
        "bsp_init",
        "bsp_rcc_init",
        "bsp_afio_init",
        "bsp_gpio_init",
        "bsp_exti_init",
        "bsp_usart_init",
        "bsp_tim_init",
        "bsp_nvic_init",
    ):
        assert f"void {function}(void);" in text, f"bsp.h 缺 {function}"


def test_merged_keeps_init_call_order(merged_result) -> None:
    """合并只搬文本，不重排 —— 调用顺序必须与分文件布局一致。"""
    text = {item.name: item.content for item in merged_result.files}["bsp.c"]
    order = ["bsp_rcc_init", "bsp_afio_init", "bsp_gpio_init", "bsp_exti_init",
             "bsp_usart_init", "bsp_tim_init", "bsp_nvic_init"]
    positions = [text.index(f"{name}();") for name in order]
    assert positions == sorted(positions)


def test_merged_body_identical_to_split(demo_result, merged_result) -> None:
    """合并只是去掉文件级脚手架，正文逐字节不变。

    这是整个方案的核心保证：合并层不能改写任何一行代码，
    否则两份产物会慢慢漂移。
    """
    split = {item.name: item.content for item in demo_result.files}
    merged = {item.name: item.content for item in merged_result.files}

    # 取分文件布局里 bsp_gpio.c 的函数体，它必须原样出现在 bsp.c 里
    gpio_body = split["bsp_gpio.c"]
    start = gpio_body.index("void bsp_gpio_init(void)")
    end = gpio_body.index("\n}", start) + 2
    fragment = gpio_body[start:end]
    assert fragment in merged["bsp.c"]


def test_merged_drops_section_banners(merged_result) -> None:
    """ST 的 4 行分隔横幅在合并后应消失（9 份叠加只会淹没声明）。"""
    text = {item.name: item.content for item in merged_result.files}["bsp.h"]
    assert "/* Exported types" not in text
    assert "/* Exported functions" not in text


def test_merged_has_no_triple_blank_lines(merged_result) -> None:
    """段间恰好一个空行。"""
    for item in merged_result.files:
        assert "\n\n\n" not in item.content, f"{item.name} 有连续 3 个空行"


def test_merged_main_includes_only_bsp_h(merged_result) -> None:
    """main.c 原来 include bsp_init.h + bsp_it.h，现在合成一个。"""
    text = {item.name: item.content for item in merged_result.files}["main.c"]
    assert '#include "bsp.h"' in text
    assert "bsp_init.h" not in text
    assert "bsp_it.h" not in text


def test_merged_files_keep_symbol_guard(demo_result, merged_result) -> None:
    """合并布局同样不引入编造符号。"""
    from stm32gen.stdlib_ref import default_index, find_invented

    index = default_index()
    problems = []
    for item in merged_result.files:
        problems.extend(find_invented(item.content, index, item.name))
    assert not problems, "\n".join(problem.format() for problem in problems)


def test_merge_split_rejects_missing_files(demo_result) -> None:
    """缺文件时抛 MergeError，而不是产出半截文件。"""
    partial = [item for item in demo_result.files if item.name != "bsp_gpio.c"]
    with pytest.raises(MergeError, match="bsp_gpio.c"):
        merge_split(partial)


def test_merge_split_rejects_broken_header(demo_result) -> None:
    """头文件的 guard 形态变了就炸，避免静默产出坏文件。"""
    from stm32gen.generator.types import GeneratedFile

    broken = [
        GeneratedFile(name=item.name, content=item.content) if item.name != "bsp_gpio.h"
        else GeneratedFile(name=item.name, content="/* 没有 guard 的头文件 */\n")
        for item in demo_result.files
    ]
    with pytest.raises(MergeError, match="条件编译行数"):
        merge_split(broken)

# --------------------------------------------------------------------------
# TIM 外部时钟的生成
# --------------------------------------------------------------------------


def _external_clock_config(**tim_overrides) -> Stm32Config:
    tim = {"instance": "TIM2", "clock_source": "ETR_MODE1", "prescaler": 0, "period": 99}
    tim.update(tim_overrides)
    return Stm32Config.model_validate({"project_name": "etc", "tim": [tim]})


def test_etr_clock_generates_configure_call_before_timebase() -> None:
    """ETR 外部时钟要生成配置调用，且必须在 TIM_TimeBaseInit **之前**。

    顺序反了的话时基先按内部时钟算好，再切时钟源，PSC 的分频基准
    就错了 —— 这是"配了外部时钟但频率不对"的典型原因。
    """
    cfg = _external_clock_config(
        etr_prescaler="TIM_ExtTRGPSC_DIV4",
        etr_polarity="TIM_ExtTRGPolarity_Inverted",
        etr_filter=5,
    )
    files = {f.name: f.content for f in generate(cfg).files}
    src = files["bsp_tim.c"]

    assert "TIM_ETRClockMode1Config(TIM2," in src
    assert "TIM_ExtTRGPSC_DIV4" in src
    assert "TIM_ExtTRGPolarity_Inverted" in src
    # 参数顺序：预分频、极性、滤波
    expected = (
        "TIM_ETRClockMode1Config(TIM2, TIM_ExtTRGPSC_DIV4, "
        "TIM_ExtTRGPolarity_Inverted, 5)"
    )
    assert expected in src
    assert src.index("TIM_ETRClockMode1Config") < src.index("TIM_TimeBaseInit")


def test_etr_mode2_uses_its_own_function() -> None:
    """模式1 与模式2 是两个不同的库函数，不能混用。

    硬件上等效，但库里是两个公开 API；写错会编译失败。
    """
    cfg = _external_clock_config(clock_source="ETR_MODE2")
    files = {f.name: f.content for f in generate(cfg).files}
    src = files["bsp_tim.c"]
    assert "TIM_ETRClockMode2Config(TIM2," in src
    assert "TIM_ETRClockMode1Config" not in src


def test_ti_clock_uses_external_clock_config() -> None:
    """TI1/TI2 走 TIM_TIxExternalClockConfig，参数是 (源宏, 极性, 滤波)。"""
    cfg = _external_clock_config(clock_source="TI1")
    files = {f.name: f.content for f in generate(cfg).files}
    src = files["bsp_tim.c"]
    assert (
        "TIM_TIxExternalClockConfig(TIM2, TIM_TIxExternalCLK1Source_TI1, "
        "TIM_ICPolarity_Rising, 0)"
    ) in src


def test_ti1_edge_uses_both_edges_source() -> None:
    """TI1_ED 是双边沿源：输入一个周期会被数成 2 次。"""
    cfg = _external_clock_config(clock_source="TI1_ED")
    files = {f.name: f.content for f in generate(cfg).files}
    assert "TIM_TIxExternalCLK1Source_TI1ED" in files["bsp_tim.c"]


def test_itr_clock_uses_two_argument_call() -> None:
    """ITR 级联只有两个参数 —— 多了会编译失败。"""
    cfg = _external_clock_config(clock_source="ITR0")
    files = {f.name: f.content for f in generate(cfg).files}
    src = files["bsp_tim.c"]
    assert "TIM_ITRxExternalClockConfig(TIM2, TIM_TS_ITR0)" in src


def test_internal_clock_generates_no_clock_source_call() -> None:
    """内部时钟不生成任何时钟源配置语句 —— 复位后默认就是内部时钟。"""
    cfg = Stm32Config.model_validate(
        {"project_name": "t", "tim": [{"instance": "TIM2"}]}
    )
    files = {f.name: f.content for f in generate(cfg).files}
    src = files["bsp_tim.c"]
    for name in (
        "TIM_ETRClockMode1Config",
        "TIM_ETRClockMode2Config",
        "TIM_TIxExternalClockConfig",
        "TIM_ITRxExternalClockConfig",
    ):
        assert name not in src


def test_external_clock_note_does_not_claim_timer_clock() -> None:
    """外部时钟的注释不能写「定时器时钟 72MHz」。

    那个值对计数毫无作用；写出来会让人按它推算周期，结论全错。
    """
    cfg = _external_clock_config()
    files = {f.name: f.content for f in generate(cfg).files}
    src = files["bsp_tim.c"]
    # 找到 TIM2 那一段
    start = src.index("/* ── TIM2")
    end = src.index("TIM_Cmd(TIM2, ENABLE)")
    segment = src[start:end]
    assert "定时器时钟 72MHz" not in segment
    assert "频率由外部信号决定" in segment


def test_external_clock_frequency_in_note_when_given() -> None:
    """填了外部频率，注释里要给出推算结果供核对。"""
    cfg = _external_clock_config(
        etr_prescaler="TIM_ExtTRGPSC_DIV2", external_clock_hz=1000.0
    )
    files = {f.name: f.content for f in generate(cfg).files}
    src = files["bsp_tim.c"]
    # 1000Hz ÷2 = 500Hz 计数；÷100 = 5Hz 更新
    assert "按你给的外部频率 1000Hz 推算" in src
    assert "更新 5Hz" in src


def test_external_clock_symbols_are_collected() -> None:
    """外部时钟用到的库符号必须进 symbols_used。

    漏收的话反编造护栏会把它们判成编造符号 —— 明明是标准库公开 API，
    正确代码却过不了检查。
    """
    cfg = _external_clock_config(
        etr_prescaler="TIM_ExtTRGPSC_DIV4",
        etr_polarity="TIM_ExtTRGPolarity_Inverted",
    )
    result = generate(cfg)
    for symbol in (
        "TIM_ETRClockMode1Config",
        "TIM_ExtTRGPSC_DIV4",
        "TIM_ExtTRGPolarity_Inverted",
    ):
        assert symbol in result.symbols_used, f"{symbol} 没有进 symbols_used"
