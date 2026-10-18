"""校验规则集。

分五组，每组一个函数，都返回 ``list[Issue]``：

- ``check_chip``      芯片与时钟树（HSE × 9 是否超频、PCLK1 是否超 36MHz）
- ``check_pins``      引脚冲突、保留引脚、调试口、重映射可行性
- ``check_usart``     波特率误差、中断与 NVIC 的对应关系
- ``check_nvic``      优先级分组位宽、通道重复、分组调用位置
- ``check_gpio``      输出引脚初始电平、速度与模式搭配

设计原则：**能给出修法的一定给**。只说「PA9 冲突」而不说怎么改，
用户还得自己去翻配置找另一处。
"""

from __future__ import annotations

from stm32gen.models import Stm32Config
from stm32gen.models.enums import (
    GpioMode,
    GpioSpeed,
    TimRemap,
    UsartInstance,
    UsartRemap,
)
from stm32gen.models.tim_pins import default_pin as tim_default_pin
from stm32gen.validate.baudrate import (
    BAUD_ERROR_FAIL_PCT,
    BAUD_ERROR_WARN_PCT,
    BAUDRATE_MAX,
    PCLK1_MAX_HZ,
    SYSCLK_MAX_HZ,
    compute_clock_tree,
    evaluate_baudrate,
)
from stm32gen.validate.issue import Issue, error, info, warning
from stm32gen.validate.pins import (
    ConflictKind,
    classify_conflict,
    collect_pin_usages,
    effective_usart_pins,
    find_conflicts,
    remapped_pins,
)

# 调试口引脚：PA13 = SWDIO，PA14 = SWCLK。
# 配成普通 GPIO 会让 SWD 下载失效，需要重新用 BOOT0 或 JTAG 才能救回来。
DEBUG_PINS: dict[tuple[str, int], str] = {
    ("A", 13): "SWDIO（SWD 数据线）",
    ("A", 14): "SWCLK（SWD 时钟线）",
    ("A", 15): "JTDI（JTAG 数据输入）",
    ("B", 3): "JTDO（JTAG 数据输出）",
    ("B", 4): "JNTRST（JTAG 复位）",
}

# USART 重映射在各芯片上的可行性。
# 值里的引脚是重映射后生效的引脚 —— 只要其中任何一个在封装上不存在，
# 这个重映射选项就不可用。
REMAP_PINS: dict[tuple[str, UsartRemap], tuple[tuple[str, int], ...]] = {
    ("USART1", UsartRemap.USART1): (("B", 6), ("B", 7)),
    ("USART2", UsartRemap.USART2): (("D", 5), ("D", 6)),
    ("USART3", UsartRemap.USART3_PARTIAL): (("C", 10), ("C", 11)),
    ("USART3", UsartRemap.USART3_FULL): (("D", 8), ("D", 9)),
}


# --------------------------------------------------------------------------
# 芯片与时钟
# --------------------------------------------------------------------------


def check_chip(config: Stm32Config) -> list[Issue]:
    """时钟树相关检查。

    现在生成器**自己配 RCC**（``config.rcc``），所以这里的检查是
    「这套时钟配置能不能正常工作」，而不只是「库默认配置的后果」。

    检查四件事：

    1. SYSCLK 是否超 72MHz
    2. PCLK1 是否超 36MHz（APB1 上挂着 USART2/3、TIM2~4 等）
    3. PLL 输入频率是否在 4~16MHz（超范围 PLL 锁不住）
    4. Flash 等待周期与时钟是否匹配

    前两条是硬错误（芯片跑不起来），后两条是提示或警告。
    """
    issues: list[Issue] = []
    tree = compute_clock_tree(config.hse_hz, config.rcc)
    spec = config.spec
    rcc = config.rcc

    # ── 1) SYSCLK 超频 ──
    if not tree.sysclk_in_spec:
        issues.append(
            error(
                "rcc.sysclk_over_spec",
                f"SYSCLK = PLL 输入 {tree.pll_input_hz / 1e6:g}MHz × "
                f"{tree.pll_factor} = {tree.sysclk_hz / 1e6:g}MHz，"
                f"超过 STM32F103 的 {SYSCLK_MAX_HZ / 1e6:g}MHz 上限",
                location="rcc.pll_multiplier",
                hint=(
                    f"把 PLL 倍频降到"
                    f" {SYSCLK_MAX_HZ // tree.pll_input_hz} 或更小"
                    f"（当前 {tree.pll_factor}）"
                ),
                subjects=("SYSCLK", rcc.pll_multiplier.value),
            )
        )
    elif not tree.pll_ratio_ok:
        issues.append(
            info(
                "rcc.sysclk_not_max",
                f"SYSCLK = {tree.sysclk_hz / 1e6:g}MHz，未达到芯片满速 "
                f"{SYSCLK_MAX_HZ / 1e6:g}MHz",
                location="rcc.pll_multiplier",
                hint=(
                    f"若要满速，PLL 倍频应为 "
                    f"{SYSCLK_MAX_HZ // tree.pll_input_hz}"
                    f"（PLL 输入 {tree.pll_input_hz / 1e6:g}MHz 时）"
                ),
                subjects=("SYSCLK",),
            )
        )

    # ── 2) PCLK1 超频 ──
    if not tree.pclk1_in_spec:
        # 建议值：把 APB1 分频再翻一倍；APB 分频最大 16，翻不动时给下限
        suggested_div = min(tree.apb1_divider * 2, 16)
        suggested_pclk1 = tree.hclk_hz // suggested_div
        issues.append(
            error(
                "rcc.pclk1_over_spec",
                f"PCLK1 = HCLK {tree.hclk_hz / 1e6:g}MHz / "
                f"{tree.apb1_divider} = {tree.pclk1_hz / 1e6:g}MHz，"
                f"超过 APB1 的 {PCLK1_MAX_HZ / 1e6:g}MHz 上限",
                location="rcc.apb1_prescaler",
                hint=(
                    f"把 APB1 分频改大（当前 ÷{tree.apb1_divider}）。"
                    f"例如 ÷{suggested_div} 可降到 "
                    f"{suggested_pclk1 / 1e6:g}MHz"
                ),
                subjects=("PCLK1",),
            )
        )

    # ── 3) PLL 输入频率范围 ──
    # 依据 STM32F103 数据手册：PLL 输入须在 4~16MHz，超范围会锁不住。
    if rcc.needs_pll and not tree.pll_input_in_spec:
        issues.append(
            error(
                "rcc.pll_input_out_of_range",
                f"PLL 输入频率 {tree.pll_input_hz / 1e6:g}MHz 超出 4~16MHz 范围",
                location="rcc.pll_source",
                hint=(
                    f"PLL 源是 {rcc.pll_source.value}"
                    f"（对 HSE {config.hse_hz / 1e6:g}MHz 做了 "
                    f"{rcc.pll_source.divider} 分频）。"
                    "HSE 高于 16MHz 时应选 HSE_Div2；低于 4MHz 则无法用 PLL，"
                    "请改用 HSE 直连或 HSI"
                ),
                subjects=("PLL", rcc.pll_source.value),
            )
        )

    # ── 4) Flash 等待周期 ──
    # 用户手动覆盖且偏低时给 ERROR —— 那是取指不稳、偶发跑飞的经典成因，
    # 极难排查；偏高只降低性能，给 WARNING。自动推导时不进这里。
    #
    # **对比基准必须是 tree.flash_latency_recommended，不是 tree.flash_latency**：
    # 用户一旦覆盖，``flash_latency`` 就被置成覆盖值了，拿它来比就成了
    # 「跟自己比」—— ``actual_ws < actual_ws`` 恒假，偏低与偏高两条分支
    # 都永远不进，整块规则变成死代码。``flash_latency_recommended`` 始终是
    # 按时钟推导的值，与是否覆盖无关（见 validate/baudrate.py:196-202）。
    if rcc.flash_latency_override is not None:
        actual_ws = rcc.flash_latency_override.wait_states
        recommended_ws = tree.flash_latency_recommended
        if actual_ws < recommended_ws:
            issues.append(
                error(
                    "rcc.flash_latency_too_low",
                    f"Flash 等待周期设为 {actual_ws}，但 HCLK "
                    f"{tree.hclk_hz / 1e6:g}MHz 需要至少 {recommended_ws}",
                    location="rcc.flash_latency_override",
                    hint=(
                        f"HCLK {tree.hclk_hz / 1e6:g}MHz 下必须用 "
                        f"{recommended_ws} 个等待周期，否则取指不稳、"
                        "会偶发跑飞（很难排查）。建议清空此覆盖项让工具自动推导"
                    ),
                    subjects=("FLASH",),
                )
            )
        elif actual_ws > recommended_ws:
            issues.append(
                warning(
                    "rcc.flash_latency_high",
                    f"Flash 等待周期设为 {actual_ws}，"
                    f"高于 HCLK {tree.hclk_hz / 1e6:g}MHz 所需的 "
                    f"{recommended_ws} 个周期",
                    location="rcc.flash_latency_override",
                    hint="等待周期偏多会降低性能（不危险）。建议清空此覆盖项",
                    subjects=("FLASH",),
                )
            )

    # HCLK > 24MHz 必须开预取
    if tree.hclk_hz > 24_000_000 and rcc.flash_prefetch.value.endswith("Disable"):
        issues.append(
            warning(
                "rcc.prefetch_disabled",
                f"HCLK {tree.hclk_hz / 1e6:g}MHz 超过 24MHz，"
                "但 Flash 预取缓冲被禁用",
                location="rcc.flash_prefetch",
                hint="HCLK 高于 24MHz 时预取必须开启，否则取指成为瓶颈",
                subjects=("FLASH",),
            )
        )

    # ── 提示：HSE 与库默认不同 ──
    if config.hse_hz != 8_000_000:
        issues.append(
            info(
                "chip.hse_not_default",
                f"HSE = {config.hse_hz / 1e6:g}MHz，与标准库默认的 8MHz 不同",
                location="hse_hz",
                hint=(
                    f"生成的编译宏已带上 -DHSE_VALUE={config.hse_hz}，"
                    "确认晶振实物也是这个频率"
                ),
                subjects=("HSE",),
            )
        )

    # ── 提示：时钟树与芯片典型值不同 ──
    if tree.pclk2_hz != spec.pclk2_hz or tree.pclk1_hz != spec.pclk1_hz:
        issues.append(
            info(
                "chip.pclk_differs_from_spec",
                f"当前 PCLK2/PCLK1 = {tree.pclk2_hz / 1e6:g}/"
                f"{tree.pclk1_hz / 1e6:g}MHz，"
                f"{spec.name} 的典型值是 {spec.pclk2_hz / 1e6:g}/"
                f"{spec.pclk1_hz / 1e6:g}MHz",
                location="rcc",
                subjects=("PCLK1", "PCLK2"),
            )
        )

    # ── 提示：未生成 RCC 代码时，实际时钟由启动文件决定 ──
    if not rcc.enabled:
        issues.append(
            info(
                "rcc.not_generated",
                "未生成 RCC 初始化代码，时钟由启动文件的 SystemInit() 决定",
                location="rcc.enabled",
                hint=(
                    "标准库的 SetSysClockTo72() 固定 HSE×9。"
                    "上面的时钟树是按 rcc 配置**推演**的，用于校验波特率等；"
                    "若要与实际一致，请勾选生成 RCC 代码"
                ),
                subjects=("RCC",),
            )
        )

    return issues


# --------------------------------------------------------------------------
# 引脚
# --------------------------------------------------------------------------


def check_pins(config: Stm32Config) -> list[Issue]:
    """引脚冲突、保留引脚、调试口占用、重映射可行性。"""
    issues: list[Issue] = []
    spec = config.spec
    usages = collect_pin_usages(config)

    # 1) 引脚复用冲突。按方向分级 —— 输出撞输出是电气冲突必须拦下，但多个
    #    输入共用引脚是合法的（见 pins.py 的模块说明），一起报会逼用户删掉
    #    正确的配置。
    for key, owners in sorted(find_conflicts(usages).items()):
        kind = classify_conflict(owners)
        if kind is None:
            continue
        letter, number = key
        name = f"P{letter}{number}"
        detail = "、".join(f"{item.owner}（{item.role}）" for item in owners)
        first = owners[0]
        subjects = tuple(item.owner for item in owners)
        if kind is ConflictKind.OUTPUT_SHADOWS_INPUT:
            issues.append(
                warning(
                    "pin.direction_conflict",
                    f"{name} 上有一个输出和若干输入：{detail}",
                    location=first.location,
                    hint=(
                        f"{name} 由外设驱动输出，同时又有输入功能在读它 —— "
                        "能跑，但读到的就是本引脚自己的输出波形。"
                        "确认这是本意，否则把输入功能换到别的引脚"
                    ),
                    subjects=subjects,
                )
            )
        elif kind is ConflictKind.ANALOG_MIXED:
            issues.append(
                error(
                    "pin.analog_conflict",
                    f"{name} 同时配了模拟与数字功能：{detail}",
                    location=first.location,
                    hint=(
                        "模拟输入是独占的：配成 GPIO_Mode_AIN 后数字输入缓冲被"
                        f"关闭，数字外设读不到 {name} 的电平。把数字功能换到别的引脚"
                    ),
                    subjects=subjects,
                )
            )
        else:
            issues.append(
                error(
                    "pin.conflict",
                    f"{name} 有多个输出驱动源：{detail}",
                    location=first.location,
                    hint=(
                        f"同一时刻一个引脚只能有一个输出驱动源 —— 两个外设同时"
                        f"推挽输出会电气打架。把 {name} 上多余的输出删掉，"
                        "或给相关外设换引脚/换重映射"
                    ),
                    subjects=subjects,
                )
            )

    # 2) 被晶振等硬件占用的引脚
    for usage in usages:
        reason = spec.reserved_reason(usage.letter, usage.number)
        if reason:
            issues.append(
                warning(
                    "pin.reserved",
                    f"{usage.name}（{usage.owner}）通常被占用：{reason}",
                    location=usage.location,
                    hint=(
                        f"确认硬件上 {usage.name} 没有接晶振。"
                        "如果确实没接晶振、要当普通 IO 用，请把 system_stm32f10x.c "
                        "改成使用内部 HSI 时钟"
                    ),
                    subjects=(usage.name,),
                )
            )

    # 3) 调试口
    for usage in usages:
        role = DEBUG_PINS.get(usage.key)
        if role:
            issues.append(
                warning(
                    "pin.debug_port",
                    f"{usage.name} 是调试口 {role}，被 {usage.owner} 占用",
                    location=usage.location,
                    hint=(
                        "配成普通 GPIO 会导致 SWD 下载失效，且难以恢复。"
                        "确需使用请在生成代码里加 "
                        "GPIO_PinRemapConfig(GPIO_Remap_SWJ_JTAGDisable, ENABLE) "
                        "（该宏定义在 stm32f10x_gpio.h:193）"
                    ),
                    subjects=(usage.name, "SWJ"),
                )
            )

    # 4) 重映射可行性：重映射后的引脚必须在封装上存在
    for index, usart in enumerate(config.usart):
        if usart.remap is UsartRemap.NONE:
            continue
        targets = REMAP_PINS.get((usart.instance.value, usart.remap), ())
        missing = [pin for pin in targets if not spec.has_pin(pin[0], pin[1])]
        if missing:
            names = "、".join(f"P{letter}{number}" for letter, number in missing)
            issues.append(
                error(
                    "usart.remap_pin_missing",
                    f"{usart.name} 选择了 {usart.remap.value}，"
                    f"但重映射后的引脚 {names} 在 {spec.name}（{spec.package}）上不存在",
                    location=f"usart[{index}].remap",
                    hint=(
                        "LQFP48 封装只引出 PA0~PA15、PB0~PB15、PC13~PC15、PD0~PD1。"
                        "STM32F103C8T6 上唯一可用的 USART 重映射是 "
                        "USART1 → PB6/PB7"
                    ),
                    subjects=(usart.remap.value,),
                )
            )
            continue

        # 重映射了却把引脚号指定成默认端口上的引脚，多半是理解有误
        if usart.tx_pin is not None or usart.rx_pin is not None:
            base_tx, base_rx = remapped_pins(usart.instance.value, usart.remap)
            effective = effective_usart_pins(
                usart.instance.value,
                usart.remap,
                usart.tx_pin,
                usart.rx_pin,
                usart.instance.default_tx_pin,
                usart.instance.default_rx_pin,
            )
            if effective != (base_tx, base_rx):
                issues.append(
                    warning(
                        "usart.remap_pin_override",
                        f"{usart.name} 既选了重映射 {usart.remap.value}"
                        f"（对应 P{base_tx[0]}{base_tx[1]}/P{base_rx[0]}{base_rx[1]}），"
                        f"又显式指定了引脚 P{effective[0][0]}{effective[0][1]}/"
                        f"P{effective[1][0]}{effective[1][1]}",
                        location=f"usart[{index}]",
                        hint=(
                            "重映射已经决定了引脚，显式指定的引脚号会覆盖它。"
                            "若要用重映射引脚，请清空 tx_pin/rx_pin"
                        ),
                    )
                )

    # 5) 同一实例配了多次
    seen: dict[str, int] = {}
    for index, usart in enumerate(config.usart):
        if usart.instance.value in seen:
            issues.append(
                error(
                    "usart.duplicate_instance",
                    f"{usart.name} 被配置了多次"
                    f"（usart[{seen[usart.instance.value]}] 与 usart[{index}]）",
                    location=f"usart[{index}]",
                    hint="同一个 USART 实例只能有一份配置",
                    subjects=(usart.name,),
                )
            )
        else:
            seen[usart.instance.value] = index

    # 6) 同一引脚在 gpio 列表里配了多次
    gpio_seen: dict[tuple[str, int], int] = {}
    for index, pin in enumerate(config.gpio):
        if pin.identifier in gpio_seen:
            issues.append(
                error(
                    "gpio.duplicate_pin",
                    f"{pin.name} 在 gpio 列表里配了多次"
                    f"（gpio[{gpio_seen[pin.identifier]}] 与 gpio[{index}]）",
                    location=f"gpio[{index}]",
                    hint="同一引脚合并成一条配置即可",
                    subjects=(pin.name,),
                )
            )
        else:
            gpio_seen[pin.identifier] = index

    return issues


# --------------------------------------------------------------------------
# USART
# --------------------------------------------------------------------------


def check_usart(config: Stm32Config) -> list[Issue]:
    """波特率精度、中断与 NVIC 的对应关系、流控引脚。"""
    issues: list[Issue] = []
    tree = compute_clock_tree(config.hse_hz, config.rcc)

    for index, usart in enumerate(config.usart):
        location = f"usart[{index}]"
        pclk = tree.pclk_of(usart.instance)

        # 1) 波特率上限（库里的 assert_param 会拦）
        if usart.baudrate > BAUDRATE_MAX:
            issues.append(
                error(
                    "usart.baudrate_over_lib_limit",
                    f"{usart.name} 波特率 {usart.baudrate} 超过库允许上限 "
                    f"{BAUDRATE_MAX}（IS_USART_BAUDRATE 要求 < 0x0044AA21）",
                    location=f"{location}.baudrate",
                    hint=f"把波特率降到 {BAUDRATE_MAX} 以内",
                    subjects=(usart.name,),
                )
            )
            continue

        # 2) 波特率误差：必须用库里那套整数算法复算
        result = evaluate_baudrate(pclk, usart.baudrate)
        if result.brr == 0:
            issues.append(
                error(
                    "usart.baudrate_unrepresentable",
                    f"{usart.name} 波特率 {usart.baudrate} 在 PCLK="
                    f"{pclk / 1e6:g}MHz 下算出的 BRR 为 0，无法表示",
                    location=f"{location}.baudrate",
                    hint="降低波特率，或提高该总线的时钟频率",
                    subjects=(usart.name,),
                )
            )
        elif not result.is_representable:
            # 库的 IS_USART_BAUDRATE 只查固定上限 0x0044AA21，不查 USARTDIV>=1，
            # 所以这个组合库不会报错，但硬件上分频比小于 1，行为未定义。
            max_baud = result.max_representable_baudrate
            issues.append(
                error(
                    "usart.baudrate_over_pclk_limit",
                    f"{usart.name} 波特率 {usart.baudrate} 超过 PCLK="
                    f"{pclk / 1e6:g}MHz 下能表示的上限 {max_baud}"
                    f"（算出的 BRR={result.brr_hex}，整数部分为 0，"
                    "分频比小于 1）",
                    location=f"{location}.baudrate",
                    hint=(
                        f"硬件要求 USARTDIV >= 1，即 BRR 整数部分至少为 1，"
                        f"上限是 PCLK/16 = {max_baud}。"
                        "库的 IS_USART_BAUDRATE 不检查这一点，"
                        f"所以它不会报错，但实际不能用。请把波特率降到 {max_baud} 以内"
                    ),
                    subjects=(usart.name,),
                )
            )
        elif result.abs_error_pct >= BAUD_ERROR_FAIL_PCT:
            issues.append(
                error(
                    "usart.baudrate_error_too_large",
                    f"{usart.name} 波特率误差过大：{result.describe()}",
                    location=f"{location}.baudrate",
                    hint=(
                        f"误差超过 {BAUD_ERROR_FAIL_PCT}% 会导致通信出错。"
                        f"PCLK={'PCLK2' if usart.instance.is_apb2 else 'PCLK1'}"
                        f"={pclk / 1e6:g}MHz，建议改用能整除的波特率"
                        "（如 9600/19200/38400/57600/115200）"
                    ),
                    subjects=(usart.name,),
                )
            )
        elif result.abs_error_pct >= BAUD_ERROR_WARN_PCT:
            issues.append(
                warning(
                    "usart.baudrate_error_large",
                    f"{usart.name} 波特率误差偏大：{result.describe()}",
                    location=f"{location}.baudrate",
                    hint=f"误差超过 {BAUD_ERROR_WARN_PCT}% 时建议用外部晶振校准或降低波特率",
                    subjects=(usart.name,),
                )
            )
        else:
            issues.append(
                info(
                    "usart.baudrate_ok",
                    f"{usart.name} 波特率 {result.describe()}",
                    location=f"{location}.baudrate",
                    subjects=(usart.name,),
                )
            )

        # 3) 使能了中断却没配对应的 NVIC 通道
        if usart.needs_nvic:
            channel = config.nvic.find_channel(usart.irqn)
            if channel is None:
                issues.append(
                    error(
                        "usart.irq_without_nvic",
                        f"{usart.name} 使能了中断"
                        f"（{'/'.join(i.name for i in usart.interrupts)}），"
                        f"但 NVIC 里没有配 {usart.irqn} 通道",
                        location=location,
                        hint=(
                            f"在 nvic.channels 里加一条 irq_channel: {usart.irqn}，"
                            "否则中断触发了也没人响应"
                        ),
                        subjects=(usart.name, usart.irqn),
                    )
                )
            elif not channel.enabled:
                issues.append(
                    warning(
                        "usart.irq_nvic_disabled",
                        f"{usart.name} 使能了中断，但 NVIC 通道 {usart.irqn} 是禁用状态",
                        location=location,
                        hint="把该通道的 enabled 改成 true，或去掉 USART 的中断配置",
                        subjects=(usart.name, usart.irqn),
                    )
                )

        # 4) 配了 NVIC 通道但 USART 没使能中断
        channel = config.nvic.find_channel(usart.irqn)
        if channel is not None and channel.enabled and not usart.needs_nvic:
            issues.append(
                warning(
                    "usart.nvic_without_irq",
                    f"NVIC 配了 {usart.irqn} 通道，但 {usart.name} 没有使能任何中断",
                    location=f"{location}",
                    hint=(
                        f"中断使能要两步：USART_ITConfig 开外设中断、"
                        f"NVIC_Init 开中断通道。当前只做了后者，中断不会触发。"
                        f"给 {usart.name} 加上需要的 interrupts"
                    ),
                    subjects=(usart.name, usart.irqn),
                )
            )

        # 5) 硬件流控需要额外的 RTS/CTS 引脚
        if usart.flow_control.value != "USART_HardwareFlowControl_None":
            issues.append(
                info(
                    "usart.flow_control_pins",
                    f"{usart.name} 使能了 {usart.flow_control.name} 硬件流控，"
                    "需要额外接 RTS/CTS 引脚",
                    location=f"{location}.flow_control",
                    hint=(
                        "本工具只生成 USART 本体的配置，"
                        "RTS/CTS 引脚需按数据手册自行配成复用推挽输出"
                    ),
                    subjects=(usart.name,),
                )
            )

    # 6) 用了 USART3 却没有可用的重映射（LQFP48 的限制）
    for index, usart in enumerate(config.usart):
        if usart.instance is UsartInstance.USART3:
            issues.append(
                info(
                    "usart.usart3_on_lqfp48",
                    f"{usart.name} 默认引脚是 PB10/PB11，"
                    "在 LQFP48 上可用；但 USART3 的两个重映射目标"
                    "（PC10/PC11、PD8/PD9）都不存在",
                    location=f"usart[{index}]",
                    subjects=(usart.name,),
                )
            )

    return issues


# --------------------------------------------------------------------------
# NVIC
# --------------------------------------------------------------------------


def check_nvic(config: Stm32Config) -> list[Issue]:
    """优先级分组位宽、通道重复、分组调用的唯一性。"""
    issues: list[Issue] = []
    group = config.nvic.priority_group

    # 1) 优先级超出分组允许的位宽
    for index, channel in enumerate(config.nvic.channels):
        location = f"nvic.channels[{index}]"

        if channel.preemption_priority > group.max_preempt:
            issues.append(
                error(
                    "nvic.preempt_out_of_range",
                    f"{channel.irq_channel} 抢占优先级 {channel.preemption_priority} "
                    f"超出 {group.value} 的范围（最大 {group.max_preempt}）",
                    location=f"{location}.preemption_priority",
                    hint=(
                        f"{group.value} 抢占占 {group.preempt_bits} 位，"
                        f"取值 0~{group.max_preempt}。"
                        "要么降低优先级数值，要么换用抢占位更多的分组"
                    ),
                    subjects=(channel.irq_channel,),
                )
            )

        if channel.sub_priority > group.max_sub:
            issues.append(
                error(
                    "nvic.sub_out_of_range",
                    f"{channel.irq_channel} 子优先级 {channel.sub_priority} "
                    f"超出 {group.value} 的范围（最大 {group.max_sub}）",
                    location=f"{location}.sub_priority",
                    hint=(
                        f"{group.value} 子优先级占 {group.sub_bits} 位，"
                        f"取值 0~{group.max_sub}。"
                        "要么降低数值，要么换用子优先级位更多的分组"
                    ),
                    subjects=(channel.irq_channel,),
                )
            )

        # 2) 中断号是否真实存在于标准库 IRQn_Type
        try:
            from stm32gen.stdlib_ref import default_index

            if not default_index().has_enum(channel.irq_channel):
                issues.append(
                    error(
                        "nvic.unknown_irqn",
                        f"{channel.irq_channel} 不是标准库 IRQn_Type 里的中断号",
                        location=f"{location}.irq_channel",
                        hint=(
                            "中断号名必须与 Startup 向量表和 IRQn_Type 枚举一致，"
                            "如 USART1_IRQn / EXTI0_IRQn"
                        ),
                        subjects=(channel.irq_channel,),
                    )
                )
        except Exception:  # 符号表不可用时不阻断校验
            pass

    # 3) 同一通道配了多次
    seen: dict[str, int] = {}
    for index, channel in enumerate(config.nvic.channels):
        if channel.irq_channel in seen:
            issues.append(
                error(
                    "nvic.duplicate_channel",
                    f"{channel.irq_channel} 在 nvic.channels 里配了多次"
                    f"（[{seen[channel.irq_channel]}] 与 [{index}]）",
                    location=f"nvic.channels[{index}]",
                    hint="同一中断通道只能有一份优先级配置",
                    subjects=(channel.irq_channel,),
                )
            )
        else:
            seen[channel.irq_channel] = index

    # 4) 优先级全 0：所有中断同优先级，抢占失效
    enabled = config.nvic.enabled_channels
    if len(enabled) > 1 and all(
        item.preemption_priority == 0 for item in enabled
    ):
        issues.append(
            warning(
                "nvic.all_same_preempt",
                f"{len(enabled)} 个中断通道的抢占优先级全是 0，"
                "它们之间无法互相抢占",
                location="nvic.channels",
                hint="如果某些中断需要打断另一些，给它们设置不同的抢占优先级",
            )
        )

    # 5) 分组说明：整个固件只能调用一次 NVIC_PriorityGroupConfig
    issues.append(
        info(
            "nvic.group_note",
            f"优先级分组 {group.value}：抢占 {group.preempt_bits} 位"
            f"（0~{group.max_preempt}）/ 子优先级 {group.sub_bits} 位"
            f"（0~{group.max_sub}）",
            location="nvic.priority_group",
            hint=(
                "NVIC_PriorityGroupConfig 在整个固件里只能调用一次。"
                "生成代码把它放在 nvic_init() 开头，若工程里别处也调用了，"
                "后调用的一方会覆盖前者"
            ),
            subjects=(group.value,),
        )
    )

    return issues


# --------------------------------------------------------------------------
# GPIO
# --------------------------------------------------------------------------


def check_gpio(config: Stm32Config) -> list[Issue]:
    """GPIO 模式与速度的搭配。"""
    issues: list[Issue] = []

    for index, pin in enumerate(config.gpio):
        location = f"gpio[{index}]"

        # 1) 输入模式不需要速度；输出模式速度为 2MHz 但接高速信号是隐患
        if pin.mode is GpioMode.AIN:
            issues.append(
                info(
                    "gpio.analog_mode",
                    f"{pin.name} 配成模拟输入（GPIO_Mode_AIN）",
                    location=location,
                    hint=(
                        "模拟输入用于 ADC 或低功耗。"
                        "若只是普通输入，用 GPIO_Mode_IN_FLOATING 或上/下拉更合适"
                    ),
                    subjects=(pin.name,),
                )
            )

        # 2) 悬空输入：浮空输入在悬空时电平不确定
        if pin.mode is GpioMode.IN_FLOATING:
            issues.append(
                warning(
                    "gpio.floating_input",
                    f"{pin.name} 配成浮空输入，引脚悬空时电平不确定",
                    location=location,
                    hint=(
                        "若外部没有上拉/下拉电阻，改用 GPIO_Mode_IPU 或 "
                        "GPIO_Mode_IPD。悬空输入还会带来额外功耗"
                    ),
                    subjects=(pin.name,),
                )
            )

        # 3) 输出模式速度过低
        if pin.is_output and pin.speed is GpioSpeed.SPEED_2MHz:
            issues.append(
                info(
                    "gpio.low_speed_output",
                    f"{pin.name} 是输出模式但速度只有 2MHz",
                    location=location,
                    hint=(
                        "2MHz 适合驱动 LED、继电器这类慢速负载，"
                        "且能降低 EMI。若接 SPI/高速信号请改 50MHz"
                    ),
                    subjects=(pin.name,),
                )
            )

        # 4) 复用模式但没提到对应外设：可能是用户手动配的
        if pin.mode.is_alternate:
            issues.append(
                info(
                    "gpio.alternate_mode",
                    f"{pin.name} 配成复用模式（{pin.mode.value}）",
                    location=location,
                    hint=(
                        "复用引脚由对应外设驱动。若该引脚是为 USART 等外设准备的，"
                        "在外设配置里指定引脚即可，不需要在 gpio 列表里重复配置"
                    ),
                    subjects=(pin.name,),
                )
            )

    # 5) 完全没有配置任何 GPIO 或 USART
    if (
        not config.gpio
        and not config.usart
        and not config.exti
        and not config.tim
    ):
        issues.append(
            info(
                "config.empty",
                "配置里没有任何 GPIO / USART / EXTI / TIM，生成的代码只包含空的初始化函数",
                location="<根>",
                hint="至少添加一个 GPIO 引脚或一个外设实例",
            )
        )

    return issues


# --------------------------------------------------------------------------
# AFIO
# --------------------------------------------------------------------------


def check_afio(config: Stm32Config) -> list[Issue]:
    """SWJ 调试口释放的安全性检查。

    这是整个工具里**唯一一处会给出"不可逆风险"级别错误**的规则：
    ``GPIO_Remap_SWJ_Disable`` 把 SWD 一起关掉之后，调试器再也连不上，
    只能靠 BOOT0 跳线或整片擦除救回来。用户在下拉框里选中它时，
    多半只看到"能多出 5 个 GPIO"，不会意识到代价。
    """
    issues: list[Issue] = []
    afio = config.afio

    # 1) 关闭 SWD 会导致无法再下载 —— ERROR，不是警告
    if afio.bricks_debugger:
        issues.append(
            error(
                "afio.swj_remap_bricks_debugger",
                "AFIO 选了 GPIO_Remap_SWJ_Disable：SWD 与 JTAG 全部关闭，"
                "调试器将无法再连接（下载不了程序）",
                location="afio.swj_remap",
                hint=(
                    "这释放了 PA13/PA14/PA15/PB3/PB4 全部 5 个引脚，但代价是"
                    "之后只能用 BOOT0 拉高进入系统存储器启动、或整片擦除才能救回来。"
                    "若只是想用 PA15/PB3/PB4，请改用 GPIO_Remap_SWJ_JTAGDisable"
                    "（只关 JTAG，保留 SWD 下载）"
                ),
                subjects=("SWJ", "PA13", "PA14"),
            )
        )

    # 2) 释放了引脚但没用到 —— 提示一下，避免白担风险
    if afio.has_swj and afio.releases_pins:
        used_names = {usage.name for usage in collect_pin_usages(config)}
        released = set(afio.releases_pins)
        if not (released & used_names):
            issues.append(
                info(
                    "afio.swj_remap_unused",
                    f"AFIO 释放了 {'、'.join(sorted(released))}，"
                    "但配置里没有任何地方用到这些引脚",
                    location="afio.swj_remap",
                    hint="若确实不需要它们，把 swj_remap 改回 NONE（保留调试口）",
                    subjects=tuple(sorted(released)),
                )
            )
        else:
            unused = sorted(released - used_names)
            if unused:
                issues.append(
                    info(
                        "afio.swj_remap_partial",
                        f"AFIO 释放了 {'、'.join(sorted(released))}，"
                        f"其中 {'、'.join(unused)} 没有被用到",
                        location="afio.swj_remap",
                        subjects=tuple(unused),
                    )
                )

    # 3) 释放了调试口引脚，但那些引脚又被配成 GPIO 却用了推挽输出等
    #    与调试相关的冲突由 check_pins 的 pin.debug_port 统一报，这里不重复。

    # 4) 有引脚重映射但配置里没生成 AFIO 时钟的情况
    #    —— 本工具的生成器会自动加 AFIO 时钟，所以这里只在**用户手工改了
    #    配置**（直接给 afio.enabled=false 但别处有重映射）时才会触发。
    #    实际上 needs_afio_clock 是推导属性，不会出现这种矛盾，
    #    因此这里只给 INFO 说明时钟是自动的，避免用户到处找 AFIO 时钟使能。
    if config.needs_afio_clock:
        reasons: list[str] = []
        if afio.needs_afio_clock:
            reasons.append("SWJ 释放")
        if config.exti:
            reasons.append(f"EXTI（{len(config.exti)} 条线，GPIO_EXTILineConfig 写 AFIO->EXTICR）")
        if any(item.remap is not UsartRemap.NONE for item in config.usart):
            reasons.append("USART 重映射")
        if any(item.remap is not TimRemap.NONE for item in config.tim):
            reasons.append("TIM 重映射")
        issues.append(
            info(
                "afio.clock_auto_enabled",
                "已自动加入 AFIO 时钟使能（RCC_APB2Periph_AFIO）："
                + "、".join(reasons),
                location="afio",
                hint=(
                    "AFIO 寄存器的写入需要先使能 AFIO 时钟。"
                    "本工具按上面这些理由自动加上，不需要手动配置"
                ),
                subjects=("AFIO",),
            )
        )

    return issues


# --------------------------------------------------------------------------
# EXTI
# --------------------------------------------------------------------------


def check_exti(config: Stm32Config) -> list[Issue]:
    """EXTI 线的端口冲突、中断与 NVIC 的对应关系、调试口占用。"""
    issues: list[Issue] = []
    spec = config.spec

    # 1) 同一线号只能接一个端口 —— 硬件约束，必检
    #
    # AFIO->EXTICR 每个引脚号只有 4 位选择字段（见 GPIO_EXTILineConfig 的实现：
    # tmp = 0x0F << (4 * (PinSource & 3))），所以 EXTI0 只能选 PA0/PB0/…/PG0
    # 中的一个。两条配置用同一条线时，后写的那个会静默覆盖前一个，
    # 表现为"有一个中断永远不触发"。
    by_line: dict[int, list[int]] = {}
    for index, line in enumerate(config.exti):
        by_line.setdefault(line.line, []).append(index)
    for line_number, indices in sorted(by_line.items()):
        if len(indices) < 2:
            continue
        names = "、".join(
            f"{config.exti[i].pin_name}（exti[{i}]）" for i in indices
        )
        issues.append(
            error(
                "exti.line_conflict",
                f"EXTI{line_number} 被多处占用：{names}",
                location=f"exti[{indices[1]}]",
                hint=(
                    f"AFIO 的 EXTICR 寄存器每条线只有一份端口选择，"
                    f"EXTI{line_number} 同时只能接一个端口 —— "
                    "后配置的会覆盖先配置的，表现为其中一个中断永不触发。"
                    "请删掉多余的配置，或改用别的线号"
                ),
                subjects=tuple(f"EXTI{line_number}" for _ in indices),
            )
        )

    for index, line in enumerate(config.exti):
        location = f"exti[{index}]"

        # 2) 中断模式却没配 NVIC 通道
        if line.needs_nvic:
            channel = config.nvic.find_channel(line.irqn)
            if channel is None:
                issues.append(
                    error(
                        "exti.irq_without_nvic",
                        f"{line.name}（{line.pin_name}）是中断模式，"
                        f"但 NVIC 里没有配 {line.irqn} 通道",
                        location=location,
                        hint=(
                            f"在 nvic.channels 里加一条 irq_channel: {line.irqn}，"
                            "否则中断触发了也没人响应"
                        ),
                        subjects=(line.name, line.irqn),
                    )
                )
            elif not channel.enabled:
                issues.append(
                    warning(
                        "exti.irq_nvic_disabled",
                        f"{line.name} 是中断模式，但 NVIC 通道 {line.irqn} 是禁用状态",
                        location=location,
                        hint="把该通道的 enabled 改成 true，或把 EXTI 改成事件模式",
                        subjects=(line.name, line.irqn),
                    )
                )
        else:
            # 3) 事件模式配了 NVIC 通道 —— 事件不产生中断，通道是多余的
            channel = config.nvic.find_channel(line.irqn)
            if channel is not None and channel.enabled:
                issues.append(
                    warning(
                        "exti.event_with_nvic",
                        f"{line.name} 是事件模式（EXTI_Mode_Event），"
                        f"但 NVIC 配了 {line.irqn} 通道 —— 事件模式不产生中断，"
                        "该通道永远不会被触发",
                        location=location,
                        hint="把 EXTI 改成中断模式，或去掉这个 NVIC 通道",
                        subjects=(line.name, line.irqn),
                    )
                )

        # 4) 共享中断通道：5~9 共用 EXTI9_5，10~15 共用 EXTI15_10
        if line.line >= 5:
            peers = [
                other
                for other in config.exti
                if other is not line and other.irqn == line.irqn
            ]
            if peers:
                names = "、".join(f"{p.name}({p.pin_name})" for p in peers)
                issues.append(
                    info(
                        "exti.shared_irq_channel",
                        f"{line.name}（{line.pin_name}）与 {names} 共享中断通道 "
                        f"{line.irqn}",
                        location=location,
                        hint=(
                            "这几条线共用一个 ISR，函数里必须逐个判标志位才能"
                            "知道是哪条线触发的。生成的中断骨架已经把它们合并，"
                            "并给出了每条线的分支"
                        ),
                        subjects=(line.irqn,),
                    )
                )

    # 5) 引脚被晶振占用（沿用规格里的保留表）
    for index, line in enumerate(config.exti):
        reason = spec.reserved_reason(line.port.letter, line.line)
        if reason:
            issues.append(
                warning(
                    "exti.pin_reserved",
                    f"{line.name} 用的 {line.pin_name} 通常被占用：{reason}",
                    location=f"exti[{index}]",
                    hint=f"确认硬件上 {line.pin_name} 没有接晶振",
                    subjects=(line.pin_name,),
                )
            )

    return issues


# --------------------------------------------------------------------------
# TIM
# --------------------------------------------------------------------------


def check_tim(config: Stm32Config) -> list[Issue]:
    """定时器：实例重复、中断与 NVIC 配对、PWM 引脚、频率合理性。"""
    issues: list[Issue] = []
    tree = compute_clock_tree(config.hse_hz, config.rcc)

    # 1) 同一实例配了多次
    seen: dict[str, int] = {}
    for index, timer in enumerate(config.tim):
        if timer.instance.value in seen:
            issues.append(
                error(
                    "tim.duplicate_instance",
                    f"{timer.name} 被配置了多次"
                    f"（tim[{seen[timer.instance.value]}] 与 tim[{index}]）",
                    location=f"tim[{index}]",
                    hint="同一个定时器实例只能有一份配置",
                    subjects=(timer.name,),
                )
            )
        else:
            seen[timer.instance.value] = index

    for index, timer in enumerate(config.tim):
        location = f"tim[{index}]"
        clock_hz = (
            tree.tim1_hz if timer.instance.is_advanced else tree.tim234_hz
        )

        # 2) 更新中断却没配 NVIC
        if timer.needs_nvic:
            channel = config.nvic.find_channel(timer.irqn)
            if channel is None:
                issues.append(
                    error(
                        "tim.irq_without_nvic",
                        f"{timer.name} 使能了中断"
                        f"（{'/'.join(i.name for i in timer.interrupts)}），"
                        f"但 NVIC 里没有配 {timer.irqn} 通道",
                        location=location,
                        hint=(
                            f"在 nvic.channels 里加一条 irq_channel: {timer.irqn}，"
                            "否则中断触发了也没人响应"
                        ),
                        subjects=(timer.name, timer.irqn),
                    )
                )
            elif not channel.enabled:
                issues.append(
                    warning(
                        "tim.irq_nvic_disabled",
                        f"{timer.name} 使能了中断，但 NVIC 通道 {timer.irqn} 是禁用状态",
                        location=location,
                        hint="把该通道的 enabled 改成 true",
                        subjects=(timer.name, timer.irqn),
                    )
                )

        # 3) 配了 NVIC 通道但没使能中断
        channel = config.nvic.find_channel(timer.irqn)
        if channel is not None and channel.enabled and not timer.needs_nvic:
            issues.append(
                warning(
                    "tim.nvic_without_irq",
                    f"NVIC 配了 {timer.irqn} 通道，但 {timer.name} 没有使能任何中断",
                    location=location,
                    hint=(
                        "中断使能要两步：TIM_ITConfig 开外设中断、"
                        "NVIC_Init 开中断通道。当前只做了后者，中断不会触发"
                    ),
                    subjects=(timer.name, timer.irqn),
                )
            )

        # 4) 更新频率与占空比 —— 把算出来的值报出来供核对
        #
        # 外部时钟下这里要分两种情况：用户填了外部频率就照常算并报出来；
        # 没填就只报「未知」。**不能**拿时钟树的 TIMxCLK 顶替 —— 那个值
        # 对外部时钟毫无意义，算出来的频率看着合理，实际差几个数量级。
        external = timer.uses_external_clock
        effective_clock = timer.effective_clock_hz(clock_hz)
        update_hz = timer.update_frequency_hz(clock_hz)

        if external and effective_clock is None:
            # 外部时钟 + 未填频率：只提示，不报错（算术上没毛病，
            # 只是工具算不出实际频率）
            source_hint = (
                "ITR 级联的时钟来自另一个定时器的更新事件，"
                "上游必须同时在运行；填上上游的更新频率到 external_clock_hz "
                "就能让工具帮你推算"
                if timer.clock_source.is_itr
                else (
                    "填上 external_clock_hz 后工具会在注释里写出推算结果；"
                    "不填不影响生成，PSC/ARR 仍按你给的值输出"
                )
            )
            issues.append(
                info(
                    "tim.external_clock_frequency_unknown",
                    f"{timer.name} 用外部时钟（{timer.clock_source.label}），"
                    f"没填该时钟源的频率 —— 实际更新频率需按信号源推算",
                    location=location,
                    hint=source_hint,
                    subjects=(timer.name,),
                )
            )
            # ITR 级联还有一个与频率无关的硬性依赖：上游停了本定时器就不计数。
            # 这条必须在 continue 之前发，否则会被跳过。
            if timer.clock_source.is_itr:
                issues.append(
                    info(
                        "tim.itr_cascade",
                        f"{timer.name} 用 ITR 级联时钟，上游定时器必须同时在运行，"
                        "否则本定时器根本不计数",
                        location=location,
                        hint=(
                            "ITR0~ITR3 对应哪个上游由芯片型号决定，"
                            "请对照参考手册的「TIM 内部触发连接」表"
                        ),
                        subjects=(timer.name,),
                    )
                )
            if timer.channels:
                duties = "、".join(
                    f"{c.channel.name}={timer.duty_percent(c):.2f}%"
                    for c in timer.channels
                )
                issues.append(
                    info(
                        "tim.duty_summary",
                        f"{timer.name} 占空比 {duties}（由 CCR/(ARR+1) 算出，"
                        "与外设时钟频率无关）",
                        location=location,
                        subjects=(timer.name,),
                    )
                )
            continue

        if update_hz <= 0:
            issues.append(
                error(
                    "tim.update_frequency_zero",
                    f"{timer.name} 的更新频率算出来是 0"
                    f"（PSC={timer.prescaler}, ARR={timer.period}）",
                    location=location,
                    hint="增大 prescaler 或 period（两者都不能为 0，它们会被 +1）",
                    subjects=(timer.name,),
                )
            )
        else:
            duty_text = ""
            if timer.channels:
                duties = "、".join(
                    f"{c.channel.name}={timer.duty_percent(c):.2f}%"
                    for c in timer.channels
                )
                duty_text = f"；占空比 {duties}"
            if external:
                hint = None
            else:
                hint = (
                    "定时器时钟不是 PCLK：APB 分频系数 != 1 时它是 PCLK 的 2 倍"
                    if clock_hz != tree.pclk1_hz
                    else None
                )
            issues.append(
                info(
                    "tim.frequency_summary",
                    f"{timer.name}: {timer.describe_frequency(clock_hz)}{duty_text}",
                    location=location,
                    hint=hint,
                    subjects=(timer.name,),
                )
            )

        # 4b) 外部时钟填了频率时，ITR 级联的依赖关系仍然要提示
        if external and timer.clock_source.is_itr:
            issues.append(
                info(
                    "tim.itr_cascade",
                    f"{timer.name} 用 ITR 级联时钟，上游定时器必须同时在运行，"
                    "否则本定时器根本不计数",
                    location=location,
                    hint=(
                        "ITR0~ITR3 对应哪个上游由芯片型号决定，"
                        "请对照参考手册的「TIM 内部触发连接」表"
                    ),
                    subjects=(timer.name,),
                )
            )

        # 5) 输出比较中断要有对应通道（模型层已拦，这里再兜一次是为了
        #    代码路径上直接构造 dict 的场景）
        configured = {c.channel for c in timer.channels}
        for interrupt in timer.interrupts:
            if interrupt.channel is not None and interrupt.channel not in configured:
                issues.append(
                    error(
                        "tim.cc_interrupt_without_channel",
                        f"{timer.name} 使能了 {interrupt.name} 中断，"
                        f"但没有配置 {interrupt.channel.value} 通道",
                        location=location,
                        hint="没有通道就没有捕获比较事件，该中断永远不会触发",
                        subjects=(timer.name, interrupt.name),
                    )
                )

        # 6) PWM 通道的引脚检查
        for channel_config in timer.channels:
            if not channel_config.needs_gpio:
                continue
            expected = tim_default_pin(timer.instance, channel_config.channel)
            if (
                expected is not None
                and (channel_config.port.letter, channel_config.pin) != expected
                and timer.remap is TimRemap.NONE
            ):
                # 只给 WARNING：数据手册在本环境不可得（见 tim_pins.py 的说明），
                # 用户按手册选了非默认引脚是完全合理的，报 ERROR 会挡住正确配置。
                issues.append(
                    warning(
                        "tim.pin_not_default",
                        f"{timer.name}_{channel_config.channel.name} 用了 "
                        f"{channel_config.pin_name}，而该通道在**默认复用**下"
                        f"通常是 P{expected[0]}{expected[1]}",
                        location=f"tim[{index}].channels",
                        hint=(
                            "若你是有意换引脚（或选了重映射），忽略此提示即可 —— "
                            "本工具没有数据手册，这张默认引脚表仅作参考，"
                            "请以你手上那份手册的「引脚定义」表为准"
                        ),
                        subjects=(channel_config.pin_name,),
                    )
                )

        # 7) 高级定时器：TIM1 的 PWM 输出必须显式开主输出（MOE）
        if timer.is_advanced and timer.channels:
            issues.append(
                info(
                    "tim.advanced_needs_moe",
                    f"{timer.name} 是高级定时器，PWM 通道已生成"
                    " TIM_CtrlPWMOutputs(TIM1, ENABLE) 调用",
                    location=location,
                    hint=(
                        "高级定时器（TIM1/TIM8）的 BDTR.MOE 位默认是 0，"
                        "不开主输出的话 PWM 引脚不会有波形 —— "
                        "这是高级定时器最常见的「配了没输出」原因"
                    ),
                    subjects=(timer.name,),
                )
            )

    return issues


# 全部规则，供 validate() 依次执行
ALL_RULES = (
    check_chip,
    check_pins,
    check_usart,
    check_nvic,
    check_gpio,
    check_afio,
    check_exti,
    check_tim,
)


def _dedupe(issues: list[Issue]) -> list[Issue]:
    """按 (code, location, message) 去重，保持首次出现的顺序。

    不同规则可能对同一现象各报一次（如引脚冲突既被 pin 规则报、
    也被 usart 规则报），去重避免刷屏。
    """
    seen: set[tuple[str, str, str]] = set()
    result: list[Issue] = []
    for item in issues:
        key = (item.code, item.location, item.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def sort_issues(issues: list[Issue]) -> list[Issue]:
    """按严重级别排序：ERROR → WARNING → INFO，同级保持原顺序。

    ``list.sort`` 是稳定排序，所以同级内的规则顺序不变。
    """
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(issues, key=lambda item: order[item.severity.value])
